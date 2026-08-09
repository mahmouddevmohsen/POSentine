# -*- coding: utf-8 -*-
"""
test_golden.py — شبكة الأمان
================================================================
الاختبارات دي مبنية على **داتا حقيقية اتحققنا منها** من جهاز العميل
ومن شاشة "يومية الخزينة" بتاعت HD Soft.

لو أي واحد منها فشل، يبقى فيه منطق اتكسر. **ممنوع تعديل القيم المتوقعة**
عشان الاختبار ينجح — القيم دي مأخوذة من شاشة العميل نفسها.

    pytest -q test_golden.py
================================================================
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import events as E
import metrics as M
from adapter_hdsoft import classify_cash_count, classify_invoice


# ════════════════════════════════════════════════════════════════
# أدوات مساعدة
# ════════════════════════════════════════════════════════════════

@dataclass
class Inv:
    salid: int
    total: float
    delivery_cost: float = 0.0
    kind: str = "cash"
    user_uid: int | None = 2
    sold_at: dt.datetime = dt.datetime(2026, 8, 8, 12, 0)
    receipt_num: int | None = None
    first_seen_at: dt.datetime | None = None


@dataclass
class Line:
    salid: int
    itid: int
    qty: float
    line_total: float
    list_price: float
    item_name: str = "صنف"


@dataclass
class Cash:
    srid: int
    counted_at: dt.datetime
    user_uid: int
    user_value: float
    app_value: float
    diff_value: float
    kind: str = ""

    def __post_init__(self):
        if not self.kind:
            self.kind = classify_cash_count(self.user_value)


# ════════════════════════════════════════════════════════════════
# 1) 🎯 المرجع الذهبي — وردية حمص 8 أغسطس 2026
#    الأرقام دي من شاشة "يومية الخزينة" بتاعتهم بالحرف
# ════════════════════════════════════════════════════════════════

def test_homs_shift_matches_hdsoft_screen():
    invs: list[Inv] = []
    # 236 إيصال بيع نقدي بإجمالي 17,570
    for i in range(236):
        invs.append(Inv(salid=1000 + i, total=17570 / 236, kind="cash"))
    # 15 إيصال خارجي بإجمالي 2,130 ومعاهم دليفري 415
    for i in range(15):
        invs.append(Inv(salid=2000 + i, total=2130 / 15,
                        delivery_cost=415 / 15, kind="external"))
    # إيصالان مرتجع بإجمالي 80
    for i in range(2):
        invs.append(Inv(salid=3000 + i, total=40.0, kind="return"))

    m = M.compute_shift(dt.date(2026, 8, 8), M.MORNING, invs)

    assert m.sales == 17570.0,       f"مبيعات: {m.sales}"
    assert m.collections == 2130.0,  f"مقبوضات: {m.collections}"
    assert m.returns == 80.0,        f"مرتجع: {m.returns}"
    assert m.delivery == 415.0,      f"دليفري: {m.delivery}"

    # 🎯 الرقم اللي على شاشتهم
    assert m.grand_total == 19205.0, f"الإجمالي: {m.grand_total} — المتوقع 19,205"

    assert (m.n_cash, m.n_external, m.n_return) == (236, 15, 2)


def test_daybook_formula_is_explicit():
    """الإجمالي = مبيعات + مقبوضات − مرتجع − دليفري"""
    m = M.compute_shift(dt.date(2026, 8, 8), M.MORNING, [
        Inv(1, 1000, kind="cash"),
        Inv(2, 300, delivery_cost=50, kind="external"),
        Inv(3, 100, kind="return"),
    ])
    assert m.grand_total == 1000 + 300 - 100 - 50 == 1150.0


# ════════════════════════════════════════════════════════════════
# 2) حدود الورديات — اليوم التجاري 07:00
# ════════════════════════════════════════════════════════════════

def test_shift_boundaries():
    # متحقق من الداتا: 110 فاتورة لمحمود بين 00:00-07:00 يوم 8 أغسطس
    # HD Soft بتحسبهم في يوم 7 مش يوم 8
    assert M.resolve_shift(dt.datetime(2026, 8, 8, 6, 59)) == (dt.date(2026, 8, 7), M.EVENING)
    assert M.resolve_shift(dt.datetime(2026, 8, 8, 7, 0)) == (dt.date(2026, 8, 8), M.MORNING)
    assert M.resolve_shift(dt.datetime(2026, 8, 8, 18, 59)) == (dt.date(2026, 8, 8), M.MORNING)
    assert M.resolve_shift(dt.datetime(2026, 8, 8, 19, 0)) == (dt.date(2026, 8, 8), M.EVENING)
    assert M.resolve_shift(dt.datetime(2026, 8, 9, 3, 0)) == (dt.date(2026, 8, 8), M.EVENING)


def test_shift_window_is_half_open():
    s, e = M.shift_window(dt.date(2026, 8, 8), M.EVENING)
    assert s == dt.datetime(2026, 8, 8, 19, 0)
    assert e == dt.datetime(2026, 8, 9, 7, 0)


# ════════════════════════════════════════════════════════════════
# 3) تصنيف الفواتير — الترتيب إلزامي
# ════════════════════════════════════════════════════════════════

def test_return_is_checked_before_salT():
    """في الداتا فاتورة salT=2 و saleRtype=1 — مرتجع دليفري.
       لو فحصنا salT الأول هتتصنّف 'external' غلط."""
    kind, _ = classify_invoice(sal_t=2, sal_type=1, sale_rtype=1)
    assert kind == "return"


def test_known_kinds():
    assert classify_invoice(1, 0, 0)[0] == "cash"
    assert classify_invoice(2, 0, 0)[0] == "external"


def test_unknown_salT_never_dropped():
    """204 فاتورة salT=3 في التاريخ — لازم تتصنّف other وتتسجل."""
    kind, unknown = classify_invoice(3, 0, 0)
    assert kind == "other"
    assert unknown == 3


# ════════════════════════════════════════════════════════════════
# 4) 🚨 الخزينة — SrUserval = 0 مش عجز
# ════════════════════════════════════════════════════════════════

def test_zero_user_value_is_no_count():
    assert classify_cash_count(0) == "no_count"
    assert classify_cash_count(4625) == "real_count"


# الـ20 صف الحقيقيين من جدول safeR
_REAL_SAFER = [
    Cash(1, dt.datetime(2025, 7, 16, 19, 5, 36), 8, 4625, 10964, -6339),
    Cash(2, dt.datetime(2025, 7, 16, 19, 8, 25), 8, 4625, 10964, -6339),
    Cash(3, dt.datetime(2025, 7, 16, 19, 8, 25), 8, 4625, 10964, -6339),
    Cash(4, dt.datetime(2025, 7, 16, 19, 8, 25), 8, 4625, 10964, -6339),
    Cash(5, dt.datetime(2025, 7, 21, 19, 6, 22), 8, 0, 1032, -1032),
    *[Cash(i, dt.datetime(2025, 7, 21, 19, 6, 22 + (i - 5)), 8, 0, 15758, -15758)
      for i in range(6, 15)],
    Cash(15, dt.datetime(2026, 1, 5, 15, 8, 30), 8, 0, 330, -330),
    Cash(16, dt.datetime(2026, 1, 5, 15, 9, 0), 8, 0, 11300, -11300),
    Cash(17, dt.datetime(2026, 1, 5, 15, 9, 6), 8, 0, 11300, -11300),
    Cash(18, dt.datetime(2026, 1, 5, 15, 9, 6), 8, 0, 11300, -11300),
    Cash(19, dt.datetime(2026, 1, 5, 15, 9, 50), 8, 0, 330, -330),
    Cash(20, dt.datetime(2026, 1, 5, 15, 9, 53), 8, 0, 11300, -11300),
]


def test_safer_dedup_collapses_duplicates():
    """20 صف خام → 5 أحداث فعلية. البرنامج بيكتب صفوف مكررة (9 في 6 ثواني)."""
    evs = E.detect_cash_diffs(_REAL_SAFER)
    assert len(evs) == 5, f"المتوقع 5 أحداث، طلع {len(evs)}"

    real = [e for e in evs if e.type == "cash_diff"]
    none_ = [e for e in evs if e.type == "cash_no_count"]
    assert len(real) == 1, "جرد حقيقي واحد بس في 14 شهر"
    assert len(none_) == 4

    assert real[0].payload["diff"] == -6339.0
    assert real[0].level == 1


def test_no_false_shortage_from_missing_count():
    """🚨 من غير الحماية دي، أول تنبيه هيقول 'عجز 15,758 ج' وهو غير صحيح."""
    evs = E.detect_cash_diffs(_REAL_SAFER)
    for e in evs:
        if e.type == "cash_diff":
            assert e.payload["actual"] != 0, "جرد بقيمة صفر اتحول لعجز — ممنوع"


# ════════════════════════════════════════════════════════════════
# 5) الإيصال الصفري — التعريف الدقيق
# ════════════════════════════════════════════════════════════════

def test_zero_invoice_with_priced_items_alerts():
    """إيصال 215514 الحقيقي: 5 أصناف مسعّرة بإجمالي 95 ج اتسجّلت بصفر."""
    inv = Inv(salid=9001, total=0.0, kind="cash")
    lines = {9001: [
        Line(9001, 1, 1, 0, 15, "فول صغير"),
        Line(9001, 2, 1, 0, 10, "عدد 7 طعمية"),
        Line(9001, 3, 1, 0, 20, "باكت بطاطس صغير"),
        Line(9001, 4, 1, 0, 25, "حمص صغير"),
        Line(9001, 5, 1, 0, 25, "بابا غنوج صغير"),
    ]}
    evs = E.detect_zero_invoices([inv], lines)
    assert len(evs) == 1
    assert evs[0].payload["list_value"] == 95.0
    assert evs[0].level == 1


def test_zero_invoice_with_only_modifiers_ignored():
    """ملاحظات مطبخ بس (سعر القائمة صفر) — مش حدث. 4 حالات في 7 أيام."""
    inv = Inv(salid=9002, total=0.0)
    lines = {9002: [Line(9002, 80, 1, 0, 0, "شطة حمراء"),
                    Line(9002, 81, 1, 0, 0, "بدون دقة")]}
    assert E.detect_zero_invoices([inv], lines) == []


def test_paid_invoice_with_small_freebie_ignored():
    """أوردر مدفوع ومعاه طحينه ببلاش — 14 حالة في 7 أيام، ضجيج."""
    inv = Inv(salid=9003, total=145.0)
    lines = {9003: [Line(9003, 10, 1, 145, 145, "وجبة"),
                    Line(9003, 11, 1, 0, 5, "طحينه")]}
    assert E.detect_zero_invoices([inv], lines) == []


# ════════════════════════════════════════════════════════════════
# 6) 🔴 المقارنة الأسبوعية — القسمة على صفر
# ════════════════════════════════════════════════════════════════

def test_comparison_survives_closed_shop():
    """المحل قفل 5 أيام في يوليو. دي هتحصل أكيد مش احتمال."""
    c = M.compare_to_last_week(14850, 0, 0)          # ← ماينفعش يرمي ZeroDivisionError
    assert c.available is False
    assert c.pct is None
    text = M.comparison_text_ar(c)
    assert text and "%" not in text, f"ماينفعش نعرض نسبة من غير بيانات: {text!r}"


def test_comparison_handles_missing_data():
    assert M.compare_to_last_week(14850, None, None).available is False


def test_comparison_shows_absolute_value():
    """الاتجاه في الكلمة مش في الإشارة — ممنوع 'أقل بـ −6.2%'."""
    c = M.compare_to_last_week(1000, 2000, 300)
    assert c.direction == "down"
    assert c.pct == 50.0 and c.pct > 0
    assert "−" not in M.comparison_text_ar(c)


def test_comparison_flat_band():
    assert M.compare_to_last_week(1020, 1000, 300).direction == "flat"


# ════════════════════════════════════════════════════════════════
# 7) أعلى الأصناف — استبعاد ملاحظات المطبخ
# ════════════════════════════════════════════════════════════════

def test_kitchen_notes_excluded_from_top_items():
    lines = [
        Line(1, 80, 500, 0, 0, "شطة حمراء"),      # ملاحظة — الأكتر عدداً
        Line(1, 1, 110, 1650, 15, "طعمية"),
        Line(1, 2, 93, 2325, 25, "بطاطس كاتشب"),
    ]
    top = M.top_items(lines, product_is_modifier={80: True, 1: False, 2: False})
    assert [n for n, _ in top] == ["طعمية", "بطاطس كاتشب"]


def test_top_items_deterministic_tiebreak():
    lines = [Line(1, 1, 10, 0, 5, "ب"), Line(1, 2, 10, 0, 5, "أ")]
    assert M.top_items(lines) == [("أ", 10.0), ("ب", 10.0)]


# ════════════════════════════════════════════════════════════════
# 8) الحذف — حارس الـ30 دقيقة
# ════════════════════════════════════════════════════════════════

def test_recent_invoice_not_flagged_as_deleted():
    """NOLOCK ممكن يقرا صف بيترجع → تنبيه حذف كاذب."""
    now = dt.datetime(2026, 8, 8, 12, 0)
    fresh = Inv(salid=100, total=50, first_seen_at=now - dt.timedelta(minutes=5))
    assert E.detect_deleted([fresh], [], 0, now) == []


def test_stable_missing_invoice_is_deletion():
    now = dt.datetime(2026, 8, 8, 12, 0)
    old = Inv(salid=100, total=50, first_seen_at=now - dt.timedelta(hours=3))
    evs = E.detect_deleted([old], [], 0, now)
    assert len(evs) == 1 and evs[0].payload["salid"] == 100


# ════════════════════════════════════════════════════════════════
# 9) الجهاز سكت — عتبة متغيّرة
# ════════════════════════════════════════════════════════════════

def test_night_threshold_is_longer():
    """58 من 77 فجوة طبيعية بتبدأ بين 2 و4 الفجر."""
    assert E.no_sales_threshold_minutes(dt.datetime(2026, 8, 8, 3, 0)) == 240
    assert E.no_sales_threshold_minutes(dt.datetime(2026, 8, 8, 13, 0)) == 120


def test_quiet_night_does_not_alert():
    last = dt.datetime(2026, 8, 8, 3, 0)
    assert E.detect_no_sales(last, last + dt.timedelta(minutes=150)) == []
    assert len(E.detect_no_sales(last, last + dt.timedelta(minutes=250))) == 1


# ════════════════════════════════════════════════════════════════
# 10) الحوكمة
# ════════════════════════════════════════════════════════════════

def test_events_suppressed_before_go_live():
    """من غير الحماية دي، أول تشغيل هيبعت مئات التنبيهات التاريخية."""
    go_live = dt.datetime(2026, 8, 10, 7, 0)
    old = E.Event("zero_invoice", "1", 1, dt.datetime(2026, 8, 1, 12, 0))
    new = E.Event("zero_invoice", "2", 1, dt.datetime(2026, 8, 11, 12, 0))
    cfg = {"zero_invoice": {"detect": True, "notify": True}}
    assert [e.dedup_key for e in E.filter_sendable([old, new], cfg, go_live)] == ["2"]


def test_notify_flag_gates_sending():
    cfg = {"zero_invoice": {"detect": True, "notify": False}}
    ev = E.Event("zero_invoice", "1", 1, dt.datetime(2026, 8, 11, 12, 0))
    assert E.filter_sendable([ev], cfg, None) == []


def test_daily_cap_of_three():
    evs = [E.Event("zero_invoice", str(i), 1, dt.datetime(2026, 8, 8, 10 + i, 0))
           for i in range(5)]
    send, defer = E.apply_daily_cap(evs, already_sent_today=0)
    assert len(send) == 3 and len(defer) == 2


def test_banned_words_rejected():
    E.assert_no_accusation("فاتورة تحتاج مراجعة")
    for bad in ("فيه سرقة", "الكاشير سرق", "عملية احتيال"):
        try:
            E.assert_no_accusation(bad)
        except AssertionError:
            continue
        raise AssertionError(f"كلمة اتهام عدّت: {bad}")


def test_deleted_user_gets_fallback_name():
    """المستخدم 5 اتمسح من جدول Users وفواتيره الـ4,288 لسه موجودة."""
    m = M.compute_shift(dt.date(2026, 8, 8), M.MORNING,
                        [Inv(i, 10, user_uid=5) for i in range(30)],
                        user_names={2: "حمص"})
    assert m.primary_user is not None
    assert "محذوف" in m.primary_user.name and "5" in m.primary_user.name


def test_thin_shift_has_no_primary_user():
    m = M.compute_shift(dt.date(2026, 8, 8), M.MORNING, [Inv(1, 10)])
    assert m.is_thin is True and m.primary_user is None


def test_cross_user_amount_still_counted():
    """تدخّل مستخدم تاني = ملاحظة، بس مبلغه محسوب في إجمالي الوردية."""
    invs = [Inv(i, 100, user_uid=1) for i in range(25)] + \
           [Inv(100 + i, 100, user_uid=2) for i in range(3)]
    m = M.compute_shift(dt.date(2026, 8, 8), M.EVENING, invs,
                        user_names={1: "محمود", 2: "حمص"})
    assert m.primary_user.name == "محمود"
    assert [u.name for u in m.other_users] == ["حمص"]
    assert m.sales == 2800.0          # 28 فاتورة × 100 — التدخّل محسوب


def test_signed_money_shows_direction():
    assert "عجز" in M.signed_money(-6339) and "−" in M.signed_money(-6339)
    assert "زيادة" in M.signed_money(1250) and "+" in M.signed_money(1250)
