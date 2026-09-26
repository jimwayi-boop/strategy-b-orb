# orb_strategy.py
import pandas as pd
import numpy as np
from config import *

class ORBStrategy:
    def __init__(self, symbol):
        self.symbol = symbol
        self.range_high = None
        self.range_low = None
        self.range_avg_vol = 0
        self.range_ready = False
        self.traded_today = False
        self.trade_count = 0
        self.cooldown_until = None
        self.position = None
        self.daily_ema = None
        self.daily_ema_slope = None
        self.prev_close = None
        self.today_open = None
        self.atr = None
        self.initial_stop = None
        self.risk_per_share = None

    def set_opening_range(self, bars_df):
        if bars_df is None or bars_df.empty:
            return False
        self.range_high = float(bars_df["high"].max())
        self.range_low = float(bars_df["low"].min())
        range_width = self.range_high - self.range_low

        if self.range_low <= 0:
            self.range_ready = False
            return False

        if range_width / self.range_low < MIN_RANGE_PCT:
            print(f"[{self.symbol}] 区间过窄 ({range_width:.2f})，跳过今日")
            self.range_ready = False
            return False

        self.range_avg_vol = float(bars_df["volume"].mean())
        self.range_ready = True
        print(f"[{self.symbol}] ORB 区间锁定: "
              f"{self.range_low:.2f} – {self.range_high:.2f} "
              f"宽度={range_width:.2f} 区间均量={self.range_avg_vol:.0f}")
        return True

    def check_gap(self):
        if self.prev_close is None or self.today_open is None:
            return False, 1.0
        gap_pct = abs(self.today_open - self.prev_close) / self.prev_close
        print(f" 开盘缺口: {gap_pct*100:.2f}%")
        if gap_pct > GAP_SKIP_THRESHOLD:
            print(f"⛔缺口 > {GAP_SKIP_THRESHOLD*100:.1f}%，跳过今日")
            return True, 1.0
        elif gap_pct > GAP_REDUCE_THRESHOLD:
            print(f"⚠️缺口 {gap_pct*100:.2f}%，仓位减半")
            return False, 0.5
        return False, 1.0

    def check_trend(self, direction):
        if self.daily_ema is None:
            return True
        if self.today_open is None:
            print(" 今日开盘价缺失，跳过趋势过滤")
            return True

        # 基础方向检查
        if direction == "buy" and self.today_open <= self.daily_ema:
            print(f" 🚫 方向 buy 与EMA{TREND_EMA_PERIOD}={self.daily_ema:.2f}不对齐，跳过")
            return False
        if direction == "sell" and self.today_open >= self.daily_ema:
            print(f" 🚫 方向 sell 与EMA{TREND_EMA_PERIOD}={self.daily_ema:.2f}不对齐，跳过")
            return False

        # EMA斜率检查
        if self.daily_ema_slope is not None:
            slope_pct = self.daily_ema_slope / self.daily_ema
            if direction == "buy" and slope_pct < -TREND_EMA_SLOPE_THRESHOLD:
                print(f" 🚫 EMA斜率向下 ({slope_pct*100:.3f}%)，跳过做多")
                return False
            if direction == "sell" and slope_pct > TREND_EMA_SLOPE_THRESHOLD:
                print(f" 🚫 EMA斜率向上 ({slope_pct*100:.3f}%)，跳过做空")
                return False

        return True

    def check_cooldown(self, current_minute_index):
        if self.cooldown_until is None:
            return True
        if current_minute_index >= self.cooldown_until:
            self.cooldown_until = None
            return True
        print(f" ⏸️冷却中，还需等待 {self.cooldown_until - current_minute_index} 根K线")
        return False

    def check_entry(self, latest_bar, current_minute_index):
        if (not self.range_ready) or self.traded_today:
            return None
        if self.trade_count >= MAX_TRADES_PER_DAY:
            return None
        if not self.check_cooldown(current_minute_index):
            return None

        # 日内交易时段限制
        now_h = current_minute_index // 60
        now_m = current_minute_index % 60
        cutoff_min = TRADE_CUTOFF_HOUR * 60 + TRADE_CUTOFF_MIN
        if current_minute_index >= cutoff_min:
            print(f" ⏰ 已过交易截止时间 {TRADE_CUTOFF_HOUR}:{TRADE_CUTOFF_MIN:02d}，不再开新仓")
            return None

        close = float(latest_bar["close"])
        volume = float(latest_bar["volume"])

        if self.range_avg_vol > 0 and volume < VOLUME_MULTIPLIER * self.range_avg_vol:
            return None

        if close > self.range_high:
            if not self.check_trend("buy"):
                return None
            return {
                "side": "buy",
                "entry": close,
                "stop": self.range_low,
            }

        if close < self.range_low:
            if not self.check_trend("sell"):
                return None
            return {
                "side": "sell",
                "entry": close,
                "stop": self.range_high,
            }
        return None

    def calc_qty(self, available_cash, entry, stop, size_multiplier=1.0, atr=None):
        risk_per_share = abs(entry - stop)
        if risk_per_share <= 0:
            return 0

        # 波动率调整
        vol_mult = 1.0
        if VOL_ADJUST_ENABLED and atr is not None and entry > 0:
            current_vol = atr / entry
            if current_vol > 0:
                vol_mult = min(VOL_TARGET / current_vol, 2.0)  # 上限2倍

        dollar_risk = available_cash * RISK_PER_TRADE * size_multiplier * vol_mult
        qty_by_risk = int(dollar_risk / risk_per_share)
        qty_by_notional = int((available_cash * MAX_NOTIONAL_PCT) / entry)

        qty = min(qty_by_risk, qty_by_notional)
        return max(qty, 1)

    def on_stop_loss_hit(self, current_minute_index):
        self.trade_count += 1
        self.cooldown_until = current_minute_index + COOLDOWN_BARS
        self.position = None
        self.traded_today = (self.trade_count >= MAX_TRADES_PER_DAY)
        print(f" 🛑 止损触发，冷却{COOLDOWN_BARS}根K线，"
              f"今日已交易{self.trade_count}/{MAX_TRADES_PER_DAY}笔")

    def reset_daily(self):
        self.range_high = None
        self.range_low = None
        self.range_avg_vol = 0
        self.range_ready = False
        self.traded_today = False
        self.trade_count = 0
        self.cooldown_until = None
        self.position = None
        self.initial_stop = None
        self.risk_per_share = None
