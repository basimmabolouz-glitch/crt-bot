# -*- coding: utf-8 -*-
"""
CRT By Diaa — نسخة GitHub Actions (تعمل مرة واحدة كل 5 دقائق، بدون سيرفر وبدون مفاتيح أسعار).

الأسعار من Yahoo Finance (عقود الذهب GC=F والفضة SI=F). التوكن ورقم المحادثة من أسرار GitHub:
TELEGRAM_TOKEN و TELEGRAM_CHAT_ID. الحالة (ما أُرسل) تُحفظ في state.json ويُرفع تلقائياً للمستودع.

    python crt_bot_actions.py              تشغيل مرة واحدة (هذا ما يفعله GitHub كل 5 دقائق)
    python crt_bot_actions.py --test       رسالة تجربة إلى تلجرام
    python crt_bot_actions.py --history 5  أحداث آخر 5 أيام على الشاشة بدون إرسال
"""
import json, os, sys, time
import pandas as pd
import requests
import yfinance as yf

import crt_bot as core
from crt_engine import Context, needed_conf, resample_min

HERE = os.path.dirname(os.path.abspath(__file__))
NY = "America/New_York"


def now_ny():
    return pd.Timestamp.now(tz=NY).tz_localize(None)


def yf_frame(ticker, interval, period, minutes):
    """شموع مكتملة فقط بتوقيت نيويورك (naive). إعادة المحاولة عند رفض Yahoo."""
    last = None
    for attempt in range(4):
        try:
            d = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=False)
            d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
            d = d[["Open", "High", "Low", "Close", "Volume"]].dropna()
            if len(d) < 100:
                raise RuntimeError(f"بيانات قليلة لـ {ticker} {interval}: {len(d)}")
            d.index = d.index.tz_convert(NY).tz_localize(None)
            d = d[~d.index.duplicated(keep="first")]
            return d[d.index + pd.Timedelta(minutes=minutes) <= now_ny()]      # استبعاد الشمعة الجارية
        except Exception as ex:
            last = ex
            time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"فشل جلب {ticker} {interval}: {last}")


# ── تصحيح فرق العقود الآجلة عن الذهب الفوري (شارت TradingView) ──
OFFSETS = {}                                   # يُحدَّث في compute() ويُحفظ في state.json
SPOT_URL = "https://api.gold-api.com/price/{}"


def spot_quote(sym, ref_price):
    """سعر الفوري الحالي (بدون مفتاح). None إذا فشل أو بدا غير منطقي."""
    try:
        r = requests.get(SPOT_URL.format(sym), timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        p = float(r.json()["price"])
        if ref_price and abs(p - ref_price) / ref_price > 0.06:     # فرق أكبر من 6%: مستبعد
            return None
        return p
    except Exception:
        return None


def calc_offset(key, sym, frames, ref_ok=True):
    """offset = سعر الآجل - سعر الفوري. يُنعَّم بمتوسط متحرك عبر التشغيلات، ويُحدَّث فقط والسوق نشط."""
    state = core.load_state()
    old = state.get("off", {}).get(key)
    cand = [(df.index[-1] + pd.Timedelta(minutes=m), float(df.Close.iloc[-1]))
            for m, df in frames.items() if m in (1, 3, 5, 15)]
    if not cand:
        return old
    t_close, last_c = max(cand)
    age_min = (now_ny() - t_close).total_seconds() / 60.0
    fresh = age_min <= 5.0 or os.environ.get("FORCE_OFFSET") == "1"       # FORCE للاختبار فقط
    q = spot_quote(sym, last_c) if fresh else None
    if q is None:
        return old
    raw = last_c - q
    new = raw if old is None else 0.7 * old + 0.3 * raw
    core.log(f"تصحيح {key}: آجل {last_c:.2f} - فوري {q:.2f} = {raw:.2f}$ (المعتمد {new:.2f}$)")
    return new


def shift(df, off):
    if not off:
        return df
    d = df.copy()
    for c in ("Open", "High", "Low", "Close"):
        d[c] = d[c] - off
    return d


OANDA_G = {1: "M1", 5: "M5", 15: "M15"}


def oanda_on():
    return bool(os.environ.get("OANDA_TOKEN", "").strip())


def oanda_get(inst, gran, count):
    """شموع OANDA المكتملة فقط → DataFrame بتوقيت نيويورك (naive)."""
    env = os.environ.get("OANDA_ENV", "practice")
    base = "https://api-fxpractice.oanda.com" if env == "practice" else "https://api-fxtrade.oanda.com"
    h = {"Authorization": "Bearer " + os.environ["OANDA_TOKEN"].strip(), "Accept-Datetime-Format": "UNIX"}
    last = None
    for attempt in range(3):
        try:
            r = requests.get(f"{base}/v3/instruments/{inst}/candles", headers=h,
                             params={"granularity": gran, "count": min(count, 5000), "price": "M"}, timeout=25)
            r.raise_for_status()
            rows = [(float(c["time"]), float(c["mid"]["o"]), float(c["mid"]["h"]), float(c["mid"]["l"]), float(c["mid"]["c"]),
                     float(c.get("volume", 0))) for c in r.json().get("candles", []) if c.get("complete")]
            if len(rows) < 100:
                raise RuntimeError(f"شموع قليلة من OANDA لـ {inst} {gran}: {len(rows)}")
            df = pd.DataFrame(rows, columns=["t", "Open", "High", "Low", "Close", "Volume"])
            idx = pd.to_datetime(df["t"], unit="s", utc=True).dt.tz_convert(NY).dt.tz_localize(None)
            out = df.drop(columns="t"); out.index = pd.DatetimeIndex(idx)
            return out[~out.index.duplicated(keep="first")]
        except Exception as ex:
            last = ex
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"فشل جلب OANDA {inst} {gran}: {last}")


def oanda_conf_frames(inst, mins, n_conf):
    out = {}
    for m in mins:
        src = 1 if m == 3 else m
        if src in OANDA_G and src not in out:
            out[src] = oanda_get(inst, OANDA_G[src], n_conf * (3 if m == 3 else 1))
    if 3 in mins and 1 in out:
        r = resample_min(out[1], 3)
        out[3] = r[r.index + pd.Timedelta(minutes=3) <= now_ny()].tail(n_conf)
    return {m: out[m] for m in mins if m in out}


YF_IV = {1: ("1m", "7d"), 5: ("5m", "30d"), 15: ("15m", "30d")}      # 3m يُبنى من 1m (Yahoo لا يوفّر 3m)


def conf_frames(ticker, mins, n_conf):
    out = {}
    for m in mins:
        src = 1 if m == 3 else m
        if src not in out and src not in {1, 5, 15}:
            continue
        if src not in out and (src in YF_IV):
            iv, per = YF_IV[src]
            out[src] = yf_frame(ticker, iv, per, src).tail(n_conf * (3 if m == 3 else 1))
    if 3 in mins and 1 in out:
        r = resample_min(out[1], 3)
        out[3] = r[r.index + pd.Timedelta(minutes=3) <= now_ny()].tail(n_conf)
    return {m: out[m] for m in mins if m in out}


def compute(cfg):
    P = core.params(cfg)
    need = sorted(m for m in needed_conf(P) if m in (1, 3, 5, 15))
    n_conf = cfg.get("historyBarsConf", 5000)
    n_h1 = cfg.get("historyBarsH1", 4000)
    if oanda_on():
        inst, sinst = cfg.get("oandaInstrument", "XAU_USD"), cfg.get("oandaSmt", "XAG_USD")
        conf = oanda_conf_frames(inst, need, n_conf)
        h1 = oanda_get(inst, "H1", n_h1).tail(n_h1)
        ag1 = None; agc = {}
        if P.get("useSMT") and sinst:
            try:
                ag1 = oanda_get(sinst, "H1", n_h1).tail(n_h1)
                agc = oanda_conf_frames(sinst, need, n_conf)
            except Exception as ex:
                core.log(f"تعذّر جلب الفضة من OANDA (SMT معطّل في هذا التشغيل): {ex}")
                ag1 = None; agc = {}
        cfg["dataNote"] = ""                     # أسعار OANDA = نفس شارتك
    else:
        conf = conf_frames(cfg["ticker"], need, n_conf)
        h1 = yf_frame(cfg["ticker"], "1h", "730d", 60).tail(n_h1)
        ag1 = None; agc = {}
        if P.get("useSMT") and cfg.get("smtTicker"):
            try:
                ag1 = yf_frame(cfg["smtTicker"], "1h", "730d", 60).tail(n_h1)
                agc = conf_frames(cfg["smtTicker"], need, n_conf)
            except Exception as ex:
                core.log(f"تعذّر جلب الفضة (SMT معطّل في هذا التشغيل): {ex}")
                ag1 = None; agc = {}
        if cfg.get("spotCorrection", True):
            og = calc_offset("xau", "XAU", conf)
            if og is not None:
                OFFSETS["xau"] = og
                conf = {m: shift(df, og) for m, df in conf.items()}
                h1 = shift(h1, og)
            if ag1 is not None:
                os_ = calc_offset("xag", "XAG", agc)
                if os_ is not None:
                    OFFSETS["xag"] = os_
                    agc = {m: shift(df, os_) for m, df in agc.items()}
                    ag1 = shift(ag1, os_)
            if og is not None:
                cfg["dataNote"] = f"الأسعار من Yahoo مصحّحة بفرق الذهب الفوري ({og:.1f}$) فتقارب شارتك بفرق صغير (قد يصل 3$): اعتمد على المسافات."
            else:
                cfg["dataNote"] = "الأسعار من عقود Yahoo الآجلة بلا تصحيح الآن (السوق مغلق)، وقد تزيد 15–28$ عن شارتك: اعتمد على المسافات."
    ctx = Context(h1, conf, P, ag1, agc)
    evs, _, _ = ctx.all_events()
    last_t = max(df.index[-1] for df in conf.values()) if conf else h1.index[-1]
    return evs, last_t, P


def main():
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    cfg["telegramToken"] = (os.environ.get("TELEGRAM_TOKEN") or cfg.get("telegramToken", "")).strip()
    cfg["chatId"] = str(cfg.get("chatId") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()   # معرّف المحادثة (ليس سرّاً) من ملف الإعدادات أولاً
    args = sys.argv[1:]
    tg = core.Telegram(cfg["telegramToken"], cfg["chatId"])
    contract = cfg.get("contractSize", 100.0)

    if "--test" in args:
        print("Telegram:", "OK" if tg.send(f"✅ {core.NAME} (GitHub): الاتصال بتلجرام يعمل.") else "فشل")
        return
    if "--history" in args:
        k = args.index("--history")
        days = int(args[k + 1]) if len(args) > k + 1 else 5
        evs, last_t, P = compute(cfg)
        for e in evs:
            if e["kind"] == "setup" and not cfg.get("notifySetups", False):
                continue
            if e["bar_time"] >= last_t - pd.Timedelta(days=days):
                print("─" * 40)
                print(core.message(e, cfg, contract, P).replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
        return

    evs, last_t, P = compute(cfg)
    state = core.load_state()
    sent = set(state["sent"])
    max_age = pd.Timedelta(minutes=cfg.get("maxAgeMin", 45))      # لا نرسل إشارة قديمة بعد تأخر التشغيل
    fresh = [e for e in evs if e["bar_time"] >= now_ny() - max_age and core.ev_key(e) not in sent
             and (e["kind"] != "setup" or cfg.get("notifySetups", False))]
    core.log(f"المصدر: {'OANDA' if oanda_on() else 'Yahoo'} | آخر شمعة مغلقة: {last_t} | أحداث جديدة: {len(fresh)}")
    for e in fresh:
        txt = core.message(e, cfg, contract, P)
        if txt and tg.send(txt):
            core.log(f"أُرسل: {e['kind']} {core.ev_key(e)}")
            sent.add(core.ev_key(e))
    state["sent"] = list(sent)
    if OFFSETS:
        state["off"] = {**state.get("off", {}), **OFFSETS}
    core.save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:                 # فشل مؤقت (Yahoo مثلاً): نسجّله ونكمل، التشغيل التالي يعيد المحاولة
        core.log(f"خطأ: {ex}")
        sys.exit(0)
