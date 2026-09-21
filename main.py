# main.py
import time
import pytz
import pandas as pd
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, StopOrderRequest,
    StopLossRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import (
    OrderSide, TimeInForce, OrderClass, QueryOrderStatus,
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


def fetch_avg_volume(data_client, symbol, lookback=20):
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

def wait_for_cancel(trading_client, order_id, max_wait_sec=5.0, interval=0.5):
    waited = 0.0
    while waited < max_wait_sec:
        try:
            o = trading_client.get_order_by_id(order_id)
            if o.status in ("canceled", "expired", "rejected", "filled"):
                print(f"  ✔️ 旧止损单状态: {o.status}")
                return True
        except Exception:
            return True
        time.sleep(interval)
        waited += interval
    print(f"  ⚠️ 等待取消超时 ({max_wait_sec}s)，仍尝试继续")
    return False


def place_bracket_order(trading_client, symbol, qty, side, stop_price):
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    stop_loss  = StopLossRequest(stop_price=round(stop_price, 2))

    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        stop_loss=stop_loss,
    )
    order = trading_client.submit_order(req)
    print(f"  ✅ Bracket 入场: {side} {qty} 股 {symbol} | 订单ID={order.id}")
    return order


def replace_stop_order(trading_client, old_order_id,
                       symbol, side, qty, new_stop):
    try:
        trading_client.cancel_order_by_id(old_order_id)
        print(f"  🗑️ 已请求取消旧止损单: {old_order_id}")
    except Exception as e:
        print(f"  ⚠️ 取消失败: {e}")

    wait_for_cancel(trading_client, old_order_id, max_wait_sec=5.0, interval=0.5)

    order_side = OrderSide.SELL if side == "buy" else OrderSide.BUY
    req = StopOrderRequest(
        symbol=symbol, qty=qty, side=order_side,
        time_in_force=TimeInForce.DAY,
        stop_price=round(new_stop, 2),
    )
    try:
        order = trading_client.submit_order(req)
        print(f"  🔄 移动止损更新: {new_stop:.2f} | 新单ID={order.id}")
        return order
    except Exception as e:
        print(f"  ⚠️ 提交新止损单失败: {e}")
        return None


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

    # ── 账户信息：打印乘数、现金、购买力 ──
    try:
        account = trading_client.get_account()
        print(f"账户乘数 (multiplier): {account.multiplier}")
        print(f"现金 (cash): ${float(account.cash):,.2f}")
        print(f"购买力 (buying_power): ${float(account.buying_power):,.2f}")
        print(f"净值 (equity): ${float(account.equity):,.2f}")

        # 使用现金作为仓位计算基础，而非净值
        available_cash = float(account.cash)

        if account.multiplier != "1":
            print(f"⚠️ 警告：账户乘数为 {account.multiplier}，可能仍在使用保证金。"
                  f"请在 Dashboard 中将 Max Margin Multiplier 设为 1。")
    except Exception as e:
        print(f"获取账户失败: {e}")
        return

    try:
        positions = {p.symbol: p for p in trading_client.get_all_positions()}
    except Exception as e:
        print(f"获取持仓失败: {e}")
        positions = {}

    # ── 计算当前持仓总市值，用于剩余现金检查 ──
    total_position_value = 0.0
    for p in positions.values():
        try:
            total_position_value += abs(float(p.market_value))
        except Exception:
            pass
    print(f"当前持仓总市值: ${total_position_value:,.2f}")

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

            stop_orders = [
                o for o in today_orders
                if o.client_order_id and "_STOP" in o.client_order_id
                and o.status in ("new", "accepted", "held")
            ]

            if new_stop and new_stop != old_stop and stop_orders:
                replace_stop_order(
                    trading_client, stop_orders[0].id,
                    sym, side, qty, new_stop
                )

            if strat.check_exit(price):
                close_position_safely(trading_client, sym)
            continue

        # ── 无持仓：检查入场 ──
        if not strat.traded_today:
            avg_vol = fetch_avg_volume(data_client, sym)
            signal = strat.check_entry(latest, avg_vol)

            if signal:
                entry = signal["entry"]
                stop  = signal["stop"]

                # ★ 关键改动：使用 available_cash 而非 equity 计算仓位
                qty = strat.calc_qty(available_cash, entry, stop)

                # 额外检查：确保新仓位不超出剩余现金
                required_cash = qty * entry
                remaining_cash = available_cash - total_position_value
                if required_cash > remaining_cash:
                    qty = int(remaining_cash / entry)
                    print(f"  ⚠️ 剩余现金不足，调整为 {qty} 股")

                if qty > 0:
                    print(f"\n🚀 突破信号: {sym} "
                          f"{signal['side'].upper()} @ {entry:.2f}")

                    place_bracket_order(
                        trading_client, sym, qty,
                        signal["side"], stop
                    )
                    # 更新已用现金
                    total_position_value += qty * entry
                else:
                    print("  仓位计算为 0，跳过")
            else:
                print("  无突破信号")

        if is_eod() and sym in positions:
            close_position_safely(trading_client, sym)

    print("\n本次运行完成")


if __name__ == "__main__":
    main()
