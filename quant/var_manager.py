"""
VaR (Value at Risk) — 风险价值仓位控制
科学计算最大仓位，替代拍脑袋的固定比例

方法：
1. 历史模拟法 VaR：用过去N根K线的收益率分布
2. 参数法 VaR：假设正态分布, VaR = μ - z*σ
3. 计算 Kelly 最优仓位比例
"""

import numpy as np
from collections import deque
from scipy import stats

class VaRManager:
    def __init__(self, lookback=100, confidence=0.95):
        self.lookback = lookback
        self.confidence = confidence
        self.returns = deque(maxlen=lookback)
        self.z_score = stats.norm.ppf(confidence)  # 95% → 1.645
        
    def update(self, price):
        """喂入新价格，计算收益率"""
        self.returns.append(price)
        
    def historical_var(self, position_size=1.0):
        """历史模拟法 VaR: 在置信水平下的最大损失"""
        if len(self.returns) < 30:
            return position_size * 0.02  # 默认2%风险
        
        prices = np.array(self.returns)
        rets = np.diff(prices) / prices[:-1]
        if len(rets) < 20:
            return position_size * 0.02
        
        # 排序收益率，取最差的(1-confidence)分位
        sorted_rets = np.sort(rets)
        var_pct = abs(sorted_rets[int(len(sorted_rets) * (1 - self.confidence))])
        
        return position_size * var_pct
    
    def parametric_var(self, position_size=1.0):
        """参数法 VaR: VaR = -(μ - z*σ) * position"""
        if len(self.returns) < 30:
            return position_size * 0.02
        
        prices = np.array(self.returns)
        rets = np.diff(np.log(prices))
        if len(rets) < 20:
            return position_size * 0.02
        
        mu = np.mean(rets)
        sigma = np.std(rets)
        
        # VaR% = -(μ - z*σ)
        var_pct = -(mu - self.z_score * sigma)
        return position_size * max(var_pct, 0.005)  # 至少0.5%
    
    def kelly_fraction(self, win_rate=0.5, avg_win=1.0, avg_loss=1.0):
        """Kelly公式: f* = (p*b - q) / b
        f* = 最优仓位比例
        p = 胜率, q = 1-p
        b = 平均盈利/平均亏损 (盈亏比)
        """
        if avg_loss == 0:
            return 0.25  # 安全默认值
        b = avg_win / avg_loss
        q = 1 - win_rate
        kelly = (win_rate * b - q) / b
        # 半凯利: 更保守
        half_kelly = max(0, kelly * 0.5)
        return min(half_kelly, 0.25)  # 上限25%
    
    def max_position_size(self, balance, win_rate=0.5, avg_win=1.0, avg_loss=1.0):
        """综合VaR+Kelly计算最大仓位
        
        Returns: (recommended_margin, max_loss_estimate)
        """
        # 1. VaR估计: 下一根K线最大可能亏损
        var_hist = self.historical_var(balance)
        var_param = self.parametric_var(balance)
        var_est = max(var_hist, var_param)
        
        # 2. Kelly最优比例
        kelly_pct = self.kelly_fraction(win_rate, avg_win, avg_loss)
        
        # 3. 仓位上限 = min(VaR限制, Kelly限制, 余额)
        var_limit = balance * 0.02 / max(var_est / balance, 0.001)  # 每笔最多亏2%
        kelly_limit = balance * kelly_pct
        
        max_size = min(var_limit, kelly_limit, balance * 0.95)  # 硬上限95%
        max_size = max(max_size, balance * 0.05)  # 软下限5%
        
        return max_size, var_est
    
    def current_risk_pct(self):
        """当前持仓的风险百分比"""
        return self.historical_var(1.0)
