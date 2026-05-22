# 🦅 PaperBot v27.5 — 币安测试网 20x 交易机器人

6 路技术信号 + DeepSeek AI 决策 + 20 倍杠杆

## 标的
BTC/ETH/DOGE 主流合约

## 参数
| 参数 | 值 |
|------|-----|
| TP | 4% |
| SL | 0.8% |
| 杠杆 | 20x |
| COOLDOWN | 300s |
| 仓位 | BTC=50% ETH=50% DOGE=40% |
| 信号门槛 | MIN_SCORE=45 |

## 风控
- 三重熔断：日亏/连亏冷却/方向熔断
- 趋势过滤：EMA50/EMA200
- 追踪止盈：激活$1.50，分档回吐0.8-3.0
- 崩溃自愈 + 属性自动修复
- Watchdog 三重检测守护

## 启动
```bash
nohup python3 bots/trader.py &
nohup bash bots/watchdog_v3.sh &
```
