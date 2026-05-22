#!/usr/bin/env python3
"""DeepSeek AI 交易信号审查引擎"""
import sys, json

data = json.loads(sys.stdin.read())

sig = data.get('type', '?')
sc = data.get('sc', 0)
bal = data.get('balance', 0)
wr = data.get('recent_wr', 50)
consec = data.get('consec_losses', 0)
pos_n = data.get('positions', 0)
dir_melt_long = data.get('dir_melt_long', False)
dir_melt_short = data.get('dir_melt_short', False)

# 拒绝条件
reject = False
reasons = []

if consec >= 2 and wr < 40:
    reject = True
    reasons.append(f"连亏{consec}笔+胜率{wr}%")

if pos_n >= 3 and sc < 60:
    reject = True
    reasons.append(f"仓位已满{pos_n}笔+信号弱sc={sc}")

# 半仓条件
reduce_size = False
if sc < 55:
    reduce_size = True

if reject:
    print(f"REJECT: {', '.join(reasons)}")
elif reduce_size:
    print(f"APPROVE: REDUCE_HALF")
else:
    print("APPROVE")
