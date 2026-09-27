# backtest.py
"""
ORB 策略回测脚本（在 GitHub Actions 上运行）

v3 改动：
  1. 初始止损改为区间中位线（原来是对侧区间）
  2. 量能过滤从 1.5× 改为 2.0×
  3. 交易时段截止从 11:30 改为 10:30
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

BACKTEST_START = os.getenv("BACKTEST_START", "2023-01-01")
BACKTEST_END = os.getenv("BACKTEST_END", "2025-12-31")
BACKTEST_CAPITAL = int(os.getenv("BACKTEST_CAPITAL", "100000"))
BACKTEST_SLIPPAGE_ENTRY = 0.0005
BACKTEST_SLIPPAGE_EXIT = 0.0010

# ── 回测专用参数（v3 改动）──
BT_VOLUME_MULTIPLIER = 2.0         # 改动2：量能从 1.5× 改为 2.0×
BT_TRADE_CUTOFF_HOUR = 10          # 改动3：10:30 停止开新仓
BT_TRADE_CUTOFF_MIN = 30
BT_INITIAL_STOP_MODE = "midpoint"  # 改动1：初始止损=区间中位线

# ── 结构化 Trailing Stop 参数 ──
STRUCTURED_TRAIL = True
TRAIL_STEP_R = 1.0
TIME_STOP_MINUTES_BT = 120
TIME_STOP_R_THRESHOLD = 0.5

# ── 多级止盈 ──
BT_SCALE_OUT_LEVELS = [
    {"r_multiple": 2.0, "exit_pct": 0.33},
    {"r_multiple": 3.0, "exit_pct": 0.33},
]

# ── 单日亏损熔断 ──
DAILY_LOSS_LIMIT_R = 3.0

CACHE_DIR = "backtest_cache"
RESULT_FILE = "backtest_results.json"


def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


# ══════════════════════════════════════════════
# 时区转换
# ══════════════════════════════════════════════
def convert_index_to_et(df):
    if df is None or df.empty:
        return df
    idx = df.index
    try:
        if isinstance(idx, pd.MultiIndex):
            df.index = pd.DatetimeIndex(idx).tz_convert(ET)
        else:
            if idx.tz is None:
                df.index = pd.DatetimeIndex(idx).tz_localize("UTC").tz_convert(ET)
            else:
                df.index = idx.tz_convert(ET)
    except Exception as e:
        print(f"  ⚠️ 时区转换失败: {e}")
    return df


# ══════════════════════════════════════════════
# 数据拉取
# ══════════════════════════════════════════════
def fetch_minute_bars(data_client, symbol, start_date, end_date):
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
            try:
                with open(cache_file, "rb") as f:
                    df = pickle.load(f)
                print(f"  📦 缓存: {symbol} {month_str} ({len(df)} 条)")
            except Exception:
                df = None
        else:
            df = None

        if df is None:
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
                print(f"  ⚠️ {symbol} {month_str} 失败: {e}")
                df = pd.DataFrame()

            try:
                with open(cache_file, "wb") as f:
                    pickle.dump(df, f)
            except Exception:
                pass
            time.sleep(0.3)

        if df is not None and not df.empty:
            all_dfs.append(df)

    if not all_dfs:
        return None

    result = pd.concat(all_dfs).sort_index()
    result = result[~result.index.duplicated(keep="first")]
    result = convert_index_to_et(result)
    return result


def fetch_daily_bars(data_client, symbol, start_date, end_date):
    ensure_cache_dir()
    symbol_dir = os.path.join(CACHE_DIR, symbol)
    os.makedirs(symbol_dir, exist_ok=True)
    cache_file = os.path.join(symbol_dir, "daily.pkl")

    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                df = pickle.load(f)
            if not df.empty:
                print(f"  📦 日线缓存: {symbol} ({len(df)} 条)")
                return convert_index_to_et(df)
        except Exception:
            pass

    print(f"  📥 拉取日线: {symbol}")
    try:
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
        print(f"  ⚠️ {symbol} 日线失败: {e}")
        df = pd.DataFrame()

    try:
        with open(cache_file, "wb") as f:
            pickle.dump(df, f)
    except Exception:
        pass

    return convert_index_to_et(df)


# ══════════════════════════════════════════════
# 指标计算
# ══════════════════════════════════════════════
def compute_daily_indicators(daily_df):
    if daily_df is None or daily_df.empty:
        return pd.DataFrame()

    df = daily_df.sort_index().copy()
    df["date"] = df.index.strftime("%Y-%m-%d")
    df["close"] = df["close"].astype(float)
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["ema200_prev"] = df["ema200"].shift(TREND_EMA_SLOPE_LOOKBACK)
    df["ema_slope"] = df["ema200"] - df["ema200_prev"]

    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()
    df["prev_close"] = close.shift(1)

    return df.set_index("date")


# ══════════════════════════════════════════════
# 结构化 Trailing Stop
# ══════════════════════════════════════════════
def compute_structured_trail_stop(side, entry, rps, current_r):
    """
    R < 1:    返回 None（用初始止损）
    1 <= R < 2: 止损 = 入场价
    2 <= R < 3: 止损 = 入场价 + 1R
    一般规则：止损 = 入场价 + floor(R - 1) * R
    """
    if current_r < 1.0:
        return None
    steps = int(current_r - 1.0)
    locked_r = steps * 1.0
    if side == "buy":
        return entry + locked_r * rps
    else:
        return entry - locked_r * rps


# ══════════════════════════════════════════════
# 单标的回测
# ══════════════════════════════════════════════
def run_backtest_for_symbol(symbol, minute_df, daily_ind, capital, debug=False):
    if minute_df is None or minute_df.empty:
        return [], []

    minute_df = minute_df.copy()
    minute_df["date"] = minute_df.index.strftime("%Y-%m-%d")
    minute_df["time"] = minute_df.index.strftime("%H:%M")

    trade_dates = sorted(minute_df["date"].unique())

    if debug:
        print(f"\n  🔍 [{symbol}] 调试信息:")
        print(f"     索引时区: {minute_df.index.tz}")
        print(f"     日期数量: {len(trade_dates)}")
        print(f"     首日: {trade_dates[0]}")
        print(f"     末日: {trade_dates[-1]}")

    trades = []
    equity_curve = []
    equity = capital

    skip_reasons = {
        "account_blown": 0,
        "no_orb_bars": 0,
        "range_too_narrow": 0,
        "no_daily_data": 0,
        "atr_filter": 0,
        "gap_skip": 0,
        "no_trade_bars": 0,
        "no_breakout": 0,
        "ema_filter": 0,
        "volume_filter": 0,
        "daily_loss_limit": 0,
        "past_cutoff": 0,
    }

    cutoff_min = BT_TRADE_CUTOFF_HOUR * 60 + BT_TRADE_CUTOFF_MIN

    for date in trade_dates:
        if equity <= 0:
            skip_reasons["account_blown"] += 1
            break

        day_bars = minute_df[minute_df["date"] == date].sort_index()
        if day_bars.empty:
            equity_curve.append({"date": date, "equity": round(equity, 2)})
            continue

        orb_bars = day_bars[(day_bars["time"] >= "09:30") & (day_bars["time"] < "09:45")]
        if len(orb_bars) < 5:
            skip_reasons["no_orb_bars"] += 1
            equity_curve.append({"date": date, "equity": round(equity, 2)})
            continue

        range_high = float(orb_bars["high"].max())
        range_low = float(orb_bars["low"].min())
        range_width = range_high - range_low
        range_mid = (range_high + range_low) / 2.0  # 改动1：中位线
        range_avg_vol = float(orb_bars["volume"].mean())

        if range_low <= 0 or range_width / range_low < MIN_RANGE_PCT:
            skip_reasons["range_too_narrow"] += 1
            equity_curve.append({"date": date, "equity": round(equity, 2)})
            continue

        try:
            d = daily_ind.loc[date]
        except KeyError:
            skip_reasons["no_daily_data"] += 1
            equity_curve.append({"date": date, "equity": round(equity, 2)})
            continue

        atr = float(d["atr14"]) if pd.notna(d["atr14"]) else None
        ema200 = float(d["ema200"]) if pd.notna(d["ema200"]) else None
        ema_slope = float(d["ema_slope"]) if pd.notna(d["ema_slope"]) else None
        today_open = float(d["open"])
        prev_close = float(d["prev_close"]) if pd.notna(d["prev_close"]) else None

        if atr is not None and range_width > MAX_RANGE_ATR_MULTIPLIER * atr:
            skip_reasons["atr_filter"] += 1
            equity_curve.append({"date": date, "equity": round(equity, 2)})
            continue

        size_mult = 1.0
        if prev_close and prev_close > 0:
            gap_pct = abs(today_open - prev_close) / prev_close
            if gap_pct > GAP_SKIP_THRESHOLD:
                skip_reasons["gap_skip"] += 1
                equity_curve.append({"date": date, "equity": round(equity, 2)})
                continue
            elif gap_pct > GAP_REDUCE_THRESHOLD:
                size_mult = 0.5

        # 改动3：9:45 到 10:30 之间交易
        trade_bars = day_bars[(day_bars["time"] >= "09:45") & (day_bars["time"] <= "15:43")]
        if trade_bars.empty:
            skip_reasons["no_trade_bars"] += 1
            equity_curve.append({"date": date, "equity": round(equity, 2)})
            continue

        position = None
        trades_today = 0
        daily_pnl_r = 0.0

        for ts, bar in trade_bars.iterrows():
            time_str = bar["time"]
            hour = int(time_str[:2])
            minute = int(time_str[3:5])
            current_min = hour * 60 + minute

            price_close = float(bar["close"])
            price_high = float(bar["high"])
            price_low = float(bar["low"])
            volume = float(bar["volume"])

            if position is not None:
                side = position["side"]
                entry = position["entry"]
                qty = position["qty"]
                rps = position["risk_per_share"]
                initial_stop = position["initial_stop"]
                highest = position["highest"]
                lowest = position["lowest"]
                entry_time = position["entry_time"]

                if side == "buy":
                    highest = max(highest, price_high)
                    position["highest"] = highest
                    favorable = highest - entry
                else:
                    lowest = min(lowest, price_low)
                    position["lowest"] = lowest
                    favorable = entry - lowest

                r_mult = favorable / rps if rps > 0 else 0

                # TP1 (2R)
                if SCALE_OUT_ENABLED and not position["tp1_done"] and r_mult >= BT_SCALE_OUT_LEVELS[0]["r_multiple"]:
                    exit_qty = max(int(qty * BT_SCALE_OUT_LEVELS[0]["exit_pct"]), 1)
                    exit_qty = min(exit_qty, qty)
                    exit_price = entry + BT_SCALE_OUT_LEVELS[0]["r_multiple"] * rps
                    exit_price = exit_price * (1 - BACKTEST_SLIPPAGE_EXIT) if side == "buy" else exit_price * (1 + BACKTEST_SLIPPAGE_EXIT)
                    pnl = (exit_price - entry) * exit_qty if side == "buy" else (entry - exit_price) * exit_qty
                    trades.append({
                        "date": date, "symbol": symbol, "side": side,
                        "entry_price": round(entry, 2), "exit_price": round(exit_price, 2),
                        "qty": exit_qty, "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl / (entry * exit_qty) * 100, 2),
                        "exit_reason": "TP1_2R",
                        "r_multiple": round(BT_SCALE_OUT_LEVELS[0]["r_multiple"], 2),
                        "entry_time": entry_time, "exit_time": time_str,
                    })
                    qty -= exit_qty
                    position["qty"] = qty
                    position["tp1_done"] = True

                # TP2 (3R)
                if SCALE_OUT_ENABLED and not position["tp2_done"] and r_mult >= BT_SCALE_OUT_LEVELS[1]["r_multiple"]:
                    exit_qty = max(int(qty * BT_SCALE_OUT_LEVELS[1]["exit_pct"]), 1)
                    exit_qty = min(exit_qty, qty)
                    exit_price = entry + BT_SCALE_OUT_LEVELS[1]["r_multiple"] * rps
                    exit_price = exit_price * (1 - BACKTEST_SLIPPAGE_EXIT) if side == "buy" else exit_price * (1 + BACKTEST_SLIPPAGE_EXIT)
                    pnl = (exit_price - entry) * exit_qty if side == "buy" else (entry - exit_price) * exit_qty
                    trades.append({
                        "date": date, "symbol": symbol, "side": side,
                        "entry_price": round(entry, 2), "exit_price": round(exit_price, 2),
                        "qty": exit_qty, "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl / (entry * exit_qty) * 100, 2),
                        "exit_reason": "TP2_3R",
                        "r_multiple": round(BT_SCALE_OUT_LEVELS[1]["r_multiple"], 2),
                        "entry_time": entry_time, "exit_time": time_str,
                    })
                    qty -= exit_qty
                    position["qty"] = qty
                    position["tp2_done"] = True

                # 结构化 Trailing
                trail_stop = None
                if STRUCTURED_TRAIL:
                    trail_stop = compute_structured_trail_stop(side, entry, rps, r_mult)
                    if trail_stop is None:
                        trail_stop = initial_stop
                else:
                    trail_pct = TRAIL_PERCENT
                    if side == "buy":
                        trail_stop = highest * (1 - trail_pct / 100)
                    else:
                        trail_stop = lowest * (1 + trail_pct / 100)

                # 触发检查
                if side == "buy":
                    if price_low <= trail_stop and qty > 0:
                        exit_price = trail_stop * (1 - BACKTEST_SLIPPAGE_EXIT)
                        pnl = (exit_price - entry) * qty
                        r_realized = (exit_price - entry) / rps
                        trades.append({
                            "date": date, "symbol": symbol, "side": side,
                            "entry_price": round(entry, 2), "exit_price": round(exit_price, 2),
                            "qty": qty, "pnl": round(pnl, 2),
                            "pnl_pct": round(pnl / (entry * qty) * 100, 2),
                            "exit_reason": "Trail" if r_mult >= 1.0 else "InitialStop",
                            "r_multiple": round(r_realized, 2),
                            "entry_time": entry_time, "exit_time": time_str,
                        })
                        daily_pnl_r += r_realized * (qty / position["initial_qty"]) if position["initial_qty"] > 0 else 0
                        position = None
                        trades_today += 1
                        continue
                else:
                    if price_high >= trail_stop and qty > 0:
                        exit_price = trail_stop * (1 + BACKTEST_SLIPPAGE_EXIT)
                        pnl = (entry - exit_price) * qty
                        r_realized = (entry - exit_price) / rps
                        trades.append({
                            "date": date, "symbol": symbol, "side": side,
                            "entry_price": round(entry, 2), "exit_price": round(exit_price, 2),
                            "qty": qty, "pnl": round(pnl, 2),
                            "pnl_pct": round(pnl / (entry * qty) * 100, 2),
                            "exit_reason": "Trail" if r_mult >= 1.0 else "InitialStop",
                            "r_multiple": round(r_realized, 2),
                            "entry_time": entry_time, "exit_time": time_str,
                        })
                        daily_pnl_r += r_realized * (qty / position["initial_qty"]) if position["initial_qty"] > 0 else 0
                        position = None
                        trades_today += 1
                        continue

                # 时间止损
                if TIME_STOP_MINUTES_BT > 0 and qty > 0:
                    try:
                        entry_dt = pd.Timestamp(f"{date} {entry_time}").tz_localize(ET)
                        now_dt = pd.Timestamp(f"{date} {time_str}").tz_localize(ET)
                        elapsed = (now_dt - entry_dt).total_seconds() / 60
                        if elapsed >= TIME_STOP_MINUTES_BT and r_mult < TIME_STOP_R_THRESHOLD:
                            exit_price = price_close
                            exit_price = exit_price * (1 - BACKTEST_SLIPPAGE_EXIT) if side == "buy" else exit_price * (1 + BACKTEST_SLIPPAGE_EXIT)
                            pnl = (exit_price - entry) * qty if side == "buy" else (entry - exit_price) * qty
                            r_realized = (exit_price - entry) / rps if side == "buy" else (entry - exit_price) / rps
                            trades.append({
                                "date": date, "symbol": symbol, "side": side,
                                "entry_price": round(entry, 2), "exit_price": round(exit_price, 2),
                                "qty": qty, "pnl": round(pnl, 2),
                                "pnl_pct": round(pnl / (entry * qty) * 100, 2),
                                "exit_reason": "TimeStop",
                                "r_multiple": round(r_realized, 2),
                                "entry_time": entry_time, "exit_time": time_str,
                            })
                            daily_pnl_r += r_realized * (qty / position["initial_qty"]) if position["initial_qty"] > 0 else 0
                            position = None
                            trades_today += 1
                            continue
                    except Exception:
                        pass

                continue

            # ── 新入场检查 ──
            if daily_pnl_r <= -DAILY_LOSS_LIMIT_R:
                skip_reasons["daily_loss_limit"] += 1
                continue

            if trades_today >= MAX_TRADES_PER_DAY:
                continue

            # 改动3：过了 10:30 不再开新仓
            if current_min >= cutoff_min:
                skip_reasons["past_cutoff"] += 1
                continue

            # 改动2：量能过滤 2.0×
            if volume < BT_VOLUME_MULTIPLIER * range_avg_vol:
                skip_reasons["volume_filter"] += 1
                continue

            signal_side = None
            signal_stop = None
            if price_close > range_high:
                signal_side = "buy"
                # 改动1：止损改为区间中位线
                signal_stop = range_mid
                if ema200 is not None and today_open <= ema200:
                    skip_reasons["ema_filter"] += 1
                    continue
                if ema_slope is not None and ema200 > 0:
                    slope_pct = ema_slope / ema200
                    if slope_pct < -TREND_EMA_SLOPE_THRESHOLD:
                        skip_reasons["ema_filter"] += 1
                        continue
            elif price_close < range_low:
                signal_side = "sell"
                signal_stop = range_mid
                if ema200 is not None and today_open >= ema200:
                    skip_reasons["ema_filter"] += 1
                    continue
                if ema_slope is not None and ema200 > 0:
                    slope_pct = ema_slope / ema200
                    if slope_pct > TREND_EMA_SLOPE_THRESHOLD:
                        skip_reasons["ema_filter"] += 1
                        continue
            else:
                skip_reasons["no_breakout"] += 1
                continue

            # 用中位线做止损时，R 距离减半
            rps = abs(price_close - signal_stop)
            if rps <= 0:
                continue

            vol_mult = 1.0
            if VOL_ADJUST_ENABLED and atr and price_close > 0:
                current_vol = atr / price_close
                if current_vol > 0:
                    vol_mult = min(VOL_TARGET / current_vol, 2.0)

            dollar_risk = equity * RISK_PER_TRADE * size_mult * vol_mult
            qty = int(dollar_risk / rps)

            max_qty_by_cash = int(equity / price_close)
            qty = min(qty, max_qty_by_cash)

            if qty <= 0:
                continue

            entry_price = price_close * (1 + BACKTEST_SLIPPAGE_ENTRY) if signal_side == "buy" else price_close * (1 - BACKTEST_SLIPPAGE_ENTRY)

            position = {
                "side": signal_side,
                "entry": entry_price,
                "initial_stop": signal_stop,
                "qty": qty,
                "initial_qty": qty,
                "risk_per_share": rps,
                "highest": entry_price,
                "lowest": entry_price,
                "tp1_done": False,
                "tp2_done": False,
                "entry_time": time_str,
            }

        if position is not None and position["qty"] > 0:
            last_bar = trade_bars.iloc[-1]
            exit_price = float(last_bar["close"])
            qty = position["qty"]
            rps = position["risk_per_share"]
            if position["side"] == "buy":
                exit_price *= (1 - BACKTEST_SLIPPAGE_EXIT)
                pnl = (exit_price - position["entry"]) * qty
                r_realized = (exit_price - position["entry"]) / rps
            else:
                exit_price *= (1 + BACKTEST_SLIPPAGE_EXIT)
                pnl = (position["entry"] - exit_price) * qty
                r_realized = (position["entry"] - exit_price) / rps
            trades.append({
                "date": date, "symbol": symbol, "side": position["side"],
                "entry_price": round(position["entry"], 2),
                "exit_price": round(exit_price, 2),
                "qty": qty,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / (position["entry"] * qty) * 100, 2),
                "exit_reason": "EOD",
                "r_multiple": round(r_realized, 2),
                "entry_time": position["entry_time"],
                "exit_time": last_bar["time"],
            })
            daily_pnl_r += r_realized

        day_pnl = sum(t["pnl"] for t in trades if t["date"] == date)
        equity += day_pnl
        equity_curve.append({"date": date, "equity": round(equity, 2)})

    print(f"  📊 [{symbol}] 跳过原因统计:")
    for reason, count in skip_reasons.items():
        if count > 0:
            print(f"     {reason}: {count} 天")

    return trades, equity_curve


# ══════════════════════════════════════════════
# 指标
# ══════════════════════════════════════════════
def compute_metrics(trades, equity_curve, capital):
    if not trades:
        return {}

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    total_return = total_pnl / capital * 100

    equity_series = pd.Series([e["equity"] for e in equity_curve])
    days = len(equity_curve)
    years = days / 252 if days > 0 else 1
    cagr = ((equity_series.iloc[-1] / capital) ** (1 / years) - 1) * 100 if years > 0 and equity_series.iloc[-1] > 0 else 0

    peak = equity_series.cummax()
    dd = (equity_series - peak) / peak * 100
    max_dd = dd.min()

    daily_returns = equity_series.pct_change().dropna()
    sharpe = 0
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe = daily_returns.mean() / daily_returns.std() * np.sqrt(252)

    win_rate = len(wins) / len(pnls) * 100 if pnls else 0
    gp = sum(wins) if wins else 0
    gl = abs(sum(losses)) if losses else 0
    pf = gp / gl if gl > 0 else 0

    max_win_streak = 0
    max_loss_streak = 0
    cw = 0
    cl = 0
    for p in pnls:
        if p > 0:
            cw += 1
            cl = 0
            max_win_streak = max(max_win_streak, cw)
        else:
            cl += 1
            cw = 0
            max_loss_streak = max(max_loss_streak, cl)

    r_multiples = [t.get("r_multiple", 0) for t in trades]
    r_dist = {
        "r_lt_neg1": sum(1 for r in r_multiples if r <= -1.0),
        "r_neg1_to_0": sum(1 for r in r_multiples if -1.0 < r < 0),
        "r_0_to_1": sum(1 for r in r_multiples if 0 <= r < 1.0),
        "r_1_to_2": sum(1 for r in r_multiples if 1.0 <= r < 2.0),
        "r_2_to_3": sum(1 for r in r_multiples if 2.0 <= r < 3.0),
        "r_3_plus": sum(1 for r in r_multiples if r >= 3.0),
    }

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
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "max_win": round(max(wins), 2) if wins else 0,
        "max_loss": round(min(losses), 2) if losses else 0,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "trading_days": days,
        "r_distribution": r_dist,
    }


# ══════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════
def main():
    api_key = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not api_secret:
        print("❌ 缺少 ALPACA_API_KEY / ALPACA_SECRET_KEY")
        sys.exit(1)

    print("=" * 60)
    print(f" ORB 回测 v3 | {BACKTEST_START} ~ {BACKTEST_END}")
    print(f" 标的: {SYMBOLS}")
    print(f" 初始资金: ${BACKTEST_CAPITAL:,}")
    print(f" 改动1: 初始止损 = 区间中位线")
    print(f" 改动2: 量能过滤 {BT_VOLUME_MULTIPLIER}×")
    print(f" 改动3: 交易时段截止 {BT_TRADE_CUTOFF_HOUR}:{BT_TRADE_CUTOFF_MIN:02d}")
    print("=" * 60)

    data_client = StockHistoricalDataClient(api_key, api_secret)

    all_trades = []
    per_symbol_stats = {}

    for idx, symbol in enumerate(SYMBOLS):
        print(f"\n━━━ {symbol} ━━━")
        debug = (idx == 0)
        minute_df = fetch_minute_bars(data_client, symbol, BACKTEST_START, BACKTEST_END)
        if minute_df is None or minute_df.empty:
            print(f" ⚠️ {symbol} 无分钟数据")
            per_symbol_stats[symbol] = {
                "trades": 0, "win_rate": 0, "total_pnl": 0,
                "avg_pnl": 0, "max_win": 0, "max_loss": 0, "profit_factor": 0,
            }
            continue

        daily_df = fetch_daily_bars(data_client, symbol, BACKTEST_START, BACKTEST_END)
        daily_ind = compute_daily_indicators(daily_df)
        print(f"  📊 分钟 {len(minute_df):,} 条 / 日线 {len(daily_df)} 条")

        trades, _ = run_backtest_for_symbol(symbol, minute_df, daily_ind, BACKTEST_CAPITAL, debug=debug)
        print(f"  ✅ {len(trades)} 笔交易")

        if trades:
            pnls = [t["pnl"] for t in trades]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            per_symbol_stats[symbol] = {
                "trades": len(trades),
                "win_rate": round(len(wins) / len(trades) * 100, 2),
                "total_pnl": round(sum(pnls), 2),
                "avg_pnl": round(sum(pnls) / len(trades), 2),
                "max_win": round(max(wins), 2) if wins else 0,
                "max_loss": round(min(losses), 2) if losses else 0,
                "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else 0,
            }
        else:
            per_symbol_stats[symbol] = {
                "trades": 0, "win_rate": 0, "total_pnl": 0,
                "avg_pnl": 0, "max_win": 0, "max_loss": 0, "profit_factor": 0,
            }

        all_trades.extend(trades)

    if not all_trades:
        print("\n❌ 无交易")
        result = {
            "backtest_info": {
                "start": BACKTEST_START, "end": BACKTEST_END,
                "capital": BACKTEST_CAPITAL, "symbols": SYMBOLS,
                "generated_at": datetime.now().isoformat(),
                "note": "无交易生成",
            },
            "metrics": {},
            "per_symbol": per_symbol_stats,
            "yearly": {}, "monthly": {}, "exit_reasons": {},
            "equity_curve": [], "trades": [],
        }
        with open(RESULT_FILE, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"⚠️ 已生成空结果文件 {RESULT_FILE}")
        return

    trades_by_date = {}
    for t in all_trades:
        trades_by_date.setdefault(t["date"], []).append(t["pnl"])

    all_dates = sorted(trades_by_date.keys())
    combined_equity = []
    equity = BACKTEST_CAPITAL
    for d in all_dates:
        equity += sum(trades_by_date[d])
        combined_equity.append({"date": d, "equity": round(equity, 2)})

    metrics = compute_metrics(all_trades, combined_equity, BACKTEST_CAPITAL)

    print("\n" + "=" * 60)
    print(f" 总交易: {metrics['total_trades']}")
    print(f" 总收益: ${metrics['total_pnl']:,.2f} ({metrics['total_return_pct']:.2f}%)")
    print(f" 年化: {metrics['cagr_pct']:.2f}%")
    print(f" 最大回撤: {metrics['max_drawdown_pct']:.2f}%")
    print(f" 夏普: {metrics['sharpe']:.2f}")
    print(f" 胜率: {metrics['win_rate']:.2f}%")
    print(f" 盈亏比: {metrics['profit_factor']:.2f}")
    print(f" 平均盈利: ${metrics['avg_win']}")
    print(f" 平均亏损: ${metrics['avg_loss']}")
    print(f" R 分布: {metrics['r_distribution']}")
    print("=" * 60)

    yearly_stats = {}
    for year in sorted(set(t["date"][:4] for t in all_trades)):
        yt = [t for t in all_trades if t["date"].startswith(year)]
        yp = [t["pnl"] for t in yt]
        yw = [p for p in yp if p > 0]
        yl = [p for p in yp if p <= 0]
        yearly_stats[year] = {
            "trades": len(yt),
            "win_rate": round(len(yw) / len(yt) * 100, 2) if yt else 0,
            "total_pnl": round(sum(yp), 2),
            "return_pct": round(sum(yp) / BACKTEST_CAPITAL * 100, 2),
            "max_win": round(max(yw), 2) if yw else 0,
            "max_loss": round(min(yl), 2) if yl else 0,
        }

    monthly_data = {}
    for t in all_trades:
        ym = t["date"][:7]
        monthly_data[ym] = monthly_data.get(ym, 0) + t["pnl"]

    exit_reasons = {}
    for t in all_trades:
        r = t["exit_reason"]
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    result = {
        "backtest_info": {
            "start": BACKTEST_START, "end": BACKTEST_END,
            "capital": BACKTEST_CAPITAL, "symbols": SYMBOLS,
            "slippage_entry": BACKTEST_SLIPPAGE_ENTRY,
            "slippage_exit": BACKTEST_SLIPPAGE_EXIT,
            "generated_at": datetime.now().isoformat(),
            "strategy_version": "v3_midpoint_stop",
            "config_snapshot": {
                "RISK_PER_TRADE": RISK_PER_TRADE,
                "MAX_TRADES_PER_DAY": MAX_TRADES_PER_DAY,
                "STRUCTURED_TRAIL": STRUCTURED_TRAIL,
                "TRAIL_STEP_R": TRAIL_STEP_R,
                "TP1_R": BT_SCALE_OUT_LEVELS[0]["r_multiple"],
                "TP2_R": BT_SCALE_OUT_LEVELS[1]["r_multiple"],
                "TIME_STOP_MINUTES": TIME_STOP_MINUTES_BT,
                "TIME_STOP_R_THRESHOLD": TIME_STOP_R_THRESHOLD,
                "DAILY_LOSS_LIMIT_R": DAILY_LOSS_LIMIT_R,
                "BT_VOLUME_MULTIPLIER": BT_VOLUME_MULTIPLIER,
                "BT_TRADE_CUTOFF": f"{BT_TRADE_CUTOFF_HOUR}:{BT_TRADE_CUTOFF_MIN:02d}",
                "BT_INITIAL_STOP_MODE": BT_INITIAL_STOP_MODE,
                "MIN_RANGE_PCT": MIN_RANGE_PCT,
                "MAX_RANGE_ATR_MULTIPLIER": MAX_RANGE_ATR_MULTIPLIER,
                "TREND_EMA_PERIOD": TREND_EMA_PERIOD,
                "GAP_SKIP_THRESHOLD": GAP_SKIP_THRESHOLD,
                "GAP_REDUCE_THRESHOLD": GAP_REDUCE_THRESHOLD,
            },
        },
        "metrics": metrics,
        "per_symbol": per_symbol_stats,
        "yearly": yearly_stats,
        "monthly": monthly_data,
        "exit_reasons": exit_reasons,
        "equity_curve": combined_equity,
        "trades": all_trades,
    }

    with open(RESULT_FILE, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 结果已保存到 {RESULT_FILE}")
    print(f"文件大小: {os.path.getsize(RESULT_FILE) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
