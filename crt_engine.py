# -*- coding: utf-8 -*-
"""
محرك CRT By Diaa v2.

التسلسل:
  تحيز يومي  →  CRT على الفريم الأعلى (W / D / 4H / 2H / 1H)  →  تأكيد على فريم أصغر مخصّص
  (W→4H ، D→1H أو 4H ، 4H→15m ، 2H→15m أو 5m ، 1H→5m أو 3m أو 1m)
  →  C2 يسحب سيولة C1 داخل منطقة اهتمام (FVG / IFVG / OB / RB / مستوى سيولة) ويغلق داخل الرينج
  →  CISD (إغلاق جسم خلف آخر شمعتين صنعتا الحركة)
  →  دخول مباشر عند CISD، أو (الأفضل) أمر معلق داخل الفجوة التي يتركها كسر آخر قاع/قمة (MSS).
  SMT مع الفضة مفضّل (يرفع الدرجة) قبل/أثناء الحركة.

المدخلات: شموع H1، وقاموس شموع التأكيد {دقائق: DataFrame} (1/3/5/15)، وشموع الفضة اختيارياً.
كل DataFrame فهرسه وقت فتح الشمعة بتوقيت نيويورك (naive) والأعمدة Open High Low Close Volume.
"""
import numpy as np
import pandas as pd

DEFAULTS = dict(
    crtTFs=["W", "D", "4H", "2H", "1H"],
    confMap={"W": [240], "D": [60, 240], "4H": [15], "2H": [15, 5], "1H": [5, 3, 1]},
    entryMode="MSS+FVG after CISD",          # أو "CISD direct"
    cisdMode="last2",                        # last2 = خلف آخر شمعتين صنعتا الحركة | run_open = خلف افتتاح أول سلسلة
    requireDailyBias=True, allowNeutralBias=False,
    requirePOI=True, requireKey=False, requireBias=False,
    minScore=8, aPlus=12,
    useRSI=True, rsiLen=14, rsiOS=30, rsiOB=70, rsiPts=2,
    useSMT=True, smtPts=1, smtBars=24,
    useTrend=True, wickMin=0.35, validCandles=1,
    ltfSwing={1: 3, 3: 3, 5: 2, 15: 2, 60: 2, 240: 2},
    fvgLookback=6, limitExpiry=20,
    slBufAtr=0.10, minRR=1.0, tp1Frac=0.5, tp1Pct=50.0, be=True, maxCandles=4,
    useKZ=True, kzAsia=False, kzLondon=True, kzNYAM=True, kzNYPM=False,
    maxTradesDay=2, maxLossesDay=2,
    anchorBodyAtr=0.2, rangeAtr=0.5, maxSweepDepth=1.0, minModelBars=2, maxModelBars=5,
    insideTol=0.02, memory=12, keyConfAtr=0.35, mintick=0.01,
    swingLen=3, trendEma=50, poiLookback=150, poiTolAtr=0.05,
)
MAX_SCORE = 17

HOURS_OK = {1, 5, 9, 13, 17, 21}
SHIFT = pd.Timedelta(hours=7)     # 17:00 نيويورك = بداية اليوم (مثل الفوركس)
SECS = {"1H": 3600, "2H": 7200, "4H": 14400, "D": 86400, "W": 604800}
_OHLCV = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}


# ───────────────────────── أدوات مساعدة ─────────────────────────
def _agg(df, rule):
    s = df.copy(); s.index = s.index + SHIFT
    r = s.resample(rule).agg(_OHLCV).dropna()
    r.index = r.index - SHIFT
    return r


def _weekly(df):
    s = df.copy(); s.index = s.index + SHIFT
    g = s.groupby(s.index.to_period("W-SUN"))
    r = g.agg(_OHLCV)
    r.index = [p.start_time - SHIFT for p in r.index]
    return r


def resample_min(df, minutes):
    """تحويل شموع صغيرة (مثلاً 1m) إلى 3m/5m... بمحاذاة منتصف الليل."""
    return df.resample(f"{minutes}min", origin="start_day").agg(_OHLCV).dropna()


def needed_conf(P):
    """فريمات التأكيد (بالدقائق) المطلوبة لفريمات CRT المفعّلة"""
    out = set()
    for tf in P["crtTFs"]:
        out.update(P["confMap"].get(tf, []))
    return out


def _rma(x, n):
    out = np.full(len(x), np.nan); a = 1.0 / n
    for i in range(len(x)):
        out[i] = x[i] if i == 0 else a * x[i] + (1 - a) * out[i - 1]
    return out


def _atr(df):
    h, l, c = df.High.values, df.Low.values, df.Close.values
    pc = np.r_[np.nan, c[:-1]]
    tr = np.where(np.isnan(pc), h - l, np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc))))
    return _rma(tr, 14)


def _pivots(df, ln):
    h, l = df.High.values, df.Low.values; n = len(df)
    ph = np.full(n, np.nan); pl = np.full(n, np.nan); lph = lpl = np.nan
    for i in range(n):
        j = i - ln
        if j - ln >= 0:
            if h[j] > h[j - ln:j].max() and h[j] > h[j + 1:i + 1].max(): lph = h[j]
            if l[j] < l[j - ln:j].min() and l[j] < l[j + 1:i + 1].min(): lpl = l[j]
        ph[i] = lph; pl[i] = lpl
    return ph, pl


def _daily_vp_bias(df, mintick, bins=32, vapct=0.7):
    o, h, l, c, v = df.Open.values, df.High.values, df.Low.values, df.Close.values, df.Volume.values
    day = (df.index.year * 10000 + df.index.month * 100 + df.index.day).values
    dh, dl, dv = [], [], []
    prev_day = None; pvah = pval = pdh = pdl = np.nan; openmode = 0; locked = 0; flag = False
    out = np.zeros(len(df), int)
    for i in range(len(df)):
        if prev_day is None or day[i] != prev_day:
            if dh:
                DH, DL = max(dh), min(dl); pdh, pdl = DH, DL
                if DH > DL:
                    bs = (DH - DL) / bins; prof = np.zeros(bins); tv = 0.0
                    for ch, cl, cv in zip(dh, dl, dv):
                        tv += cv; cr = max(ch - cl, mintick)
                        for b in range(bins):
                            bl = DL + bs * b; bh = bl + bs
                            prof[b] += cv * max(0.0, min(ch, bh) - max(cl, bl)) / cr
                    poc = int(prof.argmax()); lo = hi = poc; vv = prof[poc]; tgt = tv * vapct
                    while vv < tgt and (lo > 0 or hi < bins - 1):
                        lv = prof[lo - 1] if lo > 0 else -1.0
                        rv = prof[hi + 1] if hi < bins - 1 else -1.0
                        if rv >= lv and hi < bins - 1: hi += 1; vv += rv
                        elif lo > 0: lo -= 1; vv += lv
                        else: hi += 1; vv += rv
                    pval = DL + bs * lo; pvah = DL + bs * (hi + 1)
            dh, dl, dv = [], [], []
            openmode = (1 if o[i] > pvah else (-1 if o[i] < pval else 0)) if not np.isnan(pvah) else 0
            locked = 0; flag = False; prev_day = day[i]
        dh.append(h[i]); dl.append(l[i]); dv.append(v[i] if v[i] > 0 else 1.0)
        if not flag and not np.isnan(pvah):
            if openmode == 0:
                if h[i] > pvah and l[i] < pval: locked = 1 if c[i] >= o[i] else -1; flag = True
                elif h[i] > pvah: locked = 1; flag = True
                elif l[i] < pval: locked = -1; flag = True
            elif openmode == 1:
                if not np.isnan(pdh) and l[i] <= pvah and h[i] > pdh: locked = 1 if c[i] >= o[i] else -1; flag = True
                elif l[i] <= pvah: locked = -1; flag = True
                elif not np.isnan(pdh) and h[i] > pdh: locked = 1; flag = True
            else:
                if not np.isnan(pdl) and h[i] >= pval and l[i] < pdl: locked = 1 if c[i] >= o[i] else -1; flag = True
                elif h[i] >= pval: locked = 1; flag = True
                elif not np.isnan(pdl) and l[i] < pdl: locked = -1; flag = True
        out[i] = locked
    return out


def _small_bias(df, sw=3, fast=20, slow=50, acc=2, buf=0.2, rev=3):
    h, l, c = df.High.values, df.Low.values, df.Close.values; n = len(df)
    a = _atr(df)
    ef = pd.Series(c).ewm(span=fast, adjust=False).mean().values
    es = pd.Series(c).ewm(span=slow, adjust=False).mean().values
    lph = pph = lpl = ppl = np.nan; locked = 0; out = np.zeros(n, int)
    for i in range(n):
        j = i - sw
        if j - sw >= 0:
            if h[j] > h[j - sw:j].max() and h[j] > h[j + 1:i + 1].max(): pph, lph = lph, h[j]
            if l[j] < l[j - sw:j].min() and l[j] < l[j + 1:i + 1].min(): ppl, lpl = lpl, l[j]
        hs = not (np.isnan(lph) or np.isnan(pph) or np.isnan(lpl) or np.isnan(ppl))
        sb = hs and lph > pph and lpl >= ppl
        sr = hs and lpl < ppl and lph <= pph
        eb = i > 0 and ef[i] > es[i] and ef[i] > ef[i - 1]
        er = i > 0 and ef[i] < es[i] and ef[i] < ef[i - 1]
        up = dn = False
        if not np.isnan(lph) and i >= acc - 1:
            lvl = lph + a[i] * buf; up = all(c[i - k] > lvl for k in range(acc))
        if not np.isnan(lpl) and i >= acc - 1:
            lvl = lpl - a[i] * buf; dn = all(c[i - k] < lvl for k in range(acc))
        rbs = (not np.isnan(lpl)) and l[i] < lpl and c[i] > lpl
        rbu = (not np.isnan(lph)) and h[i] > lph and c[i] < lph
        bs = (2 if sb else 0) + (1 if eb else 0) + (2 if up else 0) + (1 if rbs else 0)
        brs = (2 if sr else 0) + (1 if er else 0) + (2 if dn else 0) + (1 if rbu else 0)
        if bs - brs >= rev: raw = 1
        elif brs - bs >= rev: raw = -1
        else: raw = 2 if (hs and ((sb and er) or (sr and eb) or rbs or rbu or up or dn)) else 0
        if locked in (0, 2): locked = raw
        elif locked == 1:
            if raw == -1 and dn: locked = -1
            elif raw == 2: locked = 2
        elif locked == -1:
            if raw == 1 and up: locked = 1
            elif raw == 2: locked = 2
        out[i] = locked
    return out


def _rsi(df, n=14):
    c = df.Close.values; d = np.diff(c, prepend=c[0])
    up = np.where(d > 0, d, 0.0); dn = np.where(d < 0, -d, 0.0)
    au = _rma(up, n); ad = _rma(dn, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = au / np.where(ad == 0, np.nan, ad)
    return 100 - 100 / (1 + rs)


def _trend(df, ln):
    c = df.Close.values; e = pd.Series(c).ewm(span=ln, adjust=False).mean().values
    out = np.zeros(len(c), int)
    for i in range(1, len(c)):
        out[i] = 1 if (c[i] > e[i] and e[i] > e[i - 1]) else (-1 if (c[i] < e[i] and e[i] < e[i - 1]) else 0)
    return out


def _poi_any(df, t):
    o, h, l, c = df.Open.values, df.High.values, df.Low.values, df.Close.values
    hi_t, lo_t = h[t], l[t]
    for i in range(1, 121):
        if t - i - 2 < 0: break
        k = t - i
        bull_i, bear_i = c[k] > o[k], c[k] < o[k]
        bull_p, bear_p = c[k - 1] > o[k - 1], c[k - 1] < o[k - 1]
        if (bear_p and bull_i) or (bull_p and bear_i):
            lo = min(min(o[k], c[k]), min(o[k - 1], c[k - 1])); hi = max(max(o[k], c[k]), max(o[k - 1], c[k - 1]))
            if hi_t >= lo and lo_t <= hi: return True
            blo, bhi = min(o[k - 1], c[k - 1]), max(o[k - 1], c[k - 1])
            if hi_t >= blo and lo_t <= bhi: return True
        if l[k] > h[k - 2] and hi_t >= h[k - 2] and lo_t <= l[k]: return True
        if h[k] < l[k - 2] and hi_t >= h[k] and lo_t <= l[k - 2]: return True
    return False


# ───────────────────────── السياق ─────────────────────────
class Context:
    def __init__(self, h1, conf, P, ag1=None, agc=None):
        """conf: {دقائق: DataFrame} لفريمات 1/3/5/15 (60 و240 تُشتق من h1). agc: نفس الشيء للفضة."""
        self.P = P; self.h1 = h1
        agc = dict(agc or {})
        self.ag = None
        if ag1 is not None and len(ag1) > 50:
            self.ag = {"1H": ag1, "2H": _agg(ag1, "2h"), "4H": _agg(ag1, "4h"), "D": _agg(ag1, "1D")}
            agc[60] = ag1; agc[240] = self.ag["4H"]
        self.tf = {"1H": h1, "2H": _agg(h1, "2h"), "4H": _agg(h1, "4h"), "D": _agg(h1, "1D"), "W": _weekly(h1)}
        self.W = self.tf["W"]
        self.Wend = self.W.index + pd.Timedelta(days=7)
        self.vp = _daily_vp_bias(h1, P["mintick"])
        self.vp_end = h1.index + pd.Timedelta(hours=1)
        self.c = {}
        for name, df in self.tf.items():
            a14 = _atr(df); ph, pl = _pivots(df, P["swingLen"])
            self.c[name] = dict(df=df, secs=SECS[name], atr=a14, ph=ph, pl=pl, det=self._detect(df, a14),
                                rsi=_rsi(df, P["rsiLen"]), end=df.index + pd.Timedelta(seconds=SECS[name]))
        self.trend4 = _trend(self.tf["4H"], P["trendEma"])
        self.cf = {}
        for m in sorted(needed_conf(P)):
            df = h1 if m == 60 else self.tf["4H"] if m == 240 else conf.get(m)
            if df is None or len(df) < 100:
                continue
            sw = P["ltfSwing"].get(m, 2)
            sf = agc.get(m)
            agL = agH = None
            if sf is not None and len(sf) > 50:
                agL = sf.Low.reindex(df.index).values; agH = sf.High.reindex(df.index).values
            self.cf[m] = dict(df=df, sb=_small_bias(df), atr=_atr(df), sw=sw, agL=agL, agH=agH)

    # ── كشف CRT ──
    def _detect(self, df, a14):
        P = self.P
        o, h, l, c = df.Open.values, df.High.values, df.Low.values, df.Close.values
        hrs = df.index.hour.values; n = len(df); out = [None] * n; tol = P["insideTol"]; mt = P["mintick"]
        for t in range(6, n):
            for m in range(P["minModelBars"], P["maxModelBars"] + 1):
                a = t - (m - 1)
                ch, cl = h[a], l[a]
                body = abs(c[a] - o[a]); rng = ch - cl
                if not (body >= P["anchorBodyAtr"] * a14[a] and rng >= P["rangeAtr"] * a14[a] and hrs[a] in HOURS_OK): continue
                if any(not (h[t - i] <= ch + tol and l[t - i] >= cl - tol) for i in range(1, m - 1)): continue
                bull = l[t] < cl - tol and c[t] > cl and (cl - l[t]) <= rng * P["maxSweepDepth"]
                bear = h[t] > ch + tol and c[t] < ch and (h[t] - ch) <= rng * P["maxSweepDepth"]
                if bull or bear:
                    c2r = max(h[t] - l[t], mt); isb = bool(bull)
                    wick = (min(o[t], c[t]) - l[t]) / c2r if isb else (h[t] - max(o[t], c[t])) / c2r
                    out[t] = dict(bull=isb, H=ch, L=cl, ext=(l[t] if isb else h[t]), wick=wick,
                                  anchor_hr=int(hrs[a]), sweep_time=df.index[t], t=t, a=a)
                    break
        return out

    @staticmethod
    def last_done(endidx, T):
        return int(endidx.searchsorted(T, side="right")) - 1

    def crt_mem(self, name, T, bull):
        c = self.c[name]; i = self.last_done(c["end"], T)
        if i < 0: return False
        for j in range(max(0, i - self.P["memory"]), i + 1):
            d = c["det"][j]
            if d is not None and d["bull"] == bull: return True
        return False

    # ── التحيز اليومي: CRT يومي حديث (آخر شمعتين)، وإلا بروفايل الحجم ──
    def daily_bias(self, T):
        d = self.c["D"]; i = self.last_done(d["end"], T)
        b = 0
        if i >= 0:
            for j in range(max(0, i - 1), i + 1):
                det = d["det"][j]
                if det is not None:
                    b = 1 if det["bull"] else -1
        if b == 0:
            vi = self.last_done(self.vp_end, T)
            b = int(self.vp[vi]) if vi >= 0 else 0
        return b if b in (1, -1) else 0

    # ── SMT مع الفضة ──
    def smt(self, name, d, isb):
        """الذهب كسر طرف C1 والفضة لم تكسر نظيره في C2 (فريم CRT)."""
        if not self.ag or name not in self.ag: return False
        sdf = self.ag[name]; df = self.c[name]["df"]
        try:
            t2 = df.index[d["t"]]; ta = df.index[d["a"]]
            if t2 not in sdf.index or ta not in sdf.index: return False
            return bool(sdf.loc[t2, "Low"] > sdf.loc[ta, "Low"]) if isb else bool(sdf.loc[t2, "High"] < sdf.loc[ta, "High"])
        except Exception:
            return False

    def smt_entry(self, isb, k, l5, h5, agL, agH):
        """SMT على فريم التأكيد: الذهب كسر قاع (قمة) النافذة السابقة والفضة لم تكسر نظيره."""
        if agL is None: return False
        N = self.P["smtBars"]
        if k - 2 * N + 1 < 0: return False
        a, b, c = k - 2 * N + 1, k - N + 1, k + 1
        try:
            if isb:
                sl, sp = np.nanmin(agL[b:c]), np.nanmin(agL[a:b])
                return bool(l5[b:c].min() < l5[a:b].min() and sl >= sp)
            sh, sp = np.nanmax(agH[b:c]), np.nanmax(agH[a:b])
            return bool(h5[b:c].max() > h5[a:b].max() and sh <= sp)
        except Exception:
            return False

    # ── مستويات السيولة ──
    def key_hit(self, isb, ext, edge, T, tol):
        lv = []
        d = self.c["D"]; i = self.last_done(d["end"], T)
        if i >= 0: lv.append(d["df"].Low.values[i] if isb else d["df"].High.values[i])
        wi = self.last_done(self.Wend, T)
        if wi >= 0: lv.append(self.W.Low.values[wi] if isb else self.W.High.values[wi])
        for nm in ("1H", "2H", "4H", "D"):
            c = self.c[nm]; k = self.last_done(c["end"], T)
            if k >= 0:
                v = c["pl"][k] if isb else c["ph"][k]
                if not np.isnan(v): lv.append(v)
        for v in lv:
            if np.isnan(v): continue
            if isb and ext - tol <= v <= edge + tol: return True
            if (not isb) and edge - tol <= v <= ext + tol: return True
        return False

    # ── مناطق الاهتمام: FVG / IFVG / OB / RB (غير مُكسورة) يقع داخلها سحب السيولة ──
    def poi_zones(self, name, d, isb):
        P = self.P; c0 = self.c[name]; df = c0["df"]; a = d["a"]; t = d["t"]; ext = d["ext"]
        o, h, l, c = df.Open.values, df.High.values, df.Low.values, df.Close.values
        atr = c0["atr"]; tol = max(P["insideTol"], P["poiTolAtr"] * atr[a])
        names = set()

        def hit(bot, top):
            return bot - tol <= ext <= top + tol

        for i in range(max(3, a - P["poiLookback"]), a):
            bar = atr[i] if not np.isnan(atr[i]) else 0.0
            if isb:
                # FVG صاعدة
                if l[i] > h[i - 2]:
                    bot, top = h[i - 2], l[i]
                    if (i + 1 >= t or c[i + 1:t].min() >= bot - tol) and hit(bot, top): names.add("FVG")
                # IFVG: فجوة هابطة أُغلق فوقها فصارت دعماً
                if h[i] < l[i - 2]:
                    bot, top = h[i], l[i - 2]
                    for j in range(i + 1, t):
                        if c[j] > top:
                            if (j + 1 >= t or c[j + 1:t].min() >= bot - tol) and hit(bot, top): names.add("IFVG")
                            break
                # OB: آخر شمعة هابطة قبل اندفاع صاعد
                if c[i] < o[i] and i + 1 < t and c[i + 1] > h[i] and (c[i + 1] - o[i + 1]) >= 0.5 * bar:
                    bot, top = l[i], h[i]
                    if (i + 2 >= t or c[i + 2:t].min() >= bot - tol) and hit(bot, top): names.add("OB")
                # RB: ذيل سفلي طويل
                wk = min(o[i], c[i]) - l[i]
                if wk >= 0.5 * max(h[i] - l[i], P["mintick"]) and wk >= 0.5 * bar:
                    bot, top = l[i], min(o[i], c[i])
                    if (i + 1 >= t or c[i + 1:t].min() >= bot - tol) and hit(bot, top): names.add("RB")
            else:
                if h[i] < l[i - 2]:
                    bot, top = h[i], l[i - 2]
                    if (i + 1 >= t or c[i + 1:t].max() <= top + tol) and hit(bot, top): names.add("FVG")
                if l[i] > h[i - 2]:
                    bot, top = h[i - 2], l[i]
                    for j in range(i + 1, t):
                        if c[j] < bot:
                            if (j + 1 >= t or c[j + 1:t].max() <= top + tol) and hit(bot, top): names.add("IFVG")
                            break
                if c[i] > o[i] and i + 1 < t and c[i + 1] < l[i] and (o[i + 1] - c[i + 1]) >= 0.5 * bar:
                    bot, top = l[i], h[i]
                    if (i + 2 >= t or c[i + 2:t].max() <= top + tol) and hit(bot, top): names.add("OB")
                wk = h[i] - max(o[i], c[i])
                if wk >= 0.5 * max(h[i] - l[i], P["mintick"]) and wk >= 0.5 * bar:
                    bot, top = max(o[i], c[i]), h[i]
                    if (i + 1 >= t or c[i + 1:t].max() <= top + tol) and hit(bot, top): names.add("RB")
        return sorted(names)

    # ───────────────────────── المحاكاة / الأحداث ─────────────────────────
    def simulate(self, name, cm):
        P = self.P; c = self.c[name]; df = c["df"]; secs = c["secs"]
        cf = self.cf[cm]; m5 = cf["df"]; sb = cf["sb"]; atr5 = cf["atr"]; LS = cf["sw"]
        agL, agH = cf["agL"], cf["agH"]
        o5, h5, l5, c5 = m5.Open.values, m5.High.values, m5.Low.values, m5.Close.values
        t5 = m5.index; n = len(m5); mt = P["mintick"]; mode = P["entryMode"]
        starts = {}
        for t, d in enumerate(c["det"]):
            if d is None or t + 1 >= len(df): continue
            c3 = df.index[t + 1]
            if c3 < t5[0]: continue                 # قبل بداية بيانات التأكيد
            k = int(t5.searchsorted(c3, side="left"))
            if k < n: starts.setdefault(k, []).append(d)
        events = []; setup = None; pending = None; trade = None; cisd_ok = False
        lph = lpl = np.nan
        bear_ops = []; bull_ops = []          # افتتاح آخر شمعتين هابطتين / صاعدتين (لـ CISD)
        bear_run = bull_run = np.nan          # افتتاح أول سلسلة (النمط القديم)

        def ev(kind, k, tr=None, **kw):
            events.append(dict(kind=kind, tf=name, conf=cm, bar_time=t5[k], k=k, trade=dict(tr) if tr else None, **kw))

        def in_kz(T):
            hm = T.hour * 60 + T.minute
            return ((not P["useKZ"]) or (P["kzAsia"] and hm >= 1200) or (P["kzLondon"] and 120 <= hm < 300)
                    or (P["kzNYAM"] and 420 <= hm < 600) or (P["kzNYPM"] and 780 <= hm < 960))

        def cancel_pending(k, reason):
            nonlocal pending
            if pending is not None and pending.get("announced"):
                ev("pending_cancel", k, setup_id=pending["sid"], pend=dict(pending), reason=reason)
            pending = None

        for k in range(n):
            T = t5[k]
            j = k - LS
            if j - LS >= 0:
                if h5[j] > h5[j - LS:j].max() and h5[j] > h5[j + 1:k + 1].max(): lph = h5[j]
                if l5[j] < l5[j - LS:j].min() and l5[j] < l5[j + 1:k + 1].min(): lpl = l5[j]
            if k > 0:
                if c5[k] < o5[k]:
                    if np.isnan(bear_run) or c5[k - 1] > o5[k - 1]: bear_run = o5[k]
                if c5[k] > o5[k]:
                    if np.isnan(bull_run) or c5[k - 1] < o5[k - 1]: bull_run = o5[k]
            # 1) إعداد جديد
            for d in starts.get(k, []):
                isb = d["bull"]; edge = d["L"] if isb else d["H"]; want = 1 if isb else -1
                i1 = self.last_done(self.c["1H"]["end"], T)
                if i1 < 0: continue
                a1 = self.c["1H"]["atr"][i1]
                bias = self.daily_bias(T)
                bias_ok = (bias == want) or (bias == 0 and P["allowNeutralBias"])
                kh = self.key_hit(isb, d["ext"], edge, T, a1 * P["keyConfAtr"])
                poi = self.poi_zones(name, d, isb)
                poi_ok = bool(poi) or kh
                sc = 1
                sc += 3 if kh else 0
                sc += 2 if poi else 0
                sc += 1 if bias == want else 0
                sc += 1 if sb[k] == want else 0
                sc += 1 if d["anchor_hr"] in HOURS_OK else 0
                sc += 1 if d["wick"] >= P["wickMin"] else 0
                htf = 0
                if secs < 14400 and self.crt_mem("4H", T, isb): htf += 2
                if secs < 7200 and self.crt_mem("2H", T, isb): htf += 1
                sc += min(htf, 2)
                ti = self.last_done(self.c["4H"]["end"], T); tb = self.trend4[ti] if ti >= 0 else 0
                sc += 1 if (P["useTrend"] and tb == want) else 0
                rs = c["rsi"]; r2 = rs[d["t"]]; r1 = rs[d["t"] - 1]
                rsi_ok = bool(P["useRSI"] and ((min(r2, r1) <= P["rsiOS"]) if isb else (max(r2, r1) >= P["rsiOB"])))
                smt_ok = bool(P["useSMT"] and self.smt(name, d, isb))
                sc += P["rsiPts"] if rsi_ok else 0
                sc += P["smtPts"] if smt_ok else 0
                ok = (sc >= P["minScore"] and (poi_ok or not P["requirePOI"]) and (kh or not P["requireKey"])
                      and (bias_ok or not P["requireDailyBias"]))
                sid = f"{name}|{d['sweep_time'].isoformat()}"
                if ok:
                    grade = "A+" if sc >= P["aPlus"] else ("A" if sc >= P["aPlus"] - 2 else "B")
                    setup = dict(isb=isb, H=d["H"], L=d["L"], ext=d["ext"], score=sc, grade=grade, key=kh, poi=poi,
                                 bias=bias, rsi=rsi_ok, smt=smt_ok, sid=sid,
                                 start=d["sweep_time"] + pd.Timedelta(seconds=secs),
                                 end=d["sweep_time"] + pd.Timedelta(seconds=secs * (1 + P["validCandles"])))
                    ev("setup", k, setup=dict(setup), setup_id=sid)
                else:
                    setup = None
                cancel_pending(k, "إعداد جديد")
                cisd_ok = False
            # 2) انتهاء / إبطال
            if setup is not None:
                isb = setup["isb"]
                mid = setup["L"] + (setup["H"] - setup["L"]) * (P["tp1Frac"] if isb else 1 - P["tp1Frac"])
                if T > setup["end"] or (c5[k] < setup["ext"] if isb else c5[k] > setup["ext"]) or (h5[k] >= mid if isb else l5[k] <= mid):
                    why_c = "انتهت مدة الإعداد" if T > setup["end"] else ("كُسر الوقف قبل التفعيل" if (c5[k] < setup["ext"] if isb else c5[k] > setup["ext"])
                                                                         else "وصل السعر للهدف الأول بدون تفعيل الأمر")
                    cancel_pending(k, why_c)
                    setup = None; cisd_ok = False
            # 3) CISD ثم دخول
            if setup is not None and trade is None and k > 1:
                isb = setup["isb"]
                if isb:
                    thr = max(bear_ops) if (P["cisdMode"] == "last2" and bear_ops) else bear_run
                    cisd = (not np.isnan(thr)) and c5[k] > o5[k] and c5[k] > thr and c5[k - 1] <= thr
                    mss = (not np.isnan(lph)) and c5[k] > lph and c5[k - 1] <= lph
                else:
                    thr = min(bull_ops) if (P["cisdMode"] == "last2" and bull_ops) else bull_run
                    cisd = (not np.isnan(thr)) and c5[k] < o5[k] and c5[k] < thr and c5[k - 1] >= thr
                    mss = (not np.isnan(lpl)) and c5[k] < lpl and c5[k - 1] >= lpl
                if cisd:
                    cisd_ok = True
                epx = np.nan; tag = ""; fill_sl = np.nan
                if mode == "CISD direct":
                    if cisd: epx, tag = c5[k], "CISD"
                else:
                    if cisd_ok and mss and pending is None:
                        ftop = fbot = np.nan
                        for q in range(0, P["fvgLookback"]):
                            if k - q - 2 < 0: break
                            if isb and l5[k - q] > h5[k - q - 2]: ftop, fbot = l5[k - q], h5[k - q - 2]; break
                            if (not isb) and h5[k - q] < l5[k - q - 2]: ftop, fbot = l5[k - q - 2], h5[k - q]; break
                        if not np.isnan(ftop):
                            ppx = (ftop + fbot) / 2
                            psl = setup["ext"] - atr5[k] * P["slBufAtr"] if isb else setup["ext"] + atr5[k] * P["slBufAtr"]
                            prisk = abs(ppx - psl)
                            ptp1 = setup["L"] + (setup["H"] - setup["L"]) * (P["tp1Frac"] if isb else 1 - P["tp1Frac"])
                            ptp2 = setup["H"] if isb else setup["L"]
                            if in_kz(T) and prisk > mt and ((psl < ppx < ptp1) if isb else (ptp1 < ppx < psl)) and abs(ptp1 - ppx) / prisk >= P["minRR"]:
                                pending = dict(px=float(ppx), bar=k, sl=float(psl), tp1=float(ptp1), tp2=float(ptp2), risk=float(prisk),
                                               rr1=float(abs(ptp1 - ppx) / prisk), rr2=float(abs(ptp2 - ppx) / prisk), isb=isb,
                                               grade=setup["grade"], score=setup["score"], poi=setup["poi"], bias=setup["bias"],
                                               rsi=setup["rsi"], smt=setup["smt"], key=setup["key"], tf=name, conf=cm, sid=setup["sid"],
                                               expire_min=P["limitExpiry"] * cm, announced=True)
                                ev("pending", k, setup_id=setup["sid"], pend=dict(pending))
                    if pending is not None:
                        if k - pending["bar"] > P["limitExpiry"]: cancel_pending(k, "انتهت مدة الأمر المعلق دون لمسه")
                        elif k > pending["bar"] and ((l5[k] <= pending["px"]) if isb else (h5[k] >= pending["px"])):
                            epx, tag = pending["px"], "CISD+MSS+FVG"
                            fill_sl = pending["sl"]
                if not np.isnan(epx):
                    pending = None
                    buf = atr5[k] * P["slBufAtr"]
                    sl = fill_sl if not np.isnan(fill_sl) else (setup["ext"] - buf if isb else setup["ext"] + buf)
                    risk = abs(epx - sl)
                    tp1 = setup["L"] + (setup["H"] - setup["L"]) * (P["tp1Frac"] if isb else 1 - P["tp1Frac"])
                    tp2 = setup["H"] if isb else setup["L"]
                    rr1 = abs(tp1 - epx) / risk if risk > 0 else 0
                    dir_ok = (sl < epx < tp1) if isb else (tp1 < epx < sl)
                    kz = in_kz(T)
                    if risk > mt and dir_ok and rr1 >= P["minRR"] and (kz or tag == "CISD+MSS+FVG"):
                        smt_ltf = bool(P["useSMT"] and self.smt_entry(isb, k, l5, h5, agL, agH))
                        tot = setup["score"] + (P["smtPts"] if smt_ltf else 0)
                        grade_t = "A+" if tot >= P["aPlus"] else ("A" if tot >= P["aPlus"] - 2 else "B")
                        trade = dict(isb=isb, dir=1 if isb else -1, entry=float(epx), sl=float(sl), sl0=float(sl), tp1=float(tp1),
                                     tp2=float(tp2), risk=float(risk), tp1hit=False, bar=k, time=T,
                                     end=T + pd.Timedelta(seconds=secs * P["maxCandles"]), grade=grade_t, score=tot,
                                     rsi=setup["rsi"], smt=setup["smt"], smt_ltf=smt_ltf, poi=setup["poi"], key=setup["key"],
                                     bias=setup["bias"], tag=tag, rr1=float(rr1), rr2=float(abs(tp2 - epx) / risk),
                                     tf=name, conf=cm, sid=setup["sid"], sig_time=T.isoformat())
                        sid_ = setup["sid"]; setup = None; cisd_ok = False
                        ev("signal", k, trade, setup_id=sid_)
            # تتبّع آخر شمعتين (بعد استخدامهما في CISD لهذه الشمعة)
            if c5[k] < o5[k]:
                bear_ops.append(o5[k]); bear_ops = bear_ops[-2:]
            elif c5[k] > o5[k]:
                bull_ops.append(o5[k]); bull_ops = bull_ops[-2:]
            # 4) إدارة الصفقة
            if trade is not None and k > trade["bar"]:
                tr = trade; isb = tr["isb"]
                r1 = abs(tr["tp1"] - tr["entry"]) / tr["risk"]; r2 = abs(tr["tp2"] - tr["entry"]) / tr["risk"]; pct = P["tp1Pct"] / 100.0
                sl_hit = (l5[k] <= tr["sl"]) if isb else (h5[k] >= tr["sl"])
                t1 = (h5[k] >= tr["tp1"]) if isb else (l5[k] <= tr["tp1"])
                t2 = (h5[k] >= tr["tp2"]) if isb else (l5[k] <= tr["tp2"])
                res = None; why = ""
                if sl_hit:
                    if tr["tp1hit"]: res, why = pct * r1 + (1 - pct) * (0.0 if P["be"] else -1.0), "TP1+BE" if P["be"] else "TP1+SL"
                    else: res, why = -1.0, "SL"
                else:
                    if (not tr["tp1hit"]) and t1:
                        tr["tp1hit"] = True
                        if P["be"]: tr["sl"] = tr["entry"]
                        ev("tp1", k, tr, setup_id=tr["sid"])
                    if tr["tp1hit"] and t2: res, why = pct * r1 + (1 - pct) * r2, "TP2"
                    elif T >= tr["end"]:
                        un = ((c5[k] - tr["entry"]) if isb else (tr["entry"] - c5[k])) / tr["risk"]
                        res = pct * r1 + (1 - pct) * un if tr["tp1hit"] else un; why = "TIME"
                if res is not None:
                    tr["R"] = float(res); tr["why"] = why
                    ev("closed", k, tr, result=why, R=float(res), setup_id=tr["sid"])
                    trade = None
        return events, trade, setup

    # ───────────────────────── كل الفريمات + اختيار نهائي ─────────────────────────
    def all_events(self):
        evs = []; open_trades = {}; setups = {}
        for nm in self.P["crtTFs"]:
            for cm in self.P["confMap"].get(nm, []):
                if cm not in self.cf:
                    continue
                e, tr, st = self.simulate(nm, cm)
                evs += e; open_trades[f"{nm}/{cm}"] = tr; setups[f"{nm}/{cm}"] = st
        return self.select_events(evs), open_trades, setups

    def select_events(self, evs):
        """لكل إعداد نختار فريم تأكيد واحداً: أبكر أمر معلق، وإلا أبكر إشارة. ثم حدود يومية عامة."""
        P = self.P
        order = {"setup": 0, "pending": 1, "signal": 2, "tp1": 3, "pending_cancel": 3, "closed": 4}
        evs = sorted(evs, key=lambda e: (e["bar_time"], order[e["kind"]], -e["conf"]))
        chosen = {}
        for e in evs:
            if e["kind"] in ("pending", "signal") and e["setup_id"] not in chosen:
                chosen[e["setup_id"]] = e["conf"]
        out = []; seen = set(); allowed = set(); day = None; trades_today = losses_today = 0
        for e in evs:
            d = (e["bar_time"].year, e["bar_time"].month, e["bar_time"].day)
            if d != day: day = d; trades_today = losses_today = 0
            sid = e["setup_id"]
            if e["kind"] == "setup":
                if sid in seen: continue
                seen.add(sid); out.append(e)
            elif chosen.get(sid) != e["conf"]:
                continue
            elif e["kind"] in ("pending", "pending_cancel"):
                out.append(e)
            elif e["kind"] == "signal":
                if trades_today >= P["maxTradesDay"] or losses_today >= P["maxLossesDay"]: continue
                trades_today += 1; allowed.add((sid, e["conf"])); out.append(e)
            else:
                if (sid, e["conf"]) not in allowed: continue
                if e["kind"] == "closed" and e["R"] < -0.001: losses_today += 1
                out.append(e)
        return out
