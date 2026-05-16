"""
Volatility Model — 波动率建模与预测
1. EWMA (指数加权移动平均) 波动率
2. Parkinson 高低价波动率 (更精确)
3. 波动率锥 (百分位排名)
用于动态仓位调整: 高波→降仓, 低波→加仓
"""
import numpy as np
from collections import deque

class VolatilityModel:
    def __init__(self, lookback=50, ewma_span=20):
        self.lookback = lookback
        self.ewma_span = ewma_span
        self.close_history = deque(maxlen=lookback)
        self.high_history = deque(maxlen=lookback)
        self.low_history = deque(maxlen=lookback)
        self.vol_history = deque(maxlen=200)  # 长期波动率历史
        
    def update(self, close, high, low):
        self.close_history.append(close)
        self.high_history.append(high)
        self.low_history.append(low)
        
    def ewma_volatility(self):
        """EWMA波动率 (年化)"""
        if len(self.close_history) < 2:
            return 0.01
        prices = np.array(self.close_history)
        returns = np.diff(np.log(prices))
        if len(returns) < 2:
            return 0.01
        
        # EWMA 权重
        alpha = 2.0 / (self.ewma_span + 1)
        weights = np.array([(1-alpha)**i * alpha for i in range(len(returns)-1, -1, -1)])
        weights = weights / weights.sum()
        
        var = np.sum(weights * returns**2)
        vol_daily = np.sqrt(var)
        vol_annual = vol_daily * np.sqrt(365 * 24)  # 年化(加密24h交易)
        
        self.vol_history.append(vol_annual)
        return vol_annual
    
    def parkinson_volatility(self):
        """Parkinson高低价波动率 (比收盘价更精确)"""
        if len(self.high_history) < 5:
            return self.ewma_volatility()
        
        highs = np.array(self.high_history)[-20:]
        lows = np.array(self.low_history)[-20:]
        
        # Parkinson estimator: σ² = (1/(4n ln2)) * Σ(ln(H/L))²
        log_hl = np.log(highs / lows)
        n = len(log_hl)
        var = (1.0 / (4 * n * np.log(2))) * np.sum(log_hl ** 2)
        vol_daily = np.sqrt(var) if var > 0 else 0.01
        vol_annual = vol_daily * np.sqrt(365 * 24)
        
        return vol_annual
    
    def volatility_percentile(self):
        """当前波动率在历史中的百分位 (0-100)"""
        if len(self.vol_history) < 10:
            return 50
        
        current = self.ewma_volatility()
        hist = np.array(self.vol_history)
        percentile = (hist < current).mean() * 100
        return percentile
    
    def position_multiplier(self):
        """波动率自适应仓位乘数: 高波→降仓, 低波→加仓"""
        pct = self.volatility_percentile()
        
        if pct > 90:    return 0.5   # 极高波动 → 半仓
        elif pct > 75:  return 0.7
        elif pct > 50:  return 0.85
        elif pct > 25:  return 1.0   # 正常
        elif pct > 10:  return 1.2   # 低波 → 加仓
        else:           return 1.5   # 极低波 → 满仓+
    
    def current_vol(self):
        return self.ewma_volatility()
