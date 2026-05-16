# 🦅 PaperBot v27 — 量化加密货币交易机器人

20x 杠杆 · 6路信号 · 满仓单吊 · 顺势过滤 · HMM 隐马尔科夫 · VaR 风控

## 策略体系

| 模块 | 算法 | 作用 |
|------|------|------|
| 信号引擎 | RSI背离 + 超买超卖 + BB反弹 + 多TF均值回归 | 6路信号生成 |
| 趋势过滤 | 周线MACD + 4h EMA | 顺势而为，周跌只做空 |
| HMM 隐马尔科夫 | 3状态高斯HMM | 识别涨/跌/横盘，顺势加分 |
| 波动率模型 | EWMA + Parkinson | 高波降仓，低波加仓 |
| VaR 风险价值 | 历史模拟+参数法 | 每笔最大亏损控制 |
| 协整检验 | Engle-Granger + ADF | BTC-ETH 价差套利信号 |
| 追迹止盈 | 5档动态回撤 | 利润越大保护越宽 |
| 自适应止损 | ATR 动态 + 信号分级 | 强信号宽止损，弱信号快砍 |

## 快速开始

```bash
pip install ccxt hmmlearn scikit-learn scipy arch numpy

export BINANCE_KEY="your_binance_api_key"
export BINANCE_SECRET="your_binance_secret"

python3 trader.py
```

## 配置

编辑 `trader.py` 顶部参数：

```python
LEVERAGE = 20         # 固定杠杆
MIN_SCORE = 50        # 最低信号分
START_BALANCE = 40.0  # 初始余额
```

## 文件结构

```
bots/
├ trader.py           # 主策略
│
quant/
├ hmm_regime.py       # HMM 隐马尔科夫模型
├ volatility.py       # EWMA 波动率模型
├ var_manager.py      # VaR 风控
└ cointegration.py    # 协整检验
```

## 风险声明

⚠️ 本策略仅供研究学习。加密货币交易风险极高，20x 杠杆可能导致全部本金亏损。请勿投入无法承受损失的资金。使用前务必在模拟盘充分验证。

## License

MIT
