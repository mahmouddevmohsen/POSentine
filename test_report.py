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
