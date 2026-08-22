# -*- coding: utf-8 -*-
"""
report.py — بناء نص التقارير والتنبيهات
================================================================
⚠️ ممنوع تعديل الملف ده من غير مراجعة.

🚫 مفيش AI. كل جملة **مكتوبة بإيدنا** ومختارة بـif/else.
   الأرقام كلها جاية محسوبة من metrics.py — الملف ده مبيحسبش أي رقم.

الترتيب بيطابق شاشة "يومية الخزينة" بتاعتهم بالظبط، عشان المالك
يقارن بشاشته ويلاقي نفس الأرقام. صفر احتكاك، صفر جدال.

الأصفار **بتتعرض** — شاشتهم بتعرضها، فده متسق مع اللي هو متعوّد عليه.
================================================================
"""

from __future__ import annotations

import datetime as _dt
from typing import Sequence

import events as E
import metrics as M

AR_WEEKDAYS = ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس",
               "الجمعة", "السبت", "الأحد")
AR_MONTHS = ("يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
             "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر")

SEP = "─" * 18


def ar_date(d: _dt.date) -> str:
    return f"{AR_WEEKDAYS[d.weekday()]} {d.day} {AR_MONTHS[d.month - 1]} {d.year}"


def ar_weekday(d: _dt.date) -> str:
    return AR_WEEKDAYS[d.weekday()]


def _hm(t: _dt.datetime) -> str:
    h = t.hour
    suffix = "ص" if h < 12 else "م"
    hh = h % 12 or 12
    return f"{hh}:{t.minute:02d} {suffix}"


def _ar_invoice_count(n: int) -> str:
    """تعداد عربي صحيح لعدد الفواتير: 1 فاتورة · 2 فاتورتين · 3-10 فواتير · 11+ فاتورة."""
    if n == 2:
        return "فاتورتين"
    if 3 <= n <= 10:
        return "فواتير"
    return "فاتورة"


# ════════════════════════════════════════════════════════════════
# حالة الوردية — جدول ثابت
# ════════════════════════════════════════════════════════════════

# الوصف الإيجابي الموحّد المعتمد (2026-08-13): الوردية **مكتملة** — سواء من
# غير أي فجوة مراقبة، أو بفجوة موضّحة (انقطاع مؤقت في المراقبة + بيانات
# الوردية كاملة ومؤكدة). "مكتملة" حلت محل "مستقرة" حسب التنسيق المعتمد.
STATUS_STABLE = "🟢 الوردية مكتملة"
STATUS_REVIEW = "🟡 الوردية تحتاج مراجعة"
STATUS_CASH = "🔴 يوجد فرق يستحق المراجعة"
STATUS_NO_DATA = "⚪ لا توجد بيانات كافية لهذه الوردية"
# H3 (hardening phase, 2026-08-11): coverage interrupted mid-shift.
# Same review-and-document discipline as STATUS_NO_DATA (commit 28cdc72):
# locked-file edit, additive only, priority pinned by tests. A shift with
# real invoices but a heartbeat gap inside its window was NOT fully
# watched — its numbers can look valid while being incomplete.
STATUS_INCOMPLETE = "🟠 بيانات هذه الوردية غير مكتملة"


def pick_status(has_cash_diff: bool, has_notes: bool, has_data: bool = True,
                has_coverage_gap: bool = False,
                gap_explained: bool = False) -> str:
    """
    الأولوية: cash_diff > no_data > coverage_gap(غير موضّحة) > explained > notes > stable.

    ⚠️ has_data=False (صفر فواتير في نافذة الوردية) لازم يسبق "مكتملة" —
       صفر فواتير معناه إننا مراقبناش الوردية دي، مش إنها كانت هادئة فعلاً.
       الفرق في الخزينة (لو موجود) بييجي من جدول تاني (cash_counts) ومستقل
       عن الفواتير، فبيفضل الأولوية الأعلى حتى لو مفيش فواتير.

    ⚠️ has_coverage_gap (H3): فواتير موجودة لكن في انقطاع مراقبة جوه
       الوردية — الحكم "مكتملة" كذب في الحالة دي. الفجوة أقوى من الملاحظات
       وأضعف من "لا بيانات": وردية من غير مراقبة أصلاً ادعاء أقوى من
       وردية مراقبة جزئي.

    ⚠️ gap_explained (2026-08-13): الفجوة **مفسّرة** — انقطاع مؤقت في
       المراقبة (جهاز دخل في وضع Sleep) وبيانات الوردية كاملة ومؤكدة
       (classify_coverage_gap في orchestrator.py). الفجوة المفسّرة بتخرج من
       الحكم، والوردية بتتقيم بشكلها الطبيعي. الفجوة من غير تفسير = incomplete.
    """
    if has_cash_diff:
        return STATUS_CASH
    if not has_data:
        return STATUS_NO_DATA
    if has_coverage_gap and not gap_explained:
        return STATUS_INCOMPLETE
    # قرار 2026-08-13 (موثّق في docstring): الفجوة المفسّرة بتخرج من الحكم
    # قبل الملاحظات — الوردية المكتملة بياناتها بتتعرض "مكتملة" حتى لو فيه
    # عمليات للمراجعة (قسم "⚠️ ملاحظات تحتاج مراجعة" لسه ظاهر فوق الخلاصة).
    # الحماية مش بتضعّف: الفجوة من غير تفسير / cash_diff / no_data ليهم
    # الأولوية زي ما هي، ومفيش أي حماية بتتقفل عشان التقرير يبقى أخضر.
    if gap_explained:
        return STATUS_STABLE
    if has_notes:
        return STATUS_REVIEW
    return STATUS_STABLE


# ════════════════════════════════════════════════════════════════
# الخلاصة — جمل ثابتة، الاختيار بشرط
# ════════════════════════════════════════════════════════════════

_SUMMARIES = {
    ("stable", "up"): ("🚀 الخلاصة",
        "الوردية أعلى من نفس الوردية الأسبوع الماضي، مع أداء قوي في المبيعات "
        "وعدم وجود فروقات مؤثرة بالخزينة."),
    ("stable", "down"): ("📉 الخلاصة",
        "المبيعات أقل من نفس الوردية الأسبوع الماضي، مع عدم وجود فروقات مؤثرة بالخزينة."),
    ("stable", "flat"): ("✅ الخلاصة",
        "الوردية مستقرة، والمبيعات في نطاقها الطبيعي مقارنة بنفس الوردية "
        "الأسبوع الماضي، ولا توجد فروقات مؤثرة."),
    ("stable", "none"): ("✅ الخلاصة",
        "الوردية مستقرة ولا توجد فروقات مؤثرة."),
    ("notes", "*"): ("⚠️ الخلاصة",
        "المبيعات طبيعية، لكن توجد عمليات تحتاج مراجعة."),
    ("cash", "*"): ("⚠️ الخلاصة",
        "المبيعات طبيعية، لكن يوجد فرق في الخزينة وبعض العمليات تحتاج مراجعة."),
    ("no_data", "*"): ("⚪ الخلاصة",
        "لا توجد فواتير مسجّلة لهذه الوردية — لا يمكن تقييمها. "
        "الأرقام أعلاه ليست نتيجة تحقّق، لأن مفيش بيانات نبني عليها الحكم."),
    ("incomplete", "*"): ("🟠 الخلاصة",
        "سجّلنا فواتير في هذه الوردية، لكن حدث انقطاع في مراقبة الجهاز خلال "
        "جزء منها — الأرقام أعلاه قد لا تعكس الوردية كاملة. يُفضّل مراجعة "
        "تغطية الرصد وتأكيد الأرقام."),
    # 2026-08-13 — فجوة مراقبة موضّحة (انقطاع مؤقت بسبب Sleep) وبيانات الوردية
    # كاملة: الوردية مكتملة والانقطاع مش نقص في الوردية. النص الثلاثة أسطر
    # بنفس صياغة الزبون المعتمدة بالحرف. ممنوع إننا نقول إننا "استرجعنا"
    # بيانات مش مثبتة — التفسير ده بيمر بس من classify_coverage_gap اللي
    # بيتأكد: المراقبة رجعت، واللحاق اكتمل، ومفيش نشاط POS جوه الفجوة.
    ("gap_explained", "*"): ("🟢 الخلاصة",
        "الوردية مكتملة وتم تسجيل بياناتها.\n"
        "حدث انقطاع مؤقت في المراقبة بسبب دخول جهاز الكمبيوتر في وضع Sleep، "
        "وليس بسبب نقص في الوردية نفسها.\n"
        "تم استئناف المراقبة بعد عودة الجهاز للعمل."),
}


def pick_summary(has_cash_diff: bool, has_notes: bool,
                 direction: str, has_data: bool = True,
                 has_coverage_gap: bool = False,
                 gap_explained: bool = False) -> tuple[str, str]:
    if has_cash_diff:
        return _SUMMARIES[("cash", "*")]
    if not has_data:
        return _SUMMARIES[("no_data", "*")]
    if has_coverage_gap and not gap_explained:
        return _SUMMARIES[("incomplete", "*")]
    if gap_explained:
        return _SUMMARIES[("gap_explained", "*")]
    if has_notes:
        return _SUMMARIES[("notes", "*")]
    return _SUMMARIES[("stable", direction if direction in
                       ("up", "down", "flat") else "none")]


# ════════════════════════════════════════════════════════════════
# الخزينة — سطر واحد
# ════════════════════════════════════════════════════════════════

def cash_line(cash_event: E.Event | None, had_no_count: bool) -> str:
    """
    🚨 لو الجرد ماتعملش، بنقول كده صراحة — **ممنوع** نحوّلها لعجز.
       17 صف من 21 في الداتا مفيهمش جرد.
    """
    if cash_event is None:
        if had_no_count:
            return "⏸ لم يتم جرد الخزينة في هذه الوردية"
        # 2026-08-13: تغيير صياغة فقط — المنطق (منطق التطابق/الفروقات) كما هو.
        return "🟢 تم تأكيد بيانات الخزينة من البيانات المسجلة"
    p = cash_event.payload
    return (f"⚠️ المتوقع {M.money(p['expected'])} ج • "
            f"الفعلي {M.money(p['actual'])} ج • "
            f"الفرق {M.signed_money(p['diff'])}")


# ════════════════════════════════════════════════════════════════
# تقرير نهاية الوردية
# ════════════════════════════════════════════════════════════════

def build_shift_report(
    m: M.ShiftMetrics,
    comparison: M.Comparison,
    notes: Sequence[str] = (),
    cash_event: E.Event | None = None,
    had_no_count: bool = False,
    max_notes: int = 5,
    has_coverage_gap: bool = False,
    gap_explained: bool = False,
    withdrawals: float | None = None,
    withdrawal_users: Sequence[dict] | None = None,
) -> str:
    """بيرجّع نص جاهز للإرسال. مبيحسبش أي رقم — كله جاي من metrics.

    withdrawals (مسحوبات): المصدر المؤكد = SUM(dbo.Personal.peramount) جوه
    نافذة الوردية (مؤكد من تحقيق العميل + تأكيد المالك). السطر والفورمولا
    بيتعرضوا دايماً من المتصل الحقيقي (الـorchestrator بيوفر القيمة حتى لو
    صفر) — السطر ممنوع يختفي ولا يتحط من غير أساس. المبلغ نفسه سطر عرض بس:
    الإجمالي محسوب بنفس الأساس في metrics.py (مبيعات + مقبوضات − مرتجع
    − دليفري − مسحوبات).

    withdrawal_users: نفس صفوف orchestrator._group_withdrawals_by_user —
    **بيانات عرض فقط**. مبلغ "مسحوبات" أعلاه يفضل هو المصدر المعتمد؛ القسم
    ده بيوريه مين سحب، مبيضيفش رقم تاني ولا بيغيّر الإجمالي. بيتعرض بس لو
    أكتر من مستخدم واحد سحب في الوردية (زي منطق other_users بالظبط) — مستخدم
    واحد مفيهوش معلومة إضافية تستاهل قسم لوحده.
    """
    has_cash = cash_event is not None
    has_notes = bool(notes)
    # صفر فواتير في نافذة الوردية = مراقبناش الوردية دي (وردية قبل التركيب،
    # عطل، انقطاع اتصال...) — مش دليل إنها كانت هادئة فعلاً. ممنوع "مكتملة".
    has_data = m.total_invoices > 0
    # H3: فواتير موجودة لكن في فجوة نبضات جوه الوردية — الرصد انقطع
    # (عطل/نت) ولو إن المبيعات سجّلت. القيم أعلاه قد تكون ناقصة.
    # مفيش تضارب مع has_data: صفر فواتير = ادعاء أقوى (الوردية كلها مراقبها
    # محدش) — بييجي الأول في الأولوية.
    # H9 (2026-08-13): لو الفجوة موضّحة (انقطاع مؤقت + البيانات كاملة)،
    # التقرير بيقول "الوردية مكتملة" وبيشرح انقطاع المراقبة في الخلاصة —
    # من غير ما يضعّف كشف الفجوات نفسه (اللي شغال في orchestrator.py).

    user = m.primary_user.name if m.primary_user else "غير محدد"
    L: list[str] = [
        "📊 تقرير نهاية الوردية",
        f"📅 {ar_date(m.shift_date)}",
        f"🕐 {M.shift_label_ar(m.shift_name)} — {user} · "
        f"{_hm(m.window_start)} ← {_hm(m.window_end)}",
        SEP,
        pick_status(has_cash, has_notes, has_data, has_coverage_gap,
                    gap_explained),
        SEP,
    ]

    # ── نشاط مستخدم آخر (القاعدة 2026-08-14) ────────────────────────────
    # قسم منفصل قبل بنود اليومية، بيتعرض **فقط** لو فيه فواتير لمستخدم غير
    # المستخدم الأساسي (other_users من نفس تجميع metrics.py). لو مفيش غير
    # المستخدم الأساسي — مفيش أي قسم/سطر مستخدم خالص (ممنوع سطر "منها" أو
    # سطر موظف لمجرد إن الوردية ليها مستخدم).
    #
    # ⚠️ مبلغ المستخدم = مجموع total لفواتيره (كل الأنواع: cash/external/
    #    return/other) — مش مجموعة فرعية مضمونة من سطر "مبيعات" (اللي هو
    #    cash بس). فالصياغة محايدة بالضرورة: القيمة محسوبة ضمن بيانات
    #    الوردية ولا تُضاف على الإجمالي. ممنوع "منها X" (ادعاء رياضي غير
    #    مضمون) وممنوع أي إضافة على الإجمالي — القسم عرض فقط.
    if m.other_users:
        L.append("👤 نشاط مستخدم آخر")
        for u in m.other_users:
            L.append(f"{u.name} — {u.invoices} "
                     f"{_ar_invoice_count(u.invoices)} — {M.money(u.amount)} ج")
        L.append("ℹ️ قيمة هذه الفواتير محسوبة ضمن بيانات الوردية "
                 "ولا تُضاف على الإجمالي.")
        L.append(SEP)

    # ── مين سحب (2026-08-22) ─────────────────────────────────────────────
    # قسم عرض إضافي **فقط** لو أكتر من مستخدم واحد سحب جوه نافذة الوردية —
    # نفس منطق قسم "نشاط مستخدم آخر" فوق بالظبط: مستخدم واحد مفيهوش معلومة
    # إضافية تستاهل قسم لوحده (هو أصلاً صاحب الوردية غالباً). القيم هنا
    # عرض فقط ومحسوبة أصلاً ضمن سطر "مسحوبات" تحت — ممنوع تتحط كإجمالي
    # تاني منافس، وممنوع تتغيّر الإجمالي المعتمد بسببها.
    if withdrawal_users and len(withdrawal_users) > 1:
        L.append("💸 المسحوبات")
        for wu in withdrawal_users:
            L.append(f"{wu['name']} — {M.money(wu['amount'])} ج")
        L.append(SEP)

    # بنود يومية الخزينة — بنفس ترتيب شاشة "يومية الخزينة" المعتمدة.
    # الإشارات عمدية: مرتجع ودليفري خطوط **خصم** مش جمع.
    # الإجمالي = مبيعات + مقبوضات − مرتجع − دليفري — القيم نفسها مفيش منها تعديل.
    L.append(f"مبيعات          +{M.money(m.sales)} ج")
    L.append(f"مقبوضات         +{M.money(m.collections)} ج")
    L.append(f"دليفري           −{M.money(m.delivery)} ج")
    if withdrawals is not None:
        L.append(f"مسحوبات         −{M.money(withdrawals)} ج")
    L.append(f"مرتجع مبيعات     −{M.money(m.returns)} ج")
    L += [SEP, f"الإجمالي         {M.money(m.grand_total)} ج", SEP]

    formula = "الإجمالي = مبيعات + مقبوضات − مرتجع مبيعات − دليفري"
    if withdrawals is not None:
        formula += " − مسحوبات"
    L.append(formula)
    L += [
        SEP,
        f"إيصالات بيع نقدي:  {m.n_cash}",
        f"إيصالات مرتجع:     {m.n_return}",
        f"إيصالات بيع خارجي: {m.n_external}",
    ]
    if m.n_other:
        L.append(f"إيصالات أخرى:      {m.n_other}")

    L += [SEP, "💵 الخزينة", cash_line(cash_event, had_no_count)]

    if m.top_items:
        medals = ("🥇", "🥈", "🥉", "4️⃣", "5️⃣")
        L += [SEP, "🔥 أكثر 5 أصناف حركة"]
        L += [f"{medals[i]} {n} — {q:g}"
              for i, (n, q) in enumerate(m.top_items[:5])]

    # المقارنة — نفس الوردية نفس اليوم الأسبوع اللي فات
    L += [SEP, f"📈 مقارنة بنفس الوردية {ar_weekday(m.shift_date)} اللي فات"]
    if comparison.available:
        L.append(f"{M.money(comparison.previous_total)} ج — "
                 f"{M.comparison_text_ar(comparison)}")
    else:
        L.append(M.comparison_text_ar(comparison))

    if has_notes:
        shown = list(notes[:max_notes])
        extra = len(notes) - len(shown)
        if extra > 0:
            shown.append(f"و {extra} غيرها")
        L += [SEP, "⚠️ ملاحظات تحتاج مراجعة"] + [f"• {n}" for n in shown]

    title, body = pick_summary(has_cash, has_notes, comparison.direction,
                               has_data, has_coverage_gap, gap_explained)
    L += [SEP, title] + body.split("\n")

    text = "\n".join(L)
    E.assert_no_accusation(text)          # حارس إجباري قبل أي إرسال
    return text


# ════════════════════════════════════════════════════════════════
# أصناف الشهر
# ════════════════════════════════════════════════════════════════

def build_monthly_products(month: _dt.date,
                           items: Sequence[tuple[str, float]]) -> str:
    """أصناف بس. مفيش مقارنة، مفيش أرقام مبيعات."""
    medals = ("🥇", "🥈", "🥉", "4️⃣", "5️⃣")
    L = ["🔥 أصناف الشهر الماضي",
         f"📅 {AR_MONTHS[month.month - 1]} {month.year}", ""]
    L += [f"{medals[i]} {n} — {q:g}" for i, (n, q) in enumerate(items[:5])]
    text = "\n".join(L)
    E.assert_no_accusation(text)
    return text


# ════════════════════════════════════════════════════════════════
# التنبيهات الفورية
# ════════════════════════════════════════════════════════════════

def _items_one_line(items: Sequence[dict]) -> str:
    """
    ⚠️ سطر واحد إجبارياً — الفاصل ' • '.
       قيد من Meta (ممنوع \\n جوه المتغيّر). بنلتزم بيه حتى في تيليجرام
       عشان القالب يتنقل لواتساب من غير تعديل.
    """
    return " • ".join(
        f"{it['qty']:g}× {it['name']} ({M.money(it['list_price'])} ج)"
        for it in items
    )


def build_alert(ev: E.Event, user_name: str = "غير محدد") -> str:
    p = ev.payload
    when = f"{ar_date(ev.occurred_at.date())} {_hm(ev.occurred_at)}"

    if ev.type == "zero_invoice":
        L = ["⚠️ أصناف بسعر صفر — تحتاج مراجعة", "",
             f"🧾 الإيصال: {p.get('receipt_num') or p['salid']}",
             f"🕐 {when}", f"👤 {user_name}",
             f"💰 قيمتها بسعر القائمة: {M.money(p['list_value'])} ج", "",
             f"🍽 {_items_one_line(p['items'])}", "", "محتاج مراجعة"]

    elif ev.type == "refund":
        L = ["🔄 مرتجع — يحتاج مراجعة", "",
             f"🧾 الإيصال: {p.get('receipt_num') or p['salid']}",
             f"🕐 {when}", f"👤 {user_name}",
             f"💰 القيمة: {M.money(p['amount'])} ج", "", "محتاج مراجعة"]

    elif ev.type == "cash_diff":
        L = ["🔴 فرق في الخزينة — يستحق المراجعة", "",
             f"🕐 {when}", f"👤 {user_name}",
             f"💵 المتوقع: {M.money(p['expected'])} ج",
             f"💵 الفعلي: {M.money(p['actual'])} ج",
             f"📊 الفرق: {M.signed_money(p['diff'])}", "", "محتاج مراجعة"]

    elif ev.type == "deleted_invoice":
        # ⚠️ الصف اتمسح — إحنا عارفين مين **أنشأ** الفاتورة، مش مين مسحها.
        #    ممنوع نلمّح إن اللي أنشأها هو اللي مسحها.
        L = ["⚠️ فاتورة محذوفة — تحتاج مراجعة", "",
             f"🧾 الإيصال: {p.get('receipt_num') or p['salid']}",
             f"🕐 {when}",
             f"👤 أنشأها: {user_name}",
             f"💰 القيمة: {M.money(p['amount'])} ج", "", "محتاج مراجعة"]

    elif ev.type == "no_sales":
        hours = p["gap_minutes"] // 60
        mins = p["gap_minutes"] % 60
        L = ["⚠️ مفيش مبيعات مسجّلة", "",
             f"🕐 آخر فاتورة: {when}",
             f"⏱ عدت: {hours} ساعة و {mins} دقيقة", "",
             "يُفضّل مراجعة حالة جهاز الكاشير."]

    elif ev.type == "db_size":
        L = ["📁 تنبيه: قاعدة بيانات الكاشير", "",
             f"الحجم الحالي: {p['size_mb']:,.0f} ميجا من 10,240 ({p['pct']}%)", "",
             "يُفضّل مراجعة مساحة قاعدة البيانات قبل الوصول للحد."]

    else:
        L = [f"ℹ️ {ev.type}", f"🕐 {when}"]

    text = "\n".join(L)
    E.assert_no_accusation(text)
    return text


def note_line(ev: E.Event, user_name: str = "") -> str:
    """سطر مختصر للملاحظات جوه تقرير الوردية."""
    p = ev.payload
    ref = p.get("receipt_num") or p.get("salid", "")
    if ev.type == "zero_invoice":
        return f"إيصال بصفر #{ref} ({M.money(p['list_value'])} ج)"
    if ev.type == "refund":
        return f"مرتجع #{ref} ({M.money(p['amount'])} ج)"
    if ev.type == "deleted_invoice":
        return f"فاتورة محذوفة #{ref} ({M.money(p['amount'])} ج)"
    if ev.type == "cash_diff":
        return f"فرق خزينة {M.signed_money(p['diff'])}"
    return ev.type
