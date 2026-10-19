# -*- coding: utf-8 -*-
"""
محرك SH v2 + RSI (مطابق لمؤشر TradingView «Sharpness Signals v2 + RSI»).

• مستويات الشاربنس: قمم/قيعان على الإغلاقات (Line Chart) من فريم الشارت 5m (فرعي) ومن 15m (أساسي).
• الإشارة عند إغلاق شمعة بجسمها خلف المستوى (كسر صريح: خلف المستوى وخلف القمة/القاع الفعلي).
• الفلاتر (حسب إعدادات المؤشر): جودة الشمعة (جسم/مدى ≥ 0.6)، تجاهل الكسر البعيد (>1×ATR)،
  RSI غير متشبّع (<70 للشراء، >30 للبيع)، الجلسات (كل سوق بتوقيته: طوكيو 09-11، لندن 08-10، نيويورك 07-11)،
  زخم اليوم، تجاهل سحب طرف آسيا المعاكس (ICT)، ADR، حد 3 صفقات باليوم، والتوقف بعد 3 ربحات متتالية.
• الستوب خلف آخر شاربنس فرعي + 10 بيب (أدنى مسافة 1×ATR). الهدف الأول TP1=2R يغلق 50% وينقل الستوب للدخول،
  والثاني TP2=3R. قبل TP1: نقل الستوب للدخول عند 1R.
• يوم التداول لحد الصفقات = يوم الشارت (يبدأ 17:00 نيويورك). أيام ADR/آسيا/زخم اليوم = UTC (مثل المؤشر).

المدخلات: chart (5m) و main (15m) = قوائم dict(time=epoch UTC بالثواني، open، high، low، close) لشموع مغلقة فقط.
"""
import math, bisect, datetime as dt
from zoneinfo import ZoneInfo

NAN = math.nan
UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")

DEFAULTS = dict(
    chartTF=5, mainTF=15, pvL=3, pvR=2, depthATR=1.0, clusterTol=0.3, usePsych=True, maxActive=30,
    trigMode="both", onlyStrong=False, mainOrStrong=False, strictBreak=True, useBody=True, bodyMin=0.6, farATR=1.0,
    farMode="limit", retestBars=6,
    useRsiMax=True, rsiLen=14, rsiMax=70,
    bufPips=10, pipSize=0.1, minSLatr=1.0, maxSLatr=0,
    tp1R=2.0, tp1Pct=50, rr=3.0, useBE=True, beAtR=1.0,
    useSessions=True,
    sessions={"tokyo": {"on": True, "range": "0900-1100", "tz": "Asia/Tokyo"},
              "london": {"on": True, "range": "0800-1000", "tz": "Europe/London"},
              "newyork": {"on": True, "range": "0700-1100", "tz": "America/New_York"}},
    maxTrades=3, stopAfterWins=3, useNews=True, newsDates=[], newsTz="Asia/Hebron",
    useDayMom=True, useNoJudas=False, asiaEndMin=300, useADR=False, adrLen=10, useNoFVG=True,
    secondBreak="none", secondBars=24, zoneATR=0.5, zoneBars=12,
)


def isnan(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


def atr_rma(h, l, c, n=14):
    out = [NAN] * len(c)
    tr = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) if i else h[0] - l[0] for i in range(len(c))]
    if len(c) >= n:
        out[n - 1] = sum(tr[:n]) / n
        for i in range(n, len(c)):
            out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def rsi_wilder(c, n=14):
    out = [NAN] * len(c); g = ls = 0.0
    for k in range(1, len(c)):
        ch = c[k] - c[k - 1]; up = max(ch, 0.0); dn = max(-ch, 0.0)
        if k <= n:
            g += up / n; ls += dn / n
        else:
            g = (g * (n - 1) + up) / n; ls = (ls * (n - 1) + dn) / n
        if k >= n:
            out[k] = 100.0 if ls == 0 else 100 - 100 / (1 + g / ls)
    return out


def psych(p):
    fl = math.floor(p)
    return fl + (fl % 10) * 0.11


def pivots(c, atr, P):
    """{شمعة التأكيد: [dict(lvl, raw, isH, strong)]} — الشاربنس على الإغلاقات"""
    pvL, pvR = P["pvL"], P["pvR"]
    ev = {}
    for i in range(pvL + pvR + 1, len(c)):
        p = i - pvR
        a0 = atr[p]
        if isnan(a0):
            continue
        left = c[p - pvL:p]; right = c[p + 1:i + 1]
        for isH in (True, False):
            if isH and not (c[p] > max(left) and c[p] > max(right)):
                continue
            if (not isH) and not (c[p] < min(left) and c[p] < min(right)):
                continue
            if isH:
                strong = (c[p] - min(left)) >= P["depthATR"] * a0 and (c[p] - min(right)) >= P["depthATR"] * a0
            else:
                strong = (max(left) - c[p]) >= P["depthATR"] * a0 and (max(right) - c[p]) >= P["depthATR"] * a0
            lo = max(i - pvR - 2, 0); hi = i - max(pvR - 2, 0)
            win = [x for x in c[lo:hi + 1] if abs(x - c[p]) <= P["clusterTol"] * a0]
            mid = (max(win) + min(win)) / 2
            ev.setdefault(i, []).append(dict(lvl=psych(mid) if P["usePsych"] else mid, raw=c[p], isH=isH, strong=strong))
    return ev


def in_session(aware_utc, sess, tz):
    """هل وقت الشمعة (UTC مع tzinfo) ضمن الجلسة sess='HHMM-HHMM' بتوقيت السوق tz (يتبع الصيفي تلقائياً)؟"""
    a, b = sess.split("-")
    loc = aware_utc.astimezone(ZoneInfo(tz)) if tz else aware_utc
    m = loc.hour * 60 + loc.minute
    ma = int(a[:2]) * 60 + int(a[2:]); mb = int(b[:2]) * 60 + int(b[2:])
    return ma <= m < mb if ma < mb else (m >= ma or m < mb)


def run_engine(chart, main, P):
    """يعيد قائمة أحداث: signal / breakeven / tp1 / closed (bar_time = وقت فتح الشمعة، epoch UTC)."""
    P = {**DEFAULTS, **P}
    T = [b["time"] for b in chart]; O = [b["open"] for b in chart]; H = [b["high"] for b in chart]
    L = [b["low"] for b in chart]; C = [b["close"] for b in chart]
    N = len(chart)
    A = atr_rma(H, L, C, 14)
    RSI = rsi_wilder(C, P["rsiLen"])
    AW = [dt.datetime.fromtimestamp(t, UTC) for t in T]                       # وقت UTC مع tzinfo
    UT = [x.replace(tzinfo=None) for x in AW]
    TDAY = [(x.astimezone(NY) + dt.timedelta(hours=7)).date() for x in AW]      # يوم الشارت (يبدأ 17:00 نيويورك)
    NEWSD = [x.astimezone(ZoneInfo(P["newsTz"])).date().isoformat() for x in AW] if P["useNews"] and P["newsDates"] else None

    sub = pivots(C, A, P)
    mc = [b["close"] for b in main]
    main_raw = pivots(mc, atr_rma([b["high"] for b in main], [b["low"] for b in main], mc, 14), P)
    mT = [b["time"] for b in main]
    mainEv = {}
    for k, lst in main_raw.items():
        j = bisect.bisect_left(T, mT[k] + P["mainTF"] * 60)
        if j < N:
            mainEv.setdefault(j, []).extend(lst)

    # سياق اليوم (UTC): مدى اليوم حتى الآن، متوسط المدى اليومي (ADR)، نطاق آسيا وسحبه
    dHi = [0.0] * N; dLo = [0.0] * N; sweptAH = [False] * N; sweptAL = [False] * N; adrArr = [NAN] * N
    asiaEnd = P["asiaEndMin"]
    ranges = []; cur = None
    for k in range(N):
        dk = UT[k].date(); mk = UT[k].hour * 60 + UT[k].minute
        if dk != cur:
            if cur is not None:
                ranges.append(hi - lo)
            cur = dk; hi = H[k]; lo = L[k]; ah = -1e18; al = 1e18; sh = sl_ = False
        hi = max(hi, H[k]); lo = min(lo, L[k])
        if mk < asiaEnd:
            ah = max(ah, H[k]); al = min(al, L[k])
        else:
            sh = sh or H[k] > ah; sl_ = sl_ or L[k] < al
        dHi[k] = hi; dLo[k] = lo; sweptAH[k] = sh; sweptAL[k] = sl_
        n_ = P["adrLen"]
        adrArr[k] = sum(ranges[-n_:]) / n_ if len(ranges) >= n_ else NAN

    news = set(P["newsDates"])
    rr = P["rr"]
    usePart = 0 < P["tp1Pct"] < 100 and P["tp1R"] < rr
    events = []
    act = []
    lastSubH = lastSubL = NAN
    tr = None
    tradesToday = winsToday = winStreak = 0
    prevDay = None

    for i in range(1, N):
        a = A[i]
        if isnan(a):
            continue
        if TDAY[i] != prevDay:
            if prevDay is not None:
                tradesToday = winsToday = winStreak = 0
            prevDay = TDAY[i]
        sessOn = [s for s in ("tokyo", "london", "newyork") if P["sessions"][s]["on"] and
                  in_session(AW[i], P["sessions"][s]["range"], P["sessions"][s].get("tz"))]
        sessOK = (not P["useSessions"]) or bool(sessOn)
        newsDay = bool(NEWSD) and NEWSD[i] in news

        # ── الأمر المعلّق (كسر بعيد): يُفعَّل عند لمس المستوى، ويُلغى بعد retestBars شموع أو إذا سبق السعرُ الهدفَ
        if tr and tr["pending"]:
            d = tr["dir"]
            filled = L[i] <= tr["entry"] if d == 1 else H[i] >= tr["entry"]
            tpFirst = H[i] >= tr["tp"] if d == 1 else L[i] <= tr["tp"]
            if filled:
                tr["pending"] = False; tr["fillBar"] = i
                events.append(dict(bar_time=T[i], kind="fill", trade=dict(tr)))
            elif i - tr["tbar"] >= P["retestBars"] or tpFirst:
                events.append(dict(bar_time=T[i], kind="cancel", trade=dict(tr),
                                   reason="وصل السعر للهدف دون أن يلمس الأمر" if tpFirst else "انتهت مدة الأمر المعلق دون لمسه"))
                tradesToday -= 1
                tr = None

        # ── الصفقة المفتوحة (الستوب أولاً عند تساوي الاحتمالين في نفس الشمعة)
        if tr and not tr["pending"]:
            d = tr["dir"]
            after = i > tr["fillBar"]
            hitSL = L[i] <= tr["sl"] if d == 1 else H[i] >= tr["sl"]
            hitTP1 = after and usePart and not tr["tp1done"] and (H[i] >= tr["tp1"] if d == 1 else L[i] <= tr["tp1"])
            hitTP = after and (H[i] >= tr["tp"] if d == 1 else L[i] <= tr["tp"])
            res = None; R = 0.0
            if hitSL:
                ex = min(tr["sl"], O[i]) if d == 1 else max(tr["sl"], O[i])        # فجوة الافتتاح خلف الستوب تُحتسب
                Rrem = (ex - tr["entry"]) * d / tr["risk"]
                R = tr["realR"] + tr["rem"] * Rrem
                if tr["tp1done"]:
                    res = "TP1+BE" if Rrem >= -0.05 else "TP1+SL"
                else:
                    res = "BE" if R >= -0.05 else "SL"
            else:
                if hitTP1:
                    tr["tp1done"] = True; tr["realR"] = P["tp1Pct"] / 100 * P["tp1R"]; tr["rem"] = 1 - P["tp1Pct"] / 100
                    tr["be"] = True; tr["sl"] = tr["entry"]
                    events.append(dict(bar_time=T[i], kind="tp1", trade=dict(tr)))
                if hitTP:
                    R = tr["realR"] + tr["rem"] * rr; res = "TP2" if usePart else "TP"
            if res:
                events.append(dict(bar_time=T[i], kind="closed", result=res, R=R, trade=dict(tr)))
                if R > 0.05:
                    winsToday += 1; winStreak += 1
                elif R < -0.05:
                    winStreak = 0                      # الخسارة تكسر سلسلة الربح، والخروج على الدخول لا يكسرها
                tr = None
            elif P["useBE"] and not tr["be"] and (H[i] >= tr["entry"] + P["beAtR"] * tr["risk"] if d == 1
                                                   else L[i] <= tr["entry"] - P["beAtR"] * tr["risk"]):
                tr["be"] = True; tr["sl"] = tr["entry"]
                events.append(dict(bar_time=T[i], kind="breakeven", trade=dict(tr)))

        # ── مستويات جديدة (أساسي ثم فرعي) + الحد المشترك
        for e in mainEv.get(i, []):
            act.append(dict(e, isMain=True))
        for e in sub.get(i, []):
            if e["isH"]: lastSubH = e["raw"]
            else: lastSubL = e["raw"]
            act.append(dict(e, isMain=False))
        while len(act) > P["maxActive"]:
            act.pop(0)

        # ── الكسر بإغلاق الجسم (كسر صريح)
        brkHi = brkLo = NAN; infoHi = infoLo = None
        for j in range(len(act) - 1, -1, -1):
            lv = act[j]
            ref = (max(lv["lvl"], lv["raw"]) if lv["isH"] else min(lv["lvl"], lv["raw"])) if P["strictBreak"] else lv["lvl"]
            up = lv["isH"] and C[i] > ref
            dn = (not lv["isH"]) and C[i] < ref
            if up or dn:
                tm = P["trigMode"]
                tfOK = tm == "both" or (tm == "sub" and not lv["isMain"]) or (tm == "main" and lv["isMain"])
                if tfOK and ((not P["onlyStrong"]) or lv["strong"]) and ((not P["mainOrStrong"]) or lv["isMain"] or lv["strong"]):
                    if up and C[i - 1] <= ref and (isnan(brkHi) or lv["lvl"] > brkHi):
                        brkHi = lv["lvl"]; infoHi = lv
                    if dn and C[i - 1] >= ref and (isnan(brkLo) or lv["lvl"] < brkLo):
                        brkLo = lv["lvl"]; infoLo = lv
                act.pop(j)

        # ── الإشارة
        stopN = P["stopAfterWins"]
        dayOK = tradesToday < P["maxTrades"] and (stopN <= 0 or winStreak < stopN)
        if tr is not None or not dayOK or not sessOK or newsDay:
            continue
        body = abs(C[i] - O[i]); rng = H[i] - L[i]
        for d in (1, -1):
            lvl = brkHi if d == 1 else brkLo
            info = infoHi if d == 1 else infoLo
            if tr is not None or isnan(lvl):
                continue
            if P["useBody"] and not ((C[i] - O[i]) * d > 0 and rng > 0 and body / rng >= P["bodyMin"]):
                continue
            far = (C[i] - lvl) * d > P["farATR"] * a
            if far and P["farMode"] == "ignore":                      # كسر بعيد ← تجاهل
                continue
            useLim = far and P["farMode"] == "limit"                  # كسر بعيد ← أمر معلق عند إعادة اختبار المستوى
            if P["useRsiMax"] and not isnan(RSI[i]) and not (RSI[i] < P["rsiMax"] if d == 1 else RSI[i] > 100 - P["rsiMax"]):
                continue
            pos = (C[i] - dLo[i]) / (dHi[i] - dLo[i]) if dHi[i] > dLo[i] else 0.5
            if P["useDayMom"] and not (pos >= 0.5 if d == 1 else pos <= 0.5):
                continue
            if P["useNoJudas"] and (sweptAL[i] if d == 1 else sweptAH[i]):
                continue
            if P["useADR"] and not isnan(adrArr[i]) and (dHi[i] - dLo[i]) >= adrArr[i]:
                continue
            if P["useNoFVG"] and any((L[k] > H[k - 2]) if d == 1 else (H[k] < L[k - 2]) for k in (i, i - 1)):
                continue
            ref = lastSubL if d == 1 else lastSubH
            if isnan(ref):
                continue
            entry = lvl if useLim else C[i]
            sl = ref - d * P["bufPips"] * P["pipSize"]
            risk = (entry - sl) * d
            if risk <= 0:
                continue
            if risk < P["minSLatr"] * a:
                risk = P["minSLatr"] * a; sl = entry - d * risk
            if P["maxSLatr"] > 0 and risk > P["maxSLatr"] * a:
                continue
            tr = dict(dir=d, entry=entry, sl=sl, sl0=sl, tp=entry + d * rr * risk, tp1=entry + d * P["tp1R"] * risk,
                      risk=risk, be=False, tp1done=False, realR=0.0, rem=1.0, sig_time=T[i], level=lvl, rr=rr,
                      tp1R=P["tp1R"], tp1Pct=P["tp1Pct"], usePart=usePart, rsi=RSI[i],
                      lvlMain=bool(info and info["isMain"]), lvlStrong=bool(info and info["strong"]),
                      sess=(sessOn[0] if sessOn else ""), atr=a, pending=useLim, tbar=i, fillBar=i, far=far, retestBars=P["retestBars"])
            tradesToday += 1
            events.append(dict(bar_time=T[i], kind="signal", trade=dict(tr)))
    return events
