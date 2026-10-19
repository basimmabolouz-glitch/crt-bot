# -*- coding: utf-8 -*-
"""رسائل تلجرام لنظام SH v2 + RSI (منفصل تماماً عن CRT): لغة بسيطة وترتيب واضح."""
import datetime as dt
from zoneinfo import ZoneInfo

NAME = "SH v2 + RSI"
LINE = "━━━━━━━━━━━━━━"
BANNER = "🟦 <b>SH v2 + RSI</b>"
SESS = {"tokyo": "طوكيو", "london": "لندن", "newyork": "نيويورك", "": "-"}
RESULT_WHY = {"TP2": "وصلت الهدف الثاني", "TP": "وصلت الهدف", "SL": "ضُرب وقف الخسارة",
              "BE": "رجعت لسعر الدخول (تعادل)", "TP1+BE": "ربحت الهدف الأول ثم رجعت لسعر الدخول",
              "TP1+SL": "ربحت الهدف الأول ثم ضُرب الوقف"}


def fmt_time(epoch, tz, add_min=0):
    t = dt.datetime.fromtimestamp(epoch + add_min * 60, dt.timezone.utc).astimezone(ZoneInfo(tz))
    return t.strftime("%Y-%m-%d %H:%M") + f" ({'توقيت فلسطين' if tz == 'Asia/Hebron' else tz})"


def lots_for(risk, cfg):
    return (cfg["accountSize"] * cfg["riskPct"] / 100) / (risk * cfg.get("ozPerLot", 100)) if risk > 0 else 0


def prio(tr):
    """طوكيو هي الأفضلية (أقوى نتيجة في الاختبار الطويل)، وغيرها ثانوية."""
    if tr.get("sess") == "tokyo":
        return "⭐ <b>أولوية — جلسة طوكيو</b> (أقوى جلسة إحصائياً)\n"
    return "⚪ <i>ثانوية — " + SESS.get(tr.get("sess", ""), "") + " (أضعف إحصائياً من طوكيو، ادخلها بحذر أو بحجم أقل)</i>\n"


def message(ev, cfg, P):
    tr = ev["trade"]; k = ev["kind"]
    side = "شراء" if tr["dir"] == 1 else "بيع"
    icon = "🟢" if tr["dir"] == 1 else "🔴"
    tz = cfg.get("displayTz", "Asia/Hebron")
    tfm = P["mainTF"] if tr.get("lvlMain") else P["chartTF"]
    star = " ★ قوي" if tr.get("lvlStrong") else ""
    note = f"<i>{cfg['dataNote']}</i>\n" if cfg.get("dataNote") else ""

    pr = prio(tr)
    if k == "signal" and tr.get("pending"):
        order = "Buy Limit" if tr["dir"] == 1 else "Sell Limit"
        usePart = tr["usePart"]
        t1 = (f"🎯 الهدف الأول ({tr['tp1R']:g}R): <b>{tr['tp1']:.2f}</b> — أغلق {tr['tp1Pct']:g}% وحرّك الوقف للدخول\n") if usePart else ""
        return (f"{BANNER}\n⏳ <b>جهّز أمراً معلقاً</b> (الكسر بعيد عن المستوى)\n{pr}"
                f"{icon} <b>{side} الذهب ({order})</b>\n{LINE}\n"
                f"📍 سعر الدخول (عند المستوى): <b>{tr['entry']:.2f}</b>\n"
                f"🛑 وقف الخسارة: <b>{tr['sl0']:.2f}</b>  (خسارة {tr['risk']:.2f}$)\n"
                f"{t1}"
                f"🎯 {'الهدف الثاني' if usePart else 'الهدف'} ({tr['rr']:g}R): <b>{tr['tp']:.2f}</b>\n"
                f"📦 الحجم المقترح: {lots_for(tr['risk'], cfg):.2f} لوت (مخاطرة {cfg['riskPct']}% من الحساب)\n{LINE}\n"
                f"📌 ضع أمر {order} عند سعر الدخول. إن لم يلمسه السعر خلال {tr['retestBars'] * P['chartTF']} دقيقة ألغِه (سأبلغك).\n"
                f"• سبب الإشارة: كسر مستوى شاربنس عند {tr['level']:.2f} (فريم {tfm} دقيقة){star}، RSI = {tr['rsi']:.0f} ✅\n"
                f"🕒 {fmt_time(ev['bar_time'], tz, P['chartTF'])}\n"
                f"{note}<i>{NAME} — إشارة تعليمية وليست نصيحة استثمارية</i>")

    if k == "fill":
        return (f"{BANNER}\n✅ <b>تفعّل أمرك المعلق — الصفقة مفتوحة الآن</b>\n{icon} {side} الذهب\n{LINE}\n"
                f"الدخول {tr['entry']:.2f} | الوقف {tr['sl0']:.2f} | الهدف الأول {tr['tp1']:.2f} | الثاني {tr['tp']:.2f}")

    if k == "cancel":
        return (f"{BANNER}\n⚠️ <b>ألغِ الأمر المعلق</b> ({side} الذهب)\n{LINE}\n"
                f"السبب: {ev['reason']}.\nإن كنت وضعت أمراً عند {tr['entry']:.2f} فاحذفه الآن.")

    if k == "signal":
        usePart = tr["usePart"]
        t1 = (f"🎯 الهدف الأول ({tr['tp1R']:g}R): <b>{tr['tp1']:.2f}</b>  (ربح {abs(tr['tp1'] - tr['entry']):.2f}$) — "
              f"أغلق {tr['tp1Pct']:g}% وحرّك الوقف إلى الدخول\n") if usePart else ""
        return (f"{BANNER}\n🔔 <b>ادخل الآن بسعر السوق</b>\n{pr}{icon} <b>{side} الذهب</b>\n{LINE}\n"
                f"📍 سعر الدخول: <b>{tr['entry']:.2f}</b>\n"
                f"🛑 وقف الخسارة: <b>{tr['sl0']:.2f}</b>  (خسارة {tr['risk']:.2f}$)\n"
                f"{t1}"
                f"🎯 {'الهدف الثاني' if usePart else 'الهدف'} ({tr['rr']:g}R): <b>{tr['tp']:.2f}</b>  (ربح {abs(tr['tp'] - tr['entry']):.2f}$)\n"
                f"📦 الحجم المقترح: {lots_for(tr['risk'], cfg):.2f} لوت (مخاطرة {cfg['riskPct']}% من الحساب)\n"
                f"{LINE}\n"
                "🧠 <b>سبب الإشارة</b>\n"
                f"• كسر {'قمة' if tr['dir'] == 1 else 'قاع'} (مستوى شاربنس) عند {tr['level']:.2f}، مستوى فريم {tfm} دقيقة{star}\n"
                f"• إغلاق شمعة {P['chartTF']} دقائق بجسم قوي خلف المستوى\n"
                f"• RSI = {tr['rsi']:.0f} (غير متشبّع) ✅\n"
                f"• الجلسة: {SESS.get(tr.get('sess', ''), tr.get('sess', ''))} ✅\n"
                f"• فلاتر اليوم: زخم اليوم + بدون فجوة اندفاع ✅\n"
                f"🕒 {fmt_time(ev['bar_time'], tz, P['chartTF'])}\n"
                f"{note}<i>{NAME} — إشارة تعليمية وليست نصيحة استثمارية</i>")

    if k == "breakeven":
        return (f"{BANNER}\n🔒 <b>حرّك الوقف إلى سعر الدخول</b> — {side} الذهب\n{LINE}\n"
                f"وصل السعر إلى +{P['beAtR']:g}R. انقل وقف الخسارة إلى {tr['entry']:.2f} فتصبح الصفقة بلا مخاطرة.")

    if k == "tp1":
        return (f"{BANNER}\n🎯 <b>تحقق الهدف الأول</b> — {side} الذهب\n{LINE}\n"
                f"سعر الهدف: {tr['tp1']:.2f}\n"
                f"أغلق {tr['tp1Pct']:g}% من الصفقة الآن، وحرّك الوقف إلى الدخول {tr['entry']:.2f}.\n"
                f"الباقي ينتظر الهدف الثاني {tr['tp']:.2f}.")

    if k == "closed":
        r = ev["R"]
        if r > 0.05:
            head = f"✅ <b>أُغلقت الصفقة بربح</b> (+{r:.2f}R)"
        elif r < -0.05:
            head = f"❌ <b>أُغلقت الصفقة بخسارة</b> ({r:.2f}R)"
        else:
            head = "⚪ <b>أُغلقت الصفقة بالتعادل</b> (0R)"
        return (f"{BANNER}\n{head}\n{icon} {side} الذهب\n{LINE}\n"
                f"السبب: {RESULT_WHY.get(ev['result'], ev['result'])}\n"
                f"الدخول {tr['entry']:.2f} | الوقف الأصلي {tr['sl0']:.2f} | الهدف الثاني {tr['tp']:.2f}\n"
                f"<i>(1R = مقدار المخاطرة في الصفقة، أي {tr['risk']:.2f}$)</i>")
    return None


def ev_key(ev):
    t = ev["trade"]
    return f"sh|{ev['kind']}|{t['sig_time']}|{t['dir']}|{ev['bar_time']}"
