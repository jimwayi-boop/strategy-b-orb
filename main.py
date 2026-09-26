# main.py
import os
import time
import smtplib
import random
import pytz
import pandas as pd
import numpy as np
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    TrailingStopOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import (
    OrderSide,
    TimeInForce,
    OrderStatus,
    QueryOrderStatus,
)
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import *
from orb_strategy import ORBStrategy

ET = pytz.timezone("America/New_York")

# ══════════════════════════════════════════════
# API 重试包装
# ══════════════════════════════════════════════
def api_call_with_retry(func, *args, max_retries=API_MAX_RETRIES,
                        base_delay=API_RETRY_BASE_DELAY, **kwargs):
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            print(f"API 调用失败 (尝试 {attempt+1}/{max_retries}): {e}，{delay:.1f}秒后重试")
            time.sleep(delay)

# ══════════════════════════════════════════════
# 邮件通知
# ══════════════════════════════════════════════
def send_email(subject, body):
    mail_user = os.getenv("MAIL_USERNAME")
    mail_pass = os.getenv("MAIL_PASSWORD")
    if not mail_user or not mail_pass:
        print(" ⚠️未配置邮箱，跳过邮件发送")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = mail_user
    msg["To"] = mail_user
    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(mail_user, mail_pass)
        server.sendmail(mail_user, [mail_user], msg.as_string())
        server.quit()
        print("✅ 邮件通知已发送")
    except Exception as e:
        print(f" ⚠️邮件发送失败: {e}")

# ══════════════════════════════════════════════
# 时间判断
# ══════════════════════════════════════════════
def now_et():
    return datetime.now(ET)

def is_market_day():
    return now_et().weekday() < 5

def is_after_orb():
    n = now_et()
    return (n.hour > ORB_END_HOUR) or (
        n.hour == ORB_END_HOUR and n.minute > ORB_END_MIN
    )

def is_eod():
    n = now_et()
    return (n.hour > EOD_CLOSE_HOUR) or (
        n.hour == EOD_CLOSE_HOUR and n.minute >= EOD_CLOSE_MIN
    )

def is_before_open():
    n = now_et()
    return (n.hour < ORB_START_HOUR) or (
        n.hour == ORB_START_HOUR and n.minute < ORB_START_MIN
    )

# ══════════════════════════════════════════════
# 数据获取
# ══════════════════════════════════════════════
def fetch_opening_range_bars(data_client, symbol):
    today = now_et().strftime("%Y-%m-%d")
    start = f"{today}T09:30:00-04:00"
    end = f"{today}T09:45:00-04:00"
    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            feed="iex",
        )
        bars = api_call_with_retry(data_client.get_stock_bars, req)
        df = bars.df
        if df.empty:
            return None
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        return df
    except Exception as e:
        print(f"[{symbol}] 获取开盘区间失败: {e}")
        return None

def fetch_latest_bar(data_client, symbol):
    end = now_et()
    start = end - timedelta(minutes=3)
    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start.strftime("%Y-%m-%dT%H:%M:%S%z"),
            end=end.strftime("%Y-%m-%dT%H:%M:%S%z"),
            feed="iex",
        )
        bars = api_call_with_retry(data_client.get_stock_bars, req)
        df = bars.df
        if df.empty:
            return None
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        return df.iloc[-1]
    except Exception as e:
        print(f"[{symbol}] 获取最新 K 线失败: {e}")
        return None

def fetch_prev_close_and_open(data_client, symbol):
    today = now_et().strftime("%Y-%m-%d")
    yesterday = (now_et() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=yesterday,
            end=today,
            feed="iex",
        )
        bars = api_call_with_retry(data_client.get_stock_bars, req)
        df = bars.df
        if df.empty:
            return None, None
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        prev_close = None
        today_open = None
        for idx, row in df.iterrows():
            row_date = idx[0] if isinstance(idx, tuple) else idx
            date_str = str(row_date)[:10]
            if date_str == yesterday:
                prev_close = float(row["close"])
            elif date_str == today:
                today_open = float(row["open"])
        return prev_close, today_open
    except Exception as e:
        print(f"[{symbol}] 获取前收/今开失败: {e}")
        return None, None

def fetch_daily_ema(data_client, symbol, period=TREND_EMA_PERIOD):
    end = now_et()
    start = end - timedelta(days=period + 30)
    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            feed="iex",
        )
        bars = api_call_with_retry(data_client.get_stock_bars, req)
        df = bars.df
        if df.empty or len(df) < period:
            return None
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        closes = df["close"].astype(float)
        ema = closes.ewm(span=period, adjust=False).mean().iloc[-1]
        return float(ema)
    except Exception as e:
        print(f"[{symbol}] 获取日线EMA 失败: {e}")
        return None

def fetch_atr(data_client, symbol, period=ATR_LOOKBACK_DAYS):
    end = now_et()
    start = end - timedelta(days=period + 10)
    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            feed="iex",
        )
        bars = api_call_with_retry(data_client.get_stock_bars, req)
        df = bars.df
        if df.empty or len(df) < period + 1:
            return None
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean().iloc[-1]
        return float(atr)
    except Exception as e:
        print(f"[{symbol}] 获取ATR 失败: {e}")
        return None

def fetch_today_orders(trading_client, symbol):
    try:
        req = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            symbols=[symbol],
            after=now_et().strftime("%Y-%m-%dT00:00:00-04:00"),
        )
        return api_call_with_retry(trading_client.get_orders, req)
    except Exception as e:
        print(f"[{symbol}] 查询订单失败: {e}")
        return []

# ══════════════════════════════════════════════
# 订单执行
# ══════════════════════════════════════════════
def wait_for_order_filled(trading_client, order_id, max_wait=10, interval=0.5):
    waited = 0
    while waited < max_wait:
        try:
            o = api_call_with_retry(trading_client.get_order_by_id, order_id)
            if o.status == OrderStatus.FILLED:
                print(f" ✔️订单 {order_id} 已成交")
                return True
            if o.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                print(f" ❌ 订单 {order_id} 状态: {o.status}")
                return False
        except Exception:
            pass
        time.sleep(interval)
        waited += interval
    print(f" ⚠️等待成交超时 ({max_wait}s)")
    return False

def place_entry_and_trailing_stop(trading_client, symbol, qty, side, trail_pct, tag):
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    entry_req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
        client_order_id=tag,
    )
    entry_order = api_call_with_retry(trading_client.submit_order, entry_req)
    print(f" ✅ 入场: {side} {qty} 股 {symbol} | 订单ID={entry_order.id}")

    filled = wait_for_order_filled(trading_client, entry_order.id)
    if not filled:
        print(f" ⚠️入场未成交，取消Trailing Stop 步骤")
        return entry_order, None

    trail_side = OrderSide.SELL if side == "buy" else OrderSide.BUY
    trail_req = TrailingStopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=trail_side,
        time_in_force=TimeInForce.DAY,
        trail_percent=trail_pct,
        client_order_id=tag + "_TRAIL",
    )
    try:
        trail_order = api_call_with_retry(trading_client.submit_order, trail_req)
        print(f" 🔄 Trailing Stop 已挂: 回撤{trail_pct}%触发 | 订单ID={trail_order.id}")
        return entry_order, trail_order
    except Exception as e:
        print(f" ⚠️Trailing Stop 提交失败: {e}")
        return entry_order, None

def close_position_safely(trading_client, symbol):
    try:
        open_orders = api_call_with_retry(
            trading_client.get_orders, status="open", symbols=[symbol]
        )
        for o in open_orders:
            try:
                api_call_with_retry(trading_client.cancel_order_by_id, o.id)
                print(f" 🗑️已取消订单: {o.id}")
            except Exception as e:
                print(f" ⚠️取消订单失败: {e}")
        time.sleep(1.0)
        api_call_with_retry(trading_client.close_position, symbol)
        print(f" ⏰ 平仓: {symbol}")
    except Exception as e:
        print(f" ⚠️平仓异常: {e}")

def enforce_no_margin(trading_client):
    try:
        if hasattr(trading_client, "patch_account_configurations"):
            api_call_with_retry(
                trading_client.patch_account_configurations,
                {"max_margin_multiplier": "1"}
            )
            print("✅ 已通过API 设置 max_margin_multiplier=1")
        else:
            print("⚠️ 当前 alpaca-py 版本不支持 API 修改保证金乘数，"
                  "请手动在 Alpaca Dashboard 中确认 Max Margin Multiplier = 1")
    except Exception as e:
        print(f"⚠️ API 设置保证金失败（不影响运行）: {e}")

# ══════════════════════════════════════════════
# 辅助：获取持仓的初始止损价
# ══════════════════════════════════════════════
def get_initial_stop(side, range_low, range_high):
    return range_low if side == "buy" else range_high

# ══════════════════════════════════════════════
# 主逻辑
# ══════════════════════════════════════════════
def main():
    print("=" * 60)
    print(f" ORB 策略单次运行 | {now_et().strftime('%Y-%m-%d %H:%M:%S ET')}")
    print("=" * 60)

    if not is_market_day():
        print("非交易日，退出")
        return

    if is_before_open():
        print("开盘前，退出")
        return

    trading_client = TradingClient(
        ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER
    )
    data_client = StockHistoricalDataClient(
        ALPACA_API_KEY, ALPACA_SECRET_KEY
    )

    enforce_no_margin(trading_client)

    # ── 补平仓兜底：如果已过 15:43 且仍有持仓，直接平仓 ──
    if is_eod():
        try:
            positions = api_call_with_retry(trading_client.get_all_positions)
            if positions:
                print("⏰ 已过平仓时间，执行补平仓")
                for p in positions:
                    close_position_safely(trading_client, p.symbol)
                print("补平仓完成")
                return
        except Exception as e:
            print(f"补平仓检查失败: {e}")

    try:
        account = api_call_with_retry(trading_client.get_account)
        print(f"账户乘数 (multiplier): {account.multiplier}")
        print(f"现金 (cash): ${float(account.cash):,.2f}")
        print(f"购买力 (buying_power): ${float(account.buying_power):,.2f}")
        print(f"净值 (equity): ${float(account.equity):,.2f}")
        available_cash = float(account.cash)
        if account.multiplier != "1":
            print(f" ⚠️警告：账户乘数为 {account.multiplier}，仍在使用保证金")
    except Exception as e:
        print(f"获取账户失败: {e}")
        send_email("ORB 策略-获取账户失败", str(e))
        return

    try:
        positions = {p.symbol: p for p in api_call_with_retry(trading_client.get_all_positions)}
    except Exception as e:
        print(f"获取持仓失败: {e}")
        positions = {}

    total_position_value = 0.0
    for p in positions.values():
        try:
            total_position_value += abs(float(p.market_value))
        except Exception:
            pass

    now = now_et()
    current_minute_index = now.hour * 60 + now.minute

    for sym in SYMBOLS:
        print(f"\n── {sym} ──")
        strat = ORBStrategy(sym)

        if not is_after_orb():
            print(" 开盘区间未完成，退出")
            continue

        bars = fetch_opening_range_bars(data_client, sym)
        if bars is None or bars.empty:
            print(" 无法获取开盘区间数据")
            continue

        if not strat.set_opening_range(bars):
            continue

        # ── 开盘区间过宽过滤 ──
        atr = fetch_atr(data_client, sym, ATR_LOOKBACK_DAYS)
        if atr is not None:
            range_width = strat.range_high - strat.range_low
            if range_width > MAX_RANGE_ATR_MULTIPLIER * atr:
                print(f" ⛔区间过宽 ({range_width:.2f} > {MAX_RANGE_ATR_MULTIPLIER} * ATR={atr:.2f})，跳过今日")
                continue

        prev_close, today_open = fetch_prev_close_and_open(data_client, sym)
        strat.prev_close = prev_close
        strat.today_open = today_open

        skip_gap, size_mult = strat.check_gap()
        if skip_gap:
            continue

        strat.daily_ema = fetch_daily_ema(data_client, sym, TREND_EMA_PERIOD)

        latest = fetch_latest_bar(data_client, sym)
        if latest is None:
            print(" 无法获取最新价格")
            continue

        price = float(latest["close"])
        print(f" 最新价: {price:.2f}")

        today_orders = fetch_today_orders(trading_client, sym)
        date_str = now_et().strftime('%Y%m%d')

        # ── 从订单历史恢复 trade_count 和冷却状态 ──
        entry_filled = [
            o for o in today_orders
            if o.client_order_id and o.client_order_id.startswith(f"ORB_{sym}_{date_str}")
            and o.status == OrderStatus.FILLED
        ]
        strat.trade_count = len(entry_filled)

        trail_filled_orders = [
            o for o in today_orders
            if o.client_order_id and "_TRAIL" in o.client_order_id
            and o.status == OrderStatus.FILLED
        ]
        if len(trail_filled_orders) >= MAX_TRADES_PER_DAY:
            strat.traded_today = True
        else:
            strat.traded_today = False

        if trail_filled_orders:
            last_trail = max(trail_filled_orders, key=lambda o: o.filled_at)
            if last_trail.filled_at:
                last_filled_et = last_trail.filled_at.astimezone(ET)
                last_minute_index = last_filled_et.hour * 60 + last_filled_et.minute
                cooldown_end = last_minute_index + COOLDOWN_BARS
                if current_minute_index < cooldown_end:
                    strat.cooldown_until = cooldown_end

        if sym in positions:
            pos = positions[sym]
            side = "buy" if float(pos.qty) > 0 else "sell"
            entry = float(pos.avg_entry_price)
            qty = abs(int(float(pos.qty)))

            initial_stop = get_initial_stop(side, strat.range_low, strat.range_high)
            if initial_stop is None:
                print(" 无法获取初始止损，跳过持仓管理")
                continue
            risk_per_share = abs(entry - initial_stop)
            if risk_per_share <= 0:
                print(" 风险距离为0，跳过持仓管理")
                continue

            # ── 自动保本 ──
            if AUTO_BE_ENABLED:
                be_trail_orders = [
                    o for o in today_orders
                    if o.client_order_id and "_BE" in o.client_order_id
                ]
                be_active = any(
                    o.status in (OrderStatus.NEW, OrderStatus.ACCEPTED)
                    for o in be_trail_orders
                )
                if not be_active:
                    if side == "buy":
                        favorable_move = price - entry
                    else:
                        favorable_move = entry - price
                    if favorable_move >= AUTO_BE_TRIGGER_R * risk_per_share:
                        print(f" 🔒 浮盈达到 {AUTO_BE_TRIGGER_R}R，收紧 Trailing Stop 至 {AUTO_BE_TRAIL_PERCENT}%")
                        trail_orders = [
                            o for o in today_orders
                            if o.order_type and "trailing" in str(o.order_type).lower()
                            and o.status in (OrderStatus.NEW, OrderStatus.ACCEPTED)
                        ]
                        for o in trail_orders:
                            try:
                                api_call_with_retry(trading_client.cancel_order_by_id, o.id)
                                print(f" 已取消旧 Trailing Stop: {o.id}")
                            except Exception as e:
                                print(f" 取消旧 Trailing Stop 失败: {e}")
                        trail_side = OrderSide.SELL if side == "buy" else OrderSide.BUY
                        trail_req = TrailingStopOrderRequest(
                            symbol=sym,
                            qty=qty,
                            side=trail_side,
                            time_in_force=TimeInForce.DAY,
                            trail_percent=AUTO_BE_TRAIL_PERCENT,
                            client_order_id=f"ORB_{sym}_{date_str}_BE_TRAIL",
                        )
                        try:
                            new_trail = api_call_with_retry(trading_client.submit_order, trail_req)
                            print(f" 🔄 新 Trailing Stop 已挂: 回撤{AUTO_BE_TRAIL_PERCENT}%触发 | 订单ID={new_trail.id}")
                        except Exception as e:
                            print(f" ⚠️新 Trailing Stop 提交失败: {e}")
                        continue

            # ── 时间止损 ──
            if TIME_STOP_MINUTES > 0:
                entry_orders = [
                    o for o in today_orders
                    if o.client_order_id and o.client_order_id.startswith(
                        f"ORB_{sym}_{date_str}"
                    ) and o.status == OrderStatus.FILLED
                ]
                if entry_orders:
                    entry_order = entry_orders[0]
                    if entry_order.filled_at:
                        filled_at = entry_order.filled_at.astimezone(ET)
                        elapsed_minutes = (now_et() - filled_at).total_seconds() / 60
                        if elapsed_minutes >= TIME_STOP_MINUTES:
                            if side == "buy":
                                favorable_move = price - entry
                            else:
                                favorable_move = entry - price
                            if favorable_move < AUTO_BE_TRIGGER_R * risk_per_share:
                                print(f" ⏰ 时间止损触发（持仓 {elapsed_minutes:.0f} 分钟，未达 1R），平仓")
                                close_position_safely(trading_client, sym)
                                continue

            # 检查 Trailing Stop 是否已成交
            trail_orders = [
                o for o in today_orders
                if o.order_type and "trailing" in str(o.order_type).lower()
            ]
            trail_filled = any(
                o.status == OrderStatus.FILLED for o in trail_orders
            )
            if trail_filled:
                print(" ✅ Trailing Stop 已触发，持仓已平")
                continue

            # 补挂 Trailing Stop
            trail_active = any(
                o.status in (OrderStatus.NEW, OrderStatus.ACCEPTED)
                for o in trail_orders
            )
            if not trail_active:
                print(" ⚠️无活跃Trailing Stop，补挂")
                trail_side = OrderSide.SELL if side == "buy" else OrderSide.BUY
                trail_req = TrailingStopOrderRequest(
                    symbol=sym,
                    qty=qty,
                    side=trail_side,
                    time_in_force=TimeInForce.DAY,
                    trail_percent=TRAIL_PERCENT,
                    client_order_id=f"ORB_{sym}_{date_str}_TRAIL_RE",
                )
                try:
                    api_call_with_retry(trading_client.submit_order, trail_req)
                    print(f" 🔄 补挂Trailing Stop 成功")
                except Exception as e:
                    print(f" ⚠️补挂失败: {e}")
            continue

        if not strat.traded_today:
            signal = strat.check_entry(latest, current_minute_index)
            if signal:
                entry = signal["entry"]
                stop = signal["stop"]
                qty = strat.calc_qty(available_cash, entry, stop, size_mult)
                required_cash = qty * entry
                remaining_cash = available_cash - total_position_value
                if required_cash > remaining_cash:
                    qty = int(remaining_cash / entry)
                    print(f" ⚠️剩余现金不足，调整为 {qty} 股")
                if qty > 0:
                    print(f"\n🚀 突破信号: {sym} "
                          f"{signal['side'].upper()} @ {entry:.2f}")
                    strat.traded_today = True
                    tag = f"ORB_{sym}_{date_str}_{int(time.time())}"
                    entry_order, trail_order = place_entry_and_trailing_stop(
                        trading_client, sym, qty,
                        signal["side"], TRAIL_PERCENT, tag
                    )
                    if entry_order:
                        total_position_value += qty * entry
                        send_email(
                            f"ORB 策略-入场通知 {sym}",
                            f"标的: {sym}\n 方向: {signal['side']}\n"
                            f"数量: {qty}\n 入场价: {entry:.2f}\n"
                            f"止损价: {stop:.2f}\n 时间: {now_et()}"
                        )
                    else:
                        strat.traded_today = False
                else:
                    print(" 仓位计算为 0，跳过")
            else:
                print(" 无突破信号")

        if is_eod() and sym in positions:
            close_position_safely(trading_client, sym)

    print("\n 本次运行完成")


if __name__ == "__main__":
    main()
