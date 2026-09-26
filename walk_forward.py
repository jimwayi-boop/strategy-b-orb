# walk_forward.py
"""
Walk-Forward Analysis 滚动前向分析
- 3年训练 + 1年验证
- 使用 Alpaca IEX 历史数据
- 加入交易成本建模：市价入场滑点 0.05%，Trailing Stop 滑点 0.1%
- 可以本地运行：python walk_forward.py
"""

import os
import time
import random
import json
import pytz
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import *

ET = pytz.timezone("America/New_York")

# ══════════════════════════════════════════════
# 数据获取
# ══════════════════════════════════════════════
def fetch_historical_data(data_client, symbol, start, end, timeframe=TimeFrame.Minute):
    """获取历史数据"""
    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
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
        print(f"[{symbol}] 历史数据获取失败: {e}")
        return None

def fetch_daily_bars(data_client, symbol, start, end):
    """获取日线数据"""
    return fetch_historical_data(data_client, symbol, start, end, TimeFrame.Day)

# ══════════════════════════════════════════════
# ORB 回测引擎
# ══════════════════════════════════════════════
def backtest_orb(data_client, symbol, start_date, end_date, params=None):
    """
    对单个标的进行 ORB 回测
    返回交易列表和统计指标
    """
    if params is None:
        params = {
            "risk_per_trade": RISK_PER_TRADE,
            "volume_multiplier": VOLUME_MULTIPLIER,
            "min_range_pct": MIN_RANGE_PCT,
            "trail_percent": TRAIL_PERCENT,
            "max_range_atr_mult": MAX_RANGE_ATR_MULTIPLIER,
            "trade_cutoff_hour": TRADE_CUTOFF_HOUR,
            "trade_cutoff_min": TRADE_CUTOFF_MIN,
            "slippage_entry": SLIPPAGE_MARKET_ENTRY,
            "slippage_trail": SLIPPAGE_TRAIL_STOP,
        }

    # 获取分钟数据
    minute_bars = fetch_historical_data(data_client, symbol, start_date, end_date)
    if minute_bars is None or minute_bars.empty:
        print(f"[{symbol}] 分钟数据为空")
        return []

    # 获取日线数据（用于趋势过滤）
    daily_bars = fetch_daily_bars(data_client, symbol, start_date, end_date)

    # 计算日线 EMA200
    daily_ema = None
    if daily_bars is not None and not daily_bars.empty:
        closes = daily_bars["close"].astype(float)
        daily_ema = closes.ewm(span=TREND_EMA_PERIOD, adjust=False).mean()

    # 获取 QQQ 市场状态
    qqq_daily = fetch_daily_bars(data_client, MARKET_REGIME_SYMBOL, start_date, end_date)
    qqq_regime = {}
    if qqq_daily is not None and not qqq_daily.empty:
        qqq_closes = qqq_daily["close"].astype(float)
        qqq_ema = qqq_closes.ewm(span=MARKET_REGIME_EMA_PERIOD, adjust=False).mean()
        for idx in qqq_daily.index:
            date_str = str(idx)[:10]
            if date_str in qqq_closes.index or date_str in qqq_ema.index:
                pass
        # 简化为逐日判断
        qqq_df = pd.DataFrame({
            "close": qqq_closes,
            "ema": qqq_ema,
        })
        qqq_df["date"] = [str(i)[:10] for i in qqq_df.index]

    trades = []
    minute_bars["date"] = [str(i)[:10] for i in minute_bars.index]
    minute_bars["time"] = [str(i)[11:19] for i in minute_bars.index]

    dates = sorted(minute_bars["date"].unique())

    for date in dates:
        day_bars = minute_bars[minute_bars["date"] == date].copy()
        if day_bars.empty:
            continue

        # 开盘区间 9:30-9:45
        orb_bars = day_bars[(day_bars["time"] >= "09:30:00") & (day_bars["time"] < "09:45:00")]
        if orb_bars.empty or len(orb_bars) < 5:
            continue

        range_high = float(orb_bars["high"].max())
        range_low = float(orb_bars["low"].min())
        range_width = range_high - range_low

        if range_low <= 0:
            continue
        if range_width / range_low < params["min_range_pct"]:
            continue

        range_avg_vol = float(orb_bars["volume"].mean())

        # 日线趋势过滤
        day_ema_value = None
        if daily_ema is not None:
            try:
                for idx in daily_ema.index:
                    if str(idx)[:10] == date:
                        day_ema_value = float(daily_ema.loc[idx])
                        break
            except Exception:
                pass

        # 市场状态
        regime_mult = 1.0
        if MARKET_REGIME_ENABLED and qqq_daily is not None and not qqq_daily.empty:
            try:
                for idx in qqq_df.index:
                    if qqq_df.loc[idx, "date"] == date:
                        if qqq_df.loc[idx, "close"] < qqq_df.loc[idx, "ema"]:
                            regime_mult = MARKET_REGIME_BEARISH_SIZE_MULT
                        break
            except Exception:
                pass

        # 开盘缺口
        prev_close = None
        today_open = None
        if daily_bars is not None:
            for idx, row in daily_bars.iterrows():
                d = str(idx)[:10]
                if d == date:
                    today_open = float(row["open"])
                # 前一天
                if d < date:
                    prev_close = float(row["close"])

        gap_pct = 0
        if prev_close and today_open:
            gap_pct = abs(today_open - prev_close) / prev_close

        size_mult = 1.0
        if gap_pct > GAP_SKIP_THRESHOLD:
            continue
        elif gap_pct > GAP_REDUCE_THRESHOLD:
            size_mult = 0.5

        # 交易时段
        trade_bars = day_bars[(day_bars["time"] >= "09:45:00") & (day_bars["time"] <= "15:43:00")]
        if trade_bars.empty:
            continue

        traded_today = False
        position = None
        entry_price = None
        entry_time = None
        stop_price = None
        qty = 0
        highest_price = None
        lowest_price = None

        for _, bar in trade_bars.iterrows():
            price = float(bar["close"])
            volume = float(bar["volume"])
            time_str = bar["time"]
            hour = int(time_str[:2])
            minute = int(time_str[3:5])
            current_min = hour * 60 + minute

            # 检查 Trailing Stop
            if position is not None:
                if position["side"] == "buy":
                    if highest_price is None or price > highest_price:
                        highest_price = price
                    trail_stop = highest_price * (1 - params["trail_percent"] / 100)
                    if price <= trail_stop:
                        # 触发止损（加入滑点）
                        exit_price = trail_stop * (1 - params["slippage_trail"])
                        pnl = (exit_price - entry_price) * qty
                        trades.append({
                            "symbol": symbol,
                            "date": date,
                            "side": "buy",
                            "entry_price": entry_price,
                            "entry_time": entry_time,
                            "exit_price": exit_price,
                            "exit_time": time_str,
                            "qty": qty,
                            "pnl": pnl,
                            "reason": "trail_stop",
                        })
                        position = None
                        traded_today = True
                else:
                    if lowest_price is None or price < lowest_price:
                        lowest_price = price
                    trail_stop = lowest_price * (1 + params["trail_percent"] / 100)
                    if price >= trail_stop:
                        exit_price = trail_stop * (1 + params["slippage_trail"])
                        pnl = (entry_price - exit_price) * qty
                        trades.append({
                            "symbol": symbol,
                            "date": date,
                            "side": "sell",
                            "entry_price": entry_price,
                            "entry_time": entry_time,
                            "exit_price": exit_price,
                            "exit_time": time_str,
                            "qty": qty,
                            "pnl": pnl,
                            "reason": "trail_stop",
                        })
                        position = None
                        traded_today = True
                continue

            # 检查入场
            if traded_today:
                continue

            cutoff = params["trade_cutoff_hour"] * 60 + params["trade_cutoff_min"]
            if current_min >= cutoff:
                continue

            if volume < params["volume_multiplier"] * range_avg_vol:
                continue

            side = None
            stop = None

            if price > range_high:
                side = "buy"
                stop = range_low
                if day_ema_value is not None and today_open <= day_ema_value:
                    continue
            elif price < range_low:
                side = "sell"
                stop = range_high
                if day_ema_value is not None and today_open >= day_ema_value:
                    continue
            else:
                continue

            # 仓位计算
            risk_per_share = abs(price - stop)
            if risk_per_share <= 0:
                continue
            dollar_risk = 100000 * params["risk_per_trade"] * size_mult * regime_mult
            qty = max(int(dollar_risk / risk_per_share), 1)
            qty = min(qty, int(100000 / price))

            # 入场滑点
            entry_price = price * (1 + params["slippage_entry"]) if side == "buy" else price * (1 - params["slippage_entry"])
            entry_time = time_str
            highest_price = entry_price
            lowest_price = entry_price
            position = {"side": side}

        # 收盘平仓
        if position is not None:
            last_bar = trade_bars.iloc[-1]
            exit_price = float(last_bar["close"])
            if position["side"] == "buy":
                pnl = (exit_price - entry_price) * qty
            else:
                pnl = (entry_price - exit_price) * qty
            trades.append({
                "symbol": symbol,
                "date": date,
                "side": position["side"],
                "entry_price": entry_price,
                "entry_time": entry_time,
                "exit_price": exit_price,
                "exit_time": last_bar["time"],
                "qty": qty,
                "pnl": pnl,
                "reason": "eod_close",
            })

    return trades

# ══════════════════════════════════════════════
# 统计指标
# ══════════════════════════════════════════════
def compute_metrics(trades):
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0, "avg_pnl": 0,
            "total_pnl": 0, "max_drawdown": 0, "profit_factor": 0,
        }
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    win_rate = len(wins) / len(pnls) * 100 if pnls else 0
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # 最大回撤
    cumulative = np.cumsum(pnls)
    peak = np.maximum.accumulate(cumulative)
    drawdown = peak - cumulative
    max_dd = float(np.max(drawdown)) if len(drawdown) > 0 else 0

    return {
        "total_trades": len(pnls),
        "win_rate": round(win_rate, 2),
        "avg_pnl": round(total_pnl / len(pnls), 2),
        "total_pnl": round(total_pnl, 2),
        "max_drawdown": round(max_dd, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
    }

# ══════════════════════════════════════════════
# Walk-Forward 主逻辑
# ══════════════════════════════════════════════
def walk_forward_analysis():
    """
    3年训练 + 1年验证
    训练期用于观察策略表现，验证期用于评估泛化能力
    """
    print("=" * 60)
    print(" Walk-Forward Analysis")
    print(" 3年训练 + 1年验证")
    print("=" * 60)

    data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    today = datetime.now(ET)
    # 4年总数据：3年训练 + 1年验证
    total_start = today - timedelta(days=365 * 4)
    train_start = total_start
    train_end = today - timedelta(days=365)
    val_start = train_end
    val_end = today

    train_start_str = train_start.strftime("%Y-%m-%d")
    train_end_str = train_end.strftime("%Y-%m-%d")
    val_start_str = val_start.strftime("%Y-%m-%d")
    val_end_str = val_end.strftime("%Y-%m-%d")

    print(f"\n训练期: {train_start_str} → {train_end_str}")
    print(f"验证期: {val_start_str} → {val_end_str}")
    print(f"标的: {SYMBOLS}")

    all_train_trades = []
    all_val_trades = []

    for sym in SYMBOLS:
        print(f"\n正在回测 {sym}...")

        # 训练期
        train_trades = backtest_orb(
            data_client, sym, train_start_str, train_end_str
        )
        all_train_trades.extend(train_trades)
        print(f"  训练期: {len(train_trades)} 笔交易")

        # 验证期
        val_trades = backtest_orb(
            data_client, sym, val_start_str, val_end_str
        )
        all_val_trades.extend(val_trades)
        print(f"  验证期: {len(val_trades)} 笔交易")

    train_metrics = compute_metrics(all_train_trades)
    val_metrics = compute_metrics(all_val_trades)

    print("\n" + "=" * 60)
    print(" 训练期结果（3年）")
    print("=" * 60)
    for k, v in train_metrics.items():
        print(f"  {k}: {v}")

    print("\n" + "=" * 60)
    print(" 验证期结果（1年）")
    print("=" * 60)
    for k, v in val_metrics.items():
        print(f"  {k}: {v}")

    # 保存结果
    results = {
        "train_period": f"{train_start_str} → {train_end_str}",
        "val_period": f"{val_start_str} → {val_end_str}",
        "train_metrics": train_metrics,
        "val_metrics": val_metrics,
        "train_trades": all_train_trades,
        "val_trades": all_val_trades,
    }
    with open("walk_forward_results.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print("\n结果已保存到 walk_forward_results.json")

    # 过拟合判断
    if train_metrics["total_trades"] > 0 and val_metrics["total_trades"] > 0:
        train_wr = train_metrics["win_rate"]
        val_wr = val_metrics["win_rate"]
        wr_diff = abs(train_wr - val_wr)
        print(f"\n胜率差异: {wr_diff:.1f}%")
        if wr_diff > 15:
            print("⚠️ 胜率差异过大，可能存在过拟合")
        else:
            print("✅ 胜率差异在可接受范围内")

if __name__ == "__main__":
    walk_forward_analysis()
