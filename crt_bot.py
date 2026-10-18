# -*- coding: utf-8 -*-
"""
CRT By Diaa — بوت تلجرام لإشارات الذهب.

يقرأ أسعار الذهب من MetaTrader 5 ويطبّق منطق CRT By Diaa (crt_engine.py):
شمعة CRT على فريم 1H/2H/4H/يومي → كسر السيولة وإغلاق داخل الرينج → تأكيد دخول على 5 دقائق
(CISD / MSS+FVG / Model #1) → يرسل الصفقة (الدخول، الستوب، TP1، TP2) فور إغلاق شمعة التأكيد،
ثم تحديثات الصفقة (TP1 + نقل الستوب، الإغلاق).
البوت لا يفتح أي صفقة على حسابك — قراءة أسعار فقط.

التشغيل:
    python crt_bot.py                 تشغيل البوت
    python crt_bot.py --get-chat-id   معرفة رقم المحادثة
    python crt_bot.py --test          رسالة تجربة إلى تلجرام
    python crt_bot.py --history 5     عرض أحداث آخر 5 أيام في الشاشة بدون إرسال
"""
import json, os, sys, time, datetime as dt
import requests
import pandas as pd
from crt_engine import Context, DEFAULTS, MAX_SCORE, needed_conf, resample_min

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "bot.log")
OFFSET_PATH = os.path.join(HERE, "utc_offset.json")
NAME = "CRT By Diaa"


def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_cfg():
    with open(CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"sent": []}


def save_state(st):
    st["sent"] = st["sent"][-800:]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ───────────────────────── تلجرام ─────────────────────────
class Telegram:
    def __init__(self, token, chat_id):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id

    def send(self, text):
        for attempt in range(3):
            try:
                r = requests.post(self.base + "/sendMessage",
                                  data={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                        "disable_web_page_preview": "true"}, timeout=15)
                if r.ok:
                    return True
                log(f"Telegram error {r.status_code}: {r.text[:200]}")
            except Exception as e:
                log(f"Telegram exception: {e}")
            time.sleep(2 * (attempt + 1))
        return False

    def chat_ids(self):
        r = requests.get(self.base + "/getUpdates", timeout=15).json()
        out = {}
        for u in r.get("result", []):
            msg = u.get("message") or u.get("channel_post") or {}
            ch = msg.get("chat", {})
            if ch:
                out[ch["id"]] = ch.get("title") or ch.get("username") or ch.get("first_name")
        return out


# ───────────────────────── الرسائل ─────────────────────────
def fmt_ny(ts, add_min=0):
    t = ts + pd.Timedelta(minutes=add_min)
    return t.strftime("%Y-%m-%d %H:%M") + " (نيويورك)"


def conf_name(m):
    return {1: "1 دقيقة", 3: "3 دقائق", 5: "5 دقائق", 15: "15 دقيقة", 60: "الساعة", 240: "4 ساعات"}.get(m, f"{m}m")


def lots_for(risk, cfg, contract):
    bal = cfg.get("_balance") or cfg["accountSize"]
    return (bal * cfg["riskPct"] / 100) / (risk * contract) if risk > 0 else 0


def message(ev, cfg, contract, P):
    tr = ev["trade"]; sym = cfg["symbol"]
    if ev["kind"] == "setup":
        st = ev["setup"]
        side = "شراء" if st["isb"] else "بيع"
        poi = "+".join(st["poi"]) if st["poi"] else "-"
        bias_t = "صاعد" if st["bias"] == 1 else "هابط" if st["bias"] == -1 else "محايد"
        return (f"👀 إعداد CRT {side} على {ev['tf']} — {st['grade']} ({st['score']}/{MAX_SCORE})\n"
                f"الرينج: {st['L']:.2f} – {st['H']:.2f} | القمة/القاع المكسور: {st['ext']:.2f}\n"
                f"منطقة الاهتمام: {poi} | سيولة مفتاحية: {'✔' if st['key'] else '✘'} | التحيز اليومي: {bias_t}\n"
                f"RSI تشبع: {'✔' if st['rsi'] else '✘'} | SMT فضة: {'✔' if st['smt'] else '✘'} — ننتظر CISD على {conf_name(ev['conf'])}")
    side = "🟢 <b>BUY</b>" if tr["dir"] == 1 else "🔴 <b>SELL</b>"
    if ev["kind"] == "signal":
        pct = P["tp1Pct"]
        poi = "+".join(tr["poi"]) if tr["poi"] else "-"
        bias_t = "صاعد" if tr["bias"] == 1 else "هابط" if tr["bias"] == -1 else "محايد"
        return (f"{side} {sym}  —  CRT {tr['tf']} ← تأكيد {conf_name(tr['conf'])}  [{tr['tag']}]\n"
                f"الدرجة: <b>{tr['grade']}</b> ({tr['score']}/{MAX_SCORE})  |  منطقة الاهتمام: {poi}  |  التحيز اليومي: {bias_t}\n"
                f"RSI تشبع: {'✔' if tr['rsi'] else '✘'}  |  SMT فضة: كسر السيولة {'✔' if tr['smt'] else '✘'} / عند الدخول {'✔' if tr['smt_ltf'] else '✘'}\n"
                f"الدخول: <b>{tr['entry']:.2f}</b>\n"
                f"الستوب: <b>{tr['sl0']:.2f}</b>  ({tr['risk']:.2f}$)\n"
                f"TP1: <b>{tr['tp1']:.2f}</b>  (1:{tr['rr1']:.2f}) — إغلاق {pct:g}% ثم الستوب للدخول\n"
                f"TP2: <b>{tr['tp2']:.2f}</b>  (1:{tr['rr2']:.2f})\n"
                f"المسافات من الدخول: ستوب {tr['risk']:.2f}$ | TP1 {abs(tr['tp1'] - tr['entry']):.2f}$ | TP2 {abs(tr['tp2'] - tr['entry']):.2f}$\n"
                + (f"<i>{cfg['dataNote']}</i>\n" if cfg.get("dataNote") else "") +
                f"اللوت المقترح: {lots_for(tr['risk'], cfg, contract):.2f}  (مخاطرة {cfg['riskPct']}%)\n"
                f"🕒 {fmt_ny(ev['bar_time'], ev['conf'])}\n"
                f"<i>{NAME} — إشارة تعليمية وليست نصيحة استثمارية</i>")
    if ev["kind"] == "tp1":
        extra = f"وانقل الستوب إلى الدخول {tr['entry']:.2f}" if P["be"] else ""
        return f"🎯 {side} {sym} (CRT {tr['tf']}): تحقق TP1 {tr['tp1']:.2f} — أغلق {P['tp1Pct']:g}% {extra}"
    if ev["kind"] == "closed":
        icon = "✅" if ev["R"] > 0.001 else ("❌" if ev["R"] < -0.001 else "⚪")
        return (f"{icon} {side} {sym} (CRT {tr['tf']}): أُغلقت الصفقة — {ev['result']} ({ev['R']:+.2f}R)\n"
                f"الدخول {tr['entry']:.2f} | الستوب الأصلي {tr['sl0']:.2f} | TP2 {tr['tp2']:.2f}")
    return None


def ev_key(ev):
    t = ev["trade"]["sig_time"] if ev.get("trade") else str(ev["bar_time"])
    d = ev["trade"]["dir"] if ev.get("trade") else ev["setup"]["isb"]
    return f"{ev['kind']}|{ev['tf']}|{t}|{d}|{ev['bar_time']}"


# ───────────────────────── MT5 ─────────────────────────
def server_utc_offset(mt5, symbol):
    tk = mt5.symbol_info_tick(symbol)
    if tk:
        off = (tk.time - time.time()) / 3600
        if abs(off - round(off)) < 0.1 and -12 <= round(off) <= 14:
            with open(OFFSET_PATH, "w") as f:
                json.dump({"offset": round(off)}, f)
            return round(off)
    if os.path.exists(OFFSET_PATH):
        with open(OFFSET_PATH) as f:
            return json.load(f)["offset"]
    return 3


def mt5_frame(mt5, symbol, tf, count, off):
    r = mt5.copy_rates_from_pos(symbol, tf, 1, count)   # start=1 → شموع مغلقة فقط
    if r is None or len(r) == 0:
        raise RuntimeError(f"لا توجد بيانات لـ {symbol}: {mt5.last_error()}")
    df = pd.DataFrame(r)
    idx = pd.to_datetime(df["time"] - off * 3600, unit="s", utc=True).dt.tz_convert("America/New_York").dt.tz_localize(None)
    out = pd.DataFrame({"Open": df["open"].values, "High": df["high"].values, "Low": df["low"].values,
                        "Close": df["close"].values, "Volume": df["tick_volume"].values.astype(float)}, index=pd.DatetimeIndex(idx))
    return out[~out.index.duplicated(keep="first")]


def connect(cfg):
    import MetaTrader5 as mt5
    kw = {"path": cfg["mt5Path"]} if cfg.get("mt5Path") else {}
    if not mt5.initialize(**kw):
        raise RuntimeError(f"فشل الاتصال بـ MT5: {mt5.last_error()}")
    if not mt5.symbol_select(cfg["symbol"], True):
        raise RuntimeError(f"الرمز {cfg['symbol']} غير موجود عند البروكر")
    info = mt5.symbol_info(cfg["symbol"])
    acct = mt5.account_info()
    if cfg.get("useAccountBalance") and acct:
        cfg["_balance"] = acct.balance
    return mt5, (info.trade_contract_size if info else 100.0)


def params(cfg):
    P = dict(DEFAULTS); P.update(cfg.get("strategy", {}))
    return P


MT5_TF = {1: "TIMEFRAME_M1", 3: "TIMEFRAME_M3", 5: "TIMEFRAME_M5", 15: "TIMEFRAME_M15"}


def compute(mt5, cfg):
    P = params(cfg); s = cfg["symbol"]
    off = server_utc_offset(mt5, s)
    need = sorted(m for m in needed_conf(P) if m in MT5_TF)
    n_conf = cfg.get("historyBarsConf", 5000)
    conf = {m: mt5_frame(mt5, s, getattr(mt5, MT5_TF[m]), n_conf, off) for m in need}
    h1 = mt5_frame(mt5, s, mt5.TIMEFRAME_H1, cfg["historyBarsH1"], off)
    ag1 = None; agc = {}
    if P.get("useSMT") and cfg.get("smtSymbol"):
        try:
            if mt5.symbol_select(cfg["smtSymbol"], True):
                ag1 = mt5_frame(mt5, cfg["smtSymbol"], mt5.TIMEFRAME_H1, cfg["historyBarsH1"], off)
                agc = {m: mt5_frame(mt5, cfg["smtSymbol"], getattr(mt5, MT5_TF[m]), n_conf, off) for m in need}
        except Exception as ex:
            log(f"تعذّر جلب الفضة (SMT معطّل): {ex}")
            ag1 = None; agc = {}
    ctx = Context(h1, conf, P, ag1, agc)
    evs, _, _ = ctx.all_events()
    last_t = max(df.index[-1] for df in conf.values()) if conf else h1.index[-1]
    return evs, last_t, P


# ───────────────────────── التشغيل ─────────────────────────
def main():
    cfg = load_cfg()
    args = sys.argv[1:]
    tg = Telegram(cfg["telegramToken"], cfg.get("chatId"))

    if "--get-chat-id" in args:
        ids = tg.chat_ids()
        if not ids:
            print("لا توجد رسائل. أرسل أي رسالة للبوت في تلجرام ثم أعد المحاولة.")
        for k, v in ids.items():
            print(f"chatId = {k}    ({v})")
        return
    if "--test" in args:
        ok = tg.send(f"✅ {NAME} Bot: الاتصال بتلجرام يعمل.")
        print("تم الإرسال" if ok else "فشل الإرسال — راجع التوكن ورقم المحادثة")
        return

    if "--history" not in args:
        import socket
        global _LOCK
        _LOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            _LOCK.bind(("127.0.0.1", 47392))
        except OSError:
            return

    while True:
        try:
            mt5, contract = connect(cfg)
            break
        except Exception as ex:
            log(f"MT5 غير جاهز: {ex} — إعادة المحاولة بعد دقيقة")
            time.sleep(60)

    if "--history" in args:
        k = args.index("--history")
        days = int(args[k + 1]) if len(args) > k + 1 else 5
        evs, last_t, P = compute(mt5, cfg)
        for e in evs:
            if e["kind"] == "setup" and not cfg.get("notifySetups", False):
                continue
            if e["bar_time"] >= last_t - pd.Timedelta(days=days):
                print("─" * 40)
                print(message(e, cfg, contract, P).replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
        return

    state = load_state()
    sent = set(state["sent"])
    log(f"البوت يعمل: {cfg['symbol']} — {NAME} — ينتظر إغلاق الشموع")
    if cfg.get("startupMessage", True):
        P0 = params(cfg)
        tg.send(f"🤖 البوت بدأ العمل على {cfg['symbol']}\nالنظام: {NAME} — CRT على {'/'.join(P0['crtTFs'])} + دخول 5m ({P0['model']})")
    last_bar = None
    trig = min([m for m in needed_conf(params(cfg)) if m in MT5_TF] or [5])
    while True:
        try:
            if not mt5.terminal_info():
                log("انقطع الاتصال بـ MT5 — إعادة الاتصال")
                mt5, contract = connect(cfg)
            r = mt5.copy_rates_from_pos(cfg["symbol"], getattr(mt5, MT5_TF[trig]), 1, 1)
            if r is not None and len(r):
                bt = int(r[0]["time"])
                if bt != last_bar:
                    last_bar = bt
                    events, last_t, P = compute(mt5, cfg)
                    fresh = [e for e in events if e["bar_time"] >= last_t - pd.Timedelta(minutes=cfg.get("freshMin", 30)) and ev_key(e) not in sent
                             and (e["kind"] != "setup" or cfg.get("notifySetups", False))]
                    for e in fresh:
                        txt = message(e, cfg, contract, P)
                        if txt and tg.send(txt):
                            log(f"أُرسل: {e['kind']} {ev_key(e)}")
                            sent.add(ev_key(e))
                    state["sent"] = list(sent)
                    save_state(state)
            time.sleep(cfg.get("pollSeconds", 5))
        except KeyboardInterrupt:
            log("تم الإيقاف")
            break
        except Exception as ex:
            log(f"خطأ: {ex}")
            time.sleep(10)


if __name__ == "__main__":
    main()
