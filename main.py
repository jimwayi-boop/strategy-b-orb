# main.py
import os
import time
import smtplib
import random
import json
import pytz
import requests
import pandas as pd
import numpy as np
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest,
    TrailingStopOrderRequest, GetOrdersRequest,
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
# 邮件
# ══════════════════════════════════════════════
def send_email(subject, body, html=False):
    mail_user = os.getenv("MAIL_USERNAME")
    mail_pass = os.getenv("MAIL_PASSWORD")
    if not mail_user or not mail_pass:
        print(" ⚠️未配置邮箱，跳过")
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
        print("✅ 邮件已发送")
    except Exception as e:
        print(f" ⚠️邮件失败: {e}")

# ══════════════════════════════════════════════
# 参数版本化
# ══════════════════════════════════════════════
def log_params():
    params = {
        "RISK_PER_TRADE": RISK_PER_TRADE,
        "MAX_TRADES_PER_DAY": MAX_TRADES_PER_DAY,
        "TRAIL_PERCENT": TRAIL_PERCENT,
        "VOLUME_MULTIPLIER": VOLUME_MULTIPLIER,
        "TRADE_CUTOFF": f"{TRADE_CUTOFF_HOUR}:{TRADE_CUTOFF_MIN:02d}",
        "EOD_CLOSE": f"{EOD_CLOSE_HOUR}:{EOD_CLOSE_MIN:02d}",
        "VIX_FILTER": VIX_FILTER_ENABLED,
        "MARKET_REGIME": MARKET_REGIME_ENABLED,
        "SHADOW_MODE": SHADOW_MODE,
    }
    print(f"📋 参数: {json.dumps(params, ensure_ascii=False)}")

# ══════════════════════════════════════════════
# 时间
# ══════════════════════════════════════════════
def now_et():
    return datetime.now(ET)

def is_market_day():
    return now_et().weekday() < 5

def is_after_orb():
    n = now_et()
    return (n.hour > ORB_END_HOUR) or (n.hour == ORB_END_HOUR and n.minute > ORB_END_MIN)

def is_eod():
    n = now_et()
    return (n.hour > EOD_CLOSE_HOUR) or (n.hour == EOD_CLOSE_HOUR and n.minute >= EOD_CLOSE_MIN)

def is_before_open():
    n = now_et()
    return (n.hour < ORB_START_HOUR) or (n.hour == ORB_START_HOUR and n.minute < ORB_START_MIN)

def is_past_cutoff():
    n = now_et()
    return (n.hour * 60 + n.minute) >= (TRADE_CUTOFF_HOUR * 60 + TRADE_CUTOFF_MIN)

# ══════════════════════════════════════════════
# VIX 获取
# ══════════════════════════════════════════════
def fetch_vix():
    if not VIX_FILTER_ENABLED:
        return None
    try:
        resp = requests.get(VIX_API_URL, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            vix = data.get("value")
            if vix is not None:
                print(f"📊 VIX = {vix:.2f}")
                return float(vix)
        print(f" ⚠️VIX API 返回 {resp.status_code}")
    except Exception as e:
        print(f" ⚠️获取 VIX 失败: {e}")
    return None

# ══════════════════════════════════════════════
# 市场状态（QQQ EMA200）
# ══════════════════════════════════════════════
def fetch_market_regime(data_client):
    if not MARKET_REGIME_ENABLED:
        return "bullish"
    end = now_et()
    start = end - timedelta(days=MARKET_REGIME_EMA_PERIOD + 30)
    try:
        req = StockBarsRequest(
            symbol_or_symbols=MARKET_REGIME_SYMBOL,
            timeframe=TimeFrame.Day,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            feed="iex",
        )
        bars = api_call_with_retry(data_client.get_stock_bars, req)
        df = bars.df
        if df.empty or len(df) < MARKET_REGIME_EMA_PERIOD:
            print(f" ⚠️QQQ 数据不足，默认看多")
            return "bullish"
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(MARKET_REGIME_SYMBOL, level="symbol")
        closes = df["close"].astype(float)
        ema = closes.ewm(span=MARKET_REGIME_EMA_PERIOD, adjust=False).mean().iloc[-1]
        current = float(closes.iloc[-1])
        if current < ema:
            print(f"📉 市场状态: 看空 (QQQ={current:.2f} < EMA{MARKET_REGIME_EMA_PERIOD}={ema:.2f})")
            return "bearish"
        print(f"📈 市场状态: 看多 (QQQ={current:.2f} > EMA{MARKET_REGIME_EMA_PERIOD}={ema:.2f})")
        return "bullish"
    except Exception as e:
        print(f" ⚠️市场状态获取失败: {e}")
        return "bullish"

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
        print(f"[{symbol}] 开盘区间失败: {e}")
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
        print(f"[{symbol}] 最新K线失败: {e}")
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
        print(f"[{symbol}] 前收/今开失败: {e}")
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
        if len(ema_series) > TREND_EMA_SLOPE_LOOKBACK:
            ema_prev = float(ema_series.iloc[-TREND_EMA_SLOPE_LOOKBACK - 1])
        else:
            ema_prev = ema_current
        return ema_current, ema_current - ema_prev
    except Exception as e:
        print(f"[{symbol}] EMA失败: {e}")
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
        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        return float(tr.rolling(window=period).mean().iloc[-1])
    except Exception as e:
        print(f"[{symbol}] ATR失败: {e}")
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
        print(f"[{symbol}] 订单查询失败: {e}")
        return []

# ══════════════════════════════════════════════
# 订单执行
# ══════════════════════════════════════════════
def wait_for_order_filled(trading_client, order_id, max_wait=10, interval=0.5):
    waited = 0
    last_status = None
    while waited < max_wait:
        try:
            o = api_call_with_retry(trading_client.get_order_by_id, order_id)
            if o.status == OrderStatus.FILLED:
                print(f" ✔️订单 {order_id} 完全成交")
                return True, int(float(o.filled_qty))
            if o.status == OrderStatus.PARTIALLY_FILLED:
                last_status = o
                print(f" 🔶部分成交: {o.filled_qty}/{o.qty}")
            if o.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                filled = int(float(o.filled_qty)) if o.filled_qty else 0
                return False, filled
        except Exception:
            pass
        time.sleep(interval)
        waited += interval

    if last_status:
        filled_qty = int(float(last_status.filled_qty))
        print(f" ⚠️超时，部分成交 {filled_qty} 股")
        return False, filled_qty
    return False, 0

def place_entry_and_trailing_stop(trading_client, symbol, qty, side, trail_pct, tag, entry_price):
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

    if SHADOW_MODE:
        print(f" 👻 [影子] 不提交: {side} {qty} 股 {symbol} @ ~{entry_price:.2f}")
        log_shadow_trade(symbol, side, qty, entry_price, tag)
        return None, None

    if LIMIT_ENTRY_ENABLED:
        limit_price = round(entry_price * (1 + LIMIT_ENTRY_OFFSET), 2) if side == "buy" else round(entry_price * (1 - LIMIT_ENTRY_OFFSET), 2)
        entry_req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=order_side,
            time_in_force=TimeInForce.DAY, limit_price=limit_price,
            client_order_id=tag,
        )
        print(f" 📤 限价入场: {side} {qty} 股 @ {limit_price:.2f}")
    else:
        entry_req = MarketOrderRequest(
            symbol=symbol, qty=qty, side=order_side,
            time_in_force=TimeInForce.DAY, client_order_id=tag,
        )
        print(f" 📤 市价入场: {side} {qty} 股 {symbol}")

    entry_order = api_call_with_retry(trading_client.submit_order, entry_req)
    filled, filled_qty = wait_for_order_filled(trading_client, entry_order.id)

    if filled_qty <= 0:
        return entry_order, None

    trail_side = OrderSide.SELL if side == "buy" else OrderSide.BUY
    trail_req = TrailingStopOrderRequest(
        symbol=symbol, qty=filled_qty, side=trail_side,
        time_in_force=TimeInForce.DAY, trail_percent=trail_pct,
        client_order_id=tag + "_TRAIL",
    )
    try:
        trail_order = api_call_with_retry(trading_client.submit_order, trail_req)
        print(f" 🔄 Trailing Stop: {trail_pct}% | 数量={filled_qty}")
        return entry_order, trail_order
    except Exception as e:
        print(f" ⚠️Trailing Stop 失败: {e}")
        return entry_order, None

def log_shadow_trade(symbol, side, qty, price, tag):
    record = {
        "timestamp": now_et().isoformat(),
        "symbol": symbol, "side": side, "qty": qty,
        "price": price, "tag": tag,
    }
    try:
        with open("shadow_trades.jsonl", "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f" 👻 影子记录已写入")
    except Exception as e:
        print(f" ⚠️影子写入失败: {e}")

def close_position_safely(trading_client, symbol):
    try:
        open_orders = api_call_with_retry(trading_client.get_orders, status="open", symbols=[symbol])
        for o in open_orders:
            try:
                api_call_with_retry(trading_client.cancel_order_by_id, o.id)
                print(f" 🗑️取消: {o.id}")
            except Exception as e:
                print(f" ⚠️取消失败: {e}")
        time.sleep(1.0)
        api_call_with_retry(trading_client.close_position, symbol)
        print(f" ⏰ 平仓: {symbol}")
    except Exception as e:
        print(f" ⚠️平仓异常: {e}")

def enforce_no_margin(trading_client):
    try:
        if hasattr(trading_client, "patch_account_configurations"):
            api_call_with_retry(trading_client.patch_account_configurations, {"max_margin_multiplier": "1"})
            print("✅ 保证金已禁用")
        else:
            print("⚠️ 当前版本不支持 API 修改保证金，请手动确认 Dashboard 中 Max Margin Multiplier = 1")
    except Exception as e:
        print(f"⚠️ 保证金设置失败: {e}")

# ══════════════════════════════════════════════
# 组合层风险
# ══════════════════════════════════════════════
def check_portfolio_risk(positions, available_cash, new_risk_dollar, new_direction, current_risks):
    if len(positions) >= MAX_CONCURRENT_POSITIONS:
        print(f" 🚫 持仓已达 {MAX_CONCURRENT_POSITIONS} 个上限")
        return False
    risk_pct = new_risk_dollar / available_cash
    current_total = sum(current_risks.values()) if current_risks else 0
    if (current_total + risk_pct) > MAX_TOTAL_RISK_PCT:
        print(f" 🚫 总风险超限")
        return False
    same_dir = sum(r for d, r in current_risks.items() if d == new_direction) if current_risks else 0
    if (same_dir + risk_pct) > MAX_SAME_DIRECTION_RISK:
        print(f" 🚫 {new_direction} 方向风险超限")
        return False
    return True

# ══════════════════════════════════════════════
# 多级止盈
# ══════════════════════════════════════════════
def check_scale_out(trading_client, symbol, position, price, entry, initial_stop, today_orders, date_str):
    if not SCALE_OUT_ENABLED:
        return False
    side = "buy" if float(position.qty) > 0 else "sell"
    risk_per_share = abs(entry - initial_stop)
    if risk_per_share <= 0:
        return False
    favorable = (price - entry) if side == "buy" else (entry - price)
    r_multiple = favorable / risk_per_share
    qty = abs(int(float(position.qty)))

    executed = set()
    for o in today_orders:
        if o.client_order_id and "_TP" in o.client_order_id and o.status == OrderStatus.FILLED:
            for i in range(len(SCALE_OUT_LEVELS)):
                if f"_TP{i+1}" in o.client_order_id:
                    executed.add(i)

    for i, level in enumerate(SCALE_OUT_LEVELS):
        if i in executed:
            continue
        if r_multiple >= level["r_multiple"]:
            exit_qty = max(int(qty * level["exit_pct"]), 1)
            exit_qty = min(exit_qty, qty)
            print(f" 🎯 TP{i+1} ({level['r_multiple']}R), 平 {exit_qty} 股")
            order_side = OrderSide.SELL if side == "buy" else OrderSide.BUY
            tp_req = MarketOrderRequest(
                symbol=symbol, qty=exit_qty, side=order_side,
                time_in_force=TimeInForce.DAY,
                client_order_id=f"ORB_{symbol}_{date_str}_TP{i+1}",
            )
            try:
                api_call_with_retry(trading_client.submit_order, tp_req)
                print(f" ✅ TP{i+1} 完成")
            except Exception as e:
                print(f" ⚠️TP{i+1} 失败: {e}")
            return True
    return False

# ══════════════════════════════════════════════
# 交易日面板数据生成
# ══════════════════════════════════════════════
def generate_dashboard_data(trading_client, data_client, shadow_signals=None):
    try:
        account = api_call_with_retry(trading_client.get_account)
        positions = api_call_with_retry(trading_client.get_all_positions)

        data = {
            "date": now_et().strftime("%Y-%m-%d"),
            "generated_at": now_et().isoformat(),
            "account": {
                "cash": float(account.cash),
                "equity": float(account.equity),
                "buying_power": float(account.buying_power),
            },
            "positions": [],
            "today_trades": [],
            "shadow_signals": shadow_signals or [],
            "shadow_mode": SHADOW_MODE,
        }

        for p in positions:
            data["positions"].append({
                "symbol": p.symbol,
                "qty": int(float(p.qty)),
                "avg_entry": float(p.avg_entry_price),
                "current_price": float(p.current_price) if p.current_price else None,
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl else 0,
                "unrealized_plpc": float(p.unrealized_plpc) * 100 if p.unrealized_plpc else 0,
            })

        for sym in SYMBOLS:
            orders = fetch_today_orders(trading_client, sym)
            for o in orders:
                if o.status == OrderStatus.FILLED:
                    data["today_trades"].append({
                        "symbol": o.symbol,
                        "side": str(o.side).replace("OrderSide.", ""),
                        "qty": int(float(o.filled_qty)) if o.filled_qty else 0,
                        "price": float(o.filled_avg_price) if o.filled_avg_price else 0,
                        "time": o.filled_at.astimezone(ET).strftime("%H:%M:%S") if o.filled_at else "",
                        "client_order_id": o.client_order_id or "",
                    })

        with open("dashboard_data.json", "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("📊 dashboard_data.json 已生成")

        return data
    except Exception as e:
        print(f"面板数据生成失败: {e}")
        return None

def format_panel_html(data):
    if not data:
        return "<p>无数据</p>"

    shadow_block = ""
    if data.get("shadow_mode"):
        shadow_block = f"""
        <h3>👻 影子模式</h3>
        <p><strong>状态：</strong>开启（只记录信号，不提交订单）</p>
        <p>今日影子信号数：{len(data.get('shadow_signals', []))}</p>
        """
        if data.get("shadow_signals"):
            shadow_block += """
            <table border="1" cellpadding="5">
                <tr><th>时间</th><th>标的</th><th>方向</th><th>数量</th><th>假设入场价</th></tr>
            """
            for s in data["shadow_signals"]:
                shadow_block += f"""
                <tr>
                    <td>{s.get('timestamp', '')}</td>
                    <td>{s.get('symbol', '')}</td>
                    <td>{s.get('side', '')}</td>
                    <td>{s.get('qty', '')}</td>
                    <td>${s.get('price', 0):.2f}</td>
                </tr>
                """
            shadow_block += "</table>"

    pos_rows = ""
    for p in data.get("positions", []):
        color = "green" if p["unrealized_pl"] >= 0 else "red"
        pos_rows += f"""
        <tr>
            <td>{p['symbol']}</td><td>{p['qty']}</td>
            <td>${p['avg_entry']:.2f}</td><td>${p['current_price']:.2f}</td>
            <td>${p['market_value']:,.2f}</td>
            <td style="color:{color}">${p['unrealized_pl']:,.2f}</td>
            <td style="color:{color}">{p['unrealized_plpc']:.2f}%</td>
        </tr>"""

    trade_rows = ""
    for t in data.get("today_trades", []):
        trade_rows += f"""
        <tr>
            <td>{t['symbol']}</td><td>{t['side']}</td><td>{t['qty']}</td>
            <td>${t['price']:.2f}</td><td>{t['time']}</td>
        </tr>"""

    html = f"""
    <h2>📊 ORB 交易日面板 — {data['date']}</h2>
    <h3>账户概览</h3>
    <table border="1" cellpadding="5">
        <tr><td>现金</td><td>${data['account']['cash']:,.2f}</td></tr>
        <tr><td>净值</td><td>${data['account']['equity']:,.2f}</td></tr>
        <tr><td>购买力</td><td>${data['account']['buying_power']:,.2f}</td></tr>
    </table>
    {shadow_block}
    <h3>当前持仓</h3>
    {f"<table border='1' cellpadding='5'><tr><th>标的</th><th>数量</th><th>成本</th><th>现价</th><th>市值</th><th>未实现盈亏</th><th>盈亏%</th></tr>{pos_rows}</table>" if pos_rows else "<p>无持仓</p>"}
    <h3>今日成交</h3>
    {f"<table border='1' cellpadding='5'><tr><th>标的</th><th>方向</th><th>数量</th><th>价格</th><th>时间</th></tr>{trade_rows}</table>" if trade_rows else "<p>无成交</p>"}
    <hr><p style="color:gray;font-size:12px;">数据来源: Alpaca | 市场数据: IEX | VIX: Data by Convex</p>
    """
    return html

# ══════════════════════════════════════════════
# 主逻辑
# ══════════════════════════════════════════════
def main():
    print("=" * 60)
    print(f" ORB 单次运行 | {now_et().strftime('%Y-%m-%d %H:%M:%S ET')}")
    print("=" * 60)

    log_params()

    if SHADOW_MODE:
        print(" 👻 影子模式已启用")

    # ⚠️ 临时改动：周末测试时改成 if False:
    # if not is_market_day():
    if False:
        print("非交易日，退出")
        return

    if is_before_open():
        print("开盘前，退出")
        return

    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
    data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    enforce_no_margin(trading_client)

    # 补平仓兜底 + 生成面板
    # ⚠️ 临时改动：周末测试时改成 if True:
    # if is_eod():
    if True:
        try:
            positions = api_call_with_retry(trading_client.get_all_positions)
            if positions:
                print("⏰ 已过平仓时间，补平仓")
                for p in positions:
                    close_position_safely(trading_client, p.symbol)
        except Exception as e:
            print(f"补平仓失败: {e}")

        shadow_signals = []
        if SHADOW_MODE and os.path.exists("shadow_trades.jsonl"):
            with open("shadow_trades.jsonl") as f:
                shadow_signals = [json.loads(line) for line in f if line.strip()]

        data = generate_dashboard_data(trading_client, data_client, shadow_signals)
        if data:
            html = format_panel_html(data)
            send_email(f"📊 ORB 面板 {data['date']}", html, html=True)
        return

    # 以下为正常交易时段逻辑（周末测试时不会执行到）
    vix = fetch_vix()
    market_regime = fetch_market_regime(data_client)

    try:
        account = api_call_with_retry(trading_client.get_account)
        print(f"乘数: {account.multiplier}")
        print(f"现金: ${float(account.cash):,.2f}")
        available_cash = float(account.cash)
    except Exception as e:
        print(f"账户失败: {e}")
        send_email("ORB 账户失败", str(e))
        return

    try:
        positions = {p.symbol: p for p in api_call_with_retry(trading_client.get_all_positions)}
    except Exception as e:
        print(f"持仓失败: {e}")
        positions = {}

    total_position_value = 0.0
    current_risks = {}
    for sym, p in positions.items():
        try:
            total_position_value += abs(float(p.market_value))
            current_risks[sym] = 0.02
        except Exception:
            pass

    now = now_et()
    current_minute_index = now.hour * 60 + now.minute
    date_str = now_et().strftime('%Y%m%d')

    for sym in SYMBOLS:
        print(f"\n── {sym} ──")
        strat = ORBStrategy(sym)
        strat.vix_value = vix
        strat.market_regime = market_regime

        if not is_after_orb():
            print(" 区间未完成")
            continue

        bars = fetch_opening_range_bars(data_client, sym)
        if bars is None or bars.empty:
            print(" 无区间数据")
            continue
        if not strat.set_opening_range(bars):
            continue

        atr = fetch_atr(data_client, sym)
        strat.atr = atr
        if atr is not None:
            rw = strat.range_high - strat.range_low
            if rw > MAX_RANGE_ATR_MULTIPLIER * atr:
                print(f" ⛔区间过宽，跳过")
                continue

        prev_close, today_open = fetch_prev_close_and_open(data_client, sym)
        strat.prev_close = prev_close
        strat.today_open = today_open
        skip_gap, size_mult = strat.check_gap()
        if skip_gap:
            continue

        ema, ema_slope = fetch_daily_ema(data_client, sym)
        strat.daily_ema = ema
        strat.daily_ema_slope = ema_slope

        latest = fetch_latest_bar(data_client, sym)
        if latest is None:
            print(" 无最新价")
            continue
        price = float(latest["close"])
        print(f" 最新价: {price:.2f}")

        today_orders = fetch_today_orders(trading_client, sym)

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
            last = max(trail_filled_orders, key=lambda o: o.filled_at if o.filled_at else now)
            if last.filled_at:
                lf = last.filled_at.astimezone(ET)
                lm = lf.hour * 60 + lf.minute
                if current_minute_index < lm + COOLDOWN_BARS:
                    strat.cooldown_until = lm + COOLDOWN_BARS

        if sym in positions:
            pos = positions[sym]
            side = "buy" if float(pos.qty) > 0 else "sell"
            entry = float(pos.avg_entry_price)
            qty = abs(int(float(pos.qty)))

            initial_stop = strat.range_low if side == "buy" else strat.range_high
            if initial_stop is None:
                continue
            risk_per_share = abs(entry - initial_stop)
            if risk_per_share <= 0:
                continue

            check_scale_out(trading_client, sym, pos, price, entry, initial_stop, today_orders, date_str)

            if AUTO_BE_ENABLED:
                be_active = any(
                    o.client_order_id and "_BE" in o.client_order_id
                    and o.status in (OrderStatus.NEW, OrderStatus.ACCEPTED)
                    for o in today_orders
                )
                if not be_active:
                    favorable = (price - entry) if side == "buy" else (entry - price)
                    if favorable >= AUTO_BE_TRIGGER_R * risk_per_share:
                        print(f" 🔒 浮盈达 {AUTO_BE_TRIGGER_R}R，收紧止损")
                        for o in today_orders:
                            if o.order_type and "trailing" in str(o.order_type).lower() and o.status in (OrderStatus.NEW, OrderStatus.ACCEPTED):
                                try:
                                    api_call_with_retry(trading_client.cancel_order_by_id, o.id)
                                except Exception:
                                    pass
                        ts = OrderSide.SELL if side == "buy" else OrderSide.BUY
                        tr = TrailingStopOrderRequest(
                            symbol=sym, qty=qty, side=ts,
                            time_in_force=TimeInForce.DAY,
                            trail_percent=AUTO_BE_TRAIL_PERCENT,
                            client_order_id=f"ORB_{sym}_{date_str}_BE_TRAIL",
                        )
                        try:
                            api_call_with_retry(trading_client.submit_order, tr)
                            print(f" 🔄 新Trailing: {AUTO_BE_TRAIL_PERCENT}%")
                        except Exception as e:
                            print(f" ⚠️新Trailing失败: {e}")
                        continue

            if TIME_STOP_MINUTES > 0 and entry_filled:
                eo = entry_filled[0]
                if eo.filled_at:
                    elapsed = (now_et() - eo.filled_at.astimezone(ET)).total_seconds() / 60
                    if elapsed >= TIME_STOP_MINUTES:
                        favorable = (price - entry) if side == "buy" else (entry - price)
                        if favorable < AUTO_BE_TRIGGER_R * risk_per_share:
                            print(f" ⏰ 时间止损 ({elapsed:.0f}分钟)")
                            close_position_safely(trading_client, sym)
                            continue

            trail_orders = [o for o in today_orders if o.order_type and "trailing" in str(o.order_type).lower()]
            if any(o.status == OrderStatus.FILLED for o in trail_orders):
                print(" ✅ Trailing Stop 已触发")
                continue

            if not any(o.status in (OrderStatus.NEW, OrderStatus.ACCEPTED) for o in trail_orders):
                print(" ⚠️补挂Trailing Stop")
                ts = OrderSide.SELL if side == "buy" else OrderSide.BUY
                tr = TrailingStopOrderRequest(
                    symbol=sym, qty=qty, side=ts,
                    time_in_force=TimeInForce.DAY,
                    trail_percent=TRAIL_PERCENT,
                    client_order_id=f"ORB_{sym}_{date_str}_TRAIL_RE",
                )
                try:
                    api_call_with_retry(trading_client.submit_order, tr)
                    print(f" 🔄 补挂成功")
                except Exception as e:
                    print(f" ⚠️补挂失败: {e}")
            continue

        if not strat.traded_today:
            signal = strat.check_entry(latest, current_minute_index)
            if signal:
                entry = signal["entry"]
                stop = signal["stop"]
                qty = strat.calc_qty(available_cash, entry, stop, size_mult, atr)

                risk_dollar = qty * abs(entry - stop)
                if not check_portfolio_risk(positions, available_cash, risk_dollar, signal["side"], current_risks):
                    continue

                remaining = available_cash - total_position_value
                if qty * entry > remaining:
                    qty = int(remaining / entry)
                    print(f" ⚠️现金不足，调整为 {qty}")

                if qty > 0:
                    print(f"\n🚀 突破: {sym} {signal['side'].upper()} @ {entry:.2f}")
                    strat.traded_today = True
                    tag = f"ORB_{sym}_{date_str}_{int(time.time())}"
                    eo, to = place_entry_and_trailing_stop(trading_client, sym, qty, signal["side"], TRAIL_PERCENT, tag, entry)
                    if eo:
                        total_position_value += qty * entry
                        current_risks[sym] = risk_dollar / available_cash
                        send_email(f"ORB 入场 {sym}",
                                   f"标的: {sym}\n方向: {signal['side']}\n数量: {qty}\n入场价: {entry:.2f}\n止损价: {stop:.2f}")
                    else:
                        strat.traded_today = False
                else:
                    print(" 仓位为0")
            else:
                print(" 无信号")

        if is_eod() and sym in positions:
            close_position_safely(trading_client, sym)

    print("\n 本次运行完成")

if __name__ == "__main__":
    main()
