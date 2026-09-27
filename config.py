# config.py
import os

# ── Alpaca API ──
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = True

# ── 运行模式 ──
SHADOW_MODE = False

# ── ORB 策略参数 ──
ORB_START_HOUR = 9
ORB_START_MIN = 30
ORB_END_HOUR = 9
ORB_END_MIN = 45
EOD_CLOSE_HOUR = 15
EOD_CLOSE_MIN = 43

# ── 日内交易时段限制 ──
TRADE_CUTOFF_HOUR = 11
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

# ── 趋势过滤 ──
TREND_EMA_PERIOD = 200
TREND_EMA_SLOPE_LOOKBACK = 5
TREND_EMA_SLOPE_THRESHOLD = 0.001

# ── 多级止盈 ──
SCALE_OUT_ENABLED = True
SCALE_OUT_LEVELS = [
    {"r_multiple": 1.0, "exit_pct": 0.33},
    {"r_multiple": 2.0, "exit_pct": 0.33},
]
SCALE_OUT_TRAIL_AFTER_TP1 = 0.15

# ── 自动保本 ──
AUTO_BE_ENABLED = True
AUTO_BE_TRIGGER_R = 1.0
AUTO_BE_TRAIL_PERCENT = 0.1

# ── 时间止损 ──
TIME_STOP_MINUTES = 60

# ── 名义仓位上限 ──
MAX_NOTIONAL_PCT = 1.0

# ── 组合层风险限制 ──
MAX_TOTAL_RISK_PCT = 0.10
MAX_CONCURRENT_POSITIONS = 3
MAX_SAME_DIRECTION_RISK = 0.08

# ── 连续亏损熔断 ──
MAX_CONSECUTIVE_LOSSES = 5
CONSECUTIVE_LOSS_PAUSE_DAYS = 1

# ── 波动率调整仓位 ──
VOL_ADJUST_ENABLED = True
VOL_TARGET = 0.02

# ── VIX 波动率过滤 ──
VIX_FILTER_ENABLED = True
VIX_HIGH_THRESHOLD = 25.0
VIX_LOW_THRESHOLD = 15.0
VIX_API_URL = "https://convextrade.com/api/public/metrics/vixcls"

# ── 市场状态分类（QQQ EMA200）──
MARKET_REGIME_ENABLED = True
MARKET_REGIME_SYMBOL = "QQQ"
MARKET_REGIME_EMA_PERIOD = 200
MARKET_REGIME_BEARISH_SIZE_MULT = 0.5

# ── 交易成本建模（回测用）──
SLIPPAGE_MARKET_ENTRY = 0.0005
SLIPPAGE_TRAIL_STOP = 0.001

# ── API 重试 ──
API_MAX_RETRIES = 3
API_RETRY_BASE_DELAY = 1.0

# ── 限价单入场 ──
LIMIT_ENTRY_ENABLED = True
LIMIT_ENTRY_OFFSET = 0.001

# ── 缺口过滤 ──
GAP_SKIP_THRESHOLD = 0.015
GAP_REDUCE_THRESHOLD = 0.005

# ── 交易标的 ──
SYMBOLS = ["TSLA", "NVDA", "META", "AMD"]

# ── 面板文件 ──
DASHBOARD_DATA_FILE = "dashboard_data.json"
EQUITY_HISTORY_FILE = "equity_history.json"
SHADOW_TRADES_FILE = "shadow_trades.jsonl"
EQUITY_HISTORY_DAYS = 5  # 保留最近 5 个交易日
