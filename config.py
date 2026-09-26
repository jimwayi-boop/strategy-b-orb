# config.py
import os

# ── Alpaca API（从 GitHub Secrets 注入）──
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = True

# ── 运行模式 ──
SHADOW_MODE = False  # 影子模式：True=只记录信号，不提交订单

# ── ORB 策略参数 ──
ORB_START_HOUR = 9
ORB_START_MIN = 30
ORB_END_HOUR = 9
ORB_END_MIN = 45
EOD_CLOSE_HOUR = 15
EOD_CLOSE_MIN = 43

# ── 日内交易时段限制 ──
TRADE_CUTOFF_HOUR = 11   # 11:30 ET 之后不再开新仓
TRADE_CUTOFF_MIN = 30

RISK_PER_TRADE = 0.05
MAX_TRADES_PER_DAY = 2
COOLDOWN_BARS = 5
TRAIL_PERCENT = 0.3
VOLUME_MULTIPLIER = 1.5
MIN_RANGE_PCT = 0.002

# ── 开盘区间过宽过滤 ──
MAX_RANGE_ATR_MULTIPLIER = 2.0
ATR_LOOKBACK_DAYS = 14

# ── 趋势过滤（EMA + 斜率）──
TREND_EMA_PERIOD = 200
TREND_EMA_SLOPE_LOOKBACK = 5   # EMA斜率回看天数
TREND_EMA_SLOPE_THRESHOLD = 0.001  # EMA斜率阈值（0.1%）

# ── 多级止盈 ──
SCALE_OUT_ENABLED = True
SCALE_OUT_LEVELS = [
    {"r_multiple": 1.0, "exit_pct": 0.33},
    {"r_multiple": 2.0, "exit_pct": 0.33},
]
SCALE_OUT_TRAIL_AFTER_TP1 = 0.15  # TP1后收紧Trailing Stop至0.15%

# ── 自动保本 ──
AUTO_BE_ENABLED = True
AUTO_BE_TRIGGER_R = 1.0
AUTO_BE_TRAIL_PERCENT = 0.1

# ── 时间止损 ──
TIME_STOP_MINUTES = 60

# ── 名义仓位上限 ──
MAX_NOTIONAL_PCT = 1.0

# ── 组合层风险限制 ──
MAX_TOTAL_RISK_PCT = 0.10      # 单日总风险不超过10%
MAX_CONCURRENT_POSITIONS = 3   # 同时持仓不超过3个标的
MAX_SAME_DIRECTION_RISK = 0.08 # 同一方向总风险不超过8%

# ── 连续亏损熔断 ──
MAX_CONSECUTIVE_LOSSES = 5
CONSECUTIVE_LOSS_PAUSE_DAYS = 1  # 暂停1个交易日

# ── 波动率调整仓位 ──
VOL_ADJUST_ENABLED = True
VOL_TARGET = 0.02  # 目标日波动率2%

# ── API 重试 ──
API_MAX_RETRIES = 3
API_RETRY_BASE_DELAY = 1.0

# ── 限价单入场 ──
LIMIT_ENTRY_ENABLED = True
LIMIT_ENTRY_OFFSET = 0.001  # 限价偏移0.1%

# ── 缺口过滤 ──
GAP_SKIP_THRESHOLD = 0.015
GAP_REDUCE_THRESHOLD = 0.005

# ── 交易标的 ──
SYMBOLS = ["TSLA", "NVDA", "META", "AMD"]

# ── Activity SSE ──
USE_ACTIVITY_SSE = True
SSE_RECONNECT_INTERVAL = 5
