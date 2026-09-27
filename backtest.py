# backtest.py
"""
ORB 策略本地回测脚本

使用方法：
  1. pip install -r requirements.txt
  2. 设置环境变量 ALPACA_API_KEY / ALPACA_SECRET_KEY
  3. python backtest.py

首次运行会从 Alpaca 拉取 3 年历史数据，缓存到 backtest_cache/。
之后重跑会直接读缓存，几秒钟完成。
"""
import os
import sys
import time
import json
import pickle
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
# 回测参数（可以在这里改）
# ══════════════════════════════════════════════
BACKTEST_START = "2023-01-01"
BACKTEST_END = "2025-12-31"
BACKTEST_CAPITAL = 100000
BACKTEST_SLIPPAGE_ENTRY = 0.0005  # 入场滑点 0.05%
BACKTEST_SLIPPAGE_EXIT = 0.0010   # 出场滑点 0.10%
BACKTEST_VIX_ENABLED = False      # Alpaca 无 VIX 历史数据，默认关闭

CACHE_DIR = "backtest_cache"
RESULT_FILE = "backtest_results.json"
PROGRESS_FILE = "backtest_progress.json"


# ══════════════════════════════════════════════
# 数据获取 + 缓存
# ══════════════════════════════════════════════
def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def fetch_minute_bars(data_client, symbol, start_date, end_date):
    """按月拉取分钟 K 线，缓存到本地"""
    ensure_cache_dir()
    symbol_dir = os.path.join(CACHE_DIR, symbol, "minute")
    os.makedirs(symbol_dir, exist_ok=True)

    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)
    months = pd.date_range(start=start_ts, end=end_ts, freq="MS")

    all_dfs = []
    for month_start in months:
        month_str = month_start.strftime("%Y-%m")
        cache_file = os.path.join(symbol_dir, f"{month_str}.pkl")

        if os.path.exists(cache_file):
            with open(cache_file, "rb") as f:
                df = pickle.load(f)
            print(f"  📦 缓存命中: {symbol} {month_str}")
        else:
            month_end = month_start + pd.offsets.MonthEnd(0)
            if month_end > end_ts:
                month_end = end_ts

            print(f"  📥 拉取: {symbol} {month_str}")
            try:
                req = StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=TimeFrame.Minute,
                    start=month_start.strftime("%Y-%m-%dT00:00:00-04:00"),
                    end=month_end.strftime("%Y-%m-%dT23:59:59-04:00"),
                    feed="iex",
                )
                bars = data_client.get_stock_bars(req)
                df = bars.df
                if not df.empty and isinstance(df.index, pd.MultiIndex):
                    df = df.xs(symbol, level="symbol")
            except Exception as e:
                print(f"  ⚠️ {symbol} {month_str} 拉取失败: {e}")
                df = pd.DataFrame()

            with open(cache_file, "wb") as f:
                pickle.dump(df, f)
            time.sleep(0.3)

        if not df.empty:
            all_dfs.append(df)

    if not all_dfs:
        return None

    result = pd.concat(all_dfs).sort_index()
    result = result[~result.index.duplicated(keep="first")]
    return result


def fetch_daily_bars(data_client, symbol, start_date, end_date):
    """拉取日线数据（用于 EMA200、ATR14）"""
    ensure_cache_dir()
    symbol_dir = os.path.join(CACHE_DIR, symbol)
    os.makedirs(symbol_dir, exist_ok=True)
    cache_file = os.path.join(symbol_dir, "daily.pkl")

    if os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            df = pickle.load(f)
        if not df.empty:
            return df

    print(f"  📥 拉取日线: {symbol}")
    try:
        # 多拉一年，保证 EMA200 有足够数据
        start_ts = pd.Timestamp(start_date) - pd.Timedelta(days=400)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start_ts.strftime("%Y-%m-%d"),
            end=end_date,
            feed="iex",
        )
        bars = data_client.get_stock_bars(req)
        df = bars.df
        if not df.empty and isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
    except Exception as e:
        print(f"  ⚠️ {symbol} 日线拉取失败: {e}")
        df = pd.DataFrame()

    with open(cache_file, "wb") as f:
        pickle.dump(df, f)
    return df


# ══════════════════════════════════════════════
# 指标计算
# ══════════════════════════════════════════════
def compute_daily_indicators(daily_df):
    """计算日线 EMA200、ATR14，返回按日期索引的 DataFrame"""
    if daily_df is None or daily_df.empty:
        return pd.DataFrame()

    df = daily_df.sort_index().copy()
    df["date"] = pd.to_datetime(df.index).strftime("%Y-%m-%d")
    df["ema200"] = df["close"].astype(float).ewm(span=200, adjust=False).mean()
    df["ema200_prev"] = df["ema200"].shift(TREND_EMA_SLOPE_LOOKBACK)
    df["ema_slope"] = df["ema200"] - df["ema200_prev"]

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()
    df["prev_close"] = close.shift(1)
    df["open"] = df["open"].astype(float)
    df["close"] = close

    return df.set_index("date")


# ══════════════════════════════════════════════
# 单标的回测
# ══════════════════════════════════════════════
def run_backtest_for_symbol(symbol, minute_df, daily_ind, capital):
    """对单个标的回测，返回交易列表和账户曲线"""
    if minute_df is None or minute_df.empty:
        return [], []

    minute_df = minute_df.copy()
    minute_df["date"] = minute_df.index.strftime("%Y-%m-%d")
    minute_df["time"] = minute_df.index.strftime("%H:%M")

    trade_dates = sorted(minute_df["date"].unique())
    trades = []
    equity_curve = []
    equity = capital

    for date in trade_dates:
        day_bars = minute_df[minute_df["date"] == date].sort_index()
        if day_bars.empty:
            equity_curve.append({"date": date, "equity": round(equity, 2)})
            continue

        # 开盘区间 9:30-9:45
        orb_mask = (day_bars["time"] >= "09:30") & (day_bars["time"] < "09:45")
        orb_bars = day_bars[orb_mask]
        if len(orb_bars) < 5:
            equity_curve.append({"date": date, "equity": round(equity, 2)})
            continue

        range_high = float(orb_bars["high"].max())
        range_low = float(orb_bars["low"].min())
        range_width = range_high - range_low
        range_avg_vol = float(orb_bars["volume"].mean())

        if range_low <= 0:
            equity_curve.append({"date": date, "equity": round(equity, 2)})
            continue
        if range_width / range_low < MIN_RANGE_PCT:
            equity_curve.append({"date": date, "equity": round(equity, 2)})
            continue

        # 日线指标
        if date not in daily_ind.index:
            equity_curve.append({"date": date, "equity": round(equity, 2)})
            continue
        d = daily_ind.loc[date]
        atr = float(d["atr14"]) if pd.notna(d["atr14"]) else None
        ema200 = float(d["ema200"]) if pd.notna(d["ema200"]) else None
        ema_slope = float(d["ema_slope"]) if pd.notna(d["ema_slope"]) else None
        today_open = float(d["open"])
        prev_close = float(d["prev_close"]) if pd.notna(d["prev_close"]) else None

        # ATR 过滤
        if atr is not None and range_width > MAX_RANGE_ATR_MULTIPLIER * atr:
            equity_curve.append({"date": date, "equity": round(equity, 2)})
            continue

        # 缺口过滤
        size_mult = 1.0
        if prev_close and prev_close > 0:
            gap_pct = abs(today_open - prev_close) / prev_close
            if gap_pct > GAP_SKIP_THRESHOLD:
                equity_curve.append({"date": date, "equity": round(equity, 2)})
                continue
            elif gap_pct > GAP_REDUCE_THRESHOLD:
                size_mult = 0.5

        # 交易时段（9:45 到截止时间）
        cutoff_min = TRADE_CUTOFF_HOUR * 60 + TRADE_CUTOFF_MIN
        trade_mask = (day_bars["time"] >= "09:45") & (day_bars["time"] <= "15:43")
        trade_bars = day_bars[trade_mask]
        if trade_bars.empty:
            equity_curve.append({"date": date, "equity": round(equity, 2)})
            continue

        # 逐分钟模拟
        position = None
        trades_today = 0

        for ts, bar in trade_bars.iterrows():
            time_str = bar["time"]
            hour = int(time_str[:2])
            minute = int(time_str[3:5])
            current_min = hour * 60 + minute

            price_close = float(bar["close"])
            price_high = float(bar["high"])
            price_low = float(bar["low"])
            volume = float(bar["volume"])

            # ── 持仓管理 ──
            if position is not None:
                side = position["side"]
                entry = position["entry"]
                stop = position["stop"]
                qty = position["qty"]
                rps = position["risk_per_share"]
                highest = position["highest"]
                lowest = position["lowest"]
                tp1_done = position["tp1_done"]
                tp2_done = position["tp2_done"]
                be_done = position["be_done"]
                entry_time = position["entry_time"]

                # 更新最高/最低
                if side == "buy":
                    highest = max(highest, price_high)
                else:
                    lowest = min(lowest, price_low)

                # 检查 TP1 / TP2
                if side == "buy":
                    favorable = highest - entry
                else:
                    favorable = entry - lowest
                r_mult = favorable / rps if rps > 0 else 0

                # TP1
                if SCALE_OUT_ENABLED and not tp1_done and r_mult >= SCALE_OUT_LEVELS[0]["r_multiple"]:
                    exit_qty = max(int(qty * SCALE_OUT_LEVELS[0]["exit_pct"]), 1)
                    exit_qty = min(exit_qty, qty)
                    exit_price = entry + SCALE_OUT_LEVELS[0]["r_multiple"] * rps
                    if side == "buy":
                        exit_price *= (1 - BACKTEST_SLIPPAGE_EXIT)
                    else:
                        exit_price *= (1 + BACKTEST_SLIPPAGE_EXIT)
                    pnl = (exit_price - entry) * exit_qty if side == "buy" else (entry - exit_price) * exit_qty
                    trades.append({
                        "date": date, "symbol": symbol, "side": side,
                        "entry_price": round(entry, 2), "exit_price": round(exit_price, 2),
                        "qty": exit_qty, "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl / (entry * exit_qty) * 100, 2),
                        "exit_reason": "TP1",
                        "entry_time": entry_time, "exit_time": time_str,
                    })
                    qty -= exit_qty
                    tp1_done = True
                    position["qty"] = qty
                    position["tp1_done"] = True

                # TP2
                if SCALE_OUT_ENABLED and not tp2_done and r_mult >= SCALE_OUT_LEVELS[1]["r_multiple"]:
                    exit_qty = max(int(qty * SCALE_OUT_LEVELS[1]["exit_pct"]), 1)
                    exit_qty = min(exit_qty, qty)
                    exit_price = entry + SCALE_OUT_LEVELS[1]["r_multiple"] * rps
                    if side == "buy":
                        exit_price *= (1 - BACKTEST_SLIPPAGE_EXIT)
                    else:
                        exit_price *= (1 + BACKTEST_SLIPPAGE_EXIT)
                    pnl = (exit_price - entry) * exit_qty if side == "buy" else (entry - exit_price) * exit_qty
                    trades.append({
                        "date": date, "symbol": symbol, "side": side,
                        "entry_price": round(entry, 2), "exit_price": round(exit_price, 2),
                        "qty": exit_qty, "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl / (entry * exit_qty) * 100, 2),
                        "exit_reason": "TP2",
                        "entry_time": entry_time, "exit_time": time_str,
                    })
                    qty -= exit_qty
                    tp2_done = True
                    position["qty"] = qty
                    position["tp2_done"] = True

                # 自动保本
                if AUTO_BE_ENABLED and not be_done and r_mult >= AUTO_BE_TRIGGER_R:
                    be_done = True
                    position["be_done"] = True
                    position["trail_pct"] = AUTO_BE_TRAIL_PERCENT

                # Trailing Stop
                trail_pct = position.get("trail_pct", TRAIL_PERCENT)
                if side == "buy":
                    trail_stop = highest * (1 - trail_pct / 100)
                    if price_low <= trail_stop:
                        exit_price = trail_stop * (1 - BACKTEST_SLIPPAGE_EXIT)
                        pnl = (exit_price - entry) * qty
                        trades.append({
                            "date": date, "symbol": symbol, "side": side,
                            "entry_price": round(entry, 2), "exit_price": round(exit_price, 2),
                            "qty": qty, "pnl": round(pnl, 2),
                            "pnl_pct": round(pnl / (entry * qty) * 100, 2),
                            "exit_reason": "Trailing",
                            "entry_time": entry_time, "exit_time": time_str,
                        })
                        position = None
                        trades_today += 1
                        continue
                else:
                    trail_stop = lowest * (1 + trail_pct / 100)
                    if price_high >= trail_stop:
                        exit_price = trail_stop * (1 + BACKTEST_SLIPPAGE_EXIT)
                        pnl = (entry - exit_price) * qty
                        trades.append({
                            "date": date, "symbol": symbol, "side": side,
                            "entry_price": round(entry, 2), "exit_price": round(exit_price, 2),
                            "qty": qty, "pnl": round(pnl, 2),
                            "pnl_pct": round(pnl / (entry * qty) * 100, 2),
                            "exit_reason": "Trailing",
                            "entry_time": entry_time, "exit_time": time_str,
                        })
                        position = None
                        trades_today += 1
                        continue

                # 时间止损
                if TIME_STOP_MINUTES > 0:
                    try:
                        entry_dt = pd.Timestamp(f"{date} {entry_time}").tz_localize(ET)
                        now_dt = pd.Timestamp(f"{date} {time_str}").tz_localize(ET)
                        elapsed = (now_dt - entry_dt).total_seconds() / 60
                        if elapsed >= TIME_STOP_MINUTES and r_mult < AUTO_BE_TRIGGER_R:
                            exit_price = price_close
                            if side == "buy":
                                exit_price *= (1 - BACKTEST_SLIPPAGE_EXIT)
                            else:
                                exit_price *= (1 + BACKTEST_SLIPPAGE_EXIT)
                            pnl = (exit_price - entry) * qty if side == "buy" else (entry - exit_price) * qty
                            trades.append({
                                "date": date, "symbol": symbol, "side": side,
                                "entry_price": round(entry, 2), "exit_price": round(exit_price, 2),
                                "qty": qty, "pnl": round(pnl, 2),
                                "pnl_pct": round(pnl / (entry * qty) * 100, 2),
                                "exit_reason": "TimeStop",
                                "entry_time": entry_time, "exit_time": time_str,
                            })
                            position = None
                            trades_today += 1
                            continue
                    except Exception:
                        pass
                continue

            # ── 无持仓：检查入场 ──
            if trades_today >= MAX_TRADES_PER_DAY:
                continue
            if current_min >= cutoff_min:
                continue
            if volume < VOLUME_MULTIPLIER * range_avg_vol:
                continue

            signal_side = None
            signal_stop = None
            if price_close > range_high:
                signal_side = "buy"
                signal_stop = range_low
                # EMA 过滤
                if ema200 is not None and today_open <= ema200:
                    continue
                if ema_slope is not None and ema200 > 0:
                    slope_pct = ema_slope / ema200
                    if slope_pct < -TREND_EMA_SLOPE_THRESHOLD:
                        continue
            elif price_close < range_low:
                signal_side = "sell"
                signal_stop = range_high
                if ema200 is not None and today_open >= ema200:
                    continue
                if ema_slope is not None and ema200 > 0:
                    slope_pct = ema_slope / ema200
                    if slope_pct > TREND_EMA_SLOPE_THRESHOLD:
                        continue
            else:
                continue

            # 仓位计算
            rps = abs(price_close - signal_stop)
            if rps <= 0:
                continue

            # 波动率调整
            vol_mult = 1.0
            if VOL_ADJUST_ENABLED and atr and price_close > 0:
                current_vol = atr / price_close
                if current_vol > 0:
                    vol_mult = min(VOL_TARGET / current_vol, 2.0)

            dollar_risk = equity * RISK_PER_TRADE * size_mult * vol_mult
            qty = int(dollar_risk / rps)
            qty = min(qty, int(equity / price_close))
            if qty <= 0:
                continue

            # 入场价加滑点
            if signal_side == "buy":
                entry_price = price_close * (1 + BACKTEST_SLIPPAGE_ENTRY)
            else:
                entry_price = price_close * (1 - BACKTEST_SLIPPAGE_ENTRY)

            position = {
                "side": signal_side,
                "entry": entry_price,
                "stop": signal_stop,
                "qty": qty,
                "risk_per_share": rps,
                "highest": entry_price,
                "lowest": entry_price,
                "tp1_done": False,
                "tp2_done": False,
                "be_done": False,
                "trail_pct": TRAIL_PERCENT,
                "entry_time": time_str,
            }

        # 收盘平仓
        if position is not None:
            last_bar = trade_bars.iloc[-1]
            exit_price = float(last_bar["close"])
            if position["side"] == "buy":
                exit_price *= (1 - BACKTEST_SLIPPAGE_EXIT)
                pnl = (exit_price - position["entry"]) * position["qty"]
            else:
                exit_price *= (1 + BACKTEST_SLIPPAGE_EXIT)
                pnl = (position["entry"] - exit_price) * position["qty"]
            trades.append({
                "date": date, "symbol": symbol, "side": position["side"],
                "entry_price": round(position["entry"], 2),
                "exit_price": round(exit_price, 2),
                "qty": position["qty"],
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / (position["entry"] * position["qty"]) * 100, 2),
                "exit_reason": "EOD",
                "entry_time": position["entry_time"],
                "exit_time": trade_bars.iloc[-1]["time"],
            })
            position = None

        # 汇总当日盈亏
        day_pnl = sum(t["pnl"] for t in trades if t["date"] == date)
        equity += day_pnl
        equity_curve.append({"date": date, "equity": round(equity, 2)})

    return trades, equity_curve


# ══════════════════════════════════════════════
# 指标计算
# ══════════════════════════════════════════════
def compute_metrics(trades, equity_curve, capital):
    if not trades:
        return {}

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl = sum(pnls)
    total_return = total_pnl / capital * 100

    # 年化收益
    equity_series = pd.Series([e["equity"] for e in equity_curve])
    days = len(equity_curve)
    years = days / 252 if days > 0 else 1
    if years > 0 and equity_series.iloc[-1] > 0:
        cagr = ((equity_series.iloc[-1] / capital) ** (1 / years) - 1) * 100
    else:
        cagr = 0

    # 最大回撤
    peak = equity_series.cummax()
    dd = (equity_series - peak) / peak * 100
    max_dd = dd.min()

    # 夏普比率（简化，按日收益算）
    daily_returns = equity_series.pct_change().dropna()
    sharpe = 0
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe = daily_returns.mean() / daily_returns.std() * np.sqrt(252)

    win_rate = len(wins) / len(pnls) * 100 if pnls else 0
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else 0

    # 连续盈亏
    max_win_streak = 0
    max_loss_streak = 0
    cur_win = 0
    cur_loss = 0
    for p in pnls:
        if p > 0:
            cur_win += 1
            cur_loss = 0
            max_win_streak = max(max_win_streak, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            max_loss_streak = max(max_loss_streak, cur_loss)

    return {
        "capital": capital,
        "final_equity": round(equity_series.iloc[-1], 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(total_return, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "total_trades": len(trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(pf, 2),
        "avg_pnl": round(total_pnl / len(pnls), 2),
        "max_win": round(max(wins), 2) if wins else 0,
        "max_loss": round(min(losses), 2) if losses else 0,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "trading_days": days,
    }


# ══════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════
def main():
    api_key = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not api_secret:
        print("❌ 请先设置环境变量 ALPACA_API_KEY 和 ALPACA_SECRET_KEY")
        sys.exit(1)

    print("=" * 60)
    print(f" ORB 回测 | {BACKTEST_START} ~ {BACKTEST_END}")
    print(f" 标的: {SYMBOLS}")
    print(f" 初始资金: ${BACKTEST_CAPITAL:,}")
    print(f" 滑点: 入场 {BACKTEST_SLIPPAGE_ENTRY*100:.2f}% / 出场 {BACKTEST_SLIPPAGE_EXIT*100:.2f}%")
    print("=" * 60)

    data_client = StockHistoricalDataClient(api_key, api_secret)

    all_trades = []
    all_equity_curves = {}
    per_symbol_stats = {}

    for symbol in SYMBOLS:
        print(f"\n━━━ {symbol} ━━━")

        # 拉取数据
        minute_df = fetch_minute_bars(data_client, symbol, BACKTEST_START, BACKTEST_END)
        if minute_df is None or minute_df.empty:
            print(f" ⚠️ {symbol} 分钟数据为空，跳过")
            continue

        daily_df = fetch_daily_bars(data_client, symbol, BACKTEST_START, BACKTEST_END)
        daily_ind = compute_daily_indicators(daily_df)

        print(f"  📊 分钟数据: {len(minute_df):,} 条 / 日线: {len(daily_df)} 条")

        # 回测
        trades, equity = run_backtest_for_symbol(symbol, minute_df, daily_ind, BACKTEST_CAPITAL)
        print(f"  ✅ 完成 {len(trades)} 笔交易")

        # 独立计算该标的统计
        sym_trades = [t for t in trades if t["symbol"] == symbol]
        if sym_trades:
            sym_pnls = [t["pnl"] for t in sym_trades]
            sym_wins = [p for p in sym_pnls if p > 0]
            sym_losses = [p for p in sym_pnls if p <= 0]
            per_symbol_stats[symbol] = {
                "trades": len(sym_trades),
                "win_rate": round(len(sym_wins) / len(sym_trades) * 100, 2) if sym_trades else 0,
                "total_pnl": round(sum(sym_pnls), 2),
                "avg_pnl": round(sum(sym_pnls) / len(sym_trades), 2),
                "max_win": round(max(sym_wins), 2) if sym_wins else 0,
                "max_loss": round(min(sym_losses), 2) if sym_losses else 0,
                "profit_factor": round(
                    sum(sym_wins) / abs(sum(sym_losses)), 2
                ) if sym_losses and sum(sym_losses) != 0 else 0,
            }
        else:
            per_symbol_stats[symbol] = {
                "trades": 0, "win_rate": 0, "total_pnl": 0,
                "avg_pnl": 0, "max_win": 0, "max_loss": 0, "profit_factor": 0,
            }

        all_trades.extend(trades)
        all_equity_curves[symbol] = equity

        # 保存中间结果（防止中途出错丢数据）
        with open(PROGRESS_FILE, "w") as f:
            json.dump({
                "completed": symbol,
                "total_trades": len(all_trades),
            }, f)

    if not all_trades:
        print("❌ 没有生成任何交易")
        return

    # 组合净值曲线（按日期合并所有标的的盈亏）
    print("\n" + "=" * 60)
    print(" 计算组合统计")
    print("=" * 60)

    trades_by_date = {}
    for t in all_trades:
        trades_by_date.setdefault(t["date"], []).append(t["pnl"])

    all_dates = sorted(trades_by_date.keys())
    combined_equity = []
    equity = BACKTEST_CAPITAL
    for d in all_dates:
        equity += sum(trades_by_date[d])
        combined_equity.append({"date": d, "equity": round(equity, 2)})

    combined_metrics = compute_metrics(all_trades, combined_equity, BACKTEST_CAPITAL)
    print(f" 总交易: {combined_metrics['total_trades']}")
    print(f" 总收益: ${combined_metrics['total_pnl']:,.2f} ({combined_metrics['total_return_pct']:.2f}%)")
    print(f" 年化: {combined_metrics['cagr_pct']:.2f}%")
    print(f" 最大回撤: {combined_metrics['max_drawdown_pct']:.2f}%")
    print(f" 夏普: {combined_metrics['sharpe']:.2f}")
    print(f" 胜率: {combined_metrics['win_rate']:.2f}%")
    print(f" 盈亏比: {combined_metrics['profit_factor']:.2f}")

    # 按年份统计
    yearly_stats = {}
    for year in sorted(set(t["date"][:4] for t in all_trades)):
        year_trades = [t for t in all_trades if t["date"].startswith(year)]
        year_pnls = [t["pnl"] for t in year_trades]
        year_wins = [p for p in year_pnls if p > 0]
        year_capital = BACKTEST_CAPITAL
        yearly_stats[year] = {
            "trades": len(year_trades),
            "win_rate": round(len(year_wins) / len(year_trades) * 100, 2) if year_trades else 0,
            "total_pnl": round(sum(year_pnls), 2),
            "return_pct": round(sum(year_pnls) / year_capital * 100, 2),
            "max_win": round(max(year_wins), 2) if year_wins else 0,
            "max_loss": round(min([p for p in year_pnls if p <= 0]), 2) if [p for p in year_pnls if p <= 0] else 0,
        }

    # 月度收益热力图
    monthly_data = {}
    for t in all_trades:
        ym = t["date"][:7]
        monthly_data[ym] = monthly_data.get(ym, 0) + t["pnl"]

    # 出场原因分布
    exit_reasons = {}
    for t in all_trades:
        r = t["exit_reason"]
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    # 结果汇总
    result = {
        "backtest_info": {
            "start": BACKTEST_START,
            "end": BACKTEST_END,
            "capital": BACKTEST_CAPITAL,
            "symbols": SYMBOLS,
            "slippage_entry": BACKTEST_SLIPPAGE_ENTRY,
            "slippage_exit": BACKTEST_SLIPPAGE_EXIT,
            "generated_at": datetime.now().isoformat(),
            "config_snapshot": {
                "RISK_PER_TRADE": RISK_PER_TRADE,
                "MAX_TRADES_PER_DAY": MAX_TRADES_PER_DAY,
                "TRAIL_PERCENT": TRAIL_PERCENT,
                "VOLUME_MULTIPLIER": VOLUME_MULTIPLIER,
                "MIN_RANGE_PCT": MIN_RANGE_PCT,
                "MAX_RANGE_ATR_MULTIPLIER": MAX_RANGE_ATR_MULTIPLIER,
                "TREND_EMA_PERIOD": TREND_EMA_PERIOD,
                "GAP_SKIP_THRESHOLD": GAP_SKIP_THRESHOLD,
                "GAP_REDUCE_THRESHOLD": GAP_REDUCE_THRESHOLD,
            },
        },
        "metrics": combined_metrics,
        "per_symbol": per_symbol_stats,
        "yearly": yearly_stats,
        "monthly": monthly_data,
        "exit_reasons": exit_reasons,
        "equity_curve": combined_equity,
        "trades": all_trades,
    }

    with open(RESULT_FILE, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 清理进度文件
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)

    print("\n" + "=" * 60)
    print(f" ✅ 结果已保存到 {RESULT_FILE}")
    print(f" 文件大小: {os.path.getsize(RESULT_FILE) / 1024:.1f} KB")
    print("=" * 60)
    print("\n下一步：")
    print("  git add backtest_results.json")
    print("  git commit -m 'Update backtest results'")
    print("  git push")


if __name__ == "__main__":
    main()
