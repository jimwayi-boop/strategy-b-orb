# main.py
import os
import time
import smtplib
import random
import json
import pytz
import pandas as pd
import numpy as np
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
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
# API 重试
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
def send_email(subject, body, html=False):
    mail_user = os.getenv("MAIL_USERNAME")
    mail_pass = os.getenv("MAIL_PASSWORD")
    if not mail_user or not mail_pass:
        print(" ⚠️未配置邮箱，跳过邮件发送")
        return
    msg = MIMEText(body, "html" if html else "plain", "utf-8")
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
# 参数版本化日志
# ══════════════════════════════════════════════
def log_params():
    params = {
        "RISK_PER_TRADE": RISK_PER_TRADE,
        "MAX_TRADES_PER_DAY": MAX_TRADES_PER_DAY,
        "TRAIL_PERCENT": TRAIL_PERCENT,
        "VOLUME_MULTIPLIER": VOLUME_MULTIPLIER,
        "MIN_RANGE_PCT": MIN_RANGE_PCT,
        "TRADE_CUTOFF": f"{TRADE_CUTOFF_HOUR}:{TRADE_CUTOFF_MIN:02d}",
        "EOD_CLOSE": f"{EOD_CLOSE_HOUR}:{EOD_CLOSE_MIN:02d}",
        "MAX_TOTAL_RISK_PCT": MAX_TOTAL_RISK_PCT,
        "MAX_CONSECUTIVE_LOSSES": MAX_CONSECUTIVE_LOSSES,
        "TREND_EMA_PERIOD": TREND_EMA_PERIOD,
        "VOL_ADJUST_ENABLED": VOL_ADJUST_ENABLED,
        "SCALE_OUT_ENABLED": SCALE_OUT_ENABLED,
        "LIMIT_ENTRY_ENABLED": LIMIT_ENTRY_ENABLED,
    }
    print(f"📋 当前参数: {json.dumps(params, ensure_ascii=False)}")
    return params

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

def is_past_cutoff():
    n = now_et()
    cutoff_min = TRADE_CUTOFF_HOUR * 60 + TRADE_CUTOFF_MIN
    now_min = n.hour * 60 + n.minute
    return now_min >= cutoff_min

# ══════════════════════════════════════════════
# 数据获取
# ══════════════════════════════════════════════
def fetch_opening_range_bars(data_client, symbol):
    today = now_et().strftime("%Y-%m-%d")
    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=f"{today}T09:30:00-04:00",
            end=f"{today}T09:45:00-04:00",
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
            start=yesterday, end=today, feed="iex",
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
            return None, None
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        closes = df["close"].astype(float)
        ema_series = closes.ewm(span=period, adjust=False).mean()
        ema_current = float(ema_series.iloc[-1])
        ema_prev = float(ema_series.iloc[-TREND_EMA_SLOPE_LOOKBACK - 1]) if len(ema_series) > TREND_EMA_SLOPE_LOOKBACK else ema_current
        ema_slope = ema_current - ema_prev
        return ema_current, ema_slope
    except Exception as e:
        print(f"[{symbol}] 获取日线EMA 失败: {e}")
        return None, None

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
    """等待订单成交，支持部分成交处理"""
    waited = 0
    last_status = None
    while waited < max_wait:
        try:
            o = api_call_with_retry(trading_client.get_order_by_id, order_id)
            if o.status == OrderStatus.FILLED:
                print(f" ✔️订单 {order_id} 已完全成交")
                return True, int(float(o.filled_qty))
            if o.status == OrderStatus.PARTIALLY_FILLED:
                last_status = o
                print(f" 🔶订单 {order_id} 部分成交: {o.filled_qty}/{o.qty}")
            if o.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                print(f" ❌ 订单 {order_id} 状态: {o.status}")
                filled_qty = int(float(o.filled_qty)) if o.filled_qty else 0
                return False, filled_qty
        except Exception:
            pass
        time.sleep(interval)
        waited += interval

    # 超时：如果有部分成交，返回已成交数量
    if last_status:
        filled_qty = int(float(last_status.filled_qty))
        print(f" ⚠️等待成交超时，部分成交 {filled_qty} 股")
        return False, filled_qty

    print(f" ⚠️等待成交超时，未成交")
    return False, 0

def place_entry_and_trailing_stop(trading_client, symbol, qty, side, trail_pct, tag, entry_price):
    """入场 + 挂 Trailing Stop。支持限价单和部分成交处理。"""
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

    if SHADOW_MODE:
        print(f" 👻 [影子模式] 不会提交订单: {side} {qty} 股 {symbol} @ ~{entry_price:.2f}")
        log_shadow_trade(symbol, side, qty, entry_price, tag)
        return None, None

    # 限价单 or 市价单
    if LIMIT_ENTRY_ENABLED:
        if side == "buy":
            limit_price = round(entry_price * (1 + LIMIT_ENTRY_OFFSET), 2)
        else:
            limit_price = round(entry_price * (1 - LIMIT_ENTRY_OFFSET), 2)
        entry_req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
            client_order_id=tag,
        )
        print(f" 📤 限价入场: {side} {qty} 股 {symbol} @ {limit_price:.2f}")
    else:
        entry_req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            client_order_id=tag,
        )
        print(f" 📤 市价入场: {side} {qty} 股 {symbol}")

    entry_order = api_call_with_retry(trading_client.submit_order, entry_req)
    print(f" ✅ 入场订单已提交 | 订单ID={entry_order.id}")

    # 等待成交（支持部分成交）
    filled, filled_qty = wait_for_order_filled(trading_client, entry_order.id)

    if filled_qty <= 0:
        print(f" ⚠️入场未成交，取消Trailing Stop 步骤")
        return entry_order, None

    if not filled and filled_qty > 0:
        print(f" ⚠️部分成交 {filled_qty} 股，为已成交部分挂Trailing Stop")

    # 用已成交数量挂Trailing Stop
    trail_side = OrderSide.SELL if side == "buy" else OrderSide.BUY
    trail_req = TrailingStopOrderRequest(
        symbol=symbol,
        qty=filled_qty,
        side=trail_side,
        time_in_force=TimeInForce.DAY,
        trail_percent=trail_pct,
        client_order_id=tag + "_TRAIL",
    )
    try:
        trail_order = api_call_with_retry(trading_client.submit_order, trail_req)
        print(f" 🔄 Trailing Stop 已挂: 回撤{trail_pct}%触发 | 数量={filled_qty} | 订单ID={trail_order.id}")
        return entry_order, trail_order
    except Exception as e:
        print(f" ⚠️Trailing Stop 提交失败: {e}")
        return entry_order, None

def log_shadow_trade(symbol, side, qty, price, tag):
    """影子模式记录"""
    shadow_file = "shadow_trades.jsonl"
    record = {
        "timestamp": now_et().isoformat(),
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "tag": tag,
    }
    try:
        with open(shadow_file, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f" 👻 影子记录已写入 {shadow_file}")
    except Exception as e:
        print(f" ⚠️影子记录写入失败: {e}")

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
# 组合层风险检查
# ══════════════════════════════════════════════
def check_portfolio_risk(positions, total_position_value, available_cash,
                         new_risk_dollar, new_direction, current_risks):
    """检查组合层风险限制"""
    # 1. 同时持仓数量
    if len(positions) >= MAX_CONCURRENT_POSITIONS:
        print(f" 🚫 组合风险: 同时持仓已达 {MAX_CONCURRENT_POSITIONS} 个上限")
        return False

    # 2. 单日总风险
    total_risk_pct = new_risk_dollar / available_cash
    current_total = sum(current_risks.values()) if current_risks else 0
    if (current_total + total_risk_pct) > MAX_TOTAL_RISK_PCT:
        print(f" 🚫 组合风险: 总风险 {current_total+total_risk_pct:.1%} > {MAX_TOTAL_RISK_PCT:.1%} 上限")
        return False

    # 3. 同方向风险
    same_dir_risk = sum(
        r for d, r in current_risks.items() if d == new_direction
    ) if current_risks else 0
    if (same_dir_risk + total_risk_pct) > MAX_SAME_DIRECTION_RISK:
        print(f" 🚫 组合风险: {new_direction} 方向风险 {same_dir_risk+total_risk_pct:.1%} > {MAX_SAME_DIRECTION_RISK:.1%} 上限")
        return False

    return True

# ══════════════════════════════════════════════
# 连续亏损熔断
# ══════════════════════════════════════════════
def check_circuit_breaker(trading_client):
    """检查是否触发连续亏损熔断"""
    try:
        # 查询最近 N 天的已实现盈亏
        from alpaca.trading.requests import GetOrdersRequest
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            after=(now_et() - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00-04:00"),
        )
        orders = api_call_with_retry(trading_client.get_orders, req)
        # 简化判断：查看最近已成交的卖出订单是否连续亏损
        # 实际盈亏需要从账户活动或持仓历史计算
        # 这里先用占位逻辑
        return False  # 待完善
    except Exception as e:
        print(f" ⚠️熔断检查失败: {e}")
        return False

# ══════════════════════════════════════════════
# 多级止盈检查
# ══════════════════════════════════════════════
def check_scale_out(trading_client, symbol, position, price, entry, initial_stop,
                    today_orders, date_str):
    """检查是否触发多级止盈"""
    if not SCALE_OUT_ENABLED:
        return False

    side = "buy" if float(position.qty) > 0 else "sell"
    risk_per_share = abs(entry - initial_stop)
    if risk_per_share <= 0:
        return False

    if side == "buy":
        favorable = price - entry
    else:
        favorable = entry - price

    r_multiple = favorable / risk_per_share
    qty = abs(int(float(position.qty)))

    # 检查已执行到哪一级
    executed_levels = set()
    for o in today_orders:
        if o.client_order_id and "_TP" in o.client_order_id and o.status == OrderStatus.FILLED:
            for i, level in enumerate(SCALE_OUT_LEVELS):
                if f"_TP{i+1}" in o.client_order_id:
                    executed_levels.add(i)

    for i, level in enumerate(SCALE_OUT_LEVELS):
        if i in executed_levels:
            continue
        if r_multiple >= level["r_multiple"]:
            exit_qty = max(int(qty * level["exit_pct"]), 1)
            if exit_qty >= qty:
                exit_qty = qty

            print(f" 🎯 触发 TP{i+1} ({level['r_multiple']}R), 平仓 {exit_qty} 股")

            order_side = OrderSide.SELL if side == "buy" else OrderSide.BUY
            tp_req = MarketOrderRequest(
                symbol=symbol,
                qty=exit_qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                client_order_id=f"ORB_{symbol}_{date_str}_TP{i+1}",
            )
            try:
                api_call_with_retry(trading_client.submit_order, tp_req)
                print(f" ✅ TP{i+1} 平仓完成")
            except Exception as e:
                print(f" ⚠️TP{i+1} 平仓失败: {e}")
            return True

    return False

# ══════════════════════════════════════════════
# 交易日面板
# ══════════════════════════════════════════════
def build_daily_panel(trading_client, data_client):
    """构建交易日面板"""
    try:
        account = api_call_with_retry(trading_client.get_account)
        positions = api_call_with_retry(trading_client.get_all_positions)

        panel = {
            "date": now_et().strftime("%Y-%m-%d"),
            "account": {
                "cash": float(account.cash),
                "equity": float(account.equity),
                "buying_power": float(account.buying_power),
            },
            "positions": [],
            "today_trades": [],
        }

        for p in positions:
            panel["positions"].append({
                "symbol": p.symbol,
                "qty": p.qty,
                "avg_entry": float(p.avg_entry_price),
                "current_price": float(p.current_price) if p.current_price else None,
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl else 0,
                "unrealized_plpc": float(p.unrealized_plpc) if p.unrealized_plpc else 0,
            })

        for sym in SYMBOLS:
            orders = fetch_today_orders(trading_client, sym)
            for o in orders:
                if o.status == OrderStatus.FILLED:
                    panel["today_trades"].append({
                        "symbol": o.symbol,
                        "side": str(o.side),
                        "qty": o.filled_qty,
                        "price": o.filled_avg_price,
                        "time": str(o.filled_at) if o.filled_at else "",
                        "client_order_id": o.client_order_id or "",
                    })

        return panel
    except Exception as e:
        print(f"构建面板失败: {e}")
        return None

def format_panel_html(panel):
    """将面板格式化为HTML邮件"""
    if not panel:
        return "<p>无法获取面板数据</p>"

    html = f"""
    <h2>📊 ORB 策略交易日面板 — {panel['date']}</h2>

    <h3>账户概览</h3>
    <table border="1" cellpadding="5">
        <tr><td>现金</td><td>${panel['account']['cash']:,.2f}</td></tr>
        <tr><td>净值</td><td>${panel['account']['equity']:,.2f}</td></tr>
        <tr><td>购买力</td><td>${panel['account']['buying_power']:,.2f}</td></tr>
    </table>

    <h3>当前持仓</h3>
    """

    if panel["positions"]:
        html += """
        <table border="1" cellpadding="5">
            <tr><th>标的</th><th>数量</th><th>成本</th><th>现价</th>
                <th>市值</th><th>未实现盈亏</th><th>盈亏%</th></tr>
        """
        for p in panel["positions"]:
            pl_color = "green" if p["unrealized_pl"] >= 0 else "red"
            html += f"""
            <tr>
                <td>{p['symbol']}</td>
                <td>{p['qty']}</td>
                <td>${p['avg_entry']:.2f}</td>
                <td>${p['current_price']:.2f}</td>
                <td>${p['market_value']:,.2f}</td>
                <td style="color:{pl_color}">${p['unrealized_pl']:,.2f}</td>
                <td style="color:{pl_color}">{p['unrealized_plpc']*100:.2f}%</td>
            </tr>
            """
        html += "</table>"
    else:
        html += "<p>无持仓</p>"

    html += "<h3>今日成交</h3>"
    if panel["today_trades"]:
        html += """
        <table border="1" cellpadding="5">
            <tr><th>标的</th><th>方向</th><th>数量</th><th>价格</th><th>时间</th></tr>
        """
        for t in panel["today_trades"]:
            html += f"""
            <tr>
                <td>{t['symbol']}</td>
                <td>{t['side']}</td>
                <td>{t['qty']}</td>
                <td>${float(t['price']):.2f}</td>
                <td>{t['time']}</td>
            </tr>
            """
        html += "</table>"
    else:
        html += "<p>无成交</p>"

    return html

# ══════════════════════════════════════════════
# 主逻辑
# ══════════════════════════════════════════════
def main():
    print("=" * 60)
    print(f" ORB 策略单次运行 | {now_et().strftime('%Y-%m-%d %H:%M:%S ET')}")
    print("=" * 60)

    log_params()

    if SHADOW_MODE:
        print(" 👻 影子模式已启用：不会提交任何订单")

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

    # 补平仓兜底
    if is_eod():
        try:
            positions = api_call_with_retry(trading_client.get_all_positions)
            if positions:
                print("⏰ 已过平仓时间，执行补平仓")
                for p in positions:
                    close_position_safely(trading_client, p.symbol)
                print("补平仓完成")
        except Exception as e:
            print(f"补平仓检查失败: {e}")

        # EOD时发送交易日面板
        panel = build_daily_panel(trading_client, data_client)
        if panel:
            html = format_panel_html(panel)
            send_email(f"📊 ORB 交易日面板 {panel['date']}", html, html=True)
        return

    try:
        account = api_call_with_retry(trading_client.get_account)
        print(f"账户乘数: {account.multiplier}")
        print(f"现金: ${float(account.cash):,.2f}")
        print(f"净值: ${float(account.equity):,.2f}")
        available_cash = float(account.cash)
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
    current_risks = {}  # symbol -> risk_pct
    for sym, p in positions.items():
        try:
            total_position_value += abs(float(p.market_value))
            entry = float(p.avg_entry_price)
            qty = abs(int(float(p.qty)))
            # 估算当前风险
            current_risks[sym] = 0.02  # 占位，实际应从初始止损计算
        except Exception:
            pass

    now = now_et()
    current_minute_index = now.hour * 60 + now.minute
    date_str = now_et().strftime('%Y%m%d')

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

        # ATR过滤
        atr = fetch_atr(data_client, sym, ATR_LOOKBACK_DAYS)
        strat.atr = atr
        if atr is not None:
            range_width = strat.range_high - strat.range_low
            if range_width > MAX_RANGE_ATR_MULTIPLIER * atr:
                print(f" ⛔区间过宽 ({range_width:.2f} > {MAX_RANGE_ATR_MULTIPLIER}*ATR={atr:.2f})，跳过今日")
                continue

        prev_close, today_open = fetch_prev_close_and_open(data_client, sym)
        strat.prev_close = prev_close
        strat.today_open = today_open

        skip_gap, size_mult = strat.check_gap()
        if skip_gap:
            continue

        # EMA + 斜率
        ema, ema_slope = fetch_daily_ema(data_client, sym, TREND_EMA_PERIOD)
        strat.daily_ema = ema
        strat.daily_ema_slope = ema_slope

        latest = fetch_latest_bar(data_client, sym)
        if latest is None:
            print(" 无法获取最新价格")
            continue

        price = float(latest["close"])
        print(f" 最新价: {price:.2f}")

        today_orders = fetch_today_orders(trading_client, sym)

        # 从订单历史恢复状态
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
        strat.traded_today = len(trail_filled_orders) >= MAX_TRADES_PER_DAY

        if trail_filled_orders:
            last_trail = max(trail_filled_orders, key=lambda o: o.filled_at if o.filled_at else now)
            if last_trail.filled_at:
                last_filled_et = last_trail.filled_at.astimezone(ET)
                last_minute_index = last_filled_et.hour * 60 + last_filled_et.minute
                cooldown_end = last_minute_index + COOLDOWN_BARS
                if current_minute_index < cooldown_end:
                    strat.cooldown_until = cooldown_end

        # ── 持仓管理 ──
        if sym in positions:
            pos = positions[sym]
            side = "buy" if float(pos.qty) > 0 else "sell"
            entry = float(pos.avg_entry_price)
            qty = abs(int(float(pos.qty)))

            # 获取初始止损
            entry_order = entry_filled[0] if entry_filled else None
            initial_stop = strat.range_low if side == "buy" else strat.range_high
            if initial_stop is None:
                print(" 无法获取初始止损，跳过持仓管理")
                continue
            strat.initial_stop = initial_stop
            risk_per_share = abs(entry - initial_stop)
            strat.risk_per_share = risk_per_share

            if risk_per_share <= 0:
                print(" 风险距离为0，跳过持仓管理")
                continue

            # ── 多级止盈 ──
            check_scale_out(trading_client, sym, pos, price, entry, initial_stop,
                          today_orders, date_str)

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
                        print(f" 🔒 浮盈达到 {AUTO_BE_TRIGGER_R}R，收紧 Trailing Stop")
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
                            print(f" 🔄 新 Trailing Stop: {AUTO_BE_TRAIL_PERCENT}% | ID={new_trail.id}")
                        except Exception as e:
                            print(f" ⚠️新 Trailing Stop 失败: {e}")
                        continue

            # ── 时间止损 ──
            if TIME_STOP_MINUTES > 0 and entry_filled:
                entry_order_obj = entry_filled[0]
                if entry_order_obj.filled_at:
                    filled_at = entry_order_obj.filled_at.astimezone(ET)
                    elapsed_minutes = (now_et() - filled_at).total_seconds() / 60
                    if elapsed_minutes >= TIME_STOP_MINUTES:
                        if side == "buy":
                            favorable_move = price - entry
                        else:
                            favorable_move = entry - price
                        if favorable_move < AUTO_BE_TRIGGER_R * risk_per_share:
                            print(f" ⏰ 时间止损 ({elapsed_minutes:.0f}分钟未达1R)，平仓")
                            close_position_safely(trading_client, sym)
                            continue

            # 检查Trailing Stop状态
            trail_orders = [
                o for o in today_orders
                if o.order_type and "trailing" in str(o.order_type).lower()
            ]
            trail_filled = any(o.status == OrderStatus.FILLED for o in trail_orders)
            if trail_filled:
                print(" ✅ Trailing Stop 已触发")
                continue

            # 补挂
            trail_active = any(
                o.status in (OrderStatus.NEW, OrderStatus.ACCEPTED)
                for o in trail_orders
            )
            if not trail_active:
                print(" ⚠️无活跃Trailing Stop，补挂")
                trail_side = OrderSide.SELL if side == "buy" else OrderSide.BUY
                trail_req = TrailingStopOrderRequest(
                    symbol=sym, qty=qty, side=trail_side,
                    time_in_force=TimeInForce.DAY,
                    trail_percent=TRAIL_PERCENT,
                    client_order_id=f"ORB_{sym}_{date_str}_TRAIL_RE",
                )
                try:
                    api_call_with_retry(trading_client.submit_order, trail_req)
                    print(f" 🔄 补挂成功")
                except Exception as e:
                    print(f" ⚠️补挂失败: {e}")
            continue

        # ── 入场信号 ──
        if not strat.traded_today:
            signal = strat.check_entry(latest, current_minute_index)
            if signal:
                entry = signal["entry"]
                stop = signal["stop"]
                qty = strat.calc_qty(available_cash, entry, stop, size_mult, atr)

                # 组合层风险检查
                risk_dollar = qty * abs(entry - stop)
                risk_pct = risk_dollar / available_cash
                if not check_portfolio_risk(positions, total_position_value,
                                           available_cash, risk_dollar,
                                           signal["side"], current_risks):
                    continue

                required_cash = qty * entry
                remaining_cash = available_cash - total_position_value
                if required_cash > remaining_cash:
                    qty = int(remaining_cash / entry)
                    print(f" ⚠️剩余现金不足，调整为 {qty} 股")

                if qty > 0:
                    print(f"\n🚀 突破信号: {sym} {signal['side'].upper()} @ {entry:.2f}")
                    strat.traded_today = True
                    tag = f"ORB_{sym}_{date_str}_{int(time.time())}"
                    entry_order, trail_order = place_entry_and_trailing_stop(
                        trading_client, sym, qty,
                        signal["side"], TRAIL_PERCENT, tag, entry
                    )
                    if entry_order:
                        total_position_value += qty * entry
                        current_risks[sym] = risk_pct
                        send_email(
                            f"ORB 入场 {sym}",
                            f"标的: {sym}\n方向: {signal['side']}\n"
                            f"数量: {qty}\n入场价: {entry:.2f}\n"
                            f"止损价: {stop:.2f}\n时间: {now_et()}"
                        )
                    else:
                        strat.traded_today = False
                else:
                    print(" 仓位为0，跳过")
            else:
                print(" 无突破信号")

        if is_eod() and sym in positions:
            close_position_safely(trading_client, sym)

    print("\n 本次运行完成")

if __name__ == "__main__":
    main()
