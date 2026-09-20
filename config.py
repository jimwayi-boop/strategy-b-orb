# config.py
import os

# ── Alpaca API（从 GitHub Secrets 注入）──
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = True   # Paper 账户固定 True；实盘改为 False

# ── ORB 策略参数 ──
ORB_START_HOUR     = 9      # 开盘区间开始 9:30 ET
ORB_START_MIN      = 30
ORB_END_HOUR       = 9      # 开盘区间结束 9:45 ET
ORB_END_MIN        = 45
EOD_CLOSE_HOUR     = 15     # 强制平仓 15:50 ET
EOD_CLOSE_MIN      = 50

RISK_PER_TRADE     = 0.01   # 单笔风险 1% 净值
MAX_TRADES_PER_DAY = 1      # 每日每标的最多 1 笔
TRAILING_STOP_PCT  = 0.003  # 移动止损 0.3%
VOLUME_MULTIPLIER  = 1.3    # 突破 bar 量能 > 1.3×均量
MIN_RANGE_PCT      = 0.002  # 区间宽度至少占股价 0.2%

# ── 交易标的 ──
SYMBOLS = ["TSLA", "NVDA", "META", "AMD"]