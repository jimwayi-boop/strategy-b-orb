# Strategy B - ORB (改进版)

## 策略逻辑
- 9:30–9:45 ET 形成开盘区间
- 突破区间高点做多，跌破低点做空
- Trailing Stop 0.3% 自动移动止损
- 15:45 ET 强制平仓，不留隔夜

## 改进项
- 仓位风险 5%
- 成交量基准改用开盘区间均量
- 趋势对齐过滤（5日SMA）
- 开盘缺口过滤
- 冷却机制（止损后等5根K线）
- Trailing Stop 替代手动止损
- 邮件通知

## Secrets
- ALPACA_API_KEY
- ALPACA_SECRET_KEY
- MAIL_USERNAME
- MAIL_PASSWORD
