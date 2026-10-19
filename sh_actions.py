# -*- coding: utf-8 -*-
"""
SH v2 + RSI — تشغيل على GitHub Actions (مرة كل 5 دقائق). نظام منفصل تماماً عن CRT:
ملف إعداداته sh_config.json، وحالته sh_state.json، ورسائله بعنوان «SH v2 + RSI».
الأسعار من Yahoo (GC=F) مصحّحة بفرق الذهب الفوري الحالي ليقترب من شارت TradingView.

    python sh_actions.py              تشغيل مرة واحدة
    python sh_actions.py --test       رسالة تجربة
    python sh_actions.py --history 3  أحداث آخر 3 أيام على الشاشة بدون إرسال
"""
import json, os, sys, time, datetime as dt
import pandas as pd
import requests
import yfinance as yf

from sh_engine import run_engine, DEFAULTS
from sh_messages import message, ev_key, NAME
import crt_bot as core                      # Telegram فقط

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "sh_state.json")
SPOT_URL = "https://api.gold-api.com/price/{}"
LIVE = {}                                   # آخر سعر حي (شمعة غير مكتملة) لكل رمز


def log(msg):
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_state():
    if os.path.exists(STATE_PATH):
        return json.load(open(STATE_PATH, encoding="utf-8"))
    return {"sent": []}


def save_state(st):
    st["sent"] = st["sent"][-800:]
    json.dump(st, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def yf_candles(ticker, interval, period, minutes):
    """شموع مكتملة فقط: قائمة dict(time=epoch UTC، open، high، low، close)."""
    last = None
    for attempt in range(4):
        try:
            d = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=False)
            d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
            d = d[["Open", "High", "Low", "Close"]].dropna()
            if len(d) < 200:
                raise RuntimeError(f"بيانات قليلة لـ {ticker} {interval}: {len(d)}")
            LIVE[ticker] = float(d.Close.values[-1])
            ts = ((d.index.tz_convert("UTC") - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)).tolist()   # آمن مع أي وحدة زمنية لـ pandas
            now = time.time()
            out = []
            for t, o, h, l, c in zip(ts, d.Open.values, d.High.values, d.Low.values, d.Close.values):
                if t + minutes * 60 <= now:
                    out.append(dict(time=int(t), open=float(o), high=float(h), low=float(l), close=float(c)))
            return out
        except Exception as ex:
            last = ex
            time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"فشل جلب {ticker} {interval}: {last}")


def spot_quote(sym, ref):
    try:
        r = requests.get(SPOT_URL.format(sym), timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        p = float(r.json()["price"])
        return None if (ref and abs(p - ref) / ref > 0.06) else p
    except Exception:
        return None


def calc_offset(state, sym, live):
    """offset = سعر الآجل الحي - سعر الفوري الآن، منعّم عبر التشغيلات، ويُحدَّث فقط والسوق نشط."""
    old = state.get("off")
    q = spot_quote(sym, live) if live else None
    if q is None:
        return old                                   # قد يكون None إن لم يُقَس الفرق بعد (السوق مغلق)
    raw = live - q
    if abs(raw) > 60:                                # قراءة شاذة
        return old
    new = raw if old is None else 0.7 * old + 0.3 * raw
    log(f"تصحيح: آجل {live:.2f} - فوري {q:.2f} = {raw:.2f}$ (المعتمد {new:.2f}$)")
    return new


def realtime_5m(now):
    """شموع 5 دقائق مكتملة لحظياً من الذهب الرقمي PAXG (Kraken ثم OKX). Yahoo متأخر نحو 10–15 دقيقة، وهذا بلا تأخير."""
    try:
        r = requests.get("https://api.kraken.com/0/public/OHLC", params={"pair": "PAXGUSD", "interval": 5}, timeout=15).json()
        rows = list(r["result"].values())[0]
        out = [dict(time=int(x[0]), open=float(x[1]), high=float(x[2]), low=float(x[3]), close=float(x[4])) for x in rows]
        out = [b for b in out if b["time"] + 300 <= now]
        if len(out) > 100:
            return out, "Kraken"
    except Exception as ex:
        log(f"Kraken فشل: {ex}")
    try:
        r = requests.get("https://www.okx.com/api/v5/market/candles", params={"instId": "PAXG-USDT", "bar": "5m", "limit": 300}, timeout=15).json()
        out = sorted((dict(time=int(x[0]) // 1000, open=float(x[1]), high=float(x[2]), low=float(x[3]), close=float(x[4])) for x in r["data"] if x[8] == "1"),
                     key=lambda b: b["time"])
        if len(out) > 100:
            return out, "OKX"
    except Exception as ex:
        log(f"OKX فشل: {ex}")
    return None, None


def merge_realtime(yb, rt, now):
    """يلصق شموع اللحظي بعد آخر شمعة موثوقة من Yahoo (تُحذف آخر 20 دقيقة منه لأنها قد تكون ناقصة) بعد مواءمة المستوى."""
    yb = [b for b in yb if b["time"] + 300 <= now - 1200]
    if not rt or not yb or now - (yb[-1]["time"] + 300) > 3600:         # لا بيانات Yahoo حديثة = السوق مغلق
        return yb, None
    rtd = {b["time"]: b for b in rt}
    diffs = sorted(b["close"] - rtd[b["time"]]["close"] for b in yb[-48:] if b["time"] in rtd)
    if len(diffs) < 2:
        return yb, None
    s = diffs[len(diffs) // 2]
    new = [dict(b, open=b["open"] + s, high=b["high"] + s, low=b["low"] + s, close=b["close"] + s) for b in rt if b["time"] > yb[-1]["time"]]
    return yb + new, s


def resample(bars, minutes):
    """يجمّع شموع 5 دقائق إلى فريم أكبر (مجموعات مكتملة فقط)."""
    sec = minutes * 60; n = sec // 300; groups = {}
    for b in bars:
        groups.setdefault(b["time"] // sec * sec, []).append(b)
    out = []
    for t in sorted(groups):
        g = groups[t]
        if len(g) == n:
            out.append(dict(time=t, open=g[0]["open"], high=max(x["high"] for x in g), low=min(x["low"] for x in g), close=g[-1]["close"]))
    return out


def shift(bars, off):
    if not off:
        return bars
    return [dict(b, open=b["open"] - off, high=b["high"] - off, low=b["low"] - off, close=b["close"] - off) for b in bars]


def params(cfg):
    P = {**DEFAULTS, **cfg.get("strategy", {})}
    if "sessions" in cfg.get("strategy", {}):                  # دمج الجلسات بدل الاستبدال الكامل
        P["sessions"] = {k: {**DEFAULTS["sessions"][k], **cfg["strategy"]["sessions"].get(k, {})} for k in DEFAULTS["sessions"]}
    return P


def compute(cfg, state):
    P = params(cfg)
    now = time.time()
    chart = yf_candles(cfg["ticker"], "5m", "30d", P["chartTF"])
    live, src = LIVE.get(cfg["ticker"]), "Yahoo"
    if P["chartTF"] == 5 and P["mainTF"] == 15:
        rt, name = realtime_5m(now)
        merged, s = merge_realtime(chart, rt, now)
        if s is not None:
            chart = merged; src = f"{name} لحظي"
            live = chart[-1]["close"]
            main = resample(chart, 15)
            log(f"مصدر الأسعار: Yahoo (تاريخ) + {name} (لحظي) | آخر شمعة مغلقة قبل {(now - chart[-1]['time'] - 300) / 60:.1f} دقيقة | مواءمة {s:+.2f}$")
        else:
            main = yf_candles(cfg["ticker"], "15m", "30d", P["mainTF"])
            log("تعذّر الدمج اللحظي (السوق مغلق أو المصدر غير متاح): نعتمد Yahoo المتأخر")
    else:
        main = yf_candles(cfg["ticker"], "15m", "30d", P["mainTF"])
    if cfg.get("spotCorrection", True):
        fresh = src != "Yahoo" or (now - (chart[-1]["time"] + 300)) <= 1500
        off = calc_offset(state, cfg.get("spotSym", "XAU"), live) if fresh else state.get("off")
        if off is not None:
            state["off"] = off
            chart, main = shift(chart, off), shift(main, off)
            cfg["dataNote"] = f"الأسعار ({src}) مصحّحة بفرق الذهب الفوري ({off:.1f}$) فتقارب شارتك بفرق صغير: اعتمد على المسافات."
        else:
            cfg["dataNote"] = "الأسعار بلا تصحيح الآن (السوق مغلق)، وقد تزيد 15–28$ عن شارتك: اعتمد على المسافات."
    return run_engine(chart, main, P), chart[-1]["time"], P


def main():
    cfg = json.load(open(os.path.join(HERE, "sh_config.json"), encoding="utf-8"))
    cfg["telegramToken"] = (os.environ.get("TELEGRAM_TOKEN") or cfg.get("telegramToken", "")).strip()
    cfg["chatId"] = str(cfg.get("chatId") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()   # معرّف المحادثة (ليس سرّاً) من ملف الإعدادات أولاً
    args = sys.argv[1:]
    tg = core.Telegram(cfg["telegramToken"], cfg["chatId"])
    if "--test" in args:
        t = cfg["telegramToken"]
        log(f"فحص الإعدادات: طول التوكن {len(t)} (المتوقع 46) | معرّف المحادثة: {cfg['chatId']!r}")
        ok = tg.send(f"🟦 {NAME} (GitHub): الاتصال بتلجرام يعمل.")
        print("Telegram:", "OK" if ok else "فشل")
        sys.exit(0 if ok else 1)                    # تظهر الخطوة حمراء في GitHub عند الفشل
    state = load_state()
    if "--history" in args:
        evs, last_t, P = compute(cfg, state)
        k = args.index("--history")
        days = int(args[k + 1]) if len(args) > k + 1 else 3
        for e in evs:
            if e["bar_time"] >= last_t - days * 86400:
                print("─" * 40)
                print(message(e, cfg, P).replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
        return
    # GitHub يؤخر الجدولة أحياناً 25–40 دقيقة، فكل تشغيل يفحص كل دقيقة لمدة --loop دقيقة ليغطي الفجوات
    loop_min = float(args[args.index("--loop") + 1]) if "--loop" in args else 0
    deadline = time.time() + loop_min * 60
    sent = set(state["sent"])
    max_age = cfg.get("maxAgeMin", 60) * 60
    while True:
        try:
            evs, last_t, P = compute(cfg, state)
            fresh = [e for e in evs if e["bar_time"] >= time.time() - max_age and ev_key(e) not in sent]
            log(f"{NAME} | آخر شمعة مغلقة: {dt.datetime.fromtimestamp(last_t, dt.timezone.utc):%Y-%m-%d %H:%M} UTC | أحداث جديدة: {len(fresh)}")
            for e in fresh:
                txt = message(e, cfg, P)
                if txt and tg.send(txt):
                    log(f"أُرسل: {e['kind']} {ev_key(e)}")
                    sent.add(ev_key(e))
            state["sent"] = list(sent)
            save_state(state)
        except Exception as ex:
            log(f"خطأ: {ex}")
        if time.time() + 60 >= deadline:
            break
        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:                 # فشل مؤقت: نسجّله ونكمل، التشغيل التالي يعيد المحاولة
        log(f"خطأ: {ex}")
        sys.exit(0)
