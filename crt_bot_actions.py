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
import yfinance as yf

import crt_bot as core
from crt_engine import Context

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


def compute(cfg):
    P = core.params(cfg)
    m5 = yf_frame(cfg["ticker"], "5m", "30d", 5).tail(cfg.get("historyBarsM5", 5000))
    h1 = yf_frame(cfg["ticker"], "1h", "730d", 60).tail(cfg.get("historyBarsH1", 4000))
    ag1 = ag5 = None
    if P.get("useSMT") and cfg.get("smtTicker"):
        try:
            ag1 = yf_frame(cfg["smtTicker"], "1h", "730d", 60).tail(cfg.get("historyBarsH1", 4000))
            ag5 = yf_frame(cfg["smtTicker"], "5m", "30d", 5).tail(cfg.get("historyBarsM5", 5000))
        except Exception as ex:
            core.log(f"تعذّر جلب الفضة (SMT معطّل في هذا التشغيل): {ex}")
    ctx = Context(h1, m5, P, ag1, ag5)
    evs, _, _ = ctx.all_events()
    return evs, m5.index[-1], P


def main():
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    cfg["telegramToken"] = os.environ.get("TELEGRAM_TOKEN", cfg.get("telegramToken", ""))
    cfg["chatId"] = os.environ.get("TELEGRAM_CHAT_ID", cfg.get("chatId", ""))
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
    core.log(f"آخر شمعة مغلقة: {last_t} | أحداث جديدة: {len(fresh)}")
    for e in fresh:
        txt = core.message(e, cfg, contract, P)
        if txt and tg.send(txt):
            core.log(f"أُرسل: {e['kind']} {core.ev_key(e)}")
            sent.add(core.ev_key(e))
    state["sent"] = list(sent)
    core.save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:                 # فشل مؤقت (Yahoo مثلاً): نسجّله ونكمل، التشغيل التالي يعيد المحاولة
        core.log(f"خطأ: {ex}")
        sys.exit(0)
