"""
Cointegration & Statistical Arbitrage — 协整性检验
检测BTC-ETH对是否存在长期均衡关系 → 价差交易信号
"""

import numpy as np
from collections import deque
from scipy import stats

class Cointegration:
    def __init__(self, lookback=100):
        self.lookback = lookback
        self.price_a = deque(maxlen=lookback)  # 币A价格
        self.price_b = deque(maxlen=lookback)  # 币B价格
        
    def update(self, price_a, price_b):
        """喂入两个价格序列"""
        self.price_a.append(price_a)
        self.price_b.append(price_b)
    
    def engle_granger_test(self):
        """Engle-Granger协整检验"""
        if len(self.price_a) < 30:
            return False, 0, 0, 0
        
        log_a = np.log(np.array(self.price_a))
        log_b = np.log(np.array(self.price_b))
        
        # OLS回归: log(A) = α + β * log(B) + ε
        X = np.column_stack([np.ones(len(log_b)), log_b])
        beta = np.linalg.lstsq(X, log_a, rcond=None)[0]
        residuals = log_a - X @ beta
        
        # ADF检验残差
        adf_stat, pvalue = self._adf_test(residuals)
        
        # 当前价差 (z-score)
        current_spread = log_a[-1] - (beta[0] + beta[1] * log_b[-1])
        spread_mean = np.mean(residuals)
        spread_std = np.std(residuals)
        zscore = (current_spread - spread_mean) / spread_std if spread_std > 0 else 0
        
        is_cointegrated = pvalue < 0.05 and adf_stat < -3.0
        return is_cointegrated, zscore, pvalue, beta[1]  # beta[1] = hedge ratio
    
    def _adf_test(self, residuals, maxlag=5):
        """简化ADF检验"""
        n = len(residuals)
        y = residuals[1:]
        y_lag = residuals[:-1]
        
        # Δy_t = γ*y_{t-1} + ε_t
        dy = y - y_lag
        X = y_lag.reshape(-1, 1)
        
        try:
            beta_hat = np.linalg.lstsq(X, dy, rcond=None)[0]
            y_pred = X @ beta_hat
            residuals_reg = dy - y_pred
            se = np.sqrt(np.sum(residuals_reg**2) / (n - 2))
            if se == 0:
                return 0, 1.0
            t_stat = beta_hat[0] / (se * np.sqrt(np.linalg.inv(X.T @ X)[0,0]))
            
            # 近似p-value
            pvalue = 2 * (1 - stats.t.cdf(abs(t_stat), n-2))
            return t_stat, pvalue
        except:
            return 0, 1.0
    
    def pair_signal(self):
        """价差交易信号: zscore>2→卖A买B, zscore<-2→买A卖B"""
        coint, zscore, pval, hedge_ratio = self.engle_granger_test()
        if not coint:
            return None
        
        if zscore > 2.5:
            return ('short', 'long', zscore)  # 卖A买B
        elif zscore < -2.5:
            return ('long', 'short', zscore)  # 买A卖B
        return None
