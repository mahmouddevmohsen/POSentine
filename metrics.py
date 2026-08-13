# -*- coding: utf-8 -*-
"""
metrics.py — حساب الورديات ويومية الخزينة
================================================================
⚠️ ممنوع تعديل الملف ده من غير مراجعة.

كل دالة هنا **نقية**:
  • مفيش datetime.now()      — النافذة الزمنية بتتمرّر كـ parameter
  • مفيش اتصال داتابيز
  • مفيش عشوائية
  → نفس الدخل = نفس الخرج بالحرف، كل مرة. قابل للـgolden tests.

المعادلة الحاكمة (متحقق منها بشاشة "يومية الخزينة" — 19,205 = 19,205):

    الإجمالي = مبيعات + مقبوضات − مرتجع − دليفري − مسحوبات

    مبيعات   = مجموع total للفواتير kind='cash'
    مقبوضات  = مجموع total للفواتير kind='external'
    مرتجع    = مجموع total للفواتير kind='return'
    دليفري   = مجموع delivery_cost لكل الفواتير
    مسحوبات  = مجموع peramount من dbo.Personal جوه نافذة الوردية

⚠️ مسحوبات بيتمرّر كقيمة محسوبة من المتصل (الـorchestrator بيفلتر
   dbo.Personal بالنافذة) — الدالة هنا بتحسب المعادلة بس، وتبقى نقية.
================================================================
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

# حدود الوردية
MORNING_START_HOUR = 7
EVENING_START_HOUR = 19

# أقل عدد فواتير عشان "المستخدم الأساسي" والمقارنة يبقى ليهم معنى
MIN_INVOICES_FOR_STATS = 20

# عتبة اعتبار التغيير "أعلى/أقل" بدل "قريب"
COMPARISON_FLAT_PCT = 5.0

MORNING = "morning"
EVENING = "evening"


# ════════════════════════════════════════════════════════════════
# نوافذ الورديات
# ════════════════════════════════════════════════════════════════

def resolve_shift(sold_at: _dt.datetime) -> tuple[_dt.date, str]:
    """
    الفاتورة دي تبع أنهي وردية؟

    الحدود صريحة — بداية شاملة ونهاية غير شاملة:
        صباح : [07:00 , 19:00)   نفس التاريخ
        مساء : [19:00 , 07:00)   تاريخ البداية

    ⚠️ فاتورة الساعة 06:59 تبع وردية **مساء إمبارح**.
       ده اللي بيخلي حسابنا يطابق يوم HD Soft (07:00 → 07:00).
    """
    h = sold_at.hour
    if MORNING_START_HOUR <= h < EVENING_START_HOUR:
        return sold_at.date(), MORNING
    if h >= EVENING_START_HOUR:
        return sold_at.date(), EVENING
    # h < 7  →  امتداد لوردية مساء اليوم اللي فات
    return (sold_at - _dt.timedelta(days=1)).date(), EVENING


def shift_window(shift_date: _dt.date, shift_name: str) -> tuple[_dt.datetime, _dt.datetime]:
    """نافذة الوردية [بداية , نهاية) بالوقت المحلي للـPOS."""
    if shift_name == MORNING:
        start = _dt.datetime.combine(shift_date, _dt.time(MORNING_START_HOUR))
        end = _dt.datetime.combine(shift_date, _dt.time(EVENING_START_HOUR))
    elif shift_name == EVENING:
        start = _dt.datetime.combine(shift_date, _dt.time(EVENING_START_HOUR))
        end = _dt.datetime.combine(shift_date + _dt.timedelta(days=1),
                                   _dt.time(MORNING_START_HOUR))
    else:
        raise ValueError(f"اسم وردية غير معروف: {shift_name!r}")
    return start, end


def previous_week_shift(shift_date: _dt.date) -> _dt.date:
    """نفس اليوم من الأسبوع اللي فات — نفس الوردية."""
    return shift_date - _dt.timedelta(days=7)


def shift_label_ar(shift_name: str) -> str:
    return "الصباح" if shift_name == MORNING else "المساء"


# ════════════════════════════════════════════════════════════════
# النتائج
# ════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class UserSlice:
    uid: int | None
    name: str
    invoices: int
    amount: float


@dataclass(slots=True)
class ShiftMetrics:
    shift_date: _dt.date
    shift_name: str
    window_start: _dt.datetime
    window_end: _dt.datetime

    # بنود يومية الخزينة — بنفس ترتيب شاشتهم
    sales: float = 0.0
    returns: float = 0.0
    delivery: float = 0.0
    collections: float = 0.0
    withdrawals: float = 0.0
    grand_total: float = 0.0

    n_cash: int = 0
    n_return: int = 0
    n_external: int = 0
    n_other: int = 0

    primary_user: UserSlice | None = None
    other_users: list[UserSlice] = field(default_factory=list)
    top_items: list[tuple[str, float]] = field(default_factory=list)
    total_invoices: int = 0
    is_thin: bool = False          # أقل من MIN_INVOICES_FOR_STATS


def _round2(x: float) -> float:
    return round(x + 0.0, 2)


# ════════════════════════════════════════════════════════════════
# الحساب الأساسي
# ════════════════════════════════════════════════════════════════

def compute_shift(
    shift_date: _dt.date,
    shift_name: str,
    invoices: Sequence,          # عناصر فيها: total, delivery_cost, kind, user_uid, salid
    lines_by_salid: dict[int, Sequence] | None = None,
    user_names: dict[int, str] | None = None,
    product_is_modifier: dict[int, bool] | None = None,
    top_n: int = 5,
    withdrawals: float = 0.0,    # مجموع peramount لصفوف Personal جوه النافذة
) -> ShiftMetrics:
    """
    بيحسب وردية واحدة من فواتيرها.

    ⚠️ الفواتير المتمرّرة لازم تكون **متفلترة بالنافذة ومستبعد منها المحذوف**.
       الدالة دي مبتفلترش — بتحسب بس (عشان تفضل نقية وقابلة للاختبار).

    ⚠️ الوردية = **نافذة الـ12 ساعة**. كل الفواتير فيها بتتحسب مهما كان المستخدم.
       تدخّل مستخدم تاني بيظهر في other_users، **ومبلغه محسوب في الإجمالي**.
    """
    start, end = shift_window(shift_date, shift_name)
    m = ShiftMetrics(shift_date=shift_date, shift_name=shift_name,
                     window_start=start, window_end=end)

    per_user_n: Counter = Counter()
    per_user_amt: defaultdict[int | None, float] = defaultdict(float)

    for inv in invoices:
        kind = inv.kind
        total = float(inv.total or 0)
        m.delivery += float(getattr(inv, "delivery_cost", 0) or 0)

        if kind == "cash":
            m.sales += total
            m.n_cash += 1
        elif kind == "external":
            m.collections += total
            m.n_external += 1
        elif kind == "return":
            m.returns += total
            m.n_return += 1
        else:                       # other — بتتعد ومتدخلش في المعادلة
            m.n_other += 1

        uid = getattr(inv, "user_uid", None)
        per_user_n[uid] += 1
        per_user_amt[uid] += total

    # 🎯 المعادلة المتحقق منها — المسحوبات بتتخصم (اليومية بتخصمها)
    m.withdrawals = withdrawals
    m.grand_total = (m.sales + m.collections - m.returns - m.delivery
                     - m.withdrawals)

    for f in ("sales", "returns", "delivery", "collections",
              "withdrawals", "grand_total"):
        setattr(m, f, _round2(getattr(m, f)))

    m.total_invoices = sum(per_user_n.values())
    m.is_thin = m.total_invoices < MIN_INVOICES_FOR_STATS

    # المستخدم الأساسي — متحقق: دايماً 89–100% من فواتير الوردية
    if per_user_n and not m.is_thin:
        ranked = per_user_n.most_common()
        top_uid = ranked[0][0]
        m.primary_user = UserSlice(
            uid=top_uid,
            name=_user_name(top_uid, user_names),
            invoices=ranked[0][1],
            amount=_round2(per_user_amt[top_uid]),
        )
        m.other_users = [
            UserSlice(uid=u, name=_user_name(u, user_names),
                      invoices=n, amount=_round2(per_user_amt[u]))
            for u, n in ranked[1:]
        ]

    if lines_by_salid:
        m.top_items = top_items(
            (ln for inv in invoices for ln in lines_by_salid.get(inv.salid, ())),
            product_is_modifier=product_is_modifier,
            top_n=top_n,
        )

    return m


def _user_name(uid: int | None, names: dict[int, str] | None) -> str:
    """المستخدم اللي اتمسح من جدول Users فواتيره بتفضل — لازم اسم بديل."""
    if uid is None:
        return "غير محدد"
    if names and names.get(uid):
        return names[uid]
    return f"مستخدم محذوف #{uid}"


# ════════════════════════════════════════════════════════════════
# أعلى الأصناف
# ════════════════════════════════════════════════════════════════

def top_items(
    lines: Iterable,
    product_is_modifier: dict[int, bool] | None = None,
    top_n: int = 5,
) -> list[tuple[str, float]]:
    """
    أكتر الأصناف حركة **بالكمية** (مش بالإيراد).

    ⚠️ ملاحظات المطبخ بتتشال.
       19 صنف من 402 سعرهم صفر وهم مش منتجات:
       شطة حمراء · بدون دقة · متكترش بطاطس · حللها · بسم الله الرحمن الرحيم …
       لو ماتشالوش، ممكن يتصدّروا القايمة ويبوّظوا التقرير.

    كسر التعادل بالاسم — عشان النتيجة تبقى ثابتة (deterministic).
    """
    agg: defaultdict[str, float] = defaultdict(float)
    for ln in lines:
        itid = getattr(ln, "itid", None)
        if product_is_modifier and itid is not None and product_is_modifier.get(itid):
            continue
        if getattr(ln, "list_price", None) == 0:      # احتياطي لو المعجم مش متوفر
            continue
        name = getattr(ln, "item_name", None) or (
            f"صنف #{itid}" if itid is not None else "غير معروف"
        )
        agg[name] += float(getattr(ln, "qty", 0) or 0)

    ranked = sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(n, round(q, 3)) for n, q in ranked[:top_n]]


# ════════════════════════════════════════════════════════════════
# المقارنة الأسبوعية
# ════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class Comparison:
    available: bool
    previous_total: float | None = None
    pct: float | None = None            # القيمة المطلقة
    direction: str = "none"             # up | down | flat | none
    reason: str | None = None


def compare_to_last_week(
    current_total: float,
    previous_total: float | None,
    previous_invoices: int | None,
) -> Comparison:
    """
    🔴 حماية إجبارية.

    المحل قفل **5 أيام في يوليو**. لو الأسبوع اللي فات نفس الوردية = 0،
    القسمة على صفر بتفشّل التقرير كله بسبب سطر واحد.

    ودي مش احتمال — عندنا دليل إنها هتحصل.
    """
    if previous_total is None:
        return Comparison(False, reason="مفيش بيانات للأسبوع اللي فات")
    if previous_total <= 0:
        return Comparison(False, previous_total=_round2(previous_total),
                          reason="الوردية المقابلة الأسبوع اللي فات مفيهاش مبيعات")
    if previous_invoices is not None and previous_invoices < MIN_INVOICES_FOR_STATS:
        return Comparison(False, previous_total=_round2(previous_total),
                          reason="بيانات الأسبوع اللي فات غير كافية للمقارنة")

    pct = (current_total - previous_total) / previous_total * 100.0
    if pct > COMPARISON_FLAT_PCT:
        direction = "up"
    elif pct < -COMPARISON_FLAT_PCT:
        direction = "down"
    else:
        direction = "flat"

    # ⚠️ المعروض دايماً القيمة المطلقة — الاتجاه في الكلمة مش في الإشارة
    return Comparison(True, _round2(previous_total), round(abs(pct), 1), direction)


def comparison_text_ar(c: Comparison) -> str:
    if not c.available:
        return c.reason or "مفيش بيانات كافية للمقارنة"
    if c.direction == "up":
        return f"📈 أعلى بـ {c.pct}%"
    if c.direction == "down":
        return f"📉 أقل بـ {c.pct}%"
    return "➡️ قريب من الأسبوع الماضي"


# ════════════════════════════════════════════════════════════════
# تنسيق الأرقام
# ════════════════════════════════════════════════════════════════

def money(v: float | None) -> str:
    """14850.0 → '14,850'"""
    if v is None:
        return "—"
    return f"{v:,.0f}"


def signed_money(v: float) -> str:
    """
    الإشارة والوصف إلزاميين في فروقات الخزينة.
    المالك محتاج يعرف: ناقصة ولا زايدة؟
    """
    label = "عجز" if v < 0 else "زيادة"
    sign = "−" if v < 0 else "+"
    return f"{sign}{abs(v):,.0f} ج ({label})"
