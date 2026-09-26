# config.py
import os

# ── Alpaca API（从 GitHub Secrets 注入）──
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = True

# ── ORB 策略参数 ──
ORB_START_HOUR = 9
ORB_START_MIN = 30
ORB_END_HOUR = 9
ORB_END_MIN = 45
EOD_CLOSE_HOUR = 15
EOD_CLOSE_MIN = 43  # 15:43 触发，给 15:45 平仓留缓冲

RISK_PER_TRADE = 0.05  # 单笔风险 5%
MAX_TRADES_PER_DAY = 2  # 每日每标的最多 2 笔
COOLDOWN_BARS = 5  # 止损后冷却 5 根 1 分钟 K 线
TRAIL_PERCENT = 0.3  # Trailing Stop 回撤 0.3% 触发
VOLUME_MULTIPLIER = 1.5  # 突破量能 > 1.5×开盘区间均量
MIN_RANGE_PCT = 0.002  # 区间宽度至少占股价 0.2%

# ── 开盘区间过宽过滤 ──
MAX_RANGE_ATR_MULTIPLIER = 2.0  # 区间宽度 > 2 * ATR(14) 跳过
ATR_LOOKBACK_DAYS = 14

# ── 趋势对齐过滤（改用 EMA）──
TREND_EMA_PERIOD = 200  # 日线 EMA 200

# ── 自动保本 ──
AUTO_BE_ENABLED = True
AUTO_BE_TRIGGER_R = 1.0  # 浮盈达到 1R 时触发
AUTO_BE_TRAIL_PERCENT = 0.1  # 收紧后的 Trailing Stop 回撤百分比

# ── 时间止损 ──
TIME_STOP_MINUTES = 60  # 入场后 60 分钟未达到 1R 则平仓

# ── 名义仓位上限 ──
MAX_NOTIONAL_PCT = 1.0  # 名义仓位不超过可用现金的 100%

# ── API 重试 ──
API_MAX_RETRIES = 3
API_RETRY_BASE_DELAY = 1.0

# ── 开盘缺口过滤 ──
GAP_SKIP_THRESHOLD = 0.015  # 缺口 > 1.5% 跳过
GAP_REDUCE_THRESHOLD = 0.005  # 缺口 0.5%~1.5% 减半仓位

# ── 交易标的 ──
SYMBOLS = ["TSLA", "NVDA", "META", "AMD"]
