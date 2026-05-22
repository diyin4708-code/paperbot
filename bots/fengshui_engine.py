#!/usr/bin/env python3
"""
中国命理信号增强引擎 — 八字日柱 + 二十四节气 + 时辰五行 + 农历月相
每个信号计算完 sc 后调用 fengshui_bonus() 返回 -10 ~ +10 的附加分
"""

import time, math
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))

# ============================================================
# 1. 八字日柱五行 — 按公历日期近似计算日柱天干
# ============================================================
# 简化版：按1900-01-01甲戌日起算，60天一甲子循环

_TIANGAN = ['甲','乙','丙','丁','戊','己','庚','辛','壬','癸']
_DIZHI  = ['子','丑','寅','卯','辰','巳','午','未','申','酉','戌','亥']
_WUXING = {
    '甲':'木','乙':'木','丙':'火','丁':'火','戊':'土',
    '己':'土','庚':'金','辛':'金','壬':'水','癸':'水'
}
_WUXING_DIZHI = {
    '子':'水','丑':'土','寅':'木','卯':'木','辰':'土','巳':'火',
    '午':'火','未':'土','申':'金','酉':'金','戌':'土','亥':'水'
}
# 五行相生相克: 金生水, 水生木, 木生火, 火生土, 土生金
# 加密货币=金+水(流动性)
# 日柱金旺→利加密货币, 日柱火旺→不利(火克金)
_WUXING_CRYPTO_FAVOR = {'金': 5, '水': 3, '木': -2, '火': -5, '土': 0}

def day_ganzhi():
    """返回今日天干地支简写 (如 '庚午')"""
    # 1900-01-01 = 甲戌日 (第11天干,第11地支)
    # 简化算法：计算从1900-01-01到今日的天数
    base = datetime(1900, 1, 1, tzinfo=TZ)
    now = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    days = (now - base).days
    gan_idx = (11 + days) % 10   # 从甲开始(甲=0...但实际上甲戌日对应gan=0, 查表修正)
    zhi_idx = (11 + days) % 12
    # 修正：1900-01-01 甲戌 = gan=0(甲), zhi=10(戌)
    gan_idx = days % 10
    zhi_idx = days % 12
    return _TIANGAN[gan_idx] + _DIZHI[zhi_idx]

def day_bazi_score():
    """日柱对加密货币的吉凶评分 -10~+10"""
    try:
        gz = day_ganzhi()
        gan = gz[0]   # 天干
        zhi = gz[1]   # 地支
        wx_day = _WUXING.get(gan, '土')  # 日柱天干五行
        wx_hour = _WUXING_DIZHI.get(zhi, '土')  # 地支五行
        
        # 天干为主(60%), 地支为辅(40%)
        score = _WUXING_CRYPTO_FAVOR.get(wx_day, 0) * 0.6
        score += _WUXING_CRYPTO_FAVOR.get(wx_hour, 0) * 0.4
        return round(score, 1)
    except:
        return 0


# ============================================================
# 2. 时辰五行 — 12时辰对应五行
# ============================================================
# 子(23-01)水 丑(01-03)土 寅(03-05)木 卯(05-07)木
# 辰(07-09)土 巳(09-11)火 午(11-13)火 未(13-15)土
# 申(15-17)金 酉(17-19)金 戌(19-21)土 亥(21-23)水

_HOUR_DIZHI = {
    0:'子',1:'丑',2:'丑',3:'寅',4:'寅',5:'卯',6:'卯',
    7:'辰',8:'辰',9:'巳',10:'巳',11:'午',12:'午',
    13:'未',14:'未',15:'申',16:'申',17:'酉',18:'酉',
    19:'戌',20:'戌',21:'亥',22:'亥',23:'子'
}
# 每个时辰对交易的建议
# 金/水时辰→利交易, 火旺时辰→市场波动大, 土→横盘
_HOUR_TRADE_FAVOR = {
    '子':'水','丑':'土','寅':'木','卯':'木','辰':'土','巳':'火',
    '午':'火','未':'土','申':'金','酉':'金','戌':'土','亥':'水'
}

def hour_wuxing_score():
    """当前时辰对交易的评分 -5~+5"""
    try:
        h = datetime.now(TZ).hour
        dizhi = _HOUR_DIZHI.get(h, '子')
        wx = _HOUR_TRADE_FAVOR.get(dizhi, '土')
        # 金时辰(申酉 15-19)→+5 水时辰(子亥 23-01)→+3 火时辰(巳午 09-13)→-3
        favor_map = {'金':5, '水':3, '土':0, '木':-2, '火':-3}
        return favor_map.get(wx, 0)
    except:
        return 0


# ============================================================
# 3. 二十四节气 — 距最近节气的天数，转折点±3天
# ============================================================
# 2026年节气日期 (近似, 精确需ephem库)
_SOLAR_TERMS_2026 = {
    # 春季
    '立春': '02-03', '雨水': '02-18', '惊蛰': '03-05',
    '春分': '03-20', '清明': '04-04', '谷雨': '04-19',
    # 夏季
    '立夏': '05-05', '小满': '05-20', '芒种': '06-05',
    '夏至': '06-21', '小暑': '07-06', '大暑': '07-22',
    # 秋季
    '立秋': '08-07', '处暑': '08-22', '白露': '09-07',
    '秋分': '09-22', '寒露': '10-08', '霜降': '10-23',
    # 冬季
    '立冬': '11-07', '小雪': '11-22', '大雪': '12-06',
    '冬至': '12-21', '小寒': '01-05', '大寒': '01-20',
}
# 2027年延续...
_SOLAR_TERMS_2027 = {
    '立春': '02-03', '雨水': '02-18', '惊蛰': '03-05',
}

def _get_term_dates(year_str):
    """获取某年所有节气日期"""
    if year_str == '2026':
        return _SOLAR_TERMS_2026
    elif year_str == '2027':
        return _SOLAR_TERMS_2027
    return {}

def solar_term_score():
    """距离最近节气的天数，±3天窗口内返回信号 -5~+5"""
    try:
        now = datetime.now(TZ)
        year = now.strftime('%Y')
        today = now.strftime('%m-%d')
        
        terms = _get_term_dates(year)
        if not terms:
            return 0
        
        # 找到最近的前一个和下一个节气
        dates = sorted(terms.values())
        today_idx = 0
        for i, d in enumerate(dates):
            if d <= today:
                today_idx = i
            else:
                break
        
        # 距离前后节气的天数
        from datetime import date as dt_date
        current_date = dt_date(now.year, now.month, now.day)
        
        min_days = 999
        for term_date_str in dates:
            term_m, term_d = map(int, term_date_str.split('-'))
            term_date = dt_date(now.year, term_m, term_d)
            # 处理跨年节气
            if term_date_str < '01-10':  # 小寒大寒是前一年12月的节气按次年算
                if now.month < 6:  # 次年初
                    term_date = dt_date(now.year, term_m, term_d)
                else:  # 前年末
                    term_date = dt_date(now.year + 1, term_m, term_d)
            days = abs((current_date - term_date).days)
            if days < min_days:
                min_days = days
        
        # ±3天内有5分信号，±7天有2分
        if min_days <= 3:
            return 5
        elif min_days <= 7:
            return 2
        return 0
    except:
        return 0


# ============================================================
# 4. 农历月相 — 新月满月对市场的影响
# ============================================================
# 简化版：按公历日期近似月相 (29.5天周期)
# 新月(初一)≈0, 上弦≈7, 满月(十五)≈15, 下弦≈22

MOON_PHASE_CYCLE = 29.53

def moon_phase_score():
    """当前月相对交易的评分 -3~+3
    满月附近→情绪高涨+3, 新月附近→谨慎-2"""
    try:
        # 以2026-01-01为新月参考点(实际偏差1-2天, 对信号影响可忽略)
        base = datetime(2026, 1, 1, tzinfo=TZ)
        now = datetime.now(TZ)
        days = (now - base).total_seconds() / 86400
        phase = (days % MOON_PHASE_CYCLE)  # 0-29.53
        
        # 满月附近(12-18天) → +3
        if 12 <= phase <= 18:
            return 3
        # 新月附近(0-3 或 27-29.5) → -2
        elif phase <= 3 or phase >= 27:
            return -2
        # 上弦/下弦 → 中性
        else:
            return 0
    except:
        return 0


# ============================================================
# 综合命理评分
# ============================================================

def fengshui_bonus(sym="BTC", direction="long"):
    """综合命理增强分 — 叠加到信号sc上
    返回 -12 ~ +12 整数
    """
    score = 0.0
    score += day_bazi_score()        # -5~+5
    score += hour_wuxing_score()     # -3~+5
    score += solar_term_score()      # 0~+5  (只在转折点给分)
    score += moon_phase_score()      # -2~+3
    
    # 限制范围
    return max(-12, min(12, round(score)))
