#!/usr/bin/env python3
"""
🦅 PaperBot v27 — 6路信号 | 20x | 满仓单吊 | 趋势过滤
标的: BTC/ETH/DOGE | 20x固定杠杆 | 动态仓位 | 时间止损
"""
import ccxt, time, sys, json, os, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field

# 🔬 量化模块
sys.path.insert(0, str(Path(__file__).parent))
from quant.hmm_regime import HMMRegime
from quant.volatility import VolatilityModel
from quant.var_manager import VaRManager

BOT_DIR = Path(__file__).parent
LOG_DIR = BOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
TZ = timezone(timedelta(hours=8))
PROXY = "http://YOUR_HOST_IP:7897"
STATE_FILE = BOT_DIR / "trader_state.json"
EVOLVE_FILE = BOT_DIR / "evolve_v25.json"
LOG_FILE = LOG_DIR / "trader_log.txt"
MAX_LOG_LINES = 500  # 日志轮转上限

# ─── 参数 ───
START_BALANCE, LEVERAGE = 40.0, 20  # 固定20x，杠杆越大越亏

def dynamic_leverage(sc):
    return 20  # 固定20x

# ─── 满仓单吊 (留5%缓冲) ───
def dynamic_size_mult(sc):
    """v27满仓: 95%可用余额做保证金"""
    return 0.95
SCAN_INTERVAL, COOLDOWN, TIMEOUT = 10, 30, 3600
TP, SL = 2.0, 0.4  # 盈亏比5:1，扣0.08%手续费后仍有净利

# ─── 自适应止损：20x专版 ───
def adaptive_sl(sc):
    """20x下止损：sc≧75→宽, sc≧60→中, <60→紧"""
    if sc >= 75: return (1.0, 2.50)   # 强信号给1%价格空间
    elif sc >= 60: return (0.8, 1.50)
    else: return (0.5, 1.00)
TRAIL_ACTIVATE_USD = 1.00  # 覆盖手续费($0.27-0.35)后仍有安全垫
TRAIL_TIERS = [(5.0, 1.50), (3.0, 0.80), (2.0, 0.50), (1.0, 0.30), (0.5, 0.20)]  # 每档留利润
MAX_DAY_LOSS, MAX_DD = -0.10, 0.15
MAX_LOSS_USD = 5.0    # 绝对亏损$5 立即永久熔断
MAX_SINGLE_LOSS = 1.50  # 硬止损 > trail激活, 确保追迹先启动
MAX_STUCK_MIN, STUCK_THRESHOLD = 60, -0.15
TIME_STOP_MIN = 15  # 15分钟内不盈利就砍
MAX_CONSEC_LOSSES = 2  # v26: 连续亏损阈值
CONSEC_COOLDOWN = 1800  # v26: 连亏熔断冷却30分钟
MIN_SCORE = 50  # 降低门槛，趋势过滤已兜底
EVOLVE_VERSION = 27  # v27: 固定20x, 满仓单吊, 趋势过滤, 时间止损, 相关性过滤

SYMBOLS = ["BTC/USDT:USDT","ETH/USDT:USDT","DOGE/USDT:USDT"]
TIERS = {"BTC/USDT:USDT":0.80,"ETH/USDT:USDT":0.80,"DOGE/USDT:USDT":0.60}
# ─── 山寨币投机通道 ───
# SOL移入妖币池（波动=妖币级别）
# 山寨币用 ATR 宽止损 (不做固定%), 顺势动量打法
# 山寨币移动止盈
# ─── 妖币扫描 ───

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
        with open(tmp, 'w') as f: json.dump(data, f)
        f.flush(); os.fsync(f.fileno())
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
MAX_POSITIONS = 1  # 满仓单吊
MAX_MAIN = 1
MAX_ALT = 3        # 最多3个山寨/妖币 (总仓位≤$10硬上限)
@dataclass
class State:
    balance: float = START_BALANCE; peak: float = START_BALANCE
    positions: list = field(default_factory=list)
    trades: int = 0; wins: int = 0; pnl: float = 0.0
    wins_list: list = field(default_factory=list); losses_list: list = field(default_factory=list)
    trade_history: list = field(default_factory=list)
    last_trade: float = 0; melt_until: float = 0
    day_start_bal: float = START_BALANCE; day_start_t: float = field(default_factory=time.time)
    stopped: bool = False; sig_stats: dict = field(default_factory=dict)
    startup_time: float = field(default_factory=time.time)
    consec_losses: int = 0; consec_melt_until: float = 0  # 连亏熔断
    # 主流统计
    main_pnl: float = 0.0
    main_trades: int = 0; main_wins: int = 0
    margin_warn_cooldown: dict = field(default_factory=dict)  # {sym: last_warn_ts}
    _vol_mult: float = 1.0  # 波动率仓位乘数 保证金不足警告冷却

    @property
    def wr(self):
        closed = len(self.wins_list) + len(self.losses_list)
        return len(self.wins_list)/closed if closed else 0.5

    def save(self):
        for retry in range(3):
            try:
                d={'v':EVOLVE_VERSION,'balance':self.balance,'peak':self.peak,'trades':self.trades,
                   'wins':self.wins,'pnl':self.pnl,'wins_list':self.wins_list,
                   'losses_list':self.losses_list,'trade_history':self.trade_history,
                   'last_trade':self.last_trade,'melt_until':self.melt_until,
                   'day_start_balance':self.day_start_bal,'day_start_time':self.day_start_t,
                   'stopped':self.stopped,'signal_stats':self.sig_stats,
                   'positions':self.positions,'startup_time':self.startup_time,
                   'consec_losses':self.consec_losses,'consec_melt_until':self.consec_melt_until,
                   'main_pnl':self.main_pnl,
                   'main_trades':self.main_trades,'main_wins':self.main_wins,
                   'margin_warn_cooldown':getattr(self,'margin_warn_cooldown',{})}
                # 🔧 原子写入: tmp → fsync → rename
                tmp=str(STATE_FILE)+'.tmp'
                with open(tmp,'w') as f:
                    json.dump(d,f)
                    f.flush(); os.fsync(f.fileno())
                try: os.replace(tmp,str(STATE_FILE))
                except FileNotFoundError: pass
                return
            except Exception as e:
                if retry == 2: log(f"⚠️ save(retry{retry}): {e}")
                else: time.sleep(0.05)

    @classmethod
    def load(cls):
        s=cls()
        if not STATE_FILE.exists(): return s
        try:
            with open(STATE_FILE) as f: d=json.load(f)
            # 简单值: 用 default_value 兜底; dict/list 类型单独处理
            SIMPLE_KEYS = ['balance','peak','trades','wins','pnl','last_trade','day_start_balance',
                           'day_start_time','stopped','startup_time','melt_until',
                           'consec_losses','consec_melt_until',
                           'main_pnl','main_trades','main_wins']
            for k in SIMPLE_KEYS:
                setattr(s, k, d.get(k, getattr(s, k, 0)))
            DICT_KEYS = ['margin_warn_cooldown']
            for k in DICT_KEYS:
                val = d.get(k, {})
                if isinstance(val, dict):
                    setattr(s, k, val)
                else:
                    setattr(s, k, {})
            # 同步 day_start_balance → day_start_bal（文件key ≠ 类属性名）
            s.day_start_bal = getattr(s, 'day_start_balance', s.day_start_bal)
            for k in ['wins_list','losses_list','trade_history','sig_stats']:
                setattr(s, k, d.get(k, [] if k!='sig_stats' else {}))
            if 'positions' in d: s.positions=d['positions']
            elif 'position' in d and d['position']:  # 迁移旧单持仓
                p=d['position']; p['trail_active']=False; p['peak_usd']=0
                s.positions=[p]
        except: pass
        # 🔧 v25.1: 确保dict类型字段不会被意外覆盖为int
        for dk in ['margin_warn_cooldown', 'sig_stats']:
            if not isinstance(getattr(s, dk, {}), dict):
                setattr(s, dk, {})
        # ── 从 trade_history 反算所有统计（防重启丢失）──
        s.trades = len(s.trade_history) + len(s.positions)
        s.wins = len(s.wins_list)
        s.pnl = sum(s.wins_list) + sum(s.losses_list)
        # 反算分类统计
        s.main_pnl = s.alt_pnl = s.yaobi_pnl = 0.0
        s.main_wins = s.alt_wins = s.yaobi_wins = 0
        s.main_trades = s.alt_trades = s.yaobi_trades = 0
        # 🔧 v25.1: 不再在每次load时重置风控状态，只在真正的新一天重置
        # 连亏计数从trade_history反算（下面循环中处理）
        s.alt_losses = s.yaobi_losses = 0  # 先清零，下面反算
        for t in s.trade_history:
            pnl = t.get('pnl_u', 0)
            typ = t.get('type', '')
            sym = t.get('sym', '')
            # 推断分类: 妖>寨>主
            if '妖' in typ:
                s.yaobi_pnl += pnl; s.yaobi_trades += 1
                if pnl > 0: s.yaobi_wins += 1
            elif '山寨' in typ or 'NEAR' in sym or 'AVAX' in sym or 'LINK' in sym or 'APT' in sym or 'ARB' in sym:
                s.alt_pnl += pnl; s.alt_trades += 1
                if pnl > 0: s.alt_wins += 1
            elif sym in ('SOL/USDT',):
                s.alt_pnl += pnl; s.alt_trades += 1
                if pnl > 0: s.alt_wins += 1
            else:
                s.main_pnl += pnl; s.main_trades += 1
                if pnl > 0: s.main_wins += 1
        return s

state = State.load()

# ─── Exchange ───
_ex = None
_ex_fails = 0  # 连续失败计数器

_ex_backoff_until = 0  # 连接失败后冷却时间戳

def exchange(force=False):
    global _ex, _ex_fails, _ex_backoff_until
    now = time.time()
    # 连接失败冷却中，直接返回None
    if not force and _ex is None and _ex_fails > 0 and now < _ex_backoff_until:
        return None
    if force or _ex is None or _ex_fails > 6:
        if _ex is not None:
            try: del _ex
            except: pass
        _ex = None
        _ex_fails = 0
        for attempt in range(3):
            try:
                _ex=ccxt.binance({'apiKey':BINANCE_KEY,'secret':BINANCE_SECRET,
                    'enableRateLimit':True,'options':{'defaultType':'future'},
                    'proxies':{'http':PROXY,'https':PROXY},'timeout':8000})
                _ex.load_markets()
                _ex_backoff_until = 0  # 成功则清除冷却
                log(f"📡 {len(_ex.markets)}对" + (" [reconnect]" if force or attempt>0 else ""))
                break
            except Exception as e:
                log(f"⚠️ conn{attempt+1}: {str(e)[:60]}")
                _ex = None
                if attempt == 2:
                    _ex_backoff_until = now + 60  # 最后尝试失败，冷却60秒
                    log(f"🧊 连接失败3次，冷却60秒")
                else:
                    time.sleep(2 ** attempt)  # 指数退避: 1s, 2s
    return _ex

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
                time.sleep(min(1.0 ** i + 0.5, 4))  # 增量退避
    _ex_fails += 2  # 全部失败大幅加罚，触发外部重建
    return 0

# ─── 实盘交易函数 ───
BINANCE_KEY = os.environ.get('BINANCE_KEY',os.environ.get('BINANCE_KEY', 'YOUR_API_KEY'))
BINANCE_SECRET = os.environ.get('BINANCE_SECRET',os.environ.get('BINANCE_SECRET', 'YOUR_SECRET'))

def real_open_order(sig, pos):
    """实盘开仓, 返回实际成交均价, 失败返回None"""
    try:
        ex=exchange()
        if not ex: return None
        sym = sig['sym']
        try: ex.set_leverage(LEVERAGE, sym)
        except: pass
        try:
            cur_pos = ex.fetch_positions([sym])
            for cp in cur_pos:
                if not isinstance(cp, dict): continue
                c = abs(float(cp.get('contracts',0) or 0))
                if c < 0.001: continue
                cur_side = cp.get('side','')
                if cur_side != sig['dir']:
                    cls = 'sell' if cur_side == 'long' else 'buy'
                    ex.create_market_order(sym, cls, c, {'reduceOnly': True})
                    log(f"🔄 平反向仓位: {cur_side} {sym.split(':')[0]} {c}张")
        except Exception as e:
            log(f"⚠️ 检查持仓异常: {str(e)[:60]}")
        side='buy' if sig['dir']=='long' else 'sell'
        lev = pos.get('leverage', LEVERAGE)
        try: ex.set_leverage(lev, sig['sym'])
        except: pass
        amount=pos['size']*lev/sig['pr']  # margin×杠杆=仓位
        # 最小名义价值$5 + 最小合约精度
        min_qty = 0.001 if 'BTC' in sig['sym'] else (1 if 'DOGE' in sig['sym'] else 0.01)
        if amount*sig['pr']<5.1: amount=5.1/sig['pr']
        amount = max(amount, min_qty)
        amount=round(amount, 0) if amount>=1 else amount
        order=ex.create_market_order(sig['sym'], side, amount)
        fill_price = float(order.get('average', order.get('price', sig['pr'])))
        log(f"🔴 实盘开仓: {side} {sig['sym'].split(':')[0]} {amount}个 @{fill_price:.5f}")
        pos['qty'] = amount  # 记住开仓数量，平仓用同一个
        return fill_price
    except Exception as e:
        log(f"💥 实盘开仓失败: {str(e)[:100]}")
        return None

def real_close_order(pos, reason):
    """实盘平仓, 返回实际成交均价, 失败返回None"""
    try:
        ex=exchange()
        if not ex: return None
        side='sell' if pos['dir']=='long' else 'buy'
        lev = pos.get('leverage', LEVERAGE)
        # 优先用开仓时记录的数量，避免浮点截断残留
        open_qty = pos.get('qty')
        if open_qty and open_qty > 0:
            amount = open_qty
        else:
            amount=pos['size']*lev/pos['entry']
            amount=round(amount, 0) if amount>=1 else amount
        order=ex.create_market_order(pos['sym'], side, amount, {'reduceOnly': True})
        fill_price = float(order.get('average', order.get('price', pos['entry'])))
        log(f"🔴 实盘平仓: {side} {pos['sym'].split(':')[0]} {amount}个 @{fill_price:.5f} [{reason}]")
        return fill_price
    except Exception as e:
        log(f"💥 实盘平仓失败: {str(e)[:100]}")
        return None

def real_balance():
    """U本位合约可用余额（free，不含已锁保证金）"""
    try:
        ex=exchange()
        if not ex: return 0
        bal = ex.fetch_balance({'type': 'swap'}).get('USDT',{})
        return float(bal.get('free', 0))
    except: return 0

# ─── Logging ───
def ts(): return datetime.now(TZ).strftime("%m-%d %H:%M:%S")
def log(msg):
    line=f"[{ts()}] {msg}"
    print(line,flush=True)
    try:
        with open(LOG_FILE,'a') as f: f.write(line+'\n')
        # 🔧 高效轮转: 只在文件过大时截断
        if f.tell() > MAX_LOG_LINES * 200:
            with open(LOG_FILE,'r') as fr:
                lines = fr.readlines()
            with open(LOG_FILE,'w') as fw:
                fw.writelines(lines[-MAX_LOG_LINES:])
    except: pass

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
    if direction=='long' and w_change>0.03 and momentum>0:
        return min(w_change*100*0.8, 20)
    elif direction=='short' and w_change<-0.03 and momentum<0:
        return min(abs(w_change)*100*0.8, 20)
    # 弱趋势 + 顺势 → 小加分
    elif direction=='long' and w_change>0:
        return min(w_change*100*0.3, 8)
    elif direction=='short' and w_change<0:
        return min(abs(w_change)*100*0.3, 8)
    # 逆势 → 降分
    elif direction=='long' and w_change<-0.02:
        return -10
    elif direction=='short' and w_change>0.02:
        return -10
    # 高波动 + 逆势 = 更大降分
    if vol>0.05 and ((direction=='long' and w_change<0) or (direction=='short' and w_change>0)):
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
    if vr > 2.5: return base_tier * 0.6   # 极端波动 → 降40%
    elif vr > 1.8: return base_tier * 0.8
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
    if direction=='long' and w_change>0: trend_score = min(w_change*100, 20)
    elif direction=='short' and w_change<0: trend_score = min(abs(w_change)*100, 20)
    elif direction=='long' and w_change<0: trend_score = max(w_change*100, -20)
    elif direction=='short' and w_change>0: trend_score = max(-w_change*100, -20)
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
    if not ex: return []
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
            
            # 4h趋势过滤: 价格 vs EMA(20)
            o4=fetch(ex.fetch_ohlcv,sym,'4h',limit=30)
            trend_up = None  # None=趋势未知，两端信号都跳过
            if o4 and len(o4)>=21:
                ema4 = sum(c[4] for c in o4[-20:])/20
                trend_up = pr > ema4  # 价格在EMA上方=上升趋势

            # LONG 信号 (需要量 + 趋势确认)
            ok,s=divergence(cl,'bullish')
            if ok and r15<58 and r1h<62 and trend_up is True:
                if not vol_ok: pass  # 背离无量直接跳过
                else:
                    sc=s*2.5+(58-r15)*0.8
                    sc*=signal_weight('RSI背离(L)')
                    sigs.append({'sym':sym,'pr':pr,'dir':'long','sc':round(sc,1),'type':'RSI背离(L)','r15':round(r15,1),'r1h':round(r1h,1),'vol_r':round(vol_ratio,2)})
            if r15<35:
                # 🔧 RSI拐头确认: 当前RSI > 前一根RSI (拒绝继续下跌)
                r15_prev = rsi(cl[:-1]) if len(cl)>20 else r15
                if r15 <= r15_prev and r15 < 25:
                    log(f"⏸️ {sym.split(':')[0]} r15={r15:.1f} 仍在下跌 → 等拐头")
                    pass  # 跳过，等RSI止跌回升
                else:
                    log(f"🔔 {sym.split(':')[0]} r15={r15:.1f}<35 超卖! sc={(35-r15)*4+15:.0f}")
                    sigs.append({'sym':sym,'pr':pr,'dir':'long','sc':round((35-r15)*4+15,1),'type':'超卖反转','r15':round(r15,1),'r1h':round(r1h,1)})
            if r1h<42 and r15>r1h+5 and r15<50 and ma200 and pr>ma200:
                sc=((42-r1h)*1.5+(r15-r1h)*2+5)*signal_weight('多TF均值回归')
                sigs.append({'sym':sym,'pr':pr,'dir':'long','sc':round(sc,1),'type':'多TF均值回归','r15':round(r15,1),'r1h':round(r1h,1)})

            # SHORT 信号 (需要量 + 趋势确认)
            ok,s=divergence(cl,'bearish')
            if ok and r15>42 and r1h>38 and trend_up is False:
                if not vol_ok: pass  # 背离无量直接跳过
                else:
                    sc=s*2.5+(r15-42)*0.8
                    sc*=signal_weight('RSI背离(S)')
                    sigs.append({'sym':sym,'pr':pr,'dir':'short','sc':round(sc,1),'type':'RSI背离(S)','r15':round(r15,1),'r1h':round(r1h,1),'vol_r':round(vol_ratio,2)})
            if r15>65:
                log(f"🔔 {sym.split(':')[0]} r15={r15:.1f}>65 超买! sc={(r15-65)*4+15:.0f}")
                sigs.append({'sym':sym,'pr':pr,'dir':'short','sc':round((r15-65)*4+15,1),'type':'超买反转','r15':round(r15,1),'r1h':round(r1h,1)})
            if r1h>58 and r15<r1h-5 and r15>50:
                sc=((r1h-58)*1.5+(r1h-r15)*2+5)*signal_weight('多TF均值回归')
                sigs.append({'sym':sym,'pr':pr,'dir':'short','sc':round(sc,1),'type':'多TF均值回归','r15':round(r15,1),'r1h':round(r1h,1)})
            
            # ── 横盘专用: BB反弹 (RSI中性区间 + 低波动) ──
            if 40<r15<60 and 38<r1h<62:
                mid, upper, lower, bbw = bollinger(cl)
                if mid and bbw>0 and bbw<4:
                    if pr <= lower*1.001 and r15>38:
                        sc = 25 + (lower-pr)/lower*100*80 + (r15-38)*1.0
                        if vol_ok: sc *= 1.3
                        sigs.append({'sym':sym,'pr':pr,'dir':'long','sc':round(sc,1),'type':'BB反弹(L)','r15':round(r15,1),'r1h':round(r1h,1),'vol_r':round(vol_ratio,2)})
                    elif pr >= upper*0.999 and r15<62:  # elif防止同币种双向触发
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

    # 🔧 顺势过滤: 周跌→只做空, 周涨→只做多, 横盘→双开
    wb_data = wb
    def trend_ok(s):
        chg = wb_data.get(s['sym'].split('/')[0],{}).get('change', 0)
        name = s['sym'].split('/')[0]
        if chg < -0.03 and s['dir'] == 'long':
            log(f"🚫 {name} 周跌{chg*100:.0f}% → 只做空不做多")
            return False
        if chg > 0.03 and s['dir'] == 'short':
            log(f"🚫 {name} 周涨{chg*100:.0f}% → 只做多不做空")
            return False
        return True
    sigs = [s for s in sigs if trend_ok(s)]

    sigs.sort(key=lambda x:-x['sc'])
    seen={}
    for s in sigs:
        k=(s['sym'],s['dir'])
        if k not in seen: seen[k]=s
    # 止损冷却过滤 (freqtrade StopLossGuard学来)
    filtered = []
    for s in sorted(seen.values(), key=lambda x:-x['sc']):
        on_cd, cd_min = sl_cooldown(s['sym'], s['dir'])
        if on_cd:
            log(f"⏳ {s['sym']} {s['dir']} 止损冷却中 ({cd_min:.0f}分) → 跳过")
            continue
        filtered.append(s)
    return sorted([s for s in filtered if s['sc']>=MIN_SCORE], key=lambda x:-x['sc'])

# 📝 扫描汇总留痕
def _log_scan_summary(sigs, filtered, min_score):
    total = len(sigs)
    above = sum(1 for s in sigs if s['sc'] >= min_score)
    below = total - above
    if total > 0 and above == 0:
        top3 = sorted(sigs, key=lambda x:-x['sc'])[:3]
        names = ', '.join(f"{s['sym'].split(':')[0]}({s['sc']:.0f})" for s in top3)
        log(f"📊 扫描: {total}信号 过{min_score}分:0 | TOP3[{names}] 全部未达标")

# ─── 山寨币扫描 (简化版: RSI极值+量确认) ───
def open_trade(sig):
    log(f"🔴 open_trade called: {sig['dir']} {sig['sym']} [{sig['type']}] sc:{sig['sc']}")
    # 🚫 硬白名单: 只允许主流币
    sym_raw = sig['sym'].split(':')[0].replace('/','')
    if sym_raw not in ('BTCUSDT','ETHUSDT','DOGEUSDT'):
        log(f"🚫 {sig['sym']} 非白名单 → 跳过")
        return
    # 仓位上限 + 防同币同向重复
    if len(state.positions)>=MAX_POSITIONS: return
    key=(sig['sym'],sig['dir'])
    for p in state.positions:
        if (p['sym'],p['dir'])==key: return
    # 🔧 相关性过滤: BTC+ETH 不同向开双仓
    sym_name = sig['sym'].split('/')[0]
    if sym_name in ('BTC','ETH'):
        other = 'ETH/USDT:USDT' if sym_name=='BTC' else 'BTC/USDT:USDT'
        for p in state.positions:
            if p['sym']==other and p['dir']==sig['dir']:
                log(f"🔗 {sym_name} {sig['dir']} 已有 {other.split('/')[0]} 同向 → 跳过")
                return
    cat='main'
    # 分类上限检查
    main_n = sum(1 for p in state.positions if p.get('cat')=='main')
    if main_n>=MAX_MAIN: return
    tier = TIERS.get(sig['sym'],0.40)
    # 动态仓位: 高波动降仓
    ex2=exchange()
    if ex2:
        o2=fetch(ex2.fetch_ohlcv,sig['sym'],'15m',limit=30)
        if o2:
            tier = dynamic_tier(sig['sym'],[c[4] for c in o2],[c[5] for c in o2])
    afternoon_mult = 0.5 if is_afternoon() else 1.0
    sz_raw = round(min(state.balance, real_balance()) * dynamic_size_mult(sig['sc']) * getattr(state, '_vol_mult', 1.0), 2)
    # 🔬 VaR风控: 仓位不超过VaR限制
    var_limit = getattr(state, '_var_limit', state.balance)
    sz_raw = min(sz_raw, var_limit)
    sz=sz_raw
    min_sz = max(state.balance * 0.08, 3.0)  # 最低仓位=余额8%, 硬下限$3
    # BTC最低精度0.001 → 需margin≥$3.93(20x)
    if 'BTC' in sig['sym']: min_sz = max(min_sz, 4.0)
    if sz<min_sz: sz=round(min_sz,2)
    pos={'sym':sig['sym'],'entry':sig['pr'],'size':sz,'time':time.time(),
        'dir':sig['dir'],'type':sig['type'],'score':sig['sc'],'cat':cat,
        'leverage':dynamic_leverage(sig['sc']),
        'sl_pct':adaptive_sl(sig['sc'])[0], 'max_loss':adaptive_sl(sig['sc'])[1],
        'trail_active':False,'peak_usd':0}
    # 开仓前检查保证金
    total_margin_needed = sum(p['size'] for p in state.positions) + sz
    free_bal = real_balance()
    # 开仓前检查保证金（满仓模式：sz已按free_bal限制，跳过）
    if False and free_bal is not None and total_margin_needed > free_bal:
        sym_short = sig['sym'].split(':')[0]
        now = time.time()
        mwc = getattr(state, 'margin_warn_cooldown', None)
        if not isinstance(mwc, dict):
            state.margin_warn_cooldown = {}
            mwc = state.margin_warn_cooldown
        last_warn = mwc.get(sym_short, 0)
        if now - last_warn > 300:
            log(f"⚠️ 保证金不足: 需{total_margin_needed:.1f}U 可用{free_bal:.1f}U | 跳过 {sym_short}")
            state.margin_warn_cooldown[sym_short] = now
        return
    state.positions.append(pos)
    state.last_trade=time.time(); state.trades+=1
    log(f"🔫 开单确认: {sig['dir']} {sig['sym']} sz:{sz}U sc:{sig['sc']} [{sig['type']}]")
    # 📝 策略决策完整日志
    sym_name = sig['sym'].split(':')[0]
    wb_data = wb_fresh(exchange()) if exchange() else {}
    wb_coin = wb_data.get(sym_name, {})
    wb_chg = wb_coin.get('change', 0) if wb_coin else 0
    trend_info = f"周{'+' if wb_chg>=0 else ''}{wb_chg*100:.0f}%"
    regime_info = sig.get('hmm_regime', '?')
    regime_names = ['🐻跌','📊横','🐂涨']
    regime_str = regime_names[min(int(regime_info) if isinstance(regime_info, (int,float)) else 1, 2)]
    var_limit = getattr(state, '_var_limit', 0)
    vol_mul = getattr(state, '_vol_mult', 1.0)
    log(f"📋 策略决策: {sym_name} {sig['dir'].upper()} | se:{sig['sc']:.1f} | 20x | {sig['type']} | {trend_info} | HMM:{regime_str} | RSI15:{sig.get('r15','?')} | VaR:{var_limit:.1f} | VolMul:{vol_mul:.1f}")
    # 🔧 费用检查: 预期利润必须覆盖手续费
    fee_estimate = sz * LEVERAGE * 0.0008
    min_profit_needed = fee_estimate * 1.5  # 至少覆盖1.5倍手续费才有意义
    log(f"📋 费用评估: 手续费${fee_estimate:.2f} | 保本需利润≥${min_profit_needed:.2f} | 余额:{state.balance:.2f}")
    fill_price = real_open_order(sig, pos)
    if fill_price is None:
        log(f"💥 开单API失败 → 回滚")
        state.positions.pop(); state.trades-=1; state.save(); return
    pos['entry'] = fill_price
    state.main_trades+=1
    em='🚀' if sig['dir']=='long' else '🔻'
    vi=f" vol:{sig.get('vol_r',1):.1f}x" if 'vol_r' in sig else ""
    chg_info = f" 24h:{sig['chg']:+.0f}%" if sig.get('chg') else ""
    log(f"🔷{em} {sig['dir'].upper()} {sig['sym'].split(':')[0]} @{sig['pr']:.2f} x{pos.get('leverage',LEVERAGE)} {sz}U | {sig['type']} sc:{sig['sc']:.1f}{vi}{chg_info} [{len(state.positions)}/{MAX_POSITIONS}]")
    state.save()

def close_trade(pos, reason, exit_price=None):
    if not pos: return
    nm='?'
    try:
        p=pos; nm=p['sym'].split(':')[0]
        if exit_price is None: exit_price=price(p['sym'])
        if not exit_price or exit_price<=0:
            if time.time()-p['time']>1200: exit_price=p['entry']
            else: return
        # 实盘平仓, 用实际成交价替代OHLCV价格
        fill_price = real_close_order(pos, reason)
        if fill_price is not None:
            exit_price = fill_price
        else:
            # ⚠️ 平仓API失败：仓位可能还在交易所，恢复持仓
            log(f"⚠️ 平仓失败 [{reason}] → 恢复持仓")
            state.positions.append(pos)
            state.trades -= 1
            return
        # 先从持仓列表移除
        if pos in state.positions: state.positions.remove(pos)
        pnl_pct=((exit_price-p['entry'])/p['entry']*100) if p['dir']=='long' else ((p['entry']-exit_price)/p['entry']*100)
        pnl_u=pnl_pct/100*p['size']*p.get('leverage', LEVERAGE)
        # 🔧 扣除双向手续费 (0.04%×2=0.08% 名义价值)
        fee_rt = p['size'] * p.get('leverage', LEVERAGE) * 0.0008
        pnl_u -= fee_rt
        if abs(pnl_u)<0.0005: state.trades-=1; state.save(); return
        state.balance+=pnl_u; state.pnl+=pnl_u
        win=pnl_u>0
        if win: state.wins+=1; state.wins_list.append(pnl_u); state.consec_losses=0
        else: state.losses_list.append(pnl_u); state.consec_losses+=1
        st=p.get('type','?')
        # ── 统计 ──
        state.main_pnl+=pnl_u
        if win: state.main_wins+=1; state.consec_losses=0
        else:
            if state.consec_losses >= MAX_CONSEC_LOSSES:
                state.consec_melt_until=time.time()+CONSEC_COOLDOWN
                log(f"🛑 连亏{state.consec_losses}笔 → 冷却{CONSEC_COOLDOWN//60}分")
        d=state.sig_stats.setdefault(st,[0,0])
        d[0]+=1 if win else 0; d[1]+=0 if win else 1
        state.trade_history.append({'sym':nm,'dir':p['dir'],'entry':round(p['entry'],2),
            'exit':round(exit_price,2),'pnl_pct':round(pnl_pct,2),'pnl_u':round(pnl_u,2),
            'type':st,'reason':reason,'time':ts(),'score':p.get('score',0)})
        if len(state.trade_history)>200: state.trade_history=state.trade_history[-200:]
        record_signal_result(st, win)
        state.last_trade=time.time()
        if state.balance>state.peak: state.peak=state.balance
        e='✅' if pnl_u>0 else '❌'
        log(f"{e} {p['dir']} {nm} {p['entry']:.2f}→{exit_price:.2f} PnL:{pnl_pct:+.2f}%×{p.get('leverage',LEVERAGE)}x=${pnl_u:+.2f} [{reason}]")
        # 📝 平仓复盘留痕
        fee_detail = p['size'] * p.get('leverage', LEVERAGE) * 0.0008
        log(f"📋 平仓复盘: {nm} {p['dir']} | 开{p['entry']:.4f} 平{exit_price:.4f} | 毛利${pnl_u+fee_detail:+.2f} 费${fee_detail:.2f} 净${pnl_u:+.2f} | sc:{p.get('score','?')} [{reason}]")
        log(f"   ${state.balance:.2f} | {state.trades}t {state.wins}w({state.wr*100:.0f}%)")
        state.save()
    except Exception:
        log(f"💥 close_trade异常 [{reason}] | pos={nm}")
        if pos in state.positions: state.positions.remove(pos)
        state.save()

def check_positions():
    for p in list(state.positions):
        _check_one(p)

def _check_one(p):
    if not isinstance(p, dict):
        log(f"⚠️ _check_one收到非dict类型 {type(p).__name__} | 从持仓移除")
        if p in state.positions: state.positions.remove(p)
        state.save()
        return
    elapsed=time.time()-p['time']; pr=price(p['sym'])
    if not pr or pr<=0:
        if int(elapsed)%120<SCAN_INTERVAL and elapsed>60: log(f"⚠️ {p['sym']} 价格获取失败 {elapsed/60:.0f}m")
        return
    pnl_pct=((pr-p['entry'])/p['entry']*100) if p['dir']=='long' else ((p['entry']-pr)/p['entry']*100)
    usd=pnl_pct/100*p['size']*p.get('leverage',LEVERAGE)
    # 🔧 扣除双向手续费 (0.04%×2=0.08% 名义价值)
    fee_rt = p['size'] * p.get('leverage', LEVERAGE) * 0.0008
    usd_net = usd - fee_rt  # 真实净盈亏
    if usd>p.get('peak_usd',0): p['peak_usd']=usd

    # ── 退出逻辑 ──
    # v27: 自适应止损 + ATR动态SL
    max_loss = p.get('max_loss', MAX_SINGLE_LOSS)
    sl_pct = p.get('sl_pct', SL)
    # 🔧 ATR动态SL: 高波动=宽止损, 低波动=紧止损
    try:
        sym_key = p['sym']
        if hasattr(scan, '_var') and sym_key in scan._var:
            atr_vol = scan._var[sym_key].current_risk_pct() * 2  # ATR约=2×VaR%
            sl_pct = max(sl_pct, atr_vol)  # 不低于ATR
    except: pass
    if usd_net <= -max_loss: close_trade(p, f"maxloss(${usd_net:+.2f})", pr); return
    if pnl_pct>=TP: close_trade(p,"tp",pr); return
    if pnl_pct<=-sl_pct: close_trade(p,"sl",pr); return
    if elapsed>TIMEOUT: close_trade(p,f"timeout({elapsed/60:.0f}m)",pr); return
    # 🔧 时间止损: 开仓15分钟仍不盈利就砍
    if elapsed>TIME_STOP_MIN*60 and usd_net<=0: close_trade(p,f"timestop({elapsed/60:.0f}m)",pr); return
    if elapsed>MAX_STUCK_MIN*60 and pnl_pct<STUCK_THRESHOLD: close_trade(p,f"stuck({elapsed/60:.0f}m)",pr); return

    pk=p.get('peak_usd',0)
    # trail激活和触发用净盈亏
    if not p.get('trail_active') and pk>=max(p['size']*0.08, 0.50)+fee_rt: p['trail_active']=True; log(f"🔒 {p['sym'].split(':')[0]} trail @${usd_net:.2f}")
    if p.get('trail_active'):
        dist=0.15  # 兜底：峰<0.5仍保留$0.15安全垫
        for tm,td in sorted(TRAIL_TIERS, reverse=True):
            if pk-fee_rt>=tm: dist=td; break
        if usd_net<=pk-fee_rt-dist: close_trade(p,f"trail(${pk-fee_rt:.2f})",pr); return

    if int(elapsed/60)%2==0 and int((elapsed-SCAN_INTERVAL)/60)%2!=0:
        t='🔒' if p.get('trail_active') else ''; sf='⏳' if elapsed>MAX_STUCK_MIN*60 and pnl_pct<STUCK_THRESHOLD else ''
        log(f"📊 {p['dir']} {p['sym'].split(':')[0]} PnL:{pnl_pct:+.2f}%×{p.get('leverage',LEVERAGE)}x ${usd_net:+.2f} | ${state.balance:.2f} {t}{sf}")

def report():
    closed = len(state.wins_list) + len(state.losses_list)
    wr_calc = len(state.wins_list)/closed*100 if closed else 0
    log(""); log("═"*55)
    log(f"💰 ${state.balance:.2f} | Peak:${state.peak:.2f} | {state.trades}t({closed}已平) {len(state.wins_list)}w(WR:{wr_calc:.0f}%)")
    log(f"📈 ${state.pnl:+.2f} | DD:{((state.peak-state.balance)/state.peak*100):.1f}% | {LEVERAGE}x")
    for st,(w,l) in sorted(state.sig_stats.items()):
        t=w+l; wr_s=w/t*100 if t>0 else 0
        ev=load_evolve(); streak=ev.get('fail_streaks',{}).get(st,0)
        log(f"   🏷 {st}: {w}/{t} ({wr_s:.0f}%){' ⚠降权' if streak>=3 else ''}")
    aw=sum(state.wins_list)/len(state.wins_list) if state.wins_list else 0
    al=sum(state.losses_list)/len(state.losses_list) if state.losses_list else 0
    log(f"   ✅均${aw:.2f} x{len(state.wins_list)}  ❌均${al:.2f} x{len(state.losses_list)}")
    # ── 复盘 ──
    mt=state.main_trades; mw=state.main_wins; mp=state.main_pnl
    if mt>0: log(f"   🔷 实盘: {mt}t {mw}w | PnL:${mp:+.2f}")
    # 当前持仓
    if state.positions:
        for p in state.positions:
            pr=price(p['sym'])
            if pr: log(f"📌 {p['dir']} {p['sym'].split(':')[0]} @{p['entry']:.2f} →{pr:.2f}")
    else: log(f"📭 等待信号")
    log("═"*55); log("")

# ─── Main ───
def main():
    global state
    if state.balance<START_BALANCE*0.1 and state.trades>0:
        log(f"🔄 重置 ${state.balance:.2f}→${START_BALANCE}"); state=State(); state.save()

    # 🔧 v25.3: 启动同步 → state余额对齐币安U本位实盘
    try:
        ex_tmp = ccxt.binance({'apiKey':BINANCE_KEY,'secret':BINANCE_SECRET,
            'enableRateLimit':True,'options':{'defaultType':'future'},
            'proxies':{'http':PROXY,'https':PROXY},'timeout':8000})
        real_bal = ex_tmp.fetch_balance({'type':'swap'})
        real_free = float(real_bal.get('USDT',{}).get('free',0))
        real_total = float(real_bal.get('USDT',{}).get('total', real_free))
        sync_bal = max(real_total, real_free)  # 取总权益，兜底用free
        if sync_bal > 1 and abs(sync_bal - state.balance) > 0.5:
            log(f"🔗 同步余额: state${state.balance:.2f} → 币安${sync_bal:.2f}")
            state.balance = sync_bal
            state.day_start_bal = sync_bal
            state.peak = max(state.peak, sync_bal)
            state.save()
        del ex_tmp
    except Exception as e:
        log(f"⚠️ 余额同步失败: {str(e)[:60]}")

    ev=load_evolve()
    killed=[k for k,v in ev.get('fail_streaks',{}).items() if v>=3]
    log(f"🦅 v{EVOLVE_VERSION} | ${state.balance:.2f} | {LEVERAGE}x | 6路信号")
    if killed: log(f"   ⚠ 已降权: {', '.join(killed)}")
    log(f"🔴 实盘 | Key: {BINANCE_KEY[:6]}...{BINANCE_KEY[-4:]}")

    import signal as sig
    def shutdown(sn,fr):
        log("🛑 退出")
        if os.environ.get('NO_SHUTDOWN_SELL'):
            log("⚠️ NO_SHUTDOWN_SELL=1 → 保持持仓不卖")
        else:
            # 🔧 用交易所实际仓位平仓，避免孤儿仓
            try:
                ex_sd = exchange()
                if ex_sd:
                    ex_pos = ex_sd.fetch_positions()
                    for ep in ex_pos:
                        if not isinstance(ep, dict): continue
                        c = abs(float(ep.get('contracts',0) or 0))
                        if c < 0.001: continue
                        ep_sym = ep.get('symbol','')
                        ep_side = (ep.get('side') or 'long').lower()
                        cls = 'sell' if ep_side == 'long' else 'buy'
                        ex_sd.create_market_order(ep_sym, cls, c, {'reduceOnly': True})
                        log(f"🔴 实盘平仓: {cls} {ep_sym.split(':')[0]} {c}个 [shutdown]")
                    del ex_sd
            except Exception as e:
                log(f"⚠️ shutdown平仓异常: {str(e)[:60]}")
                for p in list(state.positions): close_trade(p, "shutdown")
        state.save(); sys.exit(0)
    sig.signal(sig.SIGINT,shutdown); sig.signal(sig.SIGTERM,shutdown)

    sigs=scan()
    # 🔗 从交易所同步持仓（恢复重启后孤儿仓）
    try:
        ex_sync = exchange()
        if ex_sync:
            ex_positions = ex_sync.fetch_positions()
            for ep in ex_positions:
                if not isinstance(ep, dict): continue
                c = abs(float(ep.get('contracts', 0) or 0))
                if c < 0.001: continue
                ep_sym = ep.get('symbol', '')
                ep_side = (ep.get('side') or '').lower()
                ep_entry = float(ep.get('entryPrice', 0) or 0)
                ep_margin = float(ep.get('initialMargin', 0) or 0)
                if ep_sym and ep_side and ep_entry > 0:
                    exists = any(p['sym']==ep_sym and p['dir']==ep_side for p in state.positions)
                    if not exists:
                        log(f"🔗 同步持仓: {ep_side} {ep_sym.split(':')[0]} @{ep_entry:.5f} margin${ep_margin:.2f}")
                        state.positions.append({
                            'sym': ep_sym, 'entry': ep_entry, 'size': round(ep_margin, 2),
                            'time': time.time(), 'dir': ep_side, 'type': '同步恢复',
                            'score': 60, 'cat': 'main', 'leverage': int(abs(ep_entry*c/max(ep_margin,0.01))),
                            'trail_active': False, 'peak_usd': 0, 'qty': c
                        })
            del ex_sync
    except Exception as e:
        log(f"⚠️ 同步持仓异常: {str(e)[:60]}")
    if state.positions:
        state.positions = []  # 清空，由交易所同步重建
    # 🔗 从交易所同步持仓
    try:
        ex_sync = exchange()
        if ex_sync:
            ex_positions = ex_sync.fetch_positions()
            all_ex_pos = []
            for ep in ex_positions:
                if not isinstance(ep, dict): continue
                c = abs(float(ep.get('contracts', 0) or 0))
                if c < 0.001: continue
                ep_sym = ep.get('symbol', '')
                ep_side = (ep.get('side') or 'long').lower()
                ep_entry = float(ep.get('entryPrice', 0) or 0)
                ep_margin = float(ep.get('initialMargin', 0) or 0)
                all_ex_pos.append({'sym':ep_sym,'entry':ep_entry,'size':round(ep_margin,2),
                    'time':time.time(),'dir':ep_side,'type':'同步','score':60,'cat':'main',
                    'leverage':20,'trail_active':False,'peak_usd':0,'qty':c,
                    'sl_pct':0.5,'max_loss':1.0})
            state.positions = all_ex_pos
            for p in state.positions:
                log(f"📌 同步持仓: {p['dir']} {p['sym'].split(':')[0]} @{p['entry']:.5f} sz:{p['size']}U")
            state.save()
            del ex_sync
    except Exception as e:
        log(f"⚠️ 同步持仓异常: {str(e)[:60]}")
    # 启动信号分发
    if sigs:
        cand=[s for s in sigs if not any((s['sym'],s['dir'])==(p['sym'],p['dir']) for p in state.positions)]
        if cand: log(f"🎯 {cand[0]['dir']} {cand[0]['sym'].split(':')[0]} [{cand[0]['type']}] sc:{cand[0]['sc']:.1f}"); open_trade(cand[0])
    else:
        log(f"📭 启动扫描无信号")

    lr=time.time(); sc=1
    
    # 🔬 初始化量化模型
    scan._hmm = {s: HMMRegime(80) for s in SYMBOLS}
    scan._vol = {s: VolatilityModel() for s in SYMBOLS}
    scan._var = {s: VaRManager(100) for s in SYMBOLS}
    
    while True:
        try:
            now=time.time(); sc+=1
            if now-state.day_start_t>86400: state.day_start_bal=state.balance; state.day_start_t=now; state.stopped=False; log(f"🌅 新一天 | ${state.balance:.2f}")

            dp=(state.balance-state.day_start_bal)/state.day_start_bal
            loss_usd = state.balance - state.day_start_bal
            if (dp<=MAX_DAY_LOSS or loss_usd <= -MAX_LOSS_USD) and not state.stopped:
                if loss_usd <= -MAX_LOSS_USD:
                    state.stopped=True; state.melt_until=float('inf')
                    log(f"⛔ 亏损${loss_usd:.2f} ≥ $5 → 永久熔断!"); state.save()
                else:
                    state.stopped=True; state.melt_until=now+1800
                    log(f"⛔ 日亏{dp*100:.0f}% 熔断 | 30min后恢复"); state.save()
            elif state.stopped and now>=state.melt_until and dp>MAX_DAY_LOSS+0.03:
                state.stopped=False; log(f"✅ 恢复交易"); state.save()

            dd=(state.peak-state.balance)/state.peak if state.peak>0 else 0
            if dd>=MAX_DD and not state.stopped:
                state.stopped=True; state.melt_until=now+1800
                log(f"⛔ 回撤{dd*100:.0f}% 熔断 | 30min后恢复"); state.save()
            elif state.stopped and now>=state.melt_until and dd<MAX_DD-0.02 and dp>MAX_DAY_LOSS:
                state.stopped=False; log(f"✅ 恢复交易"); state.save()

            if state.positions:
                check_positions()
                if int(now)%30<SCAN_INTERVAL: state.save()

            # v25.4: 连亏熔断检查（所有类别共用consec_losses）
            if state.consec_melt_until > 0:
                if now >= state.consec_melt_until:
                    state.consec_losses = 0
                    state.consec_melt_until = 0
                    log(f"✅ 连亏冷却结束 恢复交易")
                    state.save()
                elif state.consec_losses > 0 and int(now)%60 < SCAN_INTERVAL:
                    remaining = (state.consec_melt_until - now) / 60
                    log(f"🧊 连亏冷却中 {remaining:.0f}分 (已连亏{state.consec_losses}笔)")

            if len(state.positions)<MAX_POSITIONS and not state.stopped and not (0 < state.consec_melt_until > now):
                if now-state.last_trade>=COOLDOWN:
                    sigs=scan()
                    # 🔬 HMM regime 打分
                    for s in sigs:
                        sym_key = s['sym']
                        if sym_key in scan._hmm:
                            try:
                                h = scan._hmm[sym_key]
                                tick = fetch(exchange().fetch_ticker, sym_key)
                                if tick:
                                    h.update(tick.get('last', s['pr']))
                                bonus = h.regime_score(s['dir'])
                                if bonus != 0:
                                    s['sc'] = round(s['sc'] + bonus, 1)
                                    s['hmm_regime'] = h.current_regime
                                    regime_name = ['🐻跌','📊横','🐂涨'][min(h.current_regime, 2)]
                                    log(f"🔬 HMM {sym_key.split(':')[0]} {regime_name} → {s['dir']} {bonus:+d}分 sc:{s['sc']:.0f}")
                            except: pass
                        # 喂 VaR
                        if sym_key in scan._var:
                            try:
                                tick = fetch(exchange().fetch_ticker, sym_key)
                                if tick: scan._var[sym_key].update(tick.get('last', s['pr']))
                            except: pass
                    # 加波动率仓位调整
                    try:
                        sym_key = sigs[0]['sym'] if sigs else None
                        if sym_key and sym_key in scan._vol:
                            state._vol_mult = scan._vol[sym_key].position_multiplier()
                            # 🔬 VaR 风险限制: 每笔最多亏2%余额
                            if sym_key in scan._var:
                                var_mgr = scan._var[sym_key]
                                var_pct = var_mgr.current_risk_pct()  # 单K线VaR%
                                # 仓位上限 = (5%余额) / (VaR% × 20x) 回本模式
                                risk_limit = state.balance * 0.05 / max(var_pct * LEVERAGE, 0.001)
                                state._var_limit = min(risk_limit, state.balance * 0.95)
                    except: pass
                    
                    _log_scan_summary(sigs, [], MIN_SCORE)
                    for s in sigs:
                        if len(state.positions)>=MAX_POSITIONS: break
                        open_trade(s)
                    if not sigs and sc%10==0:
                        log(f"📭 扫{sc}轮 | ${state.balance:.2f}")

            if now-lr>=1800: report(); lr=now
            # 🔧 心跳: 每5分钟确认存活
            if int(now/60)%5==0 and int(now)%60<SCAN_INTERVAL:
                pos_count = len(state.positions)
                log(f"💓 alive | ${state.balance:.2f} | {pos_count}持仓 | 扫{sc}轮")
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt: shutdown(None,None)
        except Exception as e:
            crash_count = getattr(main, '_crash_count', 0) + 1
            main._crash_count = crash_count
            log(f"💥 崩溃#{crash_count}: {str(e)[:120]}")
            traceback.print_exc()
            if crash_count >= 3 and time.time() - getattr(main, '_first_crash', time.time()) < 300:
                log(f"⛔ 5分钟内崩溃{crash_count}次 → 退出，等待人工介入")
                state.save()
                sys.exit(1)
            if crash_count == 1:
                main._first_crash = time.time()
            time.sleep(30)

if __name__=='__main__':
    # PID 锁: 防止多实例同时运行
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
    
    state.startup_time=time.time(); state.save()
    main()
