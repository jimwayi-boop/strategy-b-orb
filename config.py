# config.py
import os

# ── Alpaca API（从 GitHub Secrets 注入）──
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = True

# ── ORB 策略参数 ──
ORB_START_HOUR     = 9
ORB_START_MIN      = 30
ORB_END_HOUR       = 9
ORB_END_MIN        = 45
EOD_CLOSE_HOUR     = 15
EOD_CLOSE_MIN      = 43      # 15:43触发，给15:45平仓留缓冲

RISK_PER_TRADE     = 0.05    # 单笔风险 5%
MAX_TRADES_PER_DAY = 2       # 每日每标的最多2笔
COOLDOWN_BARS      = 5       # 止损后冷却5根1分钟K线
TRAIL_PERCENT      = 0.3     # Trailing Stop回撤0.3%触发
VOLUME_MULTIPLIER  = 1.5     # 突破量能 > 1.5×开盘区间均量
MIN_RANGE_PCT      = 0.002   # 区间宽度至少占股价0.2%

# ── 趋势对齐过滤 ──
TREND_LOOKBACK_DAYS = 5      # 用最近5日收盘价计算SMA

# ── 开盘缺口过滤 ──
GAP_SKIP_THRESHOLD   = 0.015  # 缺口 > 1.5% 跳过
GAP_REDUCE_THRESHOLD = 0.005  # 缺口 0.5%~1.5% 减半仓位

# ── 交易标的 ──
SYMBOLS = ["TSLA", "NVDA", "META", "AMD"]
