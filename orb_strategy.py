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
        self.vix_value = None
        self.market_regime = "bullish"
        # 决策日志
        self.decision_steps = []
        self.decision_action = "pending"
        self.decision_reason = ""

    def log_step(self, passed, label, detail=""):
        icon = "✔" if passed else ("⚠" if detail else "🚫")
        self.decision_steps.append({
            "passed": passed,
            "icon": icon,
            "label": label,
            "detail": detail,
        })

    def set_opening_range(self, bars_df):
        if bars_df is None or bars_df.empty:
            self.log_step(False, "开盘区间", "无数据")
            return False
        self.range_high = float(bars_df["high"].max())
        self.range_low = float(bars_df["low"].min())
        range_width = self.range_high - self.range_low

        if self.range_low <= 0:
            self.log_step(False, "开盘区间", "价格异常")
            self.range_ready = False
            return False

        pct = range_width / self.range_low
        if pct < MIN_RANGE_PCT:
            self.log_step(False, f"区间宽度 {pct*100:.2f}% < {MIN_RANGE_PCT*100:.2f}%", "过窄")
            self.range_ready = False
            return False

        self.range_avg_vol = float(bars_df["volume"].mean())
        self.range_ready = True
        self.log_step(True, f"区间宽度 {pct*100:.2f}% ≥ {MIN_RANGE_PCT*100:.2f}%",
                      f"${self.range_low:.2f} – ${self.range_high:.2f}")
        return True

    def check_atr_filter(self):
        if self.atr is None:
            self.log_step(True, "ATR 过滤", "数据不足，跳过")
            return True
        rw = self.range_high - self.range_low
        limit = MAX_RANGE_ATR_MULTIPLIER * self.atr
        if rw > limit:
            self.log_step(False, f"区间 ${rw:.2f} > 2×ATR ${limit:.2f}", "过宽")
            return False
        self.log_step(True, f"区间 ${rw:.2f} ≤ 2×ATR ${limit:.2f}")
        return True

    def check_gap(self):
        if self.prev_close is None or self.today_open is None:
            self.log_step(True, "缺口过滤", "数据不足，跳过")
            return False, 1.0
        gap_pct = abs(self.today_open - self.prev_close) / self.prev_close
        if gap_pct > GAP_SKIP_THRESHOLD:
            self.log_step(False, f"缺口 {gap_pct*100:.2f}% > {GAP_SKIP_THRESHOLD*100:.1f}%", "跳过")
            return True, 1.0
        elif gap_pct > GAP_REDUCE_THRESHOLD:
            self.log_step(True, f"缺口 {gap_pct*100:.2f}%", "仓位减半")
            return False, 0.5
        self.log_step(True, f"缺口 {gap_pct*100:.2f}% < {GAP_REDUCE_THRESHOLD*100:.1f}%")
        return False, 1.0

    def check_trend(self, direction):
        if self.daily_ema is None:
            self.log_step(True, "EMA200 对齐", "数据不足，跳过")
            return True
        if self.today_open is None:
            self.log_step(True, "EMA200 对齐", "开盘价缺失，跳过")
            return True

        if direction == "buy" and self.today_open <= self.daily_ema:
            self.log_step(False, f"buy 与 EMA200 ${self.daily_ema:.2f} 不对齐")
            return False
        if direction == "sell" and self.today_open >= self.daily_ema:
            self.log_step(False, f"sell 与 EMA200 ${self.daily_ema:.2f} 不对齐")
            return False

        if self.daily_ema_slope is not None:
            slope_pct = self.daily_ema_slope / self.daily_ema
            if direction == "buy" and slope_pct < -TREND_EMA_SLOPE_THRESHOLD:
                self.log_step(False, f"EMA 斜率 {slope_pct*100:.3f}% 向下")
                return False
            if direction == "sell" and slope_pct > TREND_EMA_SLOPE_THRESHOLD:
                self.log_step(False, f"EMA 斜率 {slope_pct*100:.3f}% 向上")
                return False

        slope_str = f"斜率 {self.daily_ema_slope/self.daily_ema*100:.3f}%" if self.daily_ema_slope else ""
        self.log_step(True, f"EMA200 ${self.daily_ema:.2f} 对齐", slope_str)
        return True

    def check_vix(self, direction):
        if not VIX_FILTER_ENABLED or self.vix_value is None:
            self.log_step(True, "VIX 过滤", "数据不足，跳过")
            return True
        if self.vix_value > VIX_HIGH_THRESHOLD and direction == "buy":
            self.log_step(False, f"VIX {self.vix_value:.1f} > {VIX_HIGH_THRESHOLD}，只做空")
            return False
        if self.vix_value < VIX_LOW_THRESHOLD and direction == "sell":
            self.log_step(False, f"VIX {self.vix_value:.1f} < {VIX_LOW_THRESHOLD}，只做多")
            return False
        self.log_step(True, f"VIX {self.vix_value:.2f}", "中性区间")
        return True

    def check_cooldown(self, current_minute_index):
        if self.cooldown_until is None:
            return True
        if current_minute_index >= self.cooldown_until:
            self.cooldown_until = None
            return True
        return False

    def check_entry(self, latest_bar, current_minute_index):
        if (not self.range_ready) or self.traded_today:
            return None
        if self.trade_count >= MAX_TRADES_PER_DAY:
            return None
        if not self.check_cooldown(current_minute_index):
            self.log_step(False, "冷却中", "等待冷却结束")
            return None

        cutoff_min = TRADE_CUTOFF_HOUR * 60 + TRADE_CUTOFF_MIN
        if current_minute_index >= cutoff_min:
            self.log_step(False, f"已过交易截止时间 {TRADE_CUTOFF_HOUR}:{TRADE_CUTOFF_MIN:02d}")
            return None

        close = float(latest_bar["close"])
        volume = float(latest_bar["volume"])

        if self.range_avg_vol > 0 and volume < VOLUME_MULTIPLIER * self.range_avg_vol:
            self.log_step(False, f"突破量 {volume:.0f} < {VOLUME_MULTIPLIER}×均量 {self.range_avg_vol*VOLUME_MULTIPLIER:.0f}")
            return None

        if close > self.range_high:
            self.log_step(True, f"突破量 {volume:.0f} ≥ {VOLUME_MULTIPLIER}×均量 {self.range_avg_vol*VOLUME_MULTIPLIER:.0f}")
            if not self.check_trend("buy"):
                return None
            if not self.check_vix("buy"):
                return None
            self.log_step(True, f"突破区间高点 ${self.range_high:.2f}", f"入场 ${close:.2f}")
            return {"side": "buy", "entry": close, "stop": self.range_low}

        if close < self.range_low:
            self.log_step(True, f"突破量 {volume:.0f} ≥ {VOLUME_MULTIPLIER}×均量 {self.range_avg_vol*VOLUME_MULTIPLIER:.0f}")
            if not self.check_trend("sell"):
                return None
            if not self.check_vix("sell"):
                return None
            self.log_step(True, f"突破区间低点 ${self.range_low:.2f}", f"入场 ${close:.2f}")
            return {"side": "sell", "entry": close, "stop": self.range_high}
        return None

    def calc_qty(self, available_cash, entry, stop, size_multiplier=1.0, atr=None):
        risk_per_share = abs(entry - stop)
        if risk_per_share <= 0:
            return 0

        vol_mult = 1.0
        if VOL_ADJUST_ENABLED and atr is not None and entry > 0:
            current_vol = atr / entry
            if current_vol > 0:
                vol_mult = min(VOL_TARGET / current_vol, 2.0)

        regime_mult = MARKET_REGIME_BEARISH_SIZE_MULT if self.market_regime == "bearish" else 1.0

        dollar_risk = available_cash * RISK_PER_TRADE * size_multiplier * vol_mult * regime_mult
        qty_by_risk = int(dollar_risk / risk_per_share)
        qty_by_notional = int((available_cash * MAX_NOTIONAL_PCT) / entry)

        qty = min(qty_by_risk, qty_by_notional)
        return max(qty, 1)

    def on_stop_loss_hit(self, current_minute_index):
        self.trade_count += 1
        self.cooldown_until = current_minute_index + COOLDOWN_BARS
        self.position = None
        self.traded_today = (self.trade_count >= MAX_TRADES_PER_DAY)

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
        self.decision_steps = []
        self.decision_action = "pending"
        self.decision_reason = ""
