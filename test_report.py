# -*- coding: utf-8 -*-
"""
test_report.py — report.py status/summary contract tests.

report.py is Phase 1 (locked, review-required) and had zero direct unit
tests before this file — it was only ever exercised indirectly through
test_orchestrator.py's integration-style checks. These tests pin the
existing STATUS_STABLE/CASH/REVIEW behavior AND cover the new has_data
branch added for the 2026-08-11 false-green audit: a shift with zero
invoices in its window must never be reported "🟢 الوردية مستقرة", because
zero observed invoices means the shift was never watched (pre-install,
outage, agent down) at least as plausibly as it means "a genuinely quiet
shift" — and the report cannot tell those apart from the count alone.

Not added to test_golden.py: that file's exact count (31 passed) is a
documented field-acceptance contract (VERIFY.md step 3, preflight.bat) that
installers on customer machines check literally. A new, separate file keeps
that contract untouched while still testing the same locked module.
"""

from __future__ import annotations

import datetime as _dt

import events as E
import metrics as M
import report as R


def _shift(total_invoices=0, n_cash=0, n_return=0, n_external=0,
           sales=0.0, returns=0.0, delivery=0.0, collections=0.0,
           grand_total=0.0):
    return M.ShiftMetrics(
        shift_date=_dt.date(2026, 8, 10), shift_name="evening",
        window_start=_dt.datetime(2026, 8, 10, 19, 0),
        window_end=_dt.datetime(2026, 8, 11, 7, 0),
        sales=sales, returns=returns, delivery=delivery,
        collections=collections, grand_total=grand_total,
        n_cash=n_cash, n_return=n_return, n_external=n_external,
        total_invoices=total_invoices,
    )


_NO_COMPARISON = M.Comparison(False, reason="مفيش بيانات للأسبوع اللي فات")


# ════════════════════════════════════════════════════════════════
# pick_status / pick_summary — unit level
# ════════════════════════════════════════════════════════════════

def test_pick_status_stable_by_default():
    assert R.pick_status(False, False) == R.STATUS_STABLE


def test_pick_status_cash_diff_wins_over_stable():
    assert R.pick_status(True, False) == R.STATUS_CASH


def test_pick_status_notes_without_cash_diff():
    assert R.pick_status(False, True) == R.STATUS_REVIEW


def test_pick_status_no_data_when_invoices_absent():
    assert R.pick_status(False, False, has_data=False) == R.STATUS_NO_DATA


def test_pick_status_no_data_overrides_notes():
    # has_notes only comes from level-2/3 events tied to real invoices —
    # with has_data=False there should be nothing to raise notes about, but
    # the priority must still hold if it ever happens.
    assert R.pick_status(False, True, has_data=False) == R.STATUS_NO_DATA


def test_pick_status_cash_diff_still_wins_when_no_invoice_data():
    # cash reconciliation comes from cash_counts, a source independent of
    # invoices — a real physical cash discrepancy must never be masked by
    # "no data" just because the shift had zero invoices.
    assert R.pick_status(True, False, has_data=False) == R.STATUS_CASH


# ════════════════════════════════════════════════════════════════
# H3 — coverage-gap status: cash > no_data > gap > notes > stable
# ════════════════════════════════════════════════════════════════

def test_pick_status_incomplete_when_coverage_gap():
    assert R.pick_status(False, False, True, has_coverage_gap=True) \
        == R.STATUS_INCOMPLETE


def test_pick_status_incomplete_beats_notes():
    # a coverage gap is a stronger claim than "a few operations to review"
    assert R.pick_status(False, True, True, has_coverage_gap=True) \
        == R.STATUS_INCOMPLETE


def test_pick_status_cash_diff_beats_incomplete():
    # cash reconciliation is an independent physical signal — it must never
    # be masked by a coverage gap (or anything else)
    assert R.pick_status(True, False, True, has_coverage_gap=True) \
        == R.STATUS_CASH


def test_pick_status_no_data_beats_incomplete():
    # a shift with zero invoices was not watched at all — a stronger claim
    # than a partially-watched one
    assert R.pick_status(False, False, False, has_coverage_gap=True) \
        == R.STATUS_NO_DATA


def test_pick_status_stable_without_gap():
    assert R.pick_status(False, False, True, has_coverage_gap=False) \
        == R.STATUS_STABLE


def test_pick_summary_incomplete_has_its_own_text():
    title, body = R.pick_summary(False, False, "none", True,
                                 has_coverage_gap=True)
    assert title == "🟠 الخلاصة"
    assert "انقطاع" in body
    assert "مستقرة" not in body


def test_build_shift_report_with_gap_says_incomplete():
    m = _shift(total_invoices=25, n_cash=25, sales=1000.0,
               collections=0.0, returns=0.0, delivery=0.0, grand_total=1000.0)
    text = R.build_shift_report(m, _NO_COMPARISON, has_coverage_gap=True)
    assert R.STATUS_INCOMPLETE in text
    assert R.STATUS_STABLE not in text


def test_build_shift_report_without_gap_stays_stable():
    m = _shift(total_invoices=25, n_cash=25, sales=1000.0, grand_total=1000.0)
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert R.STATUS_STABLE in text
    assert R.STATUS_INCOMPLETE not in text


def test_pick_summary_no_data_is_not_the_stable_text():
    title, body = R.pick_summary(False, False, "none", has_data=False)
    assert title == "⚪ الخلاصة"
    assert "مستقرة" not in body


# ════════════════════════════════════════════════════════════════
# build_shift_report — full integration, the exact text the owner sees
# ════════════════════════════════════════════════════════════════

def test_zero_invoice_shift_never_says_stable():
    m = _shift(total_invoices=0)
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert R.STATUS_STABLE not in text
    assert R.STATUS_NO_DATA in text


def test_zero_invoice_shift_produces_no_accusation_and_is_still_assertable():
    m = _shift(total_invoices=0)
    text = R.build_shift_report(m, _NO_COMPARISON)
    E.assert_no_accusation(text)   # must not raise


def test_real_shift_with_invoices_still_says_stable():
    m = _shift(total_invoices=25, n_cash=25, sales=1000.0,
               collections=0.0, returns=0.0, delivery=0.0, grand_total=1000.0)
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert R.STATUS_STABLE in text
    assert R.STATUS_NO_DATA not in text


def test_real_shift_with_cash_diff_reports_cash_not_no_data():
    m = _shift(total_invoices=25, n_cash=25, sales=1000.0, grand_total=1000.0)
    cash_event = E.Event(type="cash_diff", dedup_key="k", level=1,
                         occurred_at=_dt.datetime(2026, 8, 10, 20, 0),
                         payload={"expected": 1000.0, "actual": 900.0, "diff": -100.0})
    text = R.build_shift_report(m, _NO_COMPARISON, cash_event=cash_event)
    assert R.STATUS_CASH in text
    assert R.STATUS_STABLE not in text
    assert R.STATUS_NO_DATA not in text


def test_zero_invoice_shift_with_real_cash_diff_still_reports_cash_diff():
    # the one case where a zero-invoice shift must NOT be flattened to
    # "no data" — a cashier can log a cash count with no sales at all.
    m = _shift(total_invoices=0)
    cash_event = E.Event(type="cash_diff", dedup_key="k", level=1,
                         occurred_at=_dt.datetime(2026, 8, 10, 20, 0),
                         payload={"expected": 0.0, "actual": 200.0, "diff": 200.0})
    text = R.build_shift_report(m, _NO_COMPARISON, cash_event=cash_event)
    assert R.STATUS_CASH in text
    assert R.STATUS_NO_DATA not in text


# ════════════════════════════════════════════════════════════════
# daybook presentation — deduction direction must be explicit
# ════════════════════════════════════════════════════════════════
# The four daybook lines (مبيعات / مرتجع مبيعات / دليفري / مقبوضات) are
# NOT all additive: the verified formula (metrics.py, pinned by
# test_golden.py) is الإجمالي = مبيعات + مقبوضات − مرتجع − دليفري. The
# 2026-08-11 presentation fix makes that direction visible in the text —
# additive lines carry +, deduction lines carry −, and the formula is
# spelled out under the total. These tests pin the presentation only;
# the accounting formula is untouched and tested elsewhere.


def _daybook_lines(text: str) -> dict[str, str]:
    """Label -> rendered line, for the five daybook lines."""
    out: dict[str, str] = {}
    for ln in text.splitlines():
        for label in ("مبيعات", "مرتجع مبيعات", "دليفري", "مقبوضات",
                      "الإجمالي"):
            if ln.startswith(label) and label not in out:
                out[label] = ln
    return out


def test_daybook_returns_are_marked_as_deduction():
    m = _shift(total_invoices=25, n_cash=20, n_return=3, n_external=2,
               sales=1000.0, returns=250.0, delivery=0.0,
               collections=300.0, grand_total=1050.0)
    text = R.build_shift_report(m, _NO_COMPARISON)
    line = _daybook_lines(text)["مرتجع مبيعات"]
    assert "−250" in line, line
    assert "+250" not in line, line


def test_daybook_delivery_is_marked_as_deduction():
    m = _shift(total_invoices=25, n_cash=20, n_external=5,
               sales=1000.0, returns=0.0, delivery=635.0,
               collections=300.0, grand_total=665.0)
    text = R.build_shift_report(m, _NO_COMPARISON)
    line = _daybook_lines(text)["دليفري"]
    assert "−635" in line, line
    assert "+635" not in line, line


def test_daybook_sales_and_collections_are_additive():
    m = _shift(total_invoices=25, n_cash=20, n_external=5,
               sales=16305.0, returns=0.0, delivery=0.0,
               collections=3365.0, grand_total=19670.0)
    text = R.build_shift_report(m, _NO_COMPARISON)
    lines = _daybook_lines(text)
    assert "+16,305" in lines["مبيعات"], lines["مبيعات"]
    assert "+3,365" in lines["مقبوضات"], lines["مقبوضات"]


def test_daybook_formula_clarification_is_present():
    m = _shift(total_invoices=25, n_cash=20, n_external=5,
               sales=16305.0, returns=250.0, delivery=635.0,
               collections=3365.0, grand_total=18785.0)
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert "الإجمالي = مبيعات + مقبوضات − مرتجع مبيعات − دليفري" in text


def test_daybook_total_value_is_unchanged():
    # The presentation fix must not move the number: the customer's example
    # report (16,305 + 3,365 − 250 − 635) keeps its verified total 18,785.
    m = _shift(total_invoices=25, n_cash=20, n_external=5,
               sales=16305.0, returns=250.0, delivery=635.0,
               collections=3365.0, grand_total=18785.0)
    text = R.build_shift_report(m, _NO_COMPARISON)
    line = _daybook_lines(text)["الإجمالي"]
    assert "18,785" in line, line
    assert "20,555" not in text, text   # the naive sum must never appear


def test_daybook_zeros_render_with_signs():
    # Zero-value deduction lines still show their direction: a bare "0"
    # under the old layout read as an additive zero; −0 says "nothing
    # deducted" instead.
    m = _shift(total_invoices=25, n_cash=25, sales=0.0,
               returns=0.0, delivery=0.0, collections=0.0, grand_total=0.0)
    text = R.build_shift_report(m, _NO_COMPARISON)
    lines = _daybook_lines(text)
    assert "−0" in lines["مرتجع مبيعات"], lines["مرتجع مبيعات"]
    assert "−0" in lines["دليفري"], lines["دليفري"]
    assert "+0" in lines["مبيعات"], lines["مبيعات"]
    assert "+0" in lines["مقبوضات"], lines["مقبوضات"]


# ════════════════════════════════════════════════════════════════
# H9 — the approved shift-report presentation (2026-08-13)
# ════════════════════════════════════════════════════════════════
# The approved Telegram format: the positive status is now
# "🟢 الوردية مكتملة"; the cashier (محمود-style employee) gets a line right
# after the status and a "↳ منها" sub-breakdown under sales that must NEVER
# change the total (no double-counting). A monitoring gap is rendered
# complete only when gap_explained=True — the orchestrator proves it via
# classify_coverage_gap; otherwise STATUS_INCOMPLETE stays untouched.
#
# Golden-style fixture (task example):
#     sales 17,580 · محمود 1,630 · collections 2,150 · delivery 445
#     returns 0 · withdrawals 0  →  total 19,285

def _mahmoud_shift(withdrawals=0.0):
    total = 17580.0 + 2150.0 - 0.0 - 445.0
    if withdrawals is not None:
        total -= withdrawals
    return M.ShiftMetrics(
        shift_date=_dt.date(2026, 8, 12), shift_name="morning",   # الأربعاء
        window_start=_dt.datetime(2026, 8, 12, 7, 0),
        window_end=_dt.datetime(2026, 8, 12, 19, 0),
        sales=17580.0, returns=0.0, delivery=445.0, collections=2150.0,
        grand_total=total,
        n_cash=262, n_return=0, n_external=16,
        total_invoices=262,
        primary_user=M.UserSlice(uid=1, name="محمود", invoices=24,
                                 amount=1630.0),
    )


def test_normal_complete_shift_says_complete():
    m = _mahmoud_shift()
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert R.STATUS_STABLE in text                # 🟢 الوردية مكتملة
    assert R.STATUS_INCOMPLETE not in text


def test_sleep_explained_gap_is_complete_with_explanation():
    m = _mahmoud_shift()
    text = R.build_shift_report(m, _NO_COMPARISON, has_coverage_gap=True,
                                gap_explained=True)
    assert R.STATUS_STABLE in text
    assert R.STATUS_INCOMPLETE not in text
    assert "الوردية مكتملة وتم تسجيل بياناتها" in text
    assert "حدث انقطاع مؤقت في المراقبة بسبب دخول جهاز الكمبيوتر في وضع " \
           "Sleep، وليس بسبب نقص في الوردية نفسها" in text
    assert "تم استئناف المراقبة بعد عودة الجهاز للعمل" in text


def test_genuine_gap_without_explanation_stays_incomplete():
    m = _mahmoud_shift()
    text = R.build_shift_report(m, _NO_COMPARISON, has_coverage_gap=True)
    assert R.STATUS_INCOMPLETE in text
    assert R.STATUS_STABLE not in text
    assert "قد لا تعكس الوردية كاملة" in text


def test_pick_status_explained_gap_returns_complete():
    assert R.pick_status(False, False, True, True,
                         gap_explained=True) == R.STATUS_STABLE
    assert R.pick_status(False, False, True, True,
                         gap_explained=False) == R.STATUS_INCOMPLETE
    # cash reconciliation still outranks everything, even an explained gap
    assert R.pick_status(True, False, True, True,
                         gap_explained=True) == R.STATUS_CASH


def test_mahmoud_is_shown_as_a_breakdown_of_sales():
    m = _mahmoud_shift()
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert "👥 محمود — 24 فاتورة (1,630 ج) خلال الوردية" in text
    assert "↳ منها محمود" in text
    assert "+1,630" in text


def test_mahmoud_breakdown_does_not_change_the_total():
    # The golden assertion from the task: total stays 19,285 and محمود's
    # 1,630 appears as a breakdown of sales, NOT as an extra amount.
    m = _mahmoud_shift()
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert "19,285" in text
    assert "20,915" not in text          # 19,285 + 1,630 — double count
    assert "21,360" not in text          # naive 17,580 + 2,150 + 1,630


def test_golden_total_and_formula_remain_correct():
    m = _mahmoud_shift()
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert _daybook_lines(text)["الإجمالي"].endswith("19,285 ج")
    assert "الإجمالي = مبيعات + مقبوضات − مرتجع مبيعات − دليفري" in text


def test_zero_returns_delivery_directions_on_golden_numbers():
    m = _mahmoud_shift()
    text = R.build_shift_report(m, _NO_COMPARISON)
    lines = _daybook_lines(text)
    assert "+17,580" in lines["مبيعات"], lines["مبيعات"]
    assert "+2,150" in lines["مقبوضات"], lines["مقبوضات"]
    assert "−445" in lines["دليفري"], lines["دليفري"]
    assert "−0" in lines["مرتجع مبيعات"], lines["مرتجع مبيعات"]


def test_zero_withdrawals_line_when_provided():
    m = _mahmoud_shift(withdrawals=0.0)
    text = R.build_shift_report(m, _NO_COMPARISON, withdrawals=0.0)
    line = [ln for ln in text.splitlines() if ln.startswith("مسحوبات")][0]
    assert "−0" in line, line
    assert "− مسحوبات" in text            # formula includes it when provided


def test_nonzero_withdrawals_line_and_formula():
    m = _mahmoud_shift(withdrawals=150.0)          # total 19,285 − 150 = 19,135
    text = R.build_shift_report(m, _NO_COMPARISON, withdrawals=150.0)
    line = [ln for ln in text.splitlines() if ln.startswith("مسحوبات")][0]
    assert "−150" in line, line
    assert "19,135" in text
    assert ("الإجمالي = مبيعات + مقبوضات − مرتجع مبيعات − دليفري − مسحوبات"
            in text)


def test_withdrawals_omitted_when_data_model_has_no_source():
    # The underlying data model has NO مسحوبات accounting component — the
    # line and the formula term must NOT be fabricated (task §7: report the
    # fact, don't invent the field).
    m = _mahmoud_shift()
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert "مسحوبات" not in text
    assert "− مسحوبات" not in text


def test_cash_reconciliation_confirmation_wording():
    assert R.cash_line(None, False) == \
        "🟢 تم تأكيد بيانات الخزينة من البيانات المسجلة"
    assert R.cash_line(None, True) == "⏸ لم يتم جرد الخزينة في هذه الوردية"
    m = _mahmoud_shift()
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert "💵 الخزينة" in text
    assert "تم تأكيد بيانات الخزينة من البيانات المسجلة" in text
    assert "متطابقة" not in text


def test_previous_week_comparison_available():
    m = _mahmoud_shift()
    cmp = M.Comparison(True, 15000.0, 12.0, "up")
    text = R.build_shift_report(m, cmp)
    assert "📈 مقارنة بنفس الوردية الأربعاء اللي فات" in text
    assert "15,000 ج — " in text
    assert "أعلى بـ 12.0%" in text


def test_previous_week_comparison_unavailable():
    m = _mahmoud_shift()
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert "مفيش بيانات للأسبوع اللي فات" in text


def test_top_five_items_with_medals():
    m = _mahmoud_shift()
    m.top_items = [("طعمية", 109.0), ("فول", 84.0), ("بطاطس كاتشب", 78.0),
                   ("بطاطس", 59.0), ("بطاطس ثومية", 47.0)]
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert "🔥 أكثر 5 أصناف حركة" in text
    assert "🥇 طعمية — 109" in text
    assert "🥈 فول — 84" in text
    assert "🥉 بطاطس كاتشب — 78" in text
    assert "4️⃣ بطاطس — 59" in text
    assert "5️⃣ بطاطس ثومية — 47" in text


def test_fewer_than_five_items_still_renders():
    m = _mahmoud_shift()
    m.top_items = [("طعمية", 109.0), ("فول", 84.0)]
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert "🥇 طعمية — 109" in text
    assert "🥈 فول — 84" in text
    assert "5️⃣" not in text


def test_employee_line_prefers_other_cashiers_else_primary():
    # real-data shape: the branch account is the primary user (shown in the
    # header) and the cashier is an other user — the employee line and the
    # breakdown still lead with the cashier (محمود), dynamically, never
    # hard-coded.
    m = _mahmoud_shift()
    m.primary_user = M.UserSlice(uid=2, name="حمص", invoices=238,
                                 amount=15950.0)
    m.other_users = [M.UserSlice(uid=1, name="محمود", invoices=24,
                                 amount=1630.0)]
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert "👥 محمود — 24 فاتورة (1,630 ج) خلال الوردية" in text
    assert "↳ منها محمود     +1,630 ج" in text
    assert "19,285" in text                 # still the same total


def test_no_employee_line_when_no_valid_user_amount():
    # a shift with no primary user and no other users (thin / unknown) must
    # not invent an employee line or a breakdown
    m = _shift(total_invoices=25, n_cash=25, sales=1000.0, grand_total=1000.0)
    text = R.build_shift_report(m, _NO_COMPARISON)
    assert "خلال الوردية" not in text
    assert "↳ منها" not in text


def test_report_is_arabic_and_passes_the_no_accusation_guard():
    m = _mahmoud_shift()
    text = R.build_shift_report(m, _NO_COMPARISON, has_coverage_gap=True,
                                gap_explained=True)
    assert any("\u0600" <= ch <= "\u06ff" for ch in text), "Arabic text"
    E.assert_no_accusation(text)             # must not raise
    assert "🟢 الوردية مكتملة" in text


def test_status_incomplete_protection_remains_for_genuine_problems():
    m = _mahmoud_shift()
    text = R.build_shift_report(m, _NO_COMPARISON, has_coverage_gap=True)
    assert R.STATUS_INCOMPLETE in text
    assert "🟠 الخلاصة" in text
    assert "قد لا تعكس الوردية كاملة" in text
    E.assert_no_accusation(text)             # guard still enforced
