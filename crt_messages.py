# -*- coding: utf-8 -*-
"""رسائل تلجرام لـ CRT By Diaa: لغة بسيطة وترتيب واضح (HTML)."""
import pandas as pd
from crt_engine import MAX_SCORE

NAME = "CRT By Diaa"
LINE = "━━━━━━━━━━━━━━"


def fmt_ny(ts, add_min=0):
    t = ts + pd.Timedelta(minutes=add_min)
    return t.strftime("%Y-%m-%d %H:%M") + " (توقيت نيويورك)"


def conf_name(m):
    return {1: "دقيقة", 3: "3 دقائق", 5: "5 دقائق", 15: "15 دقيقة", 60: "ساعة", 240: "4 ساعات"}.get(m, f"{m}m")


def tf_name(t):
    return {"W": "الأسبوع", "D": "اليوم", "4H": "4 ساعات", "2H": "ساعتين", "1H": "ساعة"}.get(t, t)


def dur_text(minutes):
    minutes = int(minutes)
    if minutes >= 120 and minutes % 60 == 0:
        return f"{minutes // 60} ساعة"
    return f"{minutes} دقيقة"


def lots_for(risk, cfg, contract):
    bal = cfg.get("_balance") or cfg["accountSize"]
    return (bal * cfg["riskPct"] / 100) / (risk * contract) if risk > 0 else 0


def _yn(b):
    return "✅" if b else "❌"


def _bias(b):
    return "صاعد ⬆️" if b == 1 else "هابط ⬇️" if b == -1 else "محايد"


def _why(x, tf, conf):
    if x.get("poi"):
        poi = " + ".join(x["poi"]) + (" + مستوى سيولة رئيسي" if x.get("key") else "")
        poi_ok = True
    elif x.get("key"):
        poi = "مستوى سيولة رئيسي (قمة/قاع أمس أو الأسبوع أو سوينغ)"
        poi_ok = True
    else:
        poi, poi_ok = "لا يوجد", False
    smt = bool(x.get("smt") or x.get("smt_ltf"))
    return ("🧠 <b>لماذا هذه الصفقة؟</b>\n"
            f"• نموذج CRT على فريم {tf_name(tf)}، والتأكيد من فريم {conf_name(conf)}\n"
            f"• التحيز اليومي: {_bias(x['bias'])}\n"
            f"• سحب السيولة داخل منطقة اهتمام: {poi} {_yn(poi_ok)}\n"
            f"• SMT مع الفضة: {_yn(smt)}\n"
            f"• تشبع RSI: {_yn(x.get('rsi'))}\n"
            f"• قوة الإعداد: {x['grade']} ({x['score']} من {MAX_SCORE})")


def _foot(cfg, ev):
    note = f"{LINE}\n<i>{cfg['dataNote']}</i>\n" if cfg.get("dataNote") else ""
    return (note + f"🕒 {fmt_ny(ev['bar_time'], ev['conf'])}\n"
            f"<i>{NAME} — إشارة تعليمية وليست نصيحة استثمارية</i>")


def message(ev, cfg, contract, P):
    sym = "الذهب"
    k = ev["kind"]

    if k == "setup":
        st = ev["setup"]
        side = "شراء" if st["isb"] else "بيع"
        poi = " + ".join(st["poi"]) if st["poi"] else "مستوى سيولة رئيسي"
        return (f"👀 <b>إعداد قيد المراقبة — {side} {sym}</b>\n{LINE}\n"
                f"نموذج CRT على فريم {tf_name(ev['tf'])} تحقق، وننتظر تأكيد CISD من فريم {conf_name(ev['conf'])}.\n"
                f"منطقة الاهتمام: {poi}\n"
                f"التحيز اليومي: {_bias(st['bias'])}\n"
                f"قوة الإعداد: {st['grade']} ({st['score']} من {MAX_SCORE})")

    if k in ("pending", "pending_cancel"):
        p = ev["pend"]
        side = "شراء" if p["isb"] else "بيع"
        icon = "🟢" if p["isb"] else "🔴"
        order = "Buy Limit" if p["isb"] else "Sell Limit"
        if k == "pending_cancel":
            return (f"⚠️ <b>ألغِ الأمر المعلق</b> ({side} {sym})\n{LINE}\n"
                    f"السبب: {ev['reason']}.\n"
                    f"إن كنت وضعت أمراً عند {p['px']:.2f} فاحذفه الآن.")
        return (f"⏳ <b>جهّز أمراً معلقاً</b>\n"
                f"{icon} <b>{side} {sym}</b> ({order})\n{LINE}\n"
                f"📍 سعر الدخول: <b>{p['px']:.2f}</b>\n"
                f"🛑 وقف الخسارة: <b>{p['sl']:.2f}</b>  (خسارة {p['risk']:.2f}$)\n"
                f"🎯 الهدف الأول: <b>{p['tp1']:.2f}</b>  (ربح {abs(p['tp1'] - p['px']):.2f}$)\n"
                f"🎯 الهدف الثاني: <b>{p['tp2']:.2f}</b>  (ربح {abs(p['tp2'] - p['px']):.2f}$)\n"
                f"{LINE}\n"
                "📌 <b>ماذا تفعل؟</b>\n"
                f"1) ضع أمر {order} عند سعر الدخول مع الوقف والهدف.\n"
                "2) عند الهدف الأول أغلق نصف الصفقة وحرّك الوقف إلى سعر الدخول.\n"
                f"3) إن لم يلمس السعر الأمر خلال {dur_text(p['expire_min'])} ألغِه (سأبلغك).\n"
                f"{LINE}\n"
                + _why(p, p["tf"], p["conf"]) + "\n"
                + _foot(cfg, ev))

    tr = ev["trade"]
    side = "شراء" if tr["dir"] == 1 else "بيع"
    icon = "🟢" if tr["dir"] == 1 else "🔴"

    if k == "signal":
        filled = "MSS" in tr["tag"]
        head = "✅ <b>تفعّل أمرك المعلق — الصفقة مفتوحة الآن</b>" if filled else "🔔 <b>ادخل الآن بسعر السوق</b>"
        return (f"{head}\n{icon} <b>{side} {sym}</b>\n{LINE}\n"
                f"📍 سعر الدخول: <b>{tr['entry']:.2f}</b>\n"
                f"🛑 وقف الخسارة: <b>{tr['sl0']:.2f}</b>  (خسارة {tr['risk']:.2f}$)\n"
                f"🎯 الهدف الأول: <b>{tr['tp1']:.2f}</b>  (ربح {abs(tr['tp1'] - tr['entry']):.2f}$) — أغلق {P['tp1Pct']:g}% وحرّك الوقف للدخول\n"
                f"🎯 الهدف الثاني: <b>{tr['tp2']:.2f}</b>  (ربح {abs(tr['tp2'] - tr['entry']):.2f}$)\n"
                f"📦 الحجم المقترح: {lots_for(tr['risk'], cfg, contract):.2f} لوت (مخاطرة {cfg['riskPct']}% من الحساب)\n"
                f"{LINE}\n"
                + _why(tr, tr["tf"], tr["conf"]) + "\n"
                + _foot(cfg, ev))

    if k == "tp1":
        extra = (f"\n🔒 حرّك وقف الخسارة إلى سعر الدخول ({tr['entry']:.2f}) فتصبح الصفقة بلا مخاطرة." if P["be"] else "")
        return (f"🎯 <b>تحقق الهدف الأول</b> — {side} {sym}\n{LINE}\n"
                f"سعر الهدف: {tr['tp1']:.2f}\n"
                f"أغلق {P['tp1Pct']:g}% من الصفقة الآن.{extra}\n"
                f"الباقي ينتظر الهدف الثاني {tr['tp2']:.2f}.")

    if k == "closed":
        r = ev["R"]
        if r > 0.001:
            head = f"✅ <b>أُغلقت الصفقة بربح</b> (+{r:.2f}R)"
        elif r < -0.001:
            head = f"❌ <b>أُغلقت الصفقة بخسارة</b> ({r:.2f}R)"
        else:
            head = "⚪ <b>أُغلقت الصفقة بالتعادل</b> (0R)"
        why = {"TP2": "وصلت الهدف الثاني", "SL": "ضُرب وقف الخسارة",
               "TP1+BE": "ربحت الهدف الأول ثم عاد السعر لسعر الدخول",
               "TP1+SL": "ربحت الهدف الأول ثم ضُرب الوقف",
               "TIME": "انتهت المدة القصوى للصفقة"}.get(ev["result"], ev["result"])
        return (f"{head}\n{icon} {side} {sym}\n{LINE}\n"
                f"السبب: {why}\n"
                f"الدخول {tr['entry']:.2f} | الوقف الأصلي {tr['sl0']:.2f} | الهدف الثاني {tr['tp2']:.2f}\n"
                f"<i>(1R = مقدار المخاطرة في الصفقة، أي {tr['risk']:.2f}$)</i>")
    return None


def ev_key(ev):
    k = ev["kind"]
    if k in ("pending", "pending_cancel"):
        return f"{k}|{ev['tf']}|{ev['setup_id']}|{ev['bar_time']}"
    t = ev["trade"]["sig_time"] if ev.get("trade") else str(ev["bar_time"])
    d = ev["trade"]["dir"] if ev.get("trade") else ev["setup"]["isb"]
    return f"{k}|{ev['tf']}|{t}|{d}|{ev['bar_time']}"
