# Strategy B - ORB

美股开盘区间突破（Opening Range Breakout）自动交易策略。

## 策略逻辑

- 9:30–9:45 ET 形成开盘区间
- 价格突破区间高点 → 做多
- 价格跌破区间低点 → 做空
- 移动止损 0.3%，让利润奔跑
- 15:50 ET 强制平仓，不留隔夜

## 配置

在 GitHub 仓库 Settings → Secrets and variables → Actions 中设置：

- `ALPACA_API_KEY`：Alpaca Paper 账户的 API Key
- `ALPACA_SECRET_KEY`：Alpaca Paper 账户的 Secret Key

## 参数调整

所有参数在 `config.py` 中修改。

## 运行

GitHub Actions 每分钟自动运行一次。
也可在 Actions 页面手动触发（workflow_dispatch）。
