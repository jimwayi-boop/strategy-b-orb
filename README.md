# Strategy B - ORB (改进版)

美股开盘区间突破（Opening Range Breakout）自动交易策略。

## 策略逻辑

- 9:30–9:45 ET 形成开盘区间
- 突破区间高点做多，跌破低点做空
- Trailing Stop 0.3% 自动移动止损
- 15:43 ET 触发强制平仓，15:45 ET 前完成

## 改进项

- 单笔风险 5% 可用现金
- 成交量基准改用开盘区间均量（>1.5×）
- 趋势对齐过滤（EMA200 + 斜率）
- 开盘缺口过滤（>1.5% 跳过，0.5%–1.5% 仓位减半）
- 冷却机制（止损后等 5 根 K 线）
- 多级止盈（1R / 2R 各平 1/3）
- 自动保本（浮盈达 1R 收紧 Trailing Stop）
- 时间止损（60 分钟未达 1R 平仓）
- 组合层风险限制（总风险 ≤ 10%，最多 3 个持仓）
- VIX 波动率过滤
- 市场状态分类（QQQ EMA200，看空时仓位减半）
- 波动率调整仓位
- 外部 Cron 触发（cron-job.org）
- 邮件通知
- 交易日面板（GitHub Pages）

## Secrets

- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `MAIL_USERNAME`
- `MAIL_PASSWORD`

## 参数调整

所有参数在 `config.py` 中修改。

## 运行

GitHub Actions 通过 cron-job.org 外部触发。

## 面板

https://jimwayi-boop.github.io/strategy-b-orb/

以上仅为技术实现说明，不构成投资建议。
