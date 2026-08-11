# -*- coding: utf-8 -*-
"""
test_orchestrator.py — Phase 2 orchestrator contract tests.

No network. The pure plan() is driven with constructed state; run() is
driven through a fake client whose store honors the same UNIQUE constraints
as the real schema, so the idempotency claim is tested against the actual
arbiter (the constraint), not against application logic.

The failure shape this project keeps hitting is "a check that shares the
fault it is meant to detect". The no-pyodbc closure test therefore has a
paired falsifier (test_closure_walker_can_detect_pyodbc) that proves the
walker can fail before the main test proves the tree is clean.
"""

from __future__ import annotations

import ast
import datetime as _dt
from pathlib import Path

import pytest

import orchestrator
import report as R

TID = "57b61b47-a590-49fe-803c-0c174a07b7ec"
SID = "93f8d146-ba68-4d58-8eda-f797f3e28bd4"
REPO = Path(__file__).resolve().parent

CAIRO = "Africa/Cairo"


def t(y, mo, d, h, mi=0):
    return _dt.datetime(y, mo, d, h, mi)          # naive POS local


def utc(y, mo, d, h, mi=0):
    return _dt.datetime(y, mo, d, h, mi, tzinfo=_dt.timezone.utc)


# ════════════════════════════════════════════════════════════════
# builders
# ════════════════════════════════════════════════════════════════

def inv(salid, sold_at, total=100.0, kind="cash", user=1, delivery=0.0,
        first_seen=None, last_seen=None, deleted=None):
    return orchestrator.Inv(salid=salid, receipt_num=salid, sold_at=sold_at,
                            total=total, delivery_cost=delivery, user_uid=user,
                            kind=kind, first_seen_at=first_seen,
                            last_seen_at=last_seen, deleted_at=deleted)


def dev_rcpt(**kw):
    kw.setdefault("address", "111")
    kw.setdefault("label", "dev")
    kw.setdefault("active", True)
    kw.setdefault("notify_before_golive", True)
    return orchestrator.Recipient(**kw)


def owner_rcpt(**kw):
    kw.setdefault("address", "222")
    kw.setdefault("label", "owner")
    kw.setdefault("active", True)
    kw.setdefault("notify_before_golive", False)
    return orchestrator.Recipient(**kw)


def settings(notify=True):
    base = {"zero_invoice", "refund", "cash_diff", "deleted_invoice", "no_sales"}
    return {k: {"detect": True, "notify": notify} for k in base}


def ctx(go_live=None, recipients=None, alert_settings=None,
        morning="07:00", evening="19:00", timezone=CAIRO):
    return orchestrator.TenantContext(
        tenant_id=TID, source_id=SID, timezone=timezone,
        shift_morning_start=_dt.time.fromisoformat(morning),
        shift_evening_start=_dt.time.fromisoformat(evening),
        go_live_at=go_live,
        recipients=recipients if recipients is not None else [dev_rcpt()],
        alert_settings=alert_settings if alert_settings is not None else settings())


def state(existing=(), sent=0, invoices=(), lines=(), cash=(), users=None,
          modifiers=None, first_sync=None, heartbeat=None, last_rescan=None,
          rescan_from=0, month_invoices=(), month_lines=(), mirrored=(),
          failure=(), open_kinds=(), restore=False, schema_ok=True):
    return orchestrator.DBState(
        existing_reports=set(existing),
        sent_alerts_today=sent,
        invoices=list(invoices),
        lines=list(lines),
        cash_counts=list(cash),
        users=users if users is not None else {1: "أحمد"},
        modifiers=modifiers if modifiers is not None else {},
        first_sync_at=first_sync,
        heartbeat_at=heartbeat,
        last_rescan_at=last_rescan,
        rescan_from_salid=rescan_from,
        restore_suspected=restore,
        schema_ok=schema_ok,
        month_invoices=list(month_invoices),
        month_lines=list(month_lines),
        mirrored_heartbeat_ids=set(mirrored),
        failure_heartbeats=list(failure),
        open_anomaly_kinds=set(open_kinds))


def line(saledeid, salid, qty=1.0, list_price=50.0, item="طعمية", itid=1):
    return orchestrator.Line(saledeid=saledeid, salid=salid, itid=itid,
                             item_name=item, list_price=list_price, qty=qty,
                             line_total=qty * list_price, sold_at=None)


# ════════════════════════════════════════════════════════════════
# DST — decision 5: zoneinfo, never a hardcoded UTC+3
# ════════════════════════════════════════════════════════════════

def test_dst_winter_boundary_selects_yesterday_morning():
    """2026-01-01 04:55Z = 06:55 Cairo (UTC+2) — evening has NOT closed."""
    out = orchestrator.plan(utc(2026, 1, 1, 4, 55), ctx(), state())
    assert out.shift_row is not None
    assert out.shift_row["shift_date"] == "2025-12-31"
    assert out.shift_row["shift_name"] == "morning"
    # a hardcoded UTC+3 would read 07:55 local and wrongly pick evening


def test_dst_summer_boundary_selects_yesterday_morning():
    """2026-07-01 03:55Z = 06:55 Cairo (UTC+3)."""
    out = orchestrator.plan(utc(2026, 7, 1, 3, 55), ctx(), state())
    assert out.shift_row["shift_date"] == "2026-06-30"
    assert out.shift_row["shift_name"] == "morning"


def test_dst_winter_morning_after_seven_picks_evening():
    """2026-01-01 05:10Z = 07:10 Cairo (UTC+2) — evening(Dec 31) just closed."""
    out = orchestrator.plan(utc(2026, 1, 1, 5, 10), ctx(), state())
    assert out.shift_row["shift_date"] == "2025-12-31"
    assert out.shift_row["shift_name"] == "evening"


def test_dst_summer_morning_after_seven_picks_evening():
    """2026-07-01 04:10Z = 07:10 Cairo (UTC+3)."""
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(), state())
    assert out.shift_row["shift_date"] == "2026-06-30"
    assert out.shift_row["shift_name"] == "evening"


def test_dst_summer_evening_after_seven_picks_morning():
    """2026-07-01 16:10Z = 19:10 Cairo — morning(Jul 1) just closed."""
    out = orchestrator.plan(utc(2026, 7, 1, 16, 10), ctx(), state())
    assert out.shift_row["shift_date"] == "2026-07-01"
    assert out.shift_row["shift_name"] == "morning"


# ════════════════════════════════════════════════════════════════
# shift selection
# ════════════════════════════════════════════════════════════════

def test_late_run_still_picks_the_closed_shift():
    """Rule: 'most recent closed shift with no report', not 'exactly 07:00'."""
    for minute in (10, 20, 40, 59):
        out = orchestrator.plan(utc(2026, 7, 1, 4, minute), ctx(), state())
        assert out.shift_row["shift_name"] == "evening", f"at 07:{minute}"


def test_shift_already_reported_goes_back_one():
    """Existing morning report -> the previous evening shift is selected."""
    local_now = t(2026, 7, 1, 19, 30)
    existing = {(_dt.date(2026, 7, 1), "morning")}
    picked = orchestrator.select_shift(local_now, ctx(), existing)
    assert picked == (_dt.date(2026, 6, 30), "evening")


def test_no_shift_when_everything_is_reported():
    local_now = t(2026, 7, 1, 7, 10)
    d0 = _dt.date(2026, 7, 1)
    existing = {(d0 - _dt.timedelta(days=back), name)
                for back in range(15) for name in ("morning", "evening")}
    assert orchestrator.select_shift(local_now, ctx(), existing) is None


def test_force_shift_and_shift_date_build_exactly_that_shift():
    local_now = t(2026, 7, 3, 10, 0)
    picked = orchestrator.select_shift(local_now, ctx(), set(),
                                       force_shift="morning",
                                       shift_date=_dt.date(2026, 7, 2))
    assert picked == (_dt.date(2026, 7, 2), "morning")


def test_force_shift_alone_uses_most_recent_closed_occurrence():
    local_now = t(2026, 7, 3, 10, 0)
    picked = orchestrator.select_shift(local_now, ctx(), set(),
                                       force_shift="evening")
    assert picked == (_dt.date(2026, 7, 2), "evening")


# ════════════════════════════════════════════════════════════════
# shift report wiring
# ════════════════════════════════════════════════════════════════

def test_shift_report_body_is_the_verified_arabic_template():
    """grand_total = sales + collections - returns - delivery, per شاشة يومية الخزينة."""
    s = state(invoices=[
        inv(1, t(2026, 6, 30, 20, 0), total=100.0, kind="cash"),
        inv(2, t(2026, 6, 30, 21, 0), total=50.0, kind="cash"),
        inv(3, t(2026, 6, 30, 22, 0), total=300.0, kind="external"),
        inv(4, t(2026, 6, 30, 23, 0), total=25.0, kind="return"),
    ], lines=[line(11, 1), line(12, 2)])
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(), s)
    assert out.shift_row is not None
    assert out.shift_row["grand_total"] == 100.0 + 50.0 + 300.0 - 25.0 - 0.0
    body = _shift_body(out)
    assert "تقرير نهاية الوردية" in body
    assert "الإجمالي" in body
    assert "مفيش بيانات للأسبوع اللي فات" in body      # no last-week data


def test_shift_envelope_dedup_key_is_exact():
    s = state(invoices=[inv(1, t(2026, 6, 30, 20, 0))])
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(), s)
    shift = next(e for e in out.envelopes if e.kind == "shift_report")
    assert shift.dedup_key == "shift_report:2026-06-30:evening"


def test_partial_first_shift_is_recorded_but_never_reported():
    s = state(invoices=[inv(1, t(2026, 6, 30, 20, 0))],
              first_sync=_dt.datetime(2026, 6, 30, 20, 0,
                                      tzinfo=_dt.timezone.utc))
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(), s)
    assert out.shift_row is not None
    assert out.shift_row["is_partial"] is True
    assert not any(e.kind == "shift_report" for e in out.envelopes)


def test_shift_entirely_before_install_is_never_reported():
    """
    Regression for the live false-green: agent.py adopts MAX(salid) on first
    run and reads nothing behind it (no backfill by design — installer.py,
    VERIFY.md step 4a), so a shift whose window closed before first_sync_at
    is GUARANTEED to have zero real invoices. Reproduced live against
    production Supabase on 2026-08-11: shift_report:2026-08-07:evening and
    2026-08-08:morning/evening were built with grand_total=0, is_partial was
    False, and all three were enqueued and actually sent to the dev chat.
    This shift's window (2026-06-29 evening) ends at 2026-06-30 07:00, a
    full day before first_sync_at — no straddle, so the OLD is_partial
    condition (`start <= first_sync_at < end`) missed it entirely.
    """
    s = state(invoices=[],  # no backfill: genuinely nothing here, ever
              first_sync=_dt.datetime(2026, 7, 1, 4, 0,
                                      tzinfo=_dt.timezone.utc))  # 07:00 Cairo, Jul 1
    out = orchestrator.plan(utc(2026, 7, 2, 4, 10), ctx(), s,
                            force_shift="evening",
                            shift_date=_dt.date(2026, 6, 29))
    assert out.shift_row is not None
    assert out.shift_row["is_partial"] is True
    assert out.shift_row["grand_total"] == 0.0
    assert not any(e.kind == "shift_report" for e in out.envelopes)


def test_shift_boundary_ending_exactly_at_first_sync_has_no_coverage():
    """end == first_sync_at: the agent's very first sync happened the
    instant this shift closed — zero overlap, not a straddle."""
    first_sync_local = t(2026, 6, 30, 19, 0)          # exactly shift end
    s = state(invoices=[],
              first_sync=first_sync_local.replace(tzinfo=_dt.timezone.utc))
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(), s,
                            force_shift="morning",
                            shift_date=_dt.date(2026, 6, 30))
    assert out.shift_row["is_partial"] is True
    assert not any(e.kind == "shift_report" for e in out.envelopes)


def test_multi_shift_backfill_never_leaks_a_stable_report():
    """
    The exact live scenario: a fresh install with no existing_reports and
    MAX_SHIFT_LOOKBACK_DAYS=14 walking backward. Seed several closed shifts
    entirely before first_sync_at (no invoices — no backfill) plus one real
    post-install shift, and drive plan() repeatedly the way the cron does
    (one shift decided per call, each recorded before the next call). None
    of the pre-install shifts may ever produce a "shift_report" envelope,
    and none of their bodies (if ever built) may contain STATUS_STABLE.
    """
    first_sync = _dt.datetime(2026, 7, 10, 14, 0, tzinfo=_dt.timezone.utc)  # 17:00 Cairo Jul 10
    real_shift_invs = [inv(i, t(2026, 7, 10, 20, 0)) for i in range(1, 25)]
    now = utc(2026, 7, 11, 8, 0)

    existing: set[tuple[_dt.date, str]] = set()
    reported_stable_pre_install = []
    for _ in range(6):                     # walk back through several shifts
        s = state(existing=existing, invoices=real_shift_invs, first_sync=first_sync)
        out = orchestrator.plan(now, ctx(), s)
        if out.shift_row is None:
            break
        key = (_dt.date.fromisoformat(out.shift_row["shift_date"]),
               out.shift_row["shift_name"])
        existing.add(key)
        for e in out.envelopes:
            if e.kind == "shift_report" and R.STATUS_STABLE in e.body:
                reported_stable_pre_install.append((key, out.shift_row))

    # every shift with zero real coverage must never have been announced stable
    for key, row in reported_stable_pre_install:
        assert row["grand_total"] != 0.0 or row["is_partial"], (
            f"false green: {key} reported STATUS_STABLE with grand_total="
            f"{row['grand_total']} is_partial={row['is_partial']}")


# ════════════════════════════════════════════════════════════════
# monthly — decision 4
# ════════════════════════════════════════════════════════════════

def test_monthly_built_on_day_one_only():
    d1 = _dt.date(2026, 8, 1)
    existing = {(d1 - _dt.timedelta(days=back), n)
                for back in range(15) for n in ("morning", "evening")}
    ml = [line(1, 1, qty=4.0, list_price=10.0, item="طعمية"),
          line(2, 2, qty=2.0, list_price=5.0, item="كولا")]
    s = state(existing=existing, month_lines=ml)
    out = orchestrator.plan(utc(2026, 7, 31, 21, 10), ctx(), s)   # Aug 1 00:10 Cairo
    monthly = [e for e in out.envelopes if e.kind == "monthly_products"]
    assert len(monthly) == 1
    assert monthly[0].dedup_key == "monthly:2026-07"
    assert "أصناف الشهر الماضي" in monthly[0].body

    out2 = orchestrator.plan(utc(2026, 8, 1, 21, 10), ctx(), s)   # Aug 2 00:10 Cairo
    assert not any(e.kind == "monthly_products" for e in out2.envelopes)


# ════════════════════════════════════════════════════════════════
# daily cap — decision 3: only sent rows count, tenant-local day
# ════════════════════════════════════════════════════════════════

def _four_level1_refunds():
    return [inv(i, t(2026, 6, 30, 20 + i, 0), total=600.0, kind="return")
            for i in range(4)]


def test_daily_cap_allows_two_of_four_when_one_already_sent():
    s = state(invoices=_four_level1_refunds(), sent=1)
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(), s)
    alerts = [e for e in out.envelopes if e.kind == "alert"]
    assert len(alerts) == 2
    assert len(out.queue_keys) == 2
    # the capped two surface in the shift report notes instead
    assert "مرتجع" in _shift_body(out)
    refund_rows = [r for r in out.event_rows if r["type"] == "refund"]
    assert len([r for r in refund_rows if r["status"] == "detected"]) == 2
    assert len([r for r in refund_rows if r["status"] == "suppressed"]) == 2


def test_daily_cap_exhausted_sends_nothing():
    s = state(invoices=_four_level1_refunds(), sent=3)
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(), s)
    assert not any(e.kind == "alert" for e in out.envelopes)
    assert out.queue_keys == []


def test_alert_dedup_key_and_recipient():
    s = state(invoices=_four_level1_refunds(), sent=0)
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(), s)
    alerts = [e for e in out.envelopes if e.kind == "alert"]
    assert alerts[0].dedup_key == "alert:refund:0"
    assert all(e.recipient == "111" for e in alerts)


def _shift_body(out):
    for e in out.envelopes:
        if e.kind == "shift_report":
            return e.body
    return ""


# ════════════════════════════════════════════════════════════════
# go_live and notify_before_golive — decisions 1 and 5
# ════════════════════════════════════════════════════════════════

def test_go_live_suppresses_older_events_only():
    # go_live 19:00Z = 22:00 Cairo (UTC+3): the 21:00 refund predates it,
    # the 23:00 refund postdates it.
    go_live = _dt.datetime(2026, 6, 30, 19, 0, tzinfo=_dt.timezone.utc)
    refunds = [inv(1, t(2026, 6, 30, 21, 0), total=600.0, kind="return"),   # before
               inv(2, t(2026, 6, 30, 23, 0), total=600.0, kind="return")]   # after
    s = state(invoices=refunds)
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10),
                            ctx(go_live=go_live,
                                alert_settings=settings(notify=True)), s)
    alerts = [e for e in out.envelopes if e.kind == "alert"
              and e.dedup_key.startswith("alert:refund:")]
    assert [e.dedup_key for e in alerts] == ["alert:refund:2"]
    rows = {r["dedup_key"]: r["status"] for r in out.event_rows
            if r["type"] == "refund"}
    assert rows["1"] == "suppressed"
    assert rows["2"] == "detected"


def test_notify_before_golive_bypasses_only_the_go_live_gate():
    s = state(invoices=[inv(1, t(2026, 6, 30, 20, 0))])
    rcpts = [dev_rcpt(), owner_rcpt()]
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(go_live=None,
                                                         recipients=rcpts), s)
    sent_to = {e.recipient for e in out.envelopes if e.kind == "shift_report"}
    assert sent_to == {"111"}                      # owner stays silent pre-live

    out2 = orchestrator.plan(utc(2026, 7, 1, 4, 10),
                             ctx(go_live=_dt.datetime(2026, 7, 1, 0, 0,
                                                      tzinfo=_dt.timezone.utc),
                                 recipients=rcpts), s)
    sent_to2 = {e.recipient for e in out2.envelopes if e.kind == "shift_report"}
    assert sent_to2 == {"111", "222"}


def test_active_is_never_bypassed():
    s = state(invoices=[inv(1, t(2026, 6, 30, 20, 0))])
    rcpts = [dev_rcpt(active=False)]
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(recipients=rcpts), s)
    assert out.envelopes == []


def test_alert_gated_by_alert_settings_notify():
    s = state(invoices=_four_level1_refunds())
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10),
                            ctx(alert_settings=settings(notify=False)), s)
    assert not any(e.kind == "alert" for e in out.envelopes)
    refund_rows = [r for r in out.event_rows if r["type"] == "refund"]
    assert refund_rows and all(r["status"] == "suppressed" for r in refund_rows)


# ════════════════════════════════════════════════════════════════
# deletion detection is gated on a live agent
# ════════════════════════════════════════════════════════════════

def _deletion_state(now_utc):
    fresh = now_utc - _dt.timedelta(minutes=5)
    old = now_utc - _dt.timedelta(hours=2)
    invs = [
        inv(101, t(2026, 7, 1, 5, 0), first_seen=fresh, last_seen=fresh),
        inv(102, t(2026, 7, 1, 4, 0), first_seen=old, last_seen=old),
    ]
    return state(invoices=invs, heartbeat=fresh, last_rescan=fresh,
                 rescan_from=100)


def test_deletion_detected_when_agent_is_fresh():
    now_utc = utc(2026, 7, 1, 4, 10)
    out = orchestrator.plan(now_utc, ctx(), _deletion_state(now_utc))
    kinds = {r["type"] for r in out.event_rows}
    assert "deleted_invoice" in kinds


def test_deletion_never_inferred_from_a_stale_agent():
    now_utc = utc(2026, 7, 1, 4, 10)
    s = _deletion_state(now_utc)
    s.last_rescan_at = now_utc - _dt.timedelta(hours=3)     # agent stale
    s.heartbeat_at = now_utc - _dt.timedelta(hours=2)
    out = orchestrator.plan(now_utc, ctx(), s)
    assert not any(r["type"] == "deleted_invoice" for r in out.event_rows)


def test_deletion_never_inferred_while_restore_is_suspected():
    now_utc = utc(2026, 7, 1, 4, 10)
    s = _deletion_state(now_utc)
    s.restore_suspected = True                            # salid went backwards
    out = orchestrator.plan(now_utc, ctx(), s)
    assert not any(r["type"] == "deleted_invoice" for r in out.event_rows)


# ════════════════════════════════════════════════════════════════
# guardrails
# ════════════════════════════════════════════════════════════════

def test_no_accusation_word_can_reach_an_envelope(monkeypatch):
    monkeypatch.setattr(R, "build_shift_report",
                        lambda *a, **k: "الوردية دي فيها سرقة 100 ج")
    with pytest.raises(AssertionError):
        orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(),
                          state(invoices=[inv(1, t(2026, 6, 30, 20, 0))]))


def test_heartbeat_silence_records_dead_man_once():
    old = utc(2026, 7, 1, 1, 0)                       # 3h before now
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(),
                            state(heartbeat=old))
    kinds = [json_kind(a) for a in out.anomalies]
    assert "dead_man" in kinds
    # a second run with an open dead_man does not duplicate
    out2 = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(),
                             state(heartbeat=old, open_kinds={"dead_man"}))
    assert not any(json_kind(a) == "dead_man" for a in out2.anomalies)


def test_dead_man_re_arms_when_the_agent_returns():
    fresh = utc(2026, 7, 1, 4, 9)                     # 1 min before now
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(),
                            state(heartbeat=fresh, open_kinds={"dead_man"}))
    assert out.resolve_dead_man is True


def test_failure_heartbeat_notes_are_mirrored_exactly_once():
    import json as _json
    note = _json.dumps({"kind": "backlog",
                        "detail": {"cap": 5000, "pulled": 5000},
                        "agent_version": "v1.0.1"}, ensure_ascii=False)
    fresh = utc(2026, 7, 1, 4, 9)
    s = state(heartbeat=fresh, failure=[{"id": 7, "note": note}])
    out = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(), s)
    ids = [int(_json.loads(a["detail"])["heartbeat_id"])
           for a in out.anomalies if "heartbeat_id" in _json.loads(a["detail"])]
    assert ids == [7]
    # second run with id 7 already mirrored -> nothing new
    out2 = orchestrator.plan(utc(2026, 7, 1, 4, 10), ctx(),
                             state(heartbeat=fresh,
                                   failure=[{"id": 7, "note": note}],
                                   mirrored={7}))
    assert all("heartbeat_id" not in _json.loads(a["detail"])
               for a in out2.anomalies)


def json_kind(a):
    import json
    return json.loads(a["detail"]).get("kind") or a["kind"]


# ════════════════════════════════════════════════════════════════
# idempotency — the database constraint is the only arbiter
# ════════════════════════════════════════════════════════════════

class FakeStore:
    """In-memory store that enforces the real schema's UNIQUE constraints."""

    def __init__(self):
        self.outbox = {}
        self.shift_reports = {}
        self.events = {}
        self.anomalies = []
        self.calls = []

    def _key(self, table, row):
        if table == "events":
            return (row["tenant_id"], row["source_id"], row["type"], row["dedup_key"])
        if table == "outbox":
            return (row["tenant_id"], row["channel"], row["recipient"], row["dedup_key"])
        if table == "shift_reports":
            return (row["tenant_id"], row["source_id"], row["shift_date"], row["shift_name"])
        return None


class FakeClient:
    """Serves canned reads; writes land in a constraint-honoring store."""

    def __init__(self, tables=None, counts=None):
        self.reads = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.counts = dict(counts or {})
        self.store = FakeStore()

    def select(self, table, params=None, paginate=True):
        self.store.calls.append(("select", table))
        params = params or {}
        rows = [dict(r) for r in self.reads.get(table, [])]
        order = params.get("order")
        if order == "at.desc":
            rows = sorted(rows, key=lambda r: str(r.get("at", "")), reverse=True)
        elif order == "at.asc":
            rows = sorted(rows, key=lambda r: str(r.get("at", "")))
        if params.get("limit") is not None:
            rows = rows[: int(params["limit"])]
        return rows

    def count(self, table, params=None):
        self.store.calls.append(("count", table))
        return self.counts.get(table, 0)

    def insert_ignore(self, table, rows, on_conflict):
        self.store.calls.append(("insert_ignore", table, on_conflict))
        store = getattr(self.store, table)
        for row in rows:
            key = self.store._key(table, row)
            if key is not None and key in store:
                continue
            store[key] = dict(row)

    def insert(self, table, rows, returning=True):
        self.store.calls.append(("insert", table))
        if table == "internal_anomalies":
            self.store.anomalies.extend(dict(r) for r in rows)
            return []
        return [dict(r) for r in rows] if returning else []

    def update(self, table, filters, patch, returning=True):
        self.store.calls.append(("update", table))
        if table != "events":
            return []
        updated = []
        want_type = (filters.get("type") or "").removeprefix("eq.")
        want_dkey = (filters.get("dedup_key") or "").removeprefix("eq.")
        for row in self.store.events.values():
            if row.get("type") == want_type and row.get("dedup_key") == want_dkey \
               and row.get("status") == "detected":
                row.update(patch)
                updated.append(row)
        return updated if returning else []


def _canned_morning():
    now = utc(2026, 7, 1, 4, 10)                    # 07:10 Cairo, Jul 1
    return {
        "tenants": [{"timezone": "Africa/Cairo", "shift_morning_start": "07:00:00",
                     "shift_evening_start": "19:00:00", "go_live_at": None}],
        "recipients": [{"channel": "telegram", "address": "111", "label": "dev",
                        "active": True, "notify_before_golive": True}],
        "alert_settings": [{"alert_type": "refund", "detect": True, "notify": False}],
        "sync_state": [{"rescan_from_salid": 0, "last_rescan_at": None,
                        "restore_suspected": False, "schema_ok": True}],
        "heartbeats": [
            # id 1 is the FIRST sync: 14h before now = 17:10 local on Jun 30,
            # outside the evening window, so the shift is NOT partial.
            {"id": 1, "at": (now - _dt.timedelta(hours=14)).isoformat(), "ok": True, "note": None},
            {"id": 2, "at": (now - _dt.timedelta(minutes=1)).isoformat(), "ok": True, "note": None},
        ],
        "shift_reports": [],
        "invoices": [
            {"salid": 1, "receipt_num": 1, "sold_at": "2026-06-30T20:00:00",
             "total": 100.0, "delivery_cost": 0.0, "user_uid": 1, "kind": "cash",
             "first_seen_at": (now - _dt.timedelta(hours=2)).isoformat(),
             "last_seen_at": (now - _dt.timedelta(minutes=1)).isoformat(),
             "deleted_at": None},
        ],
        "invoice_lines": [{"saledeid": 1, "salid": 1, "itid": 1, "item_name": "طعمية",
                           "list_price": 50.0, "qty": 2.0, "line_total": 100.0,
                           "sold_at": "2026-06-30T20:00:00"}],
        "cash_counts": [],
        "pos_users": [{"uid": 1, "name": "أحمد"}],
        "pos_products": [{"itid": 1, "is_modifier": False}],
        "internal_anomalies": [],
    }


def test_run_applies_a_full_plan_to_the_fake_store():
    client = FakeClient(tables=_canned_morning(), counts={"outbox": 0})
    orchestrator.run(client, tenant_id=TID, source_id=SID,
                     now_utc=utc(2026, 7, 1, 4, 10))
    assert len(client.store.shift_reports) == 1
    shift = list(client.store.shift_reports.values())[0]
    assert shift["shift_name"] == "evening"
    assert shift["grand_total"] == 100.0
    reports = [r for r in client.store.outbox.values() if r["kind"] == "shift_report"]
    assert len(reports) == 1
    assert reports[0]["recipient"] == "111"
    assert reports[0]["dedup_key"] == "shift_report:2026-06-30:evening"
    assert reports[0]["status"] == "pending"
    assert reports[0]["attempts"] == 0


def test_double_run_over_identical_state_leaves_exactly_one_row():
    client = FakeClient(tables=_canned_morning(), counts={"outbox": 0})
    orchestrator.run(client, tenant_id=TID, source_id=SID,
                     now_utc=utc(2026, 7, 1, 4, 10))
    orchestrator.run(client, tenant_id=TID, source_id=SID,
                     now_utc=utc(2026, 7, 1, 4, 10))     # identical state
    assert len(client.store.shift_reports) == 1
    reports = [r for r in client.store.outbox.values() if r["kind"] == "shift_report"]
    assert len(reports) == 1
    # structural proof: the constrained tables are written conflict-ignoring,
    # never read-then-write. (internal_anomalies has no unique constraint and
    # legitimately uses plain insert, so it is out of scope here.)
    constrained = [c for c in client.store.calls
                   if c[0] in ("insert_ignore", "insert")
                   and c[1] in ("outbox", "shift_reports", "events")]
    assert constrained
    for call in constrained:
        assert call[0] == "insert_ignore", f"{call} — plain insert must not be used"


# ════════════════════════════════════════════════════════════════
# the no-pyodbc closure — with a falsifier that can actually fail
# ════════════════════════════════════════════════════════════════

def _module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            fn = node.func
            if (isinstance(fn, ast.Attribute) and fn.attr == "import_module") or \
               (isinstance(fn, ast.Name) and fn.id == "import_module"):
                if node.args and isinstance(node.args[0], ast.Constant):
                    mods.add(str(node.args[0].value).split(".")[0])
    return mods


def _transitive_imports(entry: Path) -> set[str]:
    seen: set[str] = set()
    stack = [entry]
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in _module_names(path):
            repo = REPO / f"{name}.py"
            if name in {"events", "metrics", "report", "supa", "rows",
                        "orchestrator"} and repo.exists():
                stack.append(repo)
    mods: set[str] = set()
    for path in seen:
        mods |= _module_names(path)
    return mods


def test_closure_walker_can_detect_pyodbc(tmp_path):
    """Falsifier: prove the walker fails before trusting its clean answer."""
    bad = tmp_path / "bad_mod.py"
    bad.write_text("import pyodbc\n", encoding="utf-8")
    assert "pyodbc" in _transitive_imports(bad)


def test_no_pyodbc_in_delivery_import_closure():
    forbidden = {"pyodbc", "adapter_hdsoft", "agent", "fake_adapter", "sqlguard"}
    mods = _transitive_imports(REPO / "orchestrator.py")
    assert not (forbidden & mods), f"delivery closure imports {forbidden & mods}"
