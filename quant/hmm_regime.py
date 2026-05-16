"""
HMM Regime Detection — 隐马尔科夫模型市场状态识别
检测三种状态: 0=下跌趋势, 1=震荡横盘, 2=上涨趋势
"""
import numpy as np
from hmmlearn import hmm
from collections import deque

class HMMRegime:
    def __init__(self, lookback=100):
        self.lookback = lookback
        self.model = hmm.GaussianHMM(
            n_components=3,      # 3种状态: 跌/横/涨
            covariance_type="full",
            n_iter=100,
            random_state=42
        )
        self.returns_history = deque(maxlen=lookback)
        self.volume_history = deque(maxlen=lookback)
        self.fitted = False
        self.current_regime = 1  # 默认震荡
        self.regime_probs = [0.33, 0.34, 0.33]
        
    def update(self, price, volume=0):
        """喂入新价格, 返回当前状态(0/1/2)和概率"""
        self.returns_history.append(price)
        if len(self.returns_history) < 50:
            return 1, [0.33, 0.34, 0.33]
        
        # 计算收益率序列
        prices = np.array(self.returns_history)
        returns = np.diff(prices) / prices[:-1]
        if len(returns) < 30:
            return 1, [0.33, 0.34, 0.33]
        
        # 特征: 收益率 + 波动率
        returns_clean = returns[-min(len(returns), self.lookback):]
        features = np.column_stack([
            returns_clean,
            np.abs(returns_clean)  # 波动率代理
        ])
        
        try:
            self.model.fit(features)
            states = self.model.predict(features)
            self.current_regime = int(states[-1])
            self.regime_probs = self.model.predict_proba(features)[-1].tolist()
            self.fitted = True
        except:
            pass
        
        return self.current_regime, self.regime_probs
    
    def is_bullish(self):
        """是否处于上涨趋势"""
        return self.current_regime == 2
    
    def is_bearish(self):
        """是否处于下跌趋势"""
        return self.current_regime == 0
    
    def is_ranging(self):
        """是否处于震荡"""
        return self.current_regime == 1
    
    def regime_score(self, direction='long'):
        """给信号打分: 顺势+分, 逆势-分"""
        if direction == 'long':
            if self.is_bullish(): return 15   # 顺势做多+15
            elif self.is_bearish(): return -20  # 逆势做多-20
            else: return 0
        else:  # short
            if self.is_bearish(): return 15
            elif self.is_bullish(): return -20
            else: return 0
