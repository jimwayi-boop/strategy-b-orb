# main.py
import os
import time
import smtplib
import pytz
import pandas as pd
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, TrailingStopOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import (
    OrderSide, TimeInForce, OrderStatus, QueryOrderStatus,
)
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import *
from orb_strategy import ORBStrategy

ET = pytz.timezone("America/New_York")


# ══════════════════════════════════════════════
#  邮件通知
# ══════════════════════════════════════════════

def send_email(subject, body):
    mail_user = os.getenv("MAIL_USERNAME")
    mail_pass = os.getenv("MAIL_PASSWORD")
    if not mail_user or not mail_pass:
        print("⚠️ 未配置邮箱，跳过邮件发送")
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
        print(f"⚠️ 邮件发送失败: {e}")


# ══════════════════════════════════════════════
#  时间判断
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
#  数据获取
# ══════════════════════════════════════════════

def fetch_opening_range_bars(data_client, symbol):
    today = now_et().strftime("%Y-%m-%d")
    start = f"{today}T09:30:00-04:00"
    end   = f"{today}T09:45:00-04:00"
    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start, end=end, feed="iex",
        )
        bars = data_client.get_stock_bars(req)
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
    end   = now_et()
    start = end - timedelta(minutes=3)
    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start.strftime("%Y-%m-%dT%H:%M:%S%z"),
            end=end.strftime("%Y-%m-%dT%H:%M:%S%z"),
            feed="iex",
        )
        bars = data_client.get_stock_bars(req)
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
            start=yesterday, end=today, feed="iex",
        )
        bars = data_client.get_stock_bars(req)
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

def fetch_daily_sma(data_client, symbol, lookback=5):
    end   = now_et()
    start = end - timedelta(days=lookback + 5)
    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            feed="iex",
        )
        bars = data_client.get_stock_bars(req)
        df = bars.df
        if df.empty or len(df) < lookback:
            return None
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        return float(df["close"].tail(lookback).mean())
    except Exception as e:
        print(f"[{symbol}] 获取日线SMA失败: {e}")
        return None

def fetch_today_orders(trading_client, symbol):
    try:
        req = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            symbols=[symbol],
            after=now_et().strftime("%Y-%m-%dT00:00:00-04:00"),
        )
        return trading_client.get_orders(req)
    except Exception as e:
        print(f"[{symbol}] 查询订单失败: {e}")
        return []


# ══════════════════════════════════════════════
#  订单执行
# ══════════════════════════════════════════════

def wait_for_order_filled(trading_client, order_id, max_wait=10, interval=0.5):
    waited = 0
    while waited < max_wait:
        try:
            o = trading_client.get_order_by_id(order_id)
            if o.status == OrderStatus.FILLED:
                print(f"  ✔️ 订单 {order_id} 已成交")
                return True
            if o.status in (OrderStatus.CANCELED, OrderStatus.REJECTED,
                            OrderStatus.EXPIRED):
                print(f"  ❌ 订单 {order_id} 状态: {o.status}")
                return False
        except Exception:
            pass
        time.sleep(interval)
        waited += interval
    print(f"  ⚠️ 等待成交超时 ({max_wait}s)")
    return False

def place_entry_and_trailing_stop(trading_client, symbol, qty, side, trail_pct):
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

    entry_req = MarketOrderRequest(
        symbol=symbol, qty=qty, side=order_side,
        time_in_force=TimeInForce.DAY,
    )
    entry_order = trading_client.submit_order(entry_req)
    print(f"  ✅ 入场: {side} {qty} 股 {symbol} | 订单ID={entry_order.id}")

    filled = wait_for_order_filled(trading_client, entry_order.id)
    if not filled:
        print(f"  ⚠️ 入场未成交，取消Trailing Stop步骤")
        return entry_order, None

    trail_side = OrderSide.SELL if side == "buy" else OrderSide.BUY
    trail_req = TrailingStopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=trail_side,
        time_in_force=TimeInForce.DAY,
        trail_percent=trail_pct,
    )
    try:
        trail_order = trading_client.submit_order(trail_req)
        print(f"  🔄 Trailing Stop已挂: 回撤{trail_pct}%触发 | 订单ID={trail_order.id}")
        return entry_order, trail_order
    except Exception as e:
        print(f"  ⚠️ Trailing Stop提交失败: {e}")
        return entry_order, None

def close_position_safely(trading_client, symbol):
    try:
        open_orders = trading_client.get_orders(
            status="open", symbols=[symbol]
        )
        for o in open_orders:
            try:
                trading_client.cancel_order_by_id(o.id)
                print(f"  🗑️ 已取消订单: {o.id}")
            except Exception as e:
                print(f"  ⚠️ 取消订单失败: {e}")

        time.sleep(1.0)
        trading_client.close_position(symbol)
        print(f"  ⏰ 平仓: {symbol}")
    except Exception as e:
        print(f"  ⚠️ 平仓异常: {e}")

def enforce_no_margin(trading_client):
    try:
        trading_client.patch_account_configurations(
            {"max_margin_multiplier": "1"}
        )
        print("✅ 已通过API设置 max_margin_multiplier=1")
    except Exception as e:
        print(f"⚠️ API设置保证金失败（不影响运行）: {e}")


# ══════════════════════════════════════════════
#  主逻辑
# ══════════════════════════════════════════════

def main():
    print("=" * 60)
    print(f"  ORB 策略单次运行 | {now_et().strftime('%Y-%m-%d %H:%M:%S ET')}")
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

    try:
        account = trading_client.get_account()
        print(f"账户乘数 (multiplier): {account.multiplier}")
        print(f"现金 (cash): ${float(account.cash):,.2f}")
        print(f"购买力 (buying_power): ${float(account.buying_power):,.2f}")
        print(f"净值 (equity): ${float(account.equity):,.2f}")

        available_cash = float(account.cash)

        if account.multiplier != "1":
            print(f"⚠️ 警告：账户乘数为 {account.multiplier}，仍在使用保证金")
    except Exception as e:
        print(f"获取账户失败: {e}")
        send_email("ORB策略-获取账户失败", str(e))
        return

    try:
        positions = {p.symbol: p for p in trading_client.get_all_positions()}
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
            print("  开盘区间未完成，退出")
            continue

        bars = fetch_opening_range_bars(data_client, sym)
        if bars is None or bars.empty:
            print("  无法获取开盘区间数据")
            continue

        if not strat.set_opening_range(bars):
            continue

        prev_close, today_open = fetch_prev_close_and_open(data_client, sym)
        strat.prev_close = prev_close
        strat.today_open = today_open

        skip_gap, size_mult = strat.check_gap()
        if skip_gap:
            continue

        strat.daily_sma = fetch_daily_sma(
            data_client, sym, TREND_LOOKBACK_DAYS
        )

        latest = fetch_latest_bar(data_client, sym)
        if latest is None:
            print("  无法获取最新价格")
            continue
        price = float(latest["close"])
        print(f"  最新价: {price:.2f}")

        today_orders = fetch_today_orders(trading_client, sym)
        traded_today = any(
            o.client_order_id and o.client_order_id.startswith(
                f"ORB_{sym}_{now_et().strftime('%Y%m%d')}"
            ) and o.status in ("filled", "partially_filled", "accepted", "new")
            for o in today_orders
        )
        strat.traded_today = traded_today

        if sym in positions:
            pos = positions[sym]
            side = "buy" if float(pos.qty) > 0 else "sell"

            trail_orders = [
                o for o in today_orders
                if o.order_type and "trailing" in str(o.order_type).lower()
            ]
            trail_filled = any(
                o.status == OrderStatus.FILLED for o in trail_orders
            )

            if trail_filled:
                print("  ✅ Trailing Stop已触发，持仓已平")
                continue

            trail_active = any(
                o.status in (OrderStatus.NEW, OrderStatus.ACCEPTED)
                for o in trail_orders
            )
            if not trail_active:
                print("  ⚠️ 无活跃Trailing Stop，补挂")
                qty = abs(int(float(pos.qty)))
                trail_side = OrderSide.SELL if side == "buy" else OrderSide.BUY
                trail_req = TrailingStopOrderRequest(
                    symbol=sym, qty=qty, side=trail_side,
                    time_in_force=TimeInForce.DAY,
                    trail_percent=TRAIL_PERCENT,
                )
                try:
                    trading_client.submit_order(trail_req)
                    print(f"  🔄 补挂Trailing Stop成功")
                except Exception as e:
                    print(f"  ⚠️ 补挂失败: {e}")
            continue

        if not strat.traded_today:
            signal = strat.check_entry(latest, current_minute_index)

            if signal:
                entry = signal["entry"]
                stop  = signal["stop"]
                qty = strat.calc_qty(available_cash, entry, stop, size_mult)

                required_cash = qty * entry
                remaining_cash = available_cash - total_position_value
                if required_cash > remaining_cash:
                    qty = int(remaining_cash / entry)
                    print(f"  ⚠️ 剩余现金不足，调整为 {qty} 股")

                if qty > 0:
                    print(f"\n🚀 突破信号: {sym} "
                          f"{signal['side'].upper()} @ {entry:.2f}")

                    strat.traded_today = True

                    entry_order, trail_order = place_entry_and_trailing_stop(
                        trading_client, sym, qty,
                        signal["side"], TRAIL_PERCENT
                    )

                    if entry_order:
                        total_position_value += qty * entry
                        send_email(
                            f"ORB策略-入场通知 {sym}",
                            f"标的: {sym}\n方向: {signal['side']}\n"
                            f"数量: {qty}\n入场价: {entry:.2f}\n"
                            f"止损价: {stop:.2f}\n时间: {now_et()}"
                        )
                    else:
                        strat.traded_today = False
                else:
                    print("  仓位计算为 0，跳过")
            else:
                print("  无突破信号")

        if is_eod() and sym in positions:
            close_position_safely(trading_client, sym)

    print("\n本次运行完成")


if __name__ == "__main__":
    main()
