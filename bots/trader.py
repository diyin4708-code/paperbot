#!/usr/bin/env python3
"""
🦅 币安期货测试网 v27.4 — 6路信号 | 20x | 主流+山寨 | 测试网API
标的: BTC/ETH/DOGE + AVAX/LINK/APT/ARB/NEAR/SOL
"""
import time, sys, json, os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from fengshui_engine import fengshui_bonus

BOT_DIR = Path(__file__).parent
LOG_DIR = BOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
TZ = timezone(timedelta(hours=8))
EVOLVE_FILE = BOT_DIR / "evolve_v25.json"
LOG_FILE = LOG_DIR / "trader_log.txt"
MAX_LOG_LINES = 500  # 日志轮转上限

# ─── 参数 ───
LEVERAGE = 20
SCAN_INTERVAL, COOLDOWN, TIMEOUT = 20, 300, 3600  # v27.1: 15s减少API压力
TP, SL = 4.0, 0.8  # 盈亏比5:1，扣0.08%手续费后仍有净利
TRAIL_ACTIVATE_USD = 1.50  # 利润够大才激活
TRAIL_TIERS = [(8.0, 3.00), (5.0, 2.00), (3.0, 1.50), (1.5, 0.80)]
MAX_DAY_LOSS = -0.99  # 熔断已取消(雷先生要求)
MAX_LOSS_USD = 99999   # 熔断已取消
# 硬止损分币种: BTC 4%, ETH 5%, DOGE 6% (20x杠杆下=80%/100%/120%仓位)
MAX_LOSS_MAP = {"BTC/USDT:USDT": 4.0, "ETH/USDT:USDT": 5.0, "DOGE/USDT:USDT": 6.0}
MAX_STUCK_MIN, STUCK_THRESHOLD = 120, -0.40
MAX_CONSEC_LOSSES = 3  # v27: 2→3(太紧致零开单), 配指数退避冷却
CONSEC_COOLDOWN_BASE = 300   # 基础冷却300秒(5min)
CONSEC_COOLDOWN_TIERS = [(2,300),(3,900),(4,2700),(5,7200)]  # (连亏数,冷却秒) 指数退避
CONSEC_DECAY_SEC = 3600  # v27.1: 1h无交易自动清零连亏计数器
MIN_SCORE = 45  # 降低门槛(原50/60)，自适应动态调节
MIN_SCORE_BEAR = 48  # 熊市(周跌>5%)门槛提升，防逆势滥开多
MIN_SCORE_BULL = 42  # 牛市(周涨>3%)门槛降低，顺势信号更宽松
BALANCE_FAULT_THRESHOLD = 3  # 余额API连续失败N次才触发熔断
EVOLVE_VERSION = 27  # v27→27: 余额API容错, 滑动窗口连亏冷却, 方向熔断, 自适应阈值, 开盘降仓

SYMBOLS = ["BTC/USDT:USDT","ETH/USDT:USDT","DOGE/USDT:USDT"]
TIERS = {"BTC/USDT:USDT":0.25,"ETH/USDT:USDT":0.22,"DOGE/USDT:USDT":0.25}  # v27.4: 适配$1300余额 单笔$280-330


# ─── Indicators ───
def rsi(closes, p=14):
    if len(closes)<p+1: return 50
    g=[max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
    l=[max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
    ag=sum(g[-p:])/p; al=sum(l[-p:])/p
    if al==0: return 100
    return 100-100/(1+ag/al)

def sma(d,p): return sum(d[-p:])/p if len(d)>=p else None

def divergence(closes, direction='bullish', lookback=30):
    if len(closes)<lookback+15: return False,0
    h=lookback//2; L=closes[-lookback:-h]; R=closes[-h:]
    rL=rsi(closes[:len(closes)-h]); rR=rsi(closes)
    if direction=='bullish':
        if min(R)<=min(L)*.998 and rR>rL+2: return True,(rR-rL)+(1-min(R)/min(L))*100
    else:
        if max(R)>=max(L)*1.002 and rR<rL-2: return True,(rL-rR)+(max(R)/max(L)-1)*100
    return False,0

def vol_check(volumes, lookback=20):
    if len(volumes)<lookback+1: return True, 1.0
    cur = volumes[-1]; avg = sum(volumes[-lookback-1:-1]) / lookback
    if avg == 0: return True, 1.0
    return cur/avg >= 1.2, cur/avg

def is_weekend(): return datetime.now(TZ).weekday() >= 5
def is_afternoon():
    """北京时间14-17点 低流动性高波动时段"""
    h = datetime.now(TZ).hour
    return 14 <= h < 17

def is_opening_hours():
    """v27: 北京时间08:00-10:00 亚盘开盘高波动时段"""
    h = datetime.now(TZ).hour
    return 8 <= h < 10

def bollinger(closes, period=20, std=2):
    """布林带: 返回 (mid, upper, lower, width%)"""
    if len(closes)<period: return None,None,None,0
    mid = sum(closes[-period:])/period
    variance = sum((c-mid)**2 for c in closes[-period:])/period
    dev = variance**0.5
    upper = mid + std*dev; lower = mid - std*dev
    width = (upper-lower)/mid*100 if mid>0 else 0
    return mid, upper, lower, width

def atr(highs, lows, closes, period=14):
    """ATR (Average True Range)"""
    if len(closes)<period+1: return 0
    trs=[]
    for i in range(1,len(closes)):
        tr=max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1]))
        trs.append(tr)
    return sum(trs[-period:])/period

# ─── Evolution Engine ───
def load_evolve():
    if not EVOLVE_FILE.exists(): return {}
    try: return json.loads(EVOLVE_FILE.read_text())
    except: return {}

def save_evolve(data):
    try:
        tmp = str(EVOLVE_FILE) + '.tmp'
        with open(tmp, 'w') as f: json.dump(data, f); f.flush(); os.fsync(f.fileno())
        try: os.replace(tmp, str(EVOLVE_FILE))
        except FileNotFoundError: pass
    except: pass

def signal_weight(sig_type):
    ev = load_evolve()
    streak = ev.get('fail_streaks', {}).get(sig_type, 0)
    return 0.3 if streak >= 3 else 1.0

def record_signal_result(sig_type, win):
    ev = load_evolve()
    streaks = ev.get('fail_streaks', {})
    records = ev.get('signal_records', {})
    streaks[sig_type] = 0 if win else streaks.get(sig_type, 0) + 1
    rec = records.get(sig_type, {'wins': 0, 'losses': 0, 'total_pnl': 0})
    if win: rec['wins'] += 1
    else: rec['losses'] += 1
    records[sig_type] = rec
    ev.update({'fail_streaks': streaks, 'signal_records': records, 'version': EVOLVE_VERSION})
    save_evolve(ev)
    if streaks.get(sig_type, 0) >= 3:
        log(f"🧬 信号降权: {sig_type} 连败{streaks[sig_type]}次 → 权重30%")

# ─── State ───
MAX_POSITIONS = 5
@dataclass
class State:
    """轻量复盘容器。余额/持仓100%从API实时读取。"""
    positions: list = field(default_factory=list)
    trades: int = 0; wins: int = 0
    wins_list: list = field(default_factory=list); losses_list: list = field(default_factory=list)
    trade_history: list = field(default_factory=list)
    last_trade: float = 0; melt_until: float = 0
    day_start_bal: float = 0.0; day_start_t: float = field(default_factory=time.time)
    testnet_balance: float = 0.0
    stopped: bool = False; sig_stats: dict = field(default_factory=dict)
    startup_time: float = field(default_factory=time.time)
    # v27 风控升级
    consec_losses: int = 0; consec_melt_until: float = 0
    dir_losses: dict = field(default_factory=dict)  # {'long':3, 'short':0} 方向连亏计数
    dir_melt: dict = field(default_factory=dict)     # {'long': timestamp, 'short': 0}
    consec_history: list = field(default_factory=list)  # 最近50笔: [(ts, win/loss, direction),...]
    consec_long_losses: int = 0; consec_short_losses: int = 0  # 方向分别计数
    consec_scan_fails: int = 0  # API全挂计数
    dir_melt_until: dict = field(default_factory=dict)  # {'long': ts, 'short': ts}
    bal_fault_count: int = 0  # 余额API连续失败计数
    last_known_balance: float = 0.0  # 最后一次成功余额
    main_pnl: float = 0.0
    main_trades: int = 0; main_wins: int = 0
    main_losses: int = 0  # v27.3: 主流连亏计数
    _evolve_path: any = None  # v27.2: set in load()
    @property
    def wr(self):
        closed = len(self.wins_list) + len(self.losses_list)
        return len(self.wins_list)/closed if closed else 0.5

    def save(self):
        """v27.2: 持久化胜率数据到 evolve 文件"""
        import json as _j
        try:
            d = _j.loads(self._evolve_path.read_text()) if self._evolve_path.exists() else {}
        except: d = {}
        d['state_snapshot'] = {
            'wins': len(self.wins_list),
            'losses': len(self.losses_list),
            'wins_list': self.wins_list[-200:],
            'losses_list': self.losses_list[-200:],
            'day_start_bal': self.day_start_bal,
            'day_start_t': self.day_start_t,
            'trades_session': len(self.wins_list) + len(self.losses_list),
            'last_save': time.time()
        }
        try:
            tmp = str(self._evolve_path) + '.tmp'
            with open(tmp, 'w') as _f:
                _j.dump(d, _f)
                _f.flush(); os.fsync(_f.fileno())
            os.replace(tmp, str(self._evolve_path))
        except: pass  # v27.3: 原子写入
    @classmethod
    def load(cls):
        s = cls()
        s._evolve_path = BOT_DIR / 'evolve_v25.json'
        # 从文件恢复持久化数据
        try:
            if s._evolve_path.exists():
                d = json.loads(s._evolve_path.read_text())
                snap = d.get('state_snapshot', {})
                if snap:
                    s.wins_list = snap.get('wins_list', [])
                    s.losses_list = snap.get('losses_list', [])
                    s.day_start_bal = snap.get('day_start_bal', s.day_start_bal)
                    s.day_start_t = snap.get('day_start_t', s.day_start_t)
        except: pass
        return s

def balance_fn():
    """从测试网API实时读取U本位可用余额（唯一数据源）
    v27: 容错增强 - API失败返回None而非0，防止假熔断"""
    try:
        b = _testnet_get_balance()
        if b:
            free = float(b.get("availableBalance", b.get("balance", 0)))
            if free >= 0.01:  # 接受任意非零值(0.01兜底, 实际余额通常>1U)
                state.bal_fault_count = 0  # 成功则清零故障计数
                state.last_known_balance = free
                return free
    except: pass
    # API失败: 增加故障计数, 返回最后一次已知余额
    state.bal_fault_count += 1
    if state.last_known_balance > 0:
        if state.bal_fault_count <= BALANCE_FAULT_THRESHOLD:
            log(f"⚠️ 余额API失败(#{state.bal_fault_count}), 使用缓存${state.last_known_balance:.2f}")
            return state.last_known_balance
        else:
            log(f"🚨 余额API连续失败{state.bal_fault_count}次 > {BALANCE_FAULT_THRESHOLD}阈值")
    return None  # 返回None = 无法获取余额

def api_positions():
    """从测试网API实时读取持仓列表"""
    import urllib.request as ub2, ssl as s2, json as j2, hmac as h2, hashlib
    try:
        ctx = s2.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = s2.CERT_NONE
        ts_int = int(time.time() * 1000)
        q = f'timestamp={ts_int}'
        sig = h2.new(TESTNET_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
        req = ub2.Request(
            'https://testnet.binancefuture.com/fapi/v2/positionRisk?' + q + '&signature=' + sig,
            headers={'X-MBX-APIKEY': TESTNET_KEY})
        resp = ub2.urlopen(req, timeout=8, context=ctx)
        data = j2.loads(resp.read())
        result = []
        for p in data:
            amt = float(p.get('positionAmt', 0))
            if abs(amt) > 0:
                ep = float(p.get('entryPrice', 0))
                result.append(dict(
                    sym=p.get('symbol',''), dir='long' if amt > 0 else 'short',
                    qty=abs(amt), entry=ep, mark=float(p.get('markPrice',0)),
                    pnl_u=float(p.get('unRealizedProfit',0)),
                    time=time.time(), size=abs(amt) * ep / LEVERAGE))
        return result
    except Exception as e:
        log("api_positions error: " + str(e)[:80])
        return []

state = State.load()

_ex = None
_ex_fails = 0

class _TestnetEx:
    markets = {}
    def fetch_ticker(self, sym):
        import urllib.request as ur2, ssl as s2, json as j2
        s=sym.split(':')[0].replace('/','')
        try:
            r=ur2.urlopen(ur2.Request(f'https://testnet.binancefuture.com/fapi/v1/ticker/price?symbol={s}'),timeout=5,context=s2.create_default_context())
            d=j2.loads(r.read()); return {'symbol':d['symbol'],'last':float(d['price'])}
        except: return None
    def fetch_ohlcv(self, sym, tf='15m', limit=30):
        import urllib.request as ur2, ssl as s2, json as j2
        s=sym.split(':')[0].replace('/','')
        try:
            r=ur2.urlopen(ur2.Request(f'https://testnet.binancefuture.com/fapi/v1/klines?symbol={s}&interval={tf}&limit={limit}'),timeout=8,context=s2.create_default_context())
            return [[float(x) for x in k] for k in j2.loads(r.read())]
        except: return None
    def fetch_funding_rate(self, sym):
        import urllib.request as ur2, ssl as s2, json as j2
        s=sym.split(':')[0].replace('/','')
        try:
            r=ur2.urlopen(ur2.Request(f'https://testnet.binancefuture.com/fapi/v1/premiumIndex?symbol={s}'),timeout=5,context=s2.create_default_context())
            d=j2.loads(r.read())
            return {'fundingRate': float(d.get('lastFundingRate', 0))}
        except: return None
    def create_market_order(self, sym, side, amount, params=None):
        import urllib.request as ur2, ssl as s2, json as j2, hmac as h2, hashlib
        s=sym.split(':')[0].replace('/','')
        ts=int(time.time()*1000); p=f'symbol={s}&side={side.upper()}&type=MARKET&quantity={amount}&timestamp={ts}&recvWindow=5000'
        sig=h2.new(TESTNET_SECRET.encode(),p.encode(),hashlib.sha256).hexdigest()
        try:
            r=ur2.urlopen(ur2.Request(f'https://testnet.binancefuture.com/fapi/v1/order?{p}&signature={sig}',method='POST',headers={'X-MBX-APIKEY':TESTNET_KEY}),timeout=10,context=s2.create_default_context())
            return j2.loads(r.read())
        except: return None

def _make_testnet_ex(): return _TestnetEx()

def _testnet_get_balance():
    """从测试网API拉取余额（直接REST）"""
    import urllib.request as ub2, ssl as s2, json as j2, hmac as h2, hashlib
    try:
        ts = int(time.time() * 1000)
        q = f"timestamp={ts}&recvWindow=5000"
        sig = h2.new(TESTNET_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
        req = ub2.Request(
            f"https://testnet.binancefuture.com/fapi/v2/balance?{q}&signature={sig}",
            headers={"X-MBX-APIKEY": TESTNET_KEY})
        ctx = s2.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = s2.CERT_NONE
        ctx.check_hostname = False; ctx.verify_mode = s2.CERT_NONE
        resp = ub2.urlopen(req, timeout=8, context=ctx)
        for b in j2.loads(resp.read()):
            if b.get("asset") == "USDT":
                return b
    except Exception as _be:
        if int(time.time()) % 300 < 5:  # 5分钟一批日志
            log(f"⚠️ _testnet_get_balance: {str(_be)[:60]}")
    return None

def exchange(force=False):
    global _ex, _ex_fails
    return _make_testnet_ex()

def fetch(fn,*a,**kw):
    global _ex_fails
    ex=exchange()
    if not ex: return None
    for i in range(4):
        try:
            result = fn(*a,**kw)
            _ex_fails = 0  # 成功则清零
            return result
        except Exception as e:
            _ex_fails += 1
            if i < 3:
                time.sleep(min(1.5 ** i, 5))  # 指数退避: 1s, 1.5s, 2.25s
            else:
                # 最后一次失败，标记需要重连
                log(f"⚠️ fetch failed after 4 retries → forcing reconnect")
    return None

def price(sym):
    global _ex_fails
    ex=exchange()
    if not ex: return 0
    for i in range(5):
        try:
            t=ex.fetch_ticker(sym)
            if not isinstance(t, dict):
                _ex_fails += 1
                continue
            if t and t.get('last',0)>0:
                _ex_fails = 0  # 成功则清零
                return t['last']
        except Exception as e:
            _ex_fails += 1
            if i == 3:  # 第4次失败先尝试重建exchange，不sleep
                log(f"⚠️ price({sym.split(':')[0]}) 重试{_ex_fails}次 → 重建连接")
                ex = exchange(force=True)
                if not ex: continue
            else:
                time.sleep(min(1.5 ** i, 4))  # 增量退避
    _ex_fails += 2  # 全部失败大幅加罚，触发外部重建
    return 0

# ─── 凭证：从 config.toml 读取，不硬编码 ───
def _load_config():
    try:
        import sys; assert sys.version_info >= (3,11), "Python 3.11+ required for tomllib"
    except: pass
    try:
        import tomllib
        with open(BOT_DIR / "config.toml", 'rb') as f:
            return tomllib.load(f)
    except:
        return {}
_cfg = _load_config()
TESTNET_KEY = _cfg.get('testnet', {}).get('api_key', os.environ.get('TESTNET_KEY',''))
TESTNET_SECRET = _cfg.get('testnet', {}).get('api_secret', os.environ.get('TESTNET_SECRET',''))
MACRO_GATE  = _cfg.get('trading', {}).get('macro_gate', True)

# ─── 测试网交易函数 ───

def sim_open_order(sig, pos):
    import urllib.request as ur2, ssl as s2, json as j2, hmac as h2, hashlib
    try:
        sym = sig['sym'].split(':')[0].replace('/','')
        side = 'BUY' if sig['dir'] == 'long' else 'SELL'
        margin = pos['size']
        ctx = s2.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = s2.CERT_NONE
        try:
            ts0 = int(time.time() * 1000)
            lp = f'symbol={sym}&leverage={LEVERAGE}&timestamp={ts0}&recvWindow=5000'
            lsig = h2.new(TESTNET_SECRET.encode(), lp.encode(), hashlib.sha256).hexdigest()
            ur2.urlopen(ur2.Request(
                f'https://testnet.binancefuture.com/fapi/v1/leverage?{lp}&signature={lsig}',
                method='POST', headers={'X-MBX-APIKEY': TESTNET_KEY}), timeout=5, context=ctx)
        except: pass
        r = ur2.urlopen(ur2.Request(f'https://testnet.binancefuture.com/fapi/v1/ticker/price?symbol={sym}'), timeout=5, context=ctx)
        pr = float(j2.loads(r.read())['price'])
        qty = round(margin * LEVERAGE / pr, 0 if 'DOGE' in sym else 3)
        if qty < (1 if 'DOGE' in sym else 0.001): qty = (1 if 'DOGE' in sym else 0.001)
        ts = int(time.time() * 1000)
        params = f'symbol={sym}&side={side}&type=MARKET&quantity={qty}&timestamp={ts}&recvWindow=5000'
        sig_hash = h2.new(TESTNET_SECRET.encode(), params.encode(), hashlib.sha256).hexdigest()
        req = ur2.Request(f'https://testnet.binancefuture.com/fapi/v1/order?{params}&signature={sig_hash}',
            method='POST', headers={'X-MBX-APIKEY': TESTNET_KEY})
        resp = ur2.urlopen(req, timeout=10, context=ctx)
        order = j2.loads(resp.read())
        actual_price = float(order.get('avgPrice', 0) or order.get('price', pr))
        if actual_price <= 0:
            actual_price = pr
        log(f"🧪 测试网下单: {side} {sym} {qty}个 @{actual_price:.5f}")
        return actual_price
    except Exception as e:
        log(f"🧪 下单失败: {str(e)[:80]}")
        return None

def sim_close_order(pos):
    """测试网API平仓，返回成交均价，3次重试"""
    import urllib.request as ur2, ssl as s2, json as j2, hmac as h2, hashlib
    for attempt in range(3):
        try:
            sym = pos['sym'].split(':')[0].replace('/','')
            side = 'BUY' if pos['dir'] == 'short' else 'SELL'
            ctx = s2.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = s2.CERT_NONE
            ts_int = int(time.time() * 1000)
            qty = pos.get('qty', 0)
            if qty <= 0:
                # v27.4: 从API实时查数量
                real_pos = api_positions()
                for rp in real_pos:
                    if rp['sym'].replace('/','') == sym:
                        qty = rp['qty']
                        break
                if qty <= 0:
                    doge_qty = 'DOGE' in sym
                    qty = round(pos.get('size', 5) * LEVERAGE / max(pos.get('entry', 1), 0.0001), 0 if doge_qty else 3)
            if qty < 0.001: qty = 0.001
            params = 'symbol=' + sym + '&side=' + side + '&type=MARKET&quantity=' + str(qty) + '&timestamp=' + str(ts_int) + '&recvWindow=5000&reduceOnly=true'
            sig = h2.new(TESTNET_SECRET.encode(), params.encode(), hashlib.sha256).hexdigest()
            req = ur2.Request('https://testnet.binancefuture.com/fapi/v1/order?' + params + '&signature=' + sig,
                method='POST', headers={'X-MBX-APIKEY': TESTNET_KEY})
            resp = ur2.urlopen(req, timeout=10, context=ctx)
            o = j2.loads(resp.read())
            if o.get('status') in ('NEW', 'FILLED', 'PARTIALLY_FILLED'):
                avg = float(o.get('avgPrice', o.get('price', pos.get('entry', 0))))
                log('🧪 测试网平仓: ' + side + ' ' + sym + ' ' + str(o.get('executedQty')) + '个 @{:.5f}'.format(avg))
                return avg if avg > 0 else pos.get('entry', 0)
            else:
                time.sleep(1)
        except Exception as e:
            if attempt == 2:
                log('🧪 平仓失败(' + str(attempt+1) + '/3): ' + str(e)[:80])
            time.sleep(1)
    return None

def ts(): return datetime.now(TZ).strftime("%m-%d %H:%M:%S")
def _parse_trade_ts(ts_str):
    """v27: 解析交易历史中的时间戳字符串 → unix时间"""
    try:
        dt = datetime.strptime(ts_str, "%m-%d %H:%M:%S")
        dt = dt.replace(year=datetime.now(TZ).year, tzinfo=TZ)
        return dt.timestamp()
    except: return 0
def log(msg):
    line=f"[{ts()}] {msg}"
    print(line,flush=True)
    try:
        with open(LOG_FILE,'a') as f: f.write(line+'\n')
        # 日志轮转：超过上限截断
        lines = LOG_FILE.read_text().split('\n')
        if len(lines) > MAX_LOG_LINES:
            LOG_FILE.write_text('\n'.join(lines[-MAX_LOG_LINES:]))
    except: pass

# ─── Macro Gate ───
def check_macro_gate():
    """如果宏观风险高，全局禁止开仓"""
    try:
        import urllib.request, ssl
        ctx = ssl.create_default_context()
        url = "https://api.alternative.me/fng/?limit=1"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        resp = urllib.request.urlopen(req, timeout=8, context=ctx)
        d = json.loads(resp.read())
        fng = int(d['data'][0]['value'])
        if fng <= 15:
            return False, f"Fear&Greed={fng} 极度恐惧，禁开仓"
        if fng >= 85:
            return False, f"Fear&Greed={fng} 极度贪婪，风险高禁开仓"
        return True, f"Safe FNG={fng}"
    except Exception as e:
        return True, f"macro gate skipped ({str(e)[:40]})"

# ─── Signal Scanner ───
# ─── 周回测集成 ───
def weekly_backtest(ex):
    """每周回测主流币，返回趋势/波动/排名"""
    btc={}; eth={}; doge={}
    for sym in SYMBOLS:
        try:
            o=fetch(ex.fetch_ohlcv,sym,'1d',limit=7)
            if not o or len(o)<3: continue
            closes=[c[4] for c in o]; highs=[c[2] for c in o]; lows=[c[3] for c in o]
            start=o[0][1]; end=closes[-1]; w_change=(end-start)/start
            w_high=max(highs); w_low=min(lows)
            w_range=(w_high-w_low)/start  # 周振幅
            daily_ret=[(closes[i]-closes[i-1])/closes[i-1] for i in range(1,len(closes))]
            w_vol=sum(abs(r) for r in daily_ret)/max(len(daily_ret),1) if daily_ret else 0
            up_days=sum(1 for r in daily_ret if r>0)
            down_days=sum(1 for r in daily_ret if r<0)
            last_3=daily_ret[-3:] if len(daily_ret)>=3 else daily_ret
            recent_momentum=sum(last_3) if last_3 else 0
            name=sym.split('/')[0]
            data={'change':w_change,'high':w_high,'low':w_low,'range':w_range,'vol':w_vol,'up':up_days,'down':down_days,'momentum':recent_momentum,'close':end}
            if name=='BTC': btc=data
            elif name=='ETH': eth=data
            elif name=='DOGE': doge=data
        except Exception as e: log(f"周回测{sym}: {str(e)[:50]}")
    return {'BTC':btc,'ETH':eth,'DOGE':doge}

# 每4小时刷新一次周回测
_wb_cache = {}
_wb_ts = 0
def wb_fresh(ex):
    global _wb_cache, _wb_ts
    if time.time()-_wb_ts > 14400:
        _wb_cache = weekly_backtest(ex)
        _wb_ts = time.time()
        b,c,d = _wb_cache.get('BTC',{}),_wb_cache.get('ETH',{}),_wb_cache.get('DOGE',{})
        if b and c and d:
            log(f"📊 周回测: BTC {b.get('change',0):+.1%} ETH {c.get('change',0):+.1%} DOGE {d.get('change',0):+.1%}")
    return _wb_cache

def weekly_alignment_bonus(sym, direction, wb, r1h=None):
    """周趋势与信号方向一致 → 加分; 相反 → 减分
    r1h: 1h RSI, 极度超卖(<25)时逆势扣分减半"""
    name = sym.split('/')[0]
    d = wb.get(name,{})
    if not d: return 0
    w_change = d.get('change',0)
    momentum = d.get('momentum',0)
    vol = d.get('vol',0)
    # 极度超卖时取消周线扣分 (反弹均值回归 > 趋势方向)
    if r1h is not None and r1h < 25:
        return 0
    # 强趋势 (>3%周涨跌) + 近期顺势 → 强加分
    if direction=='long' and d.get('change',0) > 0.03 and momentum>0:
        return min(w_change*100*0.6, 15)  # v27: 上限20→15
    elif direction=='short' and w_change<-0.03 and momentum<0:
        return min(abs(w_change)*100*0.6, 15)  # v27: 上限20→15
    # 弱趋势 + 顺势 → 小加分
    elif direction=='long' and d.get('change',0) > 0:
        return min(w_change*100*0.3, 6)  # v27: 上限8→6
    elif direction=='short' and w_change<0:
        return min(abs(w_change)*100*0.3, 6)  # v27: 上限8→6
    # 逆势 → 降分
    elif direction=='long' and w_change<-0.02:
        return -10
    elif direction=='short' and d.get('change',0) > 0.02:
        return -10
    # 高波动 + 逆势 = 更大降分
    if vol>0.05 and ((direction=='long' and w_change<0) or (direction=='short' and d.get('change',0) > 0)):
        return -15
    return 0

# ─── 🧠 多框架知识融合 ───

# === Freqtrade: 止损冷却 (不要刚止损就重开) ===
def sl_cooldown(sym, direction):
    """止损后冷却30分钟，同向不开"""
    for t in state.trade_history[-5:]:
        if t.get('sym')==sym and t.get('dir')==direction and t.get('reason','').startswith('sl'):
            td = time.time()-t.get('ts',0)
            if td < COOLDOWN: return True, round((COOLDOWN-td)/60, 0)
    return False, 0

# === Jesse / Freqtrade: 动态仓位 (波动率自适应) ===
def dynamic_tier(sym, cl, vols):
    """高波动→降仓, 低波动→升仓 (Jesse Kelly adaptation)"""
    base_tier = TIERS.get(sym, 0.1)
    if len(cl)<20: return base_tier
    # 20周期波动率
    rets = [abs(cl[i]-cl[i-1])/cl[i-1] for i in range(-20,0)]
    recent_vol = sum(rets[-5:])/5 if rets else 0.005
    avg_vol = sum(rets)/len(rets) if rets else 0.005
    vr = recent_vol / max(avg_vol, 0.001)  # 波动率比
    if vr > 2.0: return base_tier * 0.5   # 高波动 → 半仓
    elif vr > 1.5: return base_tier * 0.7
    elif vr < 0.5: return min(base_tier*1.5, 0.6)  # 低波动 → 加仓(上限60%)
    return base_tier

# === OctoBot: 情绪叠加 (周趋势+资金费率) ===
def sentiment_overlay(sym, direction, wb):
    """综合情绪打分: 周趋势60% + 近3日动量40%"""
    name = sym.split('/')[0]
    d = wb.get(name, {})
    if not d: return 0
    w_change = d.get('change',0)
    momentum = d.get('momentum',0)
    # 方向一致性
    trend_score = 0
    if direction=='long' and d.get('change',0) > 0: trend_score = min(w_change*100, 20)
    elif direction=='short' and w_change<0: trend_score = min(abs(w_change)*100, 20)
    elif direction=='long' and w_change<0: trend_score = max(w_change*100, -20)
    elif direction=='short' and d.get('change',0) > 0: trend_score = max(-w_change*100, -20)
    mom_score = max(min(momentum*200, 10), -10)  # 近3日动量
    if direction=='short': mom_score = -mom_score
    return round(trend_score*0.6 + mom_score*0.4, 1)

# === ccxt 最佳实践: 量异常过滤 ===
def vol_anomaly(vols):
    """检测成交量异常(操纵嫌疑) → 跳过"""
    if len(vols)<30: return False
    avg = sum(vols[-30:-1])/29
    last = vols[-1]
    return last > avg * 5  # 5倍正常量→异常

# === Freqtrade: 资金费率信号 ===
def funding_signal(ex, sym):
    """极端资金费率 = 反转信号
       >0.1%: 多头拥挤→偏空
       <-0.05%: 空头拥挤→偏多
    """
    try:
        fr = ex.fetch_funding_rate(sym)
        rate = float(fr.get('fundingRate',0)) if fr else 0
        if rate > 0.001: return 'short', min(rate*1000*5, 15)  # 费率>0.1%
        elif rate < -0.0005: return 'long', min(abs(rate)*1000*5, 15)
    except: pass
    return None, 0

def scan():
    ex=exchange()
    if not ex:
        state.consec_scan_fails += 1  # v27.3: +1用于API全挂检测
        return []
    state.consec_scan_fails = max(0, state.consec_scan_fails - 1)  # 成功则递减
    wb = wb_fresh(ex)  # 获取最新周回测
    sigs=[]
    for sym in SYMBOLS:
        try:
            o=fetch(ex.fetch_ohlcv,sym,'15m',limit=250)
            if not o or len(o)<55: continue
            cl=[c[4] for c in o]; vols=[c[5] for c in o]; pr=cl[-1]
            r15=rsi(cl); ma200=sma(cl,200) if len(cl)>=200 else sma(cl,len(cl))
            o1=fetch(ex.fetch_ohlcv,sym,'1h',limit=25)
            r1h=rsi([c[4] for c in o1]) if o1 and len(o1)>=15 else 50
            vol_ok, vol_ratio = vol_check(vols)
            
            # 量异常过滤 (ccxt最佳实践: 5倍均量=操纵嫌疑)
            if vol_anomaly(vols):
                log(f"⚠️ {sym} 量异常 → 跳过")
                continue
            
            # 4h趋势过滤: EMA50>EMA200 (EasyTrendline学来)
            o4=fetch(ex.fetch_ohlcv,sym,'4h',limit=210)
            trend_up = None  # None=趋势未知，两端信号都跳过
            if o4 and len(o4)>=201:
                closes=[c[4] for c in o4]
                ema50 = sum(closes[-50:])/50
                ema200 = sum(closes)/200
                trend_up = ema50 > ema200 and pr > ema50  # 牛市: 50在200上方且价格在50上方
            
            # RSI-SMA动量过滤 (EasyTrendline: 避免弱反弹)
            rsi_sma = sum([rsi([c[4] for c in o1[-i-14:-i] or [c[4]]*14]) for i in range(14)])/14 if o1 and len(o1)>=28 else 50
            momentum_ok = r15 > rsi_sma - 3  # RSI高于其SMA(14) → 动量健康

            # LONG 信号 (需要量 + 趋势确认 + 动量健康)
            ok,s=divergence(cl,'bullish')
            if ok and r15<58 and r1h<62 and trend_up is True and momentum_ok:
                if not vol_ok: pass  # 背离无量直接跳过
                else:
                    sc=s*2.5+(58-r15)*0.8
                    sc*=signal_weight('RSI背离(L)')
                    sigs.append({'sym':sym,'pr':pr,'dir':'long','sc':round(sc,1),'type':'RSI背离(L)','r15':round(r15,1),'r1h':round(r1h,1),'vol_r':round(vol_ratio,2)})
            # v27: 超卖反转 — 仅允许牛市或趋势未知(趋势未知时扣10分)，熊市禁止抄底
            if r15<35:
                if trend_up is True:
                    sigs.append({'sym':sym,'pr':pr,'dir':'long','sc':round((35-r15)*4+15,1),'type':'超卖反转','r15':round(r15,1),'r1h':round(r1h,1)})
                elif trend_up is None:
                    sigs.append({'sym':sym,'pr':pr,'dir':'long','sc':round((35-r15)*4+5,1),'type':'超卖反转','r15':round(r15,1),'r1h':round(r1h,1)})
            if r1h<42 and r15>r1h+5 and r15<50 and ma200 and pr>ma200:
                sc=((42-r1h)*1.5+(r15-r1h)*2+5)*signal_weight('多TF均值回归')
                sigs.append({'sym':sym,'pr':pr,'dir':'long','sc':round(sc,1),'type':'多TF均值回归','r15':round(r15,1),'r1h':round(r1h,1)})
            # 🚀 追涨多头: RSI>55 + 周线涨 + 量确认 + 价格高于MA200
            wdata = wb.get(sym.split(":")[0], {})
            if r15>55 and trend_up is True and vol_ok and ma200 and pr>ma200 and wdata.get('change',0) > 0:
                sc=min((r15-55)*3 + wdata.get('change',0)*100*80 + 15, 100)  # CAP 100
                sc*=signal_weight('追涨动量')
                sigs.append({'sym':sym,'pr':pr,'dir':'long','sc':round(sc,1),'type':'追涨动量','r15':round(r15,1),'r1h':round(r1h,1),'vol_r':round(vol_ratio,2)})
            # 🔻 追跌空头: RSI 30-55 + 周线跌 + 量确认 + 价格低于MA200
            if r15<55 and r15>30 and trend_up is False and vol_ok and ma200 and pr<ma200 and wdata.get('change',0) < 0:
                sc=min((55-r15)*3 + abs(wdata.get('change',0))*100*2 + 15, 100)
                sc*=signal_weight('追跌动量(S)')
                sigs.append({'sym':sym,'pr':pr,'dir':'short','sc':round(sc,1),'type':'追跌动量(S)','r15':round(r15,1),'r1h':round(r1h,1),'vol_r':round(vol_ratio,2)})

            # SHORT 信号 (需要量 + 趋势确认)
            ok,s=divergence(cl,'bearish')
            if ok and r15>42 and r1h>38 and trend_up is False:
                if not vol_ok: pass  # 背离无量直接跳过
                else:
                    sc=s*2.5+(r15-42)*0.8
                    sc*=signal_weight('RSI背离(S)')
                    sigs.append({'sym':sym,'pr':pr,'dir':'short','sc':round(sc,1),'type':'RSI背离(S)','r15':round(r15,1),'r1h':round(r1h,1),'vol_r':round(vol_ratio,2)})
            if r15>68 and trend_up is False:
                log(f"🔔 {sym.split(':')[0]} r15={r15:.1f}>65 超买! sc={(r15-65)*3+25:.0f}")
                sigs.append({'sym':sym,'pr':pr,'dir':'short','sc':round((r15-65)*3+25,1),'type':'超买反转','r15':round(r15,1),'r1h':round(r1h,1)})
            if r1h>58 and r15<r1h-5 and r15>50 and trend_up is False:
                sc=((r1h-58)*1.5+(r1h-r15)*2+5)*signal_weight('多TF均值回归')
                sigs.append({'sym':sym,'pr':pr,'dir':'short','sc':round(sc,1),'type':'多TF均值回归','r15':round(r15,1),'r1h':round(r1h,1)})
            # v27: 🐻 强趋势跟随做空 — 熊市中RSI续跌(不在极端低点) → 顺势做空
            if trend_up is False and r15<55 and r15>25 and r1h<55 and r1h>25:
                # RSI正在下降(15m < 1h) + 趋势向下 → 确认下跌动量
                rsi_falling = r15 < r1h
                if rsi_falling:
                    sc = 20 + (55-r15)*1.2 + (55-r1h)*1.0  # RSI越低→趋势越弱→分越低
                    if vol_ok: sc *= 1.2  # 量确认加分
                    sigs.append({'sym':sym,'pr':pr,'dir':'short','sc':round(sc,1),'type':'趋势跟随(S)','r15':round(r15,1),'r1h':round(r1h,1),'vol_r':round(vol_ratio,2)})
            
            # ── 横盘专用: BB反弹 (RSI中性区间 + 低波动) ──
            if 40<r15<60 and 38<r1h<62:
                mid, upper, lower, bbw = bollinger(cl)
                if mid and bbw>0 and bbw<4:
                    if pr <= lower*1.001 and r15>38 and trend_up is not False:  # 🔧 v27.2: 下跌趋势禁止BB下轨做多
                        sc = 25 + (lower-pr)/lower*100*80 + (r15-38)*1.0
                        if vol_ok: sc *= 1.3
                        sigs.append({'sym':sym,'pr':pr,'dir':'long','sc':round(sc,1),'type':'BB反弹(L)','r15':round(r15,1),'r1h':round(r1h,1),'vol_r':round(vol_ratio,2)})
                    elif pr >= upper*0.999 and r15<62 and trend_up is False:  # elif防止同币种双向触发
                        sc = 25 + (pr-upper)/upper*100*80 + (62-r15)*1.0
                        if vol_ok: sc *= 1.3
                        sigs.append({'sym':sym,'pr':pr,'dir':'short','sc':round(sc,1),'type':'BB反弹(S)','r15':round(r15,1),'r1h':round(r1h,1),'vol_r':round(vol_ratio,2)})
                        
        except Exception as e: log(f"⚠️ {sym}: {str(e)[:60]}")

    # 多框架融合：给信号加减分
    for s in sigs:
        # 周回测趋势加成
        wb_bonus = weekly_alignment_bonus(s['sym'], s['dir'], wb, s.get('r1h'))
        # 情绪叠加 (OctoBot学来)
        sent = sentiment_overlay(s['sym'], s['dir'], wb)
        total_bonus = wb_bonus + sent
        if total_bonus != 0:
            s['sc'] = round(s['sc'] + total_bonus, 1)
            s['wb_bonus'] = total_bonus

    # 资金费率信号 (freqtrade/hummingbot学来)
    for sym in SYMBOLS:
        fs_dir, fs_sc = funding_signal(ex, sym)
        if fs_dir and fs_sc >= 8:
            tick = fetch(ex.fetch_ticker, sym)
            if tick:
                sigs.append({'sym':sym,'pr':tick.get('last',0),'dir':fs_dir,'sc':round(fs_sc+20,1),
                    'type':'资金费率','r15':0,'r1h':0,'vol_r':0,'fr':True})


    sigs.sort(key=lambda x:-x['sc'])
    seen={}
    for s in sigs:
        k=(s['sym'],s['dir'])
        if k not in seen: seen[k]=s

    # 🀄 命理增强 ±12分
    try:
        divine = fengshui_bonus()
        if divine != 0:
            for s in seen.values():
                s['sc'] = round(s['sc'] + divine, 1)
            log(f"🀄 命理: {divine:+d}分")
    except Exception as e:
        log(f"🀄 命理异常: {str(e)[:60]}")

    # 止损冷却过滤 (freqtrade StopLossGuard学来)
    filtered = []
    for s in sorted(seen.values(), key=lambda x:-x['sc']):
        on_cd, cd_min = sl_cooldown(s['sym'], s['dir'])
        if on_cd:
            log(f"⏳ {s['sym']} {s['dir']} 止损冷却中 ({cd_min:.0f}分) → 跳过")
            continue
        # v27: 方向熔断过滤
        dir_melt = state.dir_melt_until.get(s['dir'], 0)
        if dir_melt > 0 and time.time() < dir_melt:
            remaining = (dir_melt - time.time()) / 60
            log(f"🚫 {s['sym']} {s['dir']} 方向熔断中 ({remaining:.0f}分) → 跳过")
            continue
        filtered.append(s)
    # v27: 自适应信号阈值 — 根据市场状态动态调整
    wb_summary = _wb_cache if _wb_cache else {}
    avg_w_change = 0
    count = 0
    for d in wb_summary.values():
        if isinstance(d, dict) and 'change' in d:
            avg_w_change += d['change']; count += 1
    avg_w_change = avg_w_change / max(count, 1)
    if abs(avg_w_change) > 0.05:
        active_min = MIN_SCORE_BEAR  # 熊市/牛市加剧 → 门槛55
    elif abs(avg_w_change) > 0.03:
        active_min = MIN_SCORE  # 温和趋势 → 门槛45
    else:
        active_min = MIN_SCORE_BULL  # 横盘 → 门槛42
    return sorted([s for s in filtered if s['sc']>=active_min], key=lambda x:-x['sc'])

def open_trade(sig):
    log(f"🔴 open_trade called: {sig['dir']} {sig['sym']} [{sig['type']}] sc:{sig['sc']}")
    # 🧠 DeepSeek AI 决策引擎 (8秒超时，注入余额/连亏/方向状态)
    try:
        import subprocess as _sp
        sig_copy = dict(sig)
        sig_copy['consec_losses'] = state.consec_losses
        wl, ll = state.wins_list, state.losses_list
        sig_copy['recent_wr'] = round(len(wl)/max(len(wl)+len(ll),1)*100)
        sig_copy['balance'] = round(balance_fn() or 0, 2)
        sig_copy['positions'] = len(state.positions)
        sig_copy['dir_melt_long'] = state.dir_melt.get('long', 0) > time.time()
        sig_copy['dir_melt_short'] = state.dir_melt.get('short', 0) > time.time()
        r = _sp.run([sys.executable, str(BOT_DIR/'deepseek_engine.py')],
                    input=json.dumps(sig_copy), capture_output=True, text=True, timeout=8)
        ai_decision = (r.stdout or '').strip()
        if ai_decision.startswith('REJECT'):
            log(f"🤖 AI拒绝: {ai_decision}")
            return
        elif ai_decision.startswith('APPROVE'):
            log(f"🤖 AI批准: {ai_decision[8:] if len(ai_decision)>8 else '通过'}")
        else:
            log(f"🤖 AI超时/降级: 半仓执行")
            # 超时降为半仓，不拒绝
            sig['_ai_reduced'] = True
    except Exception as e:
        log(f"🤖 AI异常,半仓执行: {str(e)[:50]}")
    if len(state.positions)>=MAX_POSITIONS: return
    key=(sig['sym'],sig['dir'])
    for p in state.positions:
        if (p['sym'],p['dir'])==key: return
    d = sig['dir']
    if state.dir_melt.get(d, 0) > time.time():
        remaining = int((state.dir_melt[d] - time.time()) / 60)
        if int(time.time()) % 120 < SCAN_INTERVAL:  # throttle log
            log(f"🚫 {d}方向熔断中 (剩余{remaining}分)")
        return
    tier = TIERS.get(sig['sym'], 0.40)
    # 动态仓位 (Jesse Kelly adaptation): 高波动降仓
    ex2=exchange()
    if ex2:
        o2=fetch(ex2.fetch_ohlcv,sig['sym'],'15m',limit=30)
        if o2:
            tier = dynamic_tier(sig['sym'],[c[4] for c in o2],[c[5] for c in o2])
    # v25.4: 下午14-17点仓位减半（低流动性高波动）
    afternoon_mult = 0.5 if is_afternoon() else 1.0
    # v27: 开盘时段自动降仓 (08:00-10:00 亚盘高波动 → 25%仓位)
    opening_mult = 0.25 if is_opening_hours() else 1.0
    effective_mult = min(afternoon_mult, opening_mult)  # 取最严格的降仓系数
    bal = balance_fn()
    if bal is None or bal <= 0:
        log(f"⚠️ 余额不可用 → 跳过开单")
        return
    sz_raw = round(bal * tier * (0.75 if is_weekend() else effective_mult), 2)  # v27.3: 修复运算符优先级bug

    sz=sz_raw
    # sz = 名义价值(仓位), 最低$5
    if sz<3: sz=3.0
    pos={'sym':sig['sym'],'entry':sig['pr'],'size':sz,'time':time.time(),
        'dir':sig['dir'],'type':sig['type'],'score':sig['sc'],
        'trail_active':False,'peak_usd':0}
    if not validate_trade(sig, pos): return
    state.positions.append(pos)
    state.last_trade=time.time(); state.trades+=1
    log(f"🔫 开单确认: {sig['dir']} {sig['sym']} sz:{sz}U sc:{sig['sc']} [{sig['type']}]")
    fill_price = sim_open_order(sig, pos)
    if fill_price is None:
        log(f"💥 开单API失败 → 回滚")
        state.positions.pop(); state.trades-=1; state.save(); return
    pos['entry'] = fill_price  # 用实际成交价, 非OHLCV信号价
    state.main_trades+=1
    em='🚀' if sig['dir']=='long' else '🔻'
    vi=f" vol:{sig.get('vol_r',1):.1f}x" if 'vol_r' in sig else ""
    log(f"🔷{em} {sig['dir'].upper()} {sig['sym'].split(':')[0]} @{sig['pr']:.2f} x{LEVERAGE} {sz}U | {sig['type']} sc:{sig['sc']:.1f}{vi} [{len(state.positions)}/{MAX_POSITIONS}]")
    state.save()

def validate_trade(sig, pos):
    """三层校验: 参数/RSI/EMA"""
    # 1. 参数校验
    required_sig = ['sym', 'pr', 'dir', 'sc', 'type']
    for k in required_sig:
        if k not in sig or sig[k] is None:
            log(f"❌ validate: sig缺少字段 {k}")
            return False
    required_pos = ['sym', 'entry', 'size', 'dir']
    for k in required_pos:
        if k not in pos or pos[k] is None:
            log(f"❌ validate: pos缺少字段 {k}")
            return False
    if sig['pr'] <= 0 or pos['size'] <= 0:
        log(f"❌ validate: 无效价格/仓位 pr={sig['pr']} sz={pos['size']}")
        return False
    if sig['sym'] != pos['sym'] or sig['dir'] != pos['dir']:
        log(f"❌ validate: sig/pos方向不一致")
        return False
    # 2. RSI校验: 不在极端区域开反向单
    r15 = sig.get('r15', 50)
    if sig['dir'] == 'long' and r15 > 80:
        log(f"❌ validate: RSI={r15:.1f} 超买区不做多")
        return False
    if sig['dir'] == 'short' and r15 < 20:
        log(f"❌ validate: RSI={r15:.1f} 超卖区不做空")
        return False
    # 3. EMA校验: 价格与EMA(20)关系
    try:
        ex = exchange()
        if ex:
            o = fetch(ex.fetch_ohlcv, sig['sym'], '15m', limit=30)
            if o and len(o) >= 21:
                cl = [c[4] for c in o]
                ema20 = sum(cl[-20:]) / 20
                if sig['dir'] == 'long' and sig['pr'] < ema20 * 0.97:
                    log(f"❌ validate: 价格{sig['pr']:.2f}远低于EMA{ema20:.2f} → 不做多")
                    return False
                if sig['dir'] == 'short' and sig['pr'] > ema20 * 1.03:
                    log(f"❌ validate: 价格{sig['pr']:.2f}远高于EMA{ema20:.2f} → 不做空")
                    return False
    except Exception as e:
        log(f"⚠️ validate EMA校验异常: {e} → 放行")
    return True

def close_trade(pos, reason, exit_price=None):
    if not pos: return
    nm='?'
    try:
        p=pos; nm=p['sym'].split(':')[0]
        if exit_price is None: exit_price=price(p['sym'])
        if not exit_price or exit_price<=0:
            if time.time()-p.get('time', time.time())>1200: exit_price=p['entry']
            else: return
        # 测试网API平仓
        fill_price = sim_close_order(pos)
        if fill_price is None:
            log(f"❌ 测试网平仓API失败 → 保留持仓 {nm}")
            return
        exit_price = fill_price  # API返回的成交价
        if pos in state.positions: state.positions.remove(pos)
        pnl_pct=((exit_price-p['entry'])/p['entry']*100) if p['dir']=='long' else ((p['entry']-exit_price)/p['entry']*100)
        pnl_u=pnl_pct/100*p.get('size', 0)*LEVERAGE
        if abs(pnl_u)<0.0005: state.trades-=1; state.save(); return
        win=pnl_u>0
        if win: state.wins+=1; state.wins_list.append(pnl_u); state.consec_losses=0
        else: state.losses_list.append(pnl_u); state.consec_losses+=1
        d = p.get('dir','long')
        if not win:
            state.dir_losses[d] = state.dir_losses.get(d,0) + 1
        else:
            state.dir_losses[d] = 0
            if d in state.dir_melt: del state.dir_melt[d]
        if state.dir_losses.get(d,0) >= 3:
            state.dir_melt[d] = time.time() + 3600
            log(f"🛑 {d}方向连亏{state.dir_losses[d]}笔 → 暂停{d}方向1小时")
        # 滑动窗口记录
        state.consec_history.append((time.time(), win, p['dir']))
        if len(state.consec_history) > 50:
            state.consec_history = state.consec_history[-50:]
        if not win:
            if p['dir'] == 'long': state.consec_long_losses += 1
            else: state.consec_short_losses += 1
        else:
            if p['dir'] == 'long': state.consec_long_losses = 0
            else: state.consec_short_losses = 0
        st=p.get('type','?')
        # 分类统计
        state.main_pnl+=pnl_u
        if win: state.main_wins+=1; state.consec_losses=0
        else:
            state.main_losses += 1
            window_losses = sum(1 for h in state.consec_history if not h[1])
            cooldown_sec = CONSEC_COOLDOWN_BASE
            for (n, cd) in sorted(CONSEC_COOLDOWN_TIERS, reverse=True):
                if window_losses >= n:
                    cooldown_sec = cd; break
            if window_losses >= MAX_CONSEC_LOSSES:
                state.consec_melt_until=time.time()+cooldown_sec
                log(f"🛑 窗口连亏{window_losses}笔 → 冷却{cooldown_sec//60}分")
            if p['dir'] == 'long' and state.consec_long_losses >= 3:
                state.dir_melt_until['long'] = time.time() + 3600
                log(f"🚫 LONG连亏{state.consec_long_losses}笔 → 暂停做多1h")
            elif p['dir'] == 'short' and state.consec_short_losses >= 3:
                state.dir_melt_until['short'] = time.time() + 3600
                log(f"🚫 SHORT连亏{state.consec_short_losses}笔 → 暂停做空1h")
        d=state.sig_stats.setdefault(st,[0,0])
        d[0]+=1 if win else 0; d[1]+=0 if win else 1
        ep_rec = p.get('entry', 0)
        state.trade_history.append({'sym':nm,'dir':p['dir'],'entry':round(ep_rec, 2 if ep_rec > 1 else 5),
            'exit':round(exit_price,2),'pnl_pct':round(pnl_pct,2),'pnl_u':round(pnl_u,2),
            'type':st,'reason':reason,'time':ts(),'score':p.get('score',0)})
        if len(state.trade_history)>200: state.trade_history=state.trade_history[-200:]
        record_signal_result(st, win)
        state.last_trade=time.time()
        e='✅' if pnl_u>0 else '❌'
        log(f"{e} {p['dir']} {nm} {p['entry']:.2f}→{exit_price:.2f} PnL:{pnl_pct:+.2f}%×{LEVERAGE}x=${pnl_u:+.2f} [{reason}]")
        state.save()
    except Exception as _cte:
        log(f"💥 close_trade异常 [{reason}] | pos={nm} | err={str(_cte)[:80]}")
        state.save()
def check_positions():
    if not state.positions:
        # v27.4: state.positions为空时从API强制同步
        real = api_positions()
        if real:
            state.positions = real
            log(f"🔄 state.positions为空，从API恢复{len(real)}笔持仓")
    for p in list(state.positions):
        _check_one(p)

def _check_one(p):
    if not isinstance(p, dict):
        log(f"⚠️ _check_one收到非dict类型 {type(p).__name__} | 从持仓移除")
        if p in state.positions: state.positions.remove(p)
        state.save()
        return
    elapsed=time.time()-p.get('time', time.time())
    if 'time' not in p: p['time'] = time.time()  # 补time字段防崩溃
    pr=price(p['sym'])
    if not pr or pr<=0:
        if int(elapsed)%120<SCAN_INTERVAL and elapsed>60: log(f"⚠️ {p['sym']} 价格获取失败 {elapsed/60:.0f}m")
        return
    pnl_pct=((pr-p['entry'])/p['entry']*100) if p['dir']=='long' else ((p['entry']-pr)/p['entry']*100)
    if 'size' not in p and 'qty' in p: p['size'] = p['qty'] * p['entry'] / LEVERAGE
    usd=pnl_pct/100*p.get('size', p.get('qty',1))*LEVERAGE
    if usd>p.get('peak_usd',0): p['peak_usd']=usd

    # ── 主流币: 固定TP/SL + trailing stop ──
    max_sl_pct = MAX_LOSS_MAP.get(p.get('sym',''), 4.0)  # 默认4%
    if pnl_pct <= -max_sl_pct: close_trade(p, f"maxloss({pnl_pct:+.1f}%)", pr); return
    if pnl_pct>=TP: close_trade(p,"tp",pr); return
    if pnl_pct<=-SL: close_trade(p,"sl",pr); return
    if elapsed>TIMEOUT: close_trade(p,f"timeout({elapsed/60:.0f}m)",pr); return
    if elapsed>MAX_STUCK_MIN*60 and pnl_pct<STUCK_THRESHOLD: close_trade(p,f"stuck({elapsed/60:.0f}m)",pr); return

    pk=p.get('peak_usd',0)
    if not p.get('trail_active') and pk>=TRAIL_ACTIVATE_USD: p['trail_active']=True; log(f"🔒 {p['sym'].split(':')[0]} trail @${pk:.2f}")
    if p.get('trail_active'):
        dist=0.12
        for tm,td in sorted(TRAIL_TIERS, reverse=True):
            if pk>=tm: dist=td; break
        if usd<=pk-dist: close_trade(p,f"trail(${pk:.2f})",pr); return

    if int(elapsed/60)%2==0 and int((elapsed-SCAN_INTERVAL)/60)%2!=0:
        t='🔒' if p.get('trail_active') else ''; sf='⏳' if elapsed>MAX_STUCK_MIN*60 and pnl_pct<STUCK_THRESHOLD else ''
        bal_now = balance_fn() or 0
        log(f"📊 {p['dir']} {p['sym'].split(':')[0]} PnL:{pnl_pct:+.2f}%×{LEVERAGE}x ${usd:+.2f} | ${bal_now:.2f} {t}{sf}")

def main():
    global state
    # 从测试网API同步余额和持仓
    try:
        resp = _testnet_get_balance()
        if resp:
            free = float(resp.get("availableBalance", 0))
            if free > 1:
                log("🔗 余额: ${:.2f}".format(free))
                state.testnet_balance = free
                state.day_start_bal = free
        real_pos = api_positions()
        if real_pos:
            state.positions = real_pos
            for rp in real_pos:
                log("📌 " + rp["dir"] + " " + rp["sym"] + " qty=" + str(rp["qty"]) + " @" + str(round(rp["entry"],5)))
        log("📋 启动: ${:.2f} | {}笔".format(balance_fn() or 0, len(state.positions)))
    except Exception as e:
        log("⚠️ 启动同步失败: " + str(e)[:60])

    ev=load_evolve()
    mode_tag = '🦅 测试网API'
    gate_tag = '🌐 MacroGate ON' if MACRO_GATE else '🌐 MacroGate OFF'
    killed=[k for k,v in ev.get('fail_streaks',{}).items() if v>=3]
    log(f"🦅 v{EVOLVE_VERSION} | ${balance_fn() or 0:.2f} | {LEVERAGE}x | 6路信号 | {mode_tag} | {gate_tag}")
    if killed: log(f"   ⚠ 已降权: {', '.join(killed)}")

    import signal as sig
    shutdown_requested = False
    def shutdown(sn,fr):
        nonlocal shutdown_requested
        log("🛑 signal received → 设置停止标记(不退出)")
        shutdown_requested = True
    sig.signal(sig.SIGINT,shutdown); sig.signal(sig.SIGTERM,shutdown)

    if state.positions:
        for p in state.positions:
            log(f"📌 持仓: {p['dir']} {p['sym'].split(':')[0]} @{p['entry']:.5f}")
    sigs=scan()
    lr=time.time(); sc=1
    while True:
        try:
            if shutdown_requested:
                log("🛑 收到停止信号, 保留持仓退出")
                if state.positions:
                    for p in list(state.positions):
                        log(f"  保有: {p['dir']} {p['sym']} @{p.get('entry',0):.5f}")
                sys.exit(0)
            now=time.time(); sc+=1
            if now-state.day_start_t>86400 or state.day_start_bal<1:
                new_bal = balance_fn()
                if new_bal is not None and new_bal > 0:
                    state.day_start_bal = new_bal
                elif state.last_known_balance > 0:
                    state.day_start_bal = state.last_known_balance  # 用缓存
                else:
                    pass  # 保持旧值, 等API恢复
                state.day_start_t=now
                state.stopped=False
                log(f"🌅 新一天 | ${state.day_start_bal:.2f}")

            # v27: 日亏熔断(API容错) - 余额不可用时跳过检查
            if state.day_start_bal > 1:
                bal_now = balance_fn()
                if bal_now is None:
                    pass  # API不可用, 跳过本轮熔断检查
                elif bal_now > 0:
                    dp=(bal_now-state.day_start_bal)/state.day_start_bal
                    loss_usd = bal_now - state.day_start_bal
                    if (dp<=MAX_DAY_LOSS or loss_usd <= -MAX_LOSS_USD) and not state.stopped:
                        state.stopped=True; state.melt_until=float('inf')
                        log(f"⛔ 永久熔断 | ${bal_now:.2f} day_start:${state.day_start_bal:.2f}")
                    elif state.stopped and now>=state.melt_until and dp>MAX_DAY_LOSS+0.03:
                        state.stopped=False; log(f"✅ 恢复交易")

            if state.positions:
                check_positions()

            # v27: 滑动窗口连亏冷却 + 指数退避
            # 1. 清理过期记录(超过CONSEC_DECAY_SEC的)
            cutoff = now - CONSEC_DECAY_SEC
            state.consec_history = [h for h in state.consec_history if h[0] > cutoff]
            # 2. 重新计算窗口内连亏数
            window_losses = sum(1 for h in state.consec_history if not h[1])
            # 3. 连亏自动衰减: 如果最近1h内没有亏损, 重置计数器
            has_recent_loss = any(not h[1] for h in state.consec_history if h[0] > now - 3600)
            if not has_recent_loss and state.consec_losses > 0:
                state.consec_losses = 0
                state.consec_melt_until = 0
                state.consec_history.clear()
                log(f"🟢 连亏超过1h无新亏损 → 自动重置")
            # 4. 指数退避冷却: 根据窗口内连亏数查表
            cooldown_sec = CONSEC_COOLDOWN_BASE
            for (n, cd) in sorted(CONSEC_COOLDOWN_TIERS, reverse=True):
                if window_losses >= n:
                    cooldown_sec = cd; break
            # 5. 冷却检查
            if state.consec_melt_until > 0:
                if now >= state.consec_melt_until:
                    state.consec_losses = max(0, state.consec_losses - 1)  # 渐进恢复: 减1笔而非清零
                    state.consec_melt_until = 0
                    log(f"✅ 连亏冷却结束 (剩余连亏计数: {state.consec_losses})")
                elif state.consec_losses > 0 and int(now)%60 < SCAN_INTERVAL:
                    remaining = (state.consec_melt_until - now) / 60
                    log(f"🧊 连亏冷却中 {remaining:.0f}分 (窗口{window_losses}笔连亏, 共退避{cooldown_sec//60}分)")

            # v27: 方向熔断到期检查
            for d in ['long', 'short']:
                if state.dir_melt_until.get(d, 0) > 0 and now >= state.dir_melt_until[d]:
                    del state.dir_melt_until[d]
                    if d == 'long': state.consec_long_losses = 0
                    else: state.consec_short_losses = 0
                    log(f"✅ {d}方向熔断到期，恢复交易")

            # v27: API全挂检测 — 连续10次scan失败 → 暂停30分钟
            if sc > 10 and state.consec_scan_fails > 10:
                log(f"⛔ API持续不可达({state.consec_scan_fails}次) → 暂停30分钟")
                time.sleep(1800)
                state.consec_scan_fails = 0

            if len(state.positions)<MAX_POSITIONS and not state.stopped and not (0 < state.consec_melt_until > now):
                if now-state.last_trade>=COOLDOWN:
                    macro_ok, macro_reason = True, ''
                    if MACRO_GATE and sc % 6 == 0:
                        macro_ok, macro_reason = check_macro_gate()
                        if not macro_ok:
                            log(f"🌐 Macro Gate BLOCK: {macro_reason}")
                    sigs=scan()
                    for s in sigs:
                        if len(state.positions)>=MAX_POSITIONS: break
                        if not macro_ok:
                            continue
                        # v27: 同币同向5分钟冷却(防重复开单)
                        sym_dir_key = s['sym'] + s['dir']
                        recent_same = any(
                            t.get('sym','') == s['sym'].split(':')[0] and t.get('dir','') == s['dir']
                            and time.time() - _parse_trade_ts(t.get('time','')) < 300
                            for t in state.trade_history[-10:]
                        )
                        if recent_same:
                            if sc % 30 == 0:  # 减少日志噪音
                                log(f"⏱ {s['sym'].split(':')[0]} {s['dir']} 同向5min冷却 → 跳过")
                            continue
                        open_trade(s)
                    if not sigs and sc%10==0:
                        log(f"📭 扫{sc}轮 | ${balance_fn() or 0:.2f}")

            if now-lr>=1800:
                closed = len(state.wins_list)+len(state.losses_list)
                wr_calc = len(state.wins_list)/closed*100 if closed else 0
                log(f"💰 ${balance_fn() or 0:.2f} | {len(state.wins_list)}w(WR:{wr_calc:.0f}%)")
                lr=now

            # 写入价格快照 (v27.1: 加超时保护防卡死)
            try:
                ex_prices = exchange()
                if ex_prices and sc % 3 == 0:  # 每3轮写一次, 减少IO
                    prices_data = {"ts": time.time(), "data": {}}
                    sym_count = 0
                    for sym in SYMBOLS:
                        try:
                            t = ex_prices.fetch_ticker(sym)
                            if t and t.get('last', 0) > 0:
                                prices_data["data"][sym] = {
                                    'last': t['last'], 'bid': t.get('bid', 0),
                                    'ask': t.get('ask', 0), 'change': t.get('percentage', 0)
                                }
                                sym_count += 1
                        except Exception: pass  # ticker跳过
                    if sym_count > 0:
                        tmp_path = BOT_DIR / 'prices.json.tmp'
                        with open(tmp_path, 'w') as f:
                            json.dump(prices_data, f)
                        os.replace(tmp_path, BOT_DIR / 'prices.json')
            except Exception as pe:
                if sc % 60 == 0:
                    log(f"⚠️ prices写入异常: {str(pe)[:50]}")

            # 定期同步测试网余额
            if sc % 30 == 0:
                try:
                    bal = _testnet_get_balance()
                    if bal:
                        real_free = float(bal.get('availableBalance', 0))
                        if real_free > 1:
                            state.testnet_balance = real_free
                except: pass

            # 每分钟同步测试网持仓（merge，保留运行时字段）
            if sc % 6 == 0:
                try:
                    real_pos = api_positions()
                    if real_pos:
                        # merge: 保留 cat/trail/peak 等运行时字段
                        api_map = {}
                        for rp in real_pos:
                            api_map[rp['sym']+rp['dir']] = rp
                        for existing in state.positions:
                            key = existing.get('sym','') + existing.get('dir','')
                            api_p = api_map.get(key)
                            if api_p:
                                for f in ('entry','mark','pnl_u','qty','size','time'):
                                    if f in api_p:
                                        existing[f] = api_p[f]
                        # 添加API有但state没有的新持仓
                        state_keys = set(p.get('sym','')+p.get('dir','') for p in state.positions)
                        for key, rp in api_map.items():
                            if key not in state_keys:
                                state.positions.append(rp)
                except: pass

            # 每20分钟系统巡检
            if sc % 120 == 0 and sc > 0:
                session_closed = len(state.wins_list) + len(state.losses_list)
                session_wr = len(state.wins_list)/session_closed*100 if session_closed else 0
                log(f"🔍 巡检 #{sc//120}: Bot:🟢 | 余额:${balance_fn() or 0:.0f} | 持仓:{len(state.positions)} | 今日:{session_closed}笔 WR:{session_wr:.0f}% | 熔断:{'❌' if state.stopped else '✅'}")

            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt: shutdown(None,None)
        except Exception as e:
            crash_count = getattr(main, '_crash_count', 0) + 1
            main._crash_count = crash_count
            log(f"💥 崩溃#{crash_count}: {str(e)[:120]}")
            # v27.1: 自愈 — 缺失属性自动补充
            for attr_name, attr_default in [
                ('consec_scan_fails', 0), ('consec_losses', 0), ('consec_melt_until', 0),
                ('consec_history', []), ('consec_long_losses', 0), ('consec_short_losses', 0),
                ('dir_losses', {}), ('dir_melt', {}), ('dir_melt_until', {}),
                ('bal_fault_count', 0), ('last_known_balance', 0.0),
                ('main_pnl', 0.0),
                ('main_trades', 0), ('main_wins', 0),
                ('trades', 0), ('wins', 0), ('stopped', False), ('sig_stats', {}),
                ('day_start_bal', 0.0), ('day_start_t', time.time()),
                ('trade_history', []), ('startup_time', time.time()),
                ('main_losses', 0),  # v27.3
            ]:  # v27.3: 补全自愈字段
                if not hasattr(state, attr_name):
                    setattr(state, attr_name, attr_default)
                    log(f"🩹 自愈: state.{attr_name} → {attr_default}")
            # 指数退避: 连崩超过5次, 等更久 (v27.3: 轮询支持shutdown)
            if crash_count <= 5:
                wait = min(30 * (2 ** (crash_count - 1)), 300)
            else:
                wait = 600
            log(f"⏳ 崩溃退避 {wait}s (第{crash_count}次)")
            for _ in range(min(wait, 600)):
                if shutdown_requested:
                    log("🛑 退避期间收到停止信号")
                    sys.exit(0)
                time.sleep(1)
if __name__=='__main__':
    import fcntl
    LOCK_FILE = BOT_DIR / "trader.lock"
    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        try:
            old_pid = LOCK_FILE.read_text().strip()
        except: old_pid = '?'
        print(f"❌ 另一个 trader 实例已在运行 (PID {old_pid})")
        sys.exit(1)
    lock_fd.write(str(os.getpid())); lock_fd.flush()
    os.environ['PAPERBOT_NO_SHUTDOWN_SELL']='1'
    state.startup_time=time.time()
    main()
