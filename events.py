# -*- coding: utf-8 -*-
"""
events.py — كشف الأحداث وتصنيفها
================================================================
⚠️ ممنوع تعديل الملف ده من غير مراجعة.

كل دالة نقية: بتاخد داتا وبترجّع أحداث. مفيش اتصال، مفيش ساعة، مفيش عشوائية.
مفاتيح الـdedup محسوبة من الداتا نفسها — مش عشوائية ومش من وقت التشغيل.

مبدأ حاكم: **الكشف دايماً شغّال. الإرسال بمفتاح.**
لو أوقفنا الكشف، التاريخ بيضيع للأبد (مفيش backfill).

🚫 ممنوع منعاً باتاً أي كلمة اتهام في أي مخرج.
   الكلمات المحظورة في الآخر — بتتفحص في الاختبارات.
================================================================
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# ── العتبات ─────────────────────────────────────────────────────
CASH_IGNORE_BELOW = 100.0        # أقل من كده = متطابقة
CASH_LEVEL1_ABS = 500.0
CASH_LEVEL1_PCT_OF_SHIFT = 0.02  # 2% من مبيعات الوردية

REFUND_LEVEL1_ABS = 500.0

# فجوة السكوت — متحقق: 77 فجوة طبيعية ≥ساعتين في 14 شهر، 58 منهم بين 2 و4 الفجر
NO_SALES_MINUTES_DAY = 120
NO_SALES_MINUTES_NIGHT = 240
NIGHT_FROM_HOUR, NIGHT_TO_HOUR = 1, 6

# الفاتورة لازم تكون مستقرة عندنا قبل ما نقول إنها اتمسحت
# (NOLOCK ممكن يقرا كتابة لسه ماتمّتش وترجع → تنبيه حذف كاذب)
DELETION_CONFIRM_MINUTES = 30

DB_SIZE_WARN_PCT = 80.0
CLOCK_DRIFT_WARN_SECONDS = 300

DAILY_ALERT_CAP = 3

# أحداث بتروح للمالك
PUBLIC_TYPES = {"zero_invoice", "refund", "cash_diff", "deleted_invoice",
                "no_sales", "db_size"}
# أحداث بتروح لينا إحنا بس — مشاكلنا مش تنبيهاته
INTERNAL_TYPES = {"unknown_sal_t", "date_change_log", "clock_drift",
                  "schema_drift", "restore_suspected", "agent_down",
                  "telegram_403", "dead_man"}

BANNED_WORDS = ("سرقة", "سرق", "احتيال", "نصب", "تلاعب", "حرامي", "نصاب")


@dataclass(slots=True)
class Event:
    type: str
    dedup_key: str
    level: int                     # 1 فوري · 2 في التقرير · 3 معلومة
    occurred_at: _dt.datetime
    payload: dict[str, Any] = field(default_factory=dict)
    internal: bool = False


# ════════════════════════════════════════════════════════════════
# 1) إيصال بصفر ونفّذ طلبات
# ════════════════════════════════════════════════════════════════

def detect_zero_invoices(
    invoices: Sequence,
    lines_by_salid: dict[int, Sequence],
) -> list[Event]:
    """
    الإيصال إجماليه صفر **و** فيه صنف مسعّر.

    ⚠️ التعريف ده مقصود بالظبط. على 7 أيام من الداتا الحقيقية:
        11 إيصال · 1,010 ج  → تنبيه          (أكل خرج واتسجّل بصفر)
         4 إيصال            → متجاهلة        (ملاحظات مطبخ بس)
        14 إيصال ·   120 ج  → متجاهلة        (إضافة صغيرة مجانية مع أوردر مدفوع)

    الاعتماد على list_price **المجمّد في السطر** مش على جدول الأصناف الحالي،
    عشان لو غيّروا سعر صنف بعدين، الكشف التاريخي مايتغيرش بأثر رجعي.
    """
    out: list[Event] = []
    for inv in invoices:
        if float(inv.total or 0) != 0:
            continue
        if inv.kind == "return":            # المرتجع ليه مساره
            continue

        priced = [
            ln for ln in lines_by_salid.get(inv.salid, ())
            if float(getattr(ln, "list_price", 0) or 0) > 0
            and float(getattr(ln, "line_total", 0) or 0) == 0
        ]
        if not priced:
            continue                        # ملاحظات مطبخ بس — مش حدث

        value = sum(float(ln.list_price) * float(ln.qty or 0) for ln in priced)
        out.append(Event(
            type="zero_invoice",
            dedup_key=str(inv.salid),
            level=1,
            occurred_at=inv.sold_at,
            payload={
                "salid": inv.salid,
                "receipt_num": inv.receipt_num,
                "user_uid": inv.user_uid,
                "list_value": round(value, 2),
                "items": [
                    {"name": getattr(ln, "item_name", None) or f"صنف #{ln.itid}",
                     "qty": float(ln.qty or 0),
                     "list_price": float(ln.list_price)}
                    for ln in priced
                ],
            },
        ))
    return out


# ════════════════════════════════════════════════════════════════
# 2) المرتجعات
# ════════════════════════════════════════════════════════════════

def detect_refunds(invoices: Sequence,
                   level1_threshold: float = REFUND_LEVEL1_ABS) -> list[Event]:
    """
    مش كل مرتجع يستاهل مقاطعة المالك — 46 في 14 شهر.
    الكبير Level 1، والعادي Level 2 (في تقرير الوردية).
    """
    out: list[Event] = []
    for inv in invoices:
        if inv.kind != "return":
            continue
        amount = abs(float(inv.total or 0))
        out.append(Event(
            type="refund",
            dedup_key=str(inv.salid),
            level=1 if amount >= level1_threshold else 2,
            occurred_at=inv.sold_at,
            payload={"salid": inv.salid, "receipt_num": inv.receipt_num,
                     "user_uid": inv.user_uid, "amount": round(amount, 2)},
        ))
    return out


# ════════════════════════════════════════════════════════════════
# 3) فروقات الخزينة
# ════════════════════════════════════════════════════════════════

def cash_dedup_key(uid: int | None, app_value: float,
                   user_value: float, counted_at: _dt.datetime) -> str:
    """
    🔑 المفتاح بالتاريخ مش ببكت زمني.

    البرنامج بيكتب صفوف مكررة للحدث الواحد: SRID 6→14 كانوا 9 صفوف
    بنفس القيم في 6 ثواني. ولو استخدمنا floor(epoch/300)، صفّان بفارق
    ثانيتين ممكن يقعوا في بكتين مختلفين → تنبيهين لحدث واحد.

    نفس (المستخدم، قيمة السيستم، قيمة الجرد) في نفس اليوم = حدث واحد.
    """
    return f"{uid}:{app_value:.2f}:{user_value:.2f}:{counted_at.date().isoformat()}"


def detect_cash_diffs(
    cash_counts: Sequence,
    shift_sales_lookup=None,       # callable(counted_at) -> float | None
) -> list[Event]:
    """
    🚨 أخطر كشف في النظام.

    17 صف من 21 في الداتا فيهم SrUserval = 0 = **الجرد ماتعملش**.
    لو حسبنا الفرق على علاته، أول تنبيه هيقول "عجز 15,758 ج" وهو غير صحيح
    ومعاه اتهام ضمني لموظف باسمه — وده كفيل يحرق المنتج من أول يوم.
    """
    out: list[Event] = []
    seen: set[str] = set()

    for c in cash_counts:
        key = cash_dedup_key(c.user_uid, c.app_value, c.user_value, c.counted_at)
        if key in seen:
            continue                        # صف مكرر لنفس الحدث
        seen.add(key)

        if c.kind == "no_count":
            out.append(Event(
                type="cash_no_count", dedup_key=key, level=3,
                occurred_at=c.counted_at,
                payload={"srid": c.srid, "user_uid": c.user_uid,
                         "app_value": c.app_value},
            ))
            continue                        # ⏸ مش عجز — ممنوع تتحول لتنبيه

        diff = c.user_value - c.app_value
        adiff = abs(diff)
        if adiff < CASH_IGNORE_BELOW:
            continue                        # متطابقة

        level = 2
        if adiff >= CASH_LEVEL1_ABS:
            level = 1
        elif shift_sales_lookup is not None:
            shift_sales = shift_sales_lookup(c.counted_at)
            if shift_sales and adiff >= CASH_LEVEL1_PCT_OF_SHIFT * shift_sales:
                level = 1

        out.append(Event(
            type="cash_diff", dedup_key=key, level=level,
            occurred_at=c.counted_at,
            payload={"srid": c.srid, "user_uid": c.user_uid,
                     "expected": c.app_value, "actual": c.user_value,
                     "diff": round(diff, 2)},
        ))
    return out


# ════════════════════════════════════════════════════════════════
# 4) الفواتير المحذوفة
# ════════════════════════════════════════════════════════════════

def detect_deleted(
    known_invoices: Sequence,      # فواتيرنا داخل نافذة الرصد (غير محذوفة)
    window_salids: Iterable[int],  # اللي رجعت من الـPOS دلوقتي
    window_from_salid: int,
    now_utc: _dt.datetime,         # مُمرّر — مش من الساعة
) -> list[Event]:
    """
    الغياب هو الإشارة. الـupsert لوحده مبيشوفش الغياب.

    ⚠️ حارس إجباري: الفاتورة لازم تكون عندنا من أكتر من 30 دقيقة.
       NOLOCK ممكن يقرا صف لسه بيتكتب وبعدين يترجع (rollback) →
       يختفي من النافذة → تنبيه "فاتورة اتمسحت" وهي معملتش أصلاً.

    ملحوظة: الفجوات في salid **مش** دليل حذف.
    73 فجوة بـ9,999 في التاريخ كلها identity cache jumps (bigint = 10,000).
    """
    present = set(window_salids)
    cutoff = now_utc - _dt.timedelta(minutes=DELETION_CONFIRM_MINUTES)
    out: list[Event] = []

    for inv in known_invoices:
        if inv.salid < window_from_salid:
            continue                        # برّه النافذة — مش موضوع الفحص
        if inv.salid in present:
            continue
        first_seen = getattr(inv, "first_seen_at", None)
        if first_seen is None or first_seen > cutoff:
            continue                        # لسه جديدة — مش مؤكدة

        out.append(Event(
            type="deleted_invoice", dedup_key=str(inv.salid), level=1,
            occurred_at=inv.sold_at,
            payload={"salid": inv.salid, "receipt_num": inv.receipt_num,
                     "user_uid": inv.user_uid,
                     "amount": round(float(inv.total or 0), 2)},
        ))
    return out


# ════════════════════════════════════════════════════════════════
# 5) الجهاز سكت
# ════════════════════════════════════════════════════════════════

def no_sales_threshold_minutes(gap_start: _dt.datetime) -> int:
    """
    عتبة متغيّرة حسب الساعة.

    متحقق على 14 شهر: 77 فجوة طبيعية ≥ساعتين، **58 منهم بيبدأوا بين 2 و4 الفجر**.
    عتبة ثابتة ساعتين = ~5-6 تنبيه كاذب في الشهر أغلبهم الفجر.
    """
    h = gap_start.hour
    if NIGHT_FROM_HOUR <= h < NIGHT_TO_HOUR:
        return NO_SALES_MINUTES_NIGHT
    return NO_SALES_MINUTES_DAY


def detect_no_sales(
    last_invoice_at: _dt.datetime | None,
    pos_now: _dt.datetime,
) -> list[Event]:
    """تنبيه واحد لكل فترة سكوت — الـdedup_key بيثبّته على وقت آخر فاتورة."""
    if last_invoice_at is None:
        return []
    gap_minutes = (pos_now - last_invoice_at).total_seconds() / 60.0
    threshold = no_sales_threshold_minutes(last_invoice_at)
    if gap_minutes < threshold:
        return []
    return [Event(
        type="no_sales",
        dedup_key=last_invoice_at.isoformat(timespec="minutes"),
        level=1, occurred_at=last_invoice_at,
        payload={"last_invoice_at": last_invoice_at.isoformat(),
                 "gap_minutes": int(gap_minutes),
                 "threshold_minutes": threshold},
    )]


# ════════════════════════════════════════════════════════════════
# 6) أحداث داخلية — تروح لينا مش للمالك
# ════════════════════════════════════════════════════════════════

def detect_internal(
    pull_result,
    previous_date_change_rows: int,
    our_local_now: _dt.datetime,
) -> list[Event]:
    out: list[Event] = []
    at = pull_result.pos_clock or our_local_now

    for sal_t in sorted(set(pull_result.unknown_sal_t)):
        out.append(Event("unknown_sal_t", f"salT={sal_t}", 3, at,
                         {"sal_t": sal_t}, internal=True))

    if pull_result.date_change_rows > previous_date_change_rows:
        out.append(Event("date_change_log", str(pull_result.date_change_rows), 1, at,
                         {"rows": pull_result.date_change_rows,
                          "previous": previous_date_change_rows}, internal=True))

    if pull_result.pos_clock is not None:
        drift = int((pull_result.pos_clock - our_local_now).total_seconds())
        if abs(drift) > CLOCK_DRIFT_WARN_SECONDS:
            out.append(Event("clock_drift", our_local_now.date().isoformat(), 1, at,
                             {"drift_seconds": drift}, internal=True))

    if pull_result.db_size_mb:
        pct = pull_result.db_size_mb / 10240.0 * 100.0
        if pct >= DB_SIZE_WARN_PCT:
            out.append(Event("db_size", our_local_now.date().isoformat(), 2, at,
                             {"size_mb": pull_result.db_size_mb,
                              "pct": round(pct, 2)}))
    return out


# ════════════════════════════════════════════════════════════════
# التصفية والحد اليومي
# ════════════════════════════════════════════════════════════════

def filter_sendable(
    events: Sequence[Event],
    settings: dict[str, dict],     # {type: {"detect":bool,"notify":bool}}
    go_live_at: _dt.datetime | None,
) -> list[Event]:
    """
    الكشف بيفضل شغّال دايماً. الإرسال بيتحكم فيه:
      • notify في الإعدادات
      • go_live_at — أي حدث أقدم منه بيتقمع

    ⚠️ من غير شرط go_live_at، أول تشغيل هيبعت مئات التنبيهات التاريخية.
    """
    out: list[Event] = []
    for e in events:
        if e.internal:
            continue
        cfg = settings.get(e.type)
        if not cfg or not cfg.get("notify"):
            continue
        if go_live_at and e.occurred_at < go_live_at:
            continue
        out.append(e)
    return out


def apply_daily_cap(
    events: Sequence[Event],
    already_sent_today: int,
    cap: int = DAILY_ALERT_CAP,
) -> tuple[list[Event], list[Event]]:
    """
    حد 3 تنبيهات Level 1 في اليوم التجاري. الزيادة بترجع في تقرير الوردية.
    الترتيب ثابت (الوقت ثم النوع ثم المفتاح) عشان النتيجة تبقى قابلة للتكرار.
    """
    room = max(0, cap - already_sent_today)
    l1 = sorted((e for e in events if e.level == 1),
                key=lambda e: (e.occurred_at, e.type, e.dedup_key))
    rest = [e for e in events if e.level != 1]
    return l1[:room], l1[room:] + rest


def assert_no_accusation(text: str) -> None:
    """بيتنادى في الاختبارات وقبل كل إرسال."""
    for w in BANNED_WORDS:
        if w in text:
            raise AssertionError(f"كلمة اتهام محظورة في المخرج: {w!r}")
