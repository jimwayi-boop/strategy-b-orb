# close_all.py
import os
import time
import smtplib
import json
import pytz
from datetime import datetime

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderStatus

from config import *
from main import (
    api_call_with_retry,
    now_et,
    fetch_vix,
    fetch_market_regime,
    fetch_today_orders,
    append_equity_snapshot,
    load_equity_history,
    load_period_pnl,
    generate_dashboard_data,
    send_email,
)

ET = pytz.timezone("America/New_York")


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


def main():
    print("=" * 60)
    print(f" 强制平仓 | {now_et().strftime('%Y-%m-%d %H:%M:%S ET')}")
    print("=" * 60)

    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)

    try:
        account = api_call_with_retry(trading_client.get_account)
        equity = float(account.equity)
    except Exception as e:
        send_email("ORB 平仓-账户获取失败", f"账户获取失败: {e}")
        return

    equity_history = append_equity_snapshot(equity)

    try:
        positions = api_call_with_retry(trading_client.get_all_positions)
    except Exception as e:
        send_email("ORB 平仓-持仓获取失败", f"持仓获取失败: {e}")
        return

    errors = []
    if positions:
        print(f"⏰ 开始平仓 {len(positions)} 个持仓")
        for p in positions:
            symbol = p.symbol
            print(f"\n── {symbol} ──")
            try:
                close_position_safely(trading_client, symbol)
            except Exception as e:
                errors.append(f"{symbol} 平仓失败: {e}")
                print(f" ⚠️平仓失败: {e}")

    # 获取市场信息
    data_client = None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    except Exception:
        pass

    vix = fetch_vix()
    market_regime = "bullish"
    if data_client:
        try:
            market_regime = fetch_market_regime(data_client)
        except Exception as e:
            print(f" ⚠️市场状态失败: {e}")

    market_info = {
        "regime": market_regime,
        "vix": vix,
        "data_source": "IEX",
        "multiplier": account.multiplier,
        "eod_close": f"{EOD_CLOSE_HOUR}:{EOD_CLOSE_MIN:02d}",
    }

    # 周期盈亏（带缓存）
    try:
        period_pnl = load_period_pnl(trading_client)
    except Exception as e:
        print(f" ⚠️周期盈亏加载失败: {e}")
        period_pnl = {}

    # 生成面板数据
    if data_client:
        try:
            generate_dashboard_data(trading_client, data_client, [],
                                     [], market_info, equity_history,
                                     period_pnl)
        except Exception as e:
            print(f" ⚠️面板生成失败: {e}")

    if errors:
        send_email("ORB 策略-平仓异常", "\n".join(errors))
    else:
        send_email("ORB 策略-平仓完成",
                   f"已成功平仓所有持仓，时间: {now_et()}")

    print("\n 平仓完成")


if __name__ == "__main__":
    main()
