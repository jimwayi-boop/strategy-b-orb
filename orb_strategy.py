# orb_strategy.py
import pandas as pd
import numpy as np
from config import *


class ORBStrategy:
    """
    Opening Range Breakout 策略
    高回报模式：裸突破 + 移动止损，让利润奔跑
    """

    def __init__(self, symbol):
        self.symbol          = symbol
        self.range_high      = None
        self.range_low       = None
        self.range_ready     = False
        self.traded_today    = False
        self.position        = None
        self.trailing_active = False

    # ── 1. 计算开盘区间（9:30–9:45 ET）──
    def set_opening_range(self, bars_df):
        if bars_df is None or bars_df.empty:
            return False

        self.range_high = float(bars_df["high"].max())
        self.range_low  = float(bars_df["low"].min())
        range_width     = self.range_high - self.range_low

        if self.range_low <= 0:
            self.range_ready = False
            return False

        if range_width / self.range_low < MIN_RANGE_PCT:
            print(f"[{self.symbol}] 区间过窄 ({range_width:.2f})，跳过今日")
            self.range_ready = False
            return False

        self.range_ready = True
        print(f"[{self.symbol}] ORB 区间锁定: "
              f"{self.range_low:.2f} – {self.range_high:.2f}  "
              f"宽度={range_width:.2f}")
        return True

    # ── 2. 检查入场信号 ──
    def check_entry(self, latest_bar, avg_volume):
        if (not self.range_ready) or self.traded_today:
            return None

        close  = float(latest_bar["close"])
        volume = float(latest_bar["volume"])

        if avg_volume > 0 and volume < VOLUME_MULTIPLIER * avg_volume:
            return None

        # 向上突破 → 做多
        if close > self.range_high:
            return {
                "side":  "buy",
                "entry": close,
                "stop":  self.range_low,
            }

        # 向下突破 → 做空
        if close < self.range_low:
            return {
                "side":  "sell",
                "entry": close,
                "stop":  self.range_high,
            }

        return None

    # ── 3. 仓位计算 ──
    def calc_qty(self, equity, entry, stop):
        risk_per_share = abs(entry - stop)
        if risk_per_share <= 0:
            return 0
        dollar_risk = equity * RISK_PER_TRADE
        qty = int(dollar_risk / risk_per_share)
        return max(qty, 1)

    # ── 4. 更新移动止损 ──
    def update_trailing_stop(self, current_price):
        if self.position is None:
            return None

        side = self.position["side"]

        if side == "buy":
            new_stop = current_price * (1 - TRAILING_STOP_PCT)
            if new_stop > self.position["stop"]:
                self.position["stop"] = round(new_stop, 2)
                self.trailing_active = True
        else:
            new_stop = current_price * (1 + TRAILING_STOP_PCT)
            if new_stop < self.position["stop"]:
                self.position["stop"] = round(new_stop, 2)
                self.trailing_active = True

        return self.position["stop"]

    # ── 5. 检查是否触发出场 ──
    def check_exit(self, current_price):
        if self.position is None:
            return None

        side = self.position["side"]
        stop = self.position["stop"]

        if side == "buy" and current_price <= stop:
            return "stop_loss"
        if side == "sell" and current_price >= stop:
            return "stop_loss"

        return None

    def reset(self):
        self.range_high      = None
        self.range_low       = None
        self.range_ready     = False
        self.traded_today    = False
        self.position        = None
        self.trailing_active = False