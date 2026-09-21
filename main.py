# main.py
import time
import pytz
import pandas as pd
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, StopOrderRequest,
)
from alpaca.trading.enums import (
    OrderSide, TimeInForce,
)
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import *
from orb_strategy import ORBStrategy

ET = pytz.timezone("America/New_York")


# ══════════════════════════════════════════════
#  时间判断
# ══════════════════════════════════════════════

def now_et():
    return datetime.now(ET)


def is_market_day():
    """周一到周五"""
    return now_et().weekday() < 5


def is_after_orb():
    """9:45 ET 之后"""
    n = now_et()
    return (n.hour > ORB_END_HOUR) or (
        n.hour == ORB_END_HOUR and n.minute > ORB_END_MIN
    )


def is_eod():
    """15:50 ET 之后"""
    n = now_et()
    return (n.hour > EOD_CLOSE_HOUR) or (
        n.hour == EOD_CLOSE_HOUR and n.minute >= EOD_CLOSE_MIN
    )


def is_before_open():
    """9:30 ET 之前"""
    n = now_et()
    return (n.hour < ORB_START_HOUR) or (
        n.hour == ORB_START_HOUR and n.minute < ORB_START_MIN
    )


# ══════════════════════════════════════════════
#  数据获取
# ══════════════════════════════════════════════

def fetch_opening_range_bars(data_client, symbol):
    """获取今日 9:30–9:45 ET 的 1 分钟 K 线"""
    today = now_et().strftime("%Y-%m-%d")
    start = f"{today}T09:30:00-04:00"
    end   = f"{today}T09:45:00-04:00"

    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            feed="iex",
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
    """获取最新 1 分钟 K 线"""
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


def fetch_avg_volume(data_client, symbol, lookback=20):
    """近 20 根 1 分钟 K 线的平均成交量"""
    end   = now_et()
    start = end - timedelta(minutes=lookback + 5)

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
            return 0
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        return float(df["volume"].tail(lookback).mean())
    except Exception as e:
        print(f"[{symbol}] 获取均量失败: {e}")
        return 0


def fetch_today_orders(trading_client, symbol):
    """查询今日该标的的所有订单，用于判断今天是否已交易"""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        req = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            symbols=[symbol],
            after=now_et().strftime("%Y-%m-%dT00:00:00-04:00"),
        )
        orders = trading_client.get_orders(req)
        return orders
    except Exception as e:
        print(f"[{symbol}] 查询订单失败: {e}")
        return []


# ══════════════════════════════════════════════
#  订单执行
# ══════════════════════════════════════════════

def place_entry_order(trading_client, symbol, side, qty, tag):
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
        client_order_id=tag,
    )
    order = trading_client.submit_order(req)
    print(f"  ✅ 入场: {side} {qty} 股 {symbol} | 订单ID={order.id}")
    return order


def place_stop_order(trading_client, symbol, side, qty, stop_price, tag):
    order_side = OrderSide.SELL if side == "buy" else OrderSide.BUY
    req = StopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
        stop_price=round(stop_price, 2),
        client_order_id=tag + "_STOP",
    )
    order = trading_client.submit_order(req)
    print(f"  🛑 止损单: {order_side} {qty} 股 @ {stop_price:.2f}")
    return order


def replace_stop_order(trading_client, old_order_id,
                       symbol, side, qty, new_stop, tag):
    try:
        trading_client.cancel_order_by_id(old_order_id)
    except Exception:
        pass

    return place_stop_order(
        trading_client, symbol, side, qty, new_stop, tag
    )


def close_position(trading_client, symbol):
    try:
        trading_client.close_position(symbol)
        print(f"  ⏰ 平仓: {symbol}")
    except Exception as e:
        print(f"  ⚠️ 平仓异常: {e}")


# ══════════════════════════════════════════════
#  主逻辑（每次运行执行一次）
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

    # 初始化客户端
    trading_client = TradingClient(
        ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER
    )
    data_client = StockHistoricalDataClient(
        ALPACA_API_KEY, ALPACA_SECRET_KEY
    )

    # 账户信息
    try:
        account = trading_client.get_account()
        equity  = float(account.equity)
        print(f"账户净值: ${equity:,.2f}")
    except Exception as e:
        print(f"获取账户失败: {e}")
        return

    # 获取当前持仓
    try:
        positions = {p.symbol: p for p in trading_client.get_all_positions()}
    except Exception as e:
        print(f"获取持仓失败: {e}")
        positions = {}

    for sym in SYMBOLS:
        print(f"\n── {sym} ──")
        strat = ORBStrategy(sym)

        # ── 获取开盘区间 ──
        if not is_after_orb():
            print("  开盘区间未完成，退出")
            continue

        bars = fetch_opening_range_bars(data_client, sym)
        if bars is None or bars.empty:
            print("  无法获取开盘区间数据")
            continue

        if not strat.set_opening_range(bars):
            continue

        # ── 获取最新价格 ──
        latest = fetch_latest_bar(data_client, sym)
        if latest is None:
            print("  无法获取最新价格")
            continue
        price = float(latest["close"])
        print(f"  最新价: {price:.2f}")

        # ── 检查今日订单，判断是否已交易 ──
        today_orders = fetch_today_orders(trading_client, sym)
        traded_today = any(
            o.client_order_id and o.client_order_id.startswith(
                f"ORB_{sym}_{now_et().strftime('%Y%m%d')}"
            ) and o.status in ("filled", "partially_filled", "accepted", "new")
            for o in today_orders
        )
        strat.traded_today = traded_today

        # ── 已有持仓：更新移动止损 ──
        if sym in positions:
            pos = positions[sym]
            side = "buy" if float(pos.qty) > 0 else "sell"
            qty  = abs(int(float(pos.qty)))
            entry = float(pos.avg_entry_price)
            strat.position = {
                "side":  side,
                "entry": entry,
                "stop":  strat.range_low if side == "buy" else strat.range_high,
                "qty":   qty,
            }

            old_stop = strat.position["stop"]
            new_stop = strat.update_trailing_stop(price)

            # 找出现有的止损单
            stop_orders = [
                o for o in today_orders
                if o.client_order_id and "_STOP" in o.client_order_id
                and o.status in ("new", "accepted", "held")
            ]

            if new_stop and new_stop != old_stop and stop_orders:
                tag = f"ORB_{sym}_{now_et().strftime('%Y%m%d')}"
                replace_stop_order(
                    trading_client, stop_orders[0].id,
                    sym, side, qty, new_stop, tag
                )

            # 检查止损
            if strat.check_exit(price):
                close_position(trading_client, sym)
            continue

        # ── 无持仓：检查入场 ──
        if not strat.traded_today:
            avg_vol = fetch_avg_volume(data_client, sym)
            signal = strat.check_entry(latest, avg_vol)

            if signal:
                entry = signal["entry"]
                stop  = signal["stop"]
                qty = strat.calc_qty(equity, entry, stop)

                if qty > 0:
                    tag = f"ORB_{sym}_{now_et().strftime('%Y%m%d')}_{int(time.time())}"
                    print(f"\n🚀 突破信号: {sym} "
                          f"{signal['side'].upper()} @ {entry:.2f}")

                    place_entry_order(
                        trading_client, sym, signal["side"], qty, tag
                    )
                    place_stop_order(
                        trading_client, sym, signal["side"], qty, stop, tag
                    )
                else:
                    print("  仓位计算为 0，跳过")
            else:
                print("  无突破信号")

        # ── 15:50 ET 强制平仓 ──
        if is_eod() and sym in positions:
            close_position(trading_client, sym)

    print("\n本次运行完成")


if __name__ == "__main__":
    main()
