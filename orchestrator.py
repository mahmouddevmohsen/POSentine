# -*- coding: utf-8 -*-
"""
orchestrator.py — Phase 2: decides what to build and when, then enqueues it
==========================================================================
Cloud-side only. This module never touches the POS: no pyodbc, no
adapter_hdsoft, and no SQL Server connection anywhere in its import
closure (enforced mechanically by test_orchestrator.py). It reads Supabase
over HTTPS and writes only to the delivery tables.

Design
------
    plan()   a pure function of (now_utc, TenantContext, DBState) -> Plan.
             No clock, no network, no randomness: the same inputs give the
             same Plan every time.
    run()    the thin runner. Loads state over HTTPS via supa.Supa, calls
             plan(), and applies the Plan through conflict-ignoring writes.

Idempotency is the database's job, never read-then-write:
    shift_reports PK (tenant_id, source_id, shift_date, shift_name)
    outbox        UNIQUE (tenant_id, channel, recipient, dedup_key)
    events        UNIQUE (tenant_id, source_id, type, dedup_key)
Every write uses `on_conflict ... do nothing` (insert_ignore) and lets the
constraint decide. Two overlapping runs both attempt the same INSERT; one
row lands.

Clock rules (from rows.py — the two clocks must never mix):
    events.occurred_at, invoices.sold_at   naive POS-local wall time
    go_live_at, created_at, sent_at        aware timestamptz
Before an event is compared with go_live_at, its naive timestamp is
localized with the tenant's IANA timezone (never a hardcoded UTC+3 —
Egypt observes DST: UTC+3 in summer, UTC+2 in winter).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import zoneinfo
from dataclasses import dataclass, field
from typing import Any, Sequence

import events as E
import metrics as M
import report as R
import supa
from rows import pos_ts

# ── dedup keys (decisions 2 and 4 from the Task 2 approval) ─────
SHIFT_DEDUP = "shift_report:{shift_date}:{shift_name}"
MONTHLY_DEDUP = "monthly:{year}-{month:02d}"
ALERT_DEDUP = "alert:{type}:{dedup_key}"

# ── limits ──────────────────────────────────────────────────────
MAX_SHIFT_LOOKBACK_DAYS = 14      # how far back to look for a missing report
OBSERVATION_HOURS = 24            # events are detected over the last 24h only
DAILY_ALERT_CAP = 3               # matches events.DAILY_ALERT_CAP
MAX_REPORT_NOTES = 5              # matches report.build_shift_report max_notes

# Deletion detection is trustworthy only while the agent is demonstrably
# alive: a rescan refreshes last_seen_at for every invoice in its window,
# so a stale last_seen_at means "absent", not "agent down".
HEARTBEAT_FRESH_MINUTES = 15      # 5 missed 3-minute cycles
DELETION_RESCAN_MAX_AGE_MINUTES = 60   # last completed rescan
DELETION_SEEN_MAX_AGE_MINUTES = 45     # invoice last refreshed by a rescan
HEARTBEAT_DEAD_MINUTES = 60       # an internal dead_man anomaly fires here


# ════════════════════════════════════════════════════════════════
# pure data
# ════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class Recipient:
    address: str
    label: str | None
    active: bool
    notify_before_golive: bool = False
    channel: str = "telegram"


@dataclass(slots=True)
class TenantContext:
    tenant_id: str
    source_id: str
    timezone: str
    shift_morning_start: _dt.time
    shift_evening_start: _dt.time
    go_live_at: _dt.datetime | None        # aware, or None (not live yet)
    recipients: list[Recipient]
    alert_settings: dict[str, dict]        # {type: {"detect","notify"}}


@dataclass(slots=True)
class Inv:
    salid: int
    receipt_num: int | None
    sold_at: _dt.datetime                  # naive, POS local
    total: float
    delivery_cost: float
    user_uid: int | None
    kind: str
    first_seen_at: _dt.datetime | None = None   # aware
    last_seen_at: _dt.datetime | None = None    # aware
    deleted_at: _dt.datetime | None = None      # aware


@dataclass(slots=True)
class Line:
    saledeid: int
    salid: int
    itid: int | None
    item_name: str | None
    list_price: float | None
    qty: float
    line_total: float
    sold_at: _dt.datetime | None           # naive, POS local


@dataclass(slots=True)
class Cash:
    srid: int
    counted_at: _dt.datetime               # naive, POS local
    user_uid: int | None
    user_value: float
    app_value: float
    kind: str


@dataclass(slots=True)
class DBState:
    existing_reports: set[tuple[_dt.date, str]]   # (shift_date, shift_name)
    sent_alerts_today: int
    invoices: list[Inv]                            # last 8 days, deleted included
    lines: list[Line]                              # matching lines
    cash_counts: list[Cash]                        # last 8 days
    users: dict[int, str]
    modifiers: dict[int, bool]
    first_sync_at: _dt.datetime | None             # earliest heartbeat, aware
    heartbeat_at: _dt.datetime | None              # latest heartbeat, aware
    last_rescan_at: _dt.datetime | None            # aware
    rescan_from_salid: int
    restore_suspected: bool = False
    schema_ok: bool = True
    month_invoices: list[Inv] = field(default_factory=list)   # prev month, day 1 only
    month_lines: list[Line] = field(default_factory=list)
    mirrored_heartbeat_ids: set[int] = field(default_factory=set)
    failure_heartbeats: list[dict] = field(default_factory=list)  # {"id","note"}
    open_anomaly_kinds: set[str] = field(default_factory=set)


@dataclass(slots=True)
class Envelope:
    kind: str                       # shift_report | monthly_products | alert
    channel: str
    recipient: str                  # the recipients.address (chat id)
    body: str
    dedup_key: str
    event_id: int | None = None


@dataclass(slots=True)
class Plan:
    shift_row: dict | None = None
    envelopes: list[Envelope] = field(default_factory=list)
    event_rows: list[dict] = field(default_factory=list)
    queue_keys: list[tuple[str, str]] = field(default_factory=list)
    anomalies: list[dict] = field(default_factory=list)
    resolve_dead_man: bool = False


# ════════════════════════════════════════════════════════════════
# pure: shift selection
# ════════════════════════════════════════════════════════════════

def _closes_at(d: _dt.date, name: str, morning_start: _dt.time,
               evening_start: _dt.time) -> _dt.datetime:
    """Local wall-clock instant a shift ends."""
    if name == M.MORNING:
        return _dt.datetime.combine(d, evening_start)
    return _dt.datetime.combine(d + _dt.timedelta(days=1), morning_start)


def _closed_shifts(local_now: _dt.datetime, ctx: TenantContext,
                   days_back: int) -> list[tuple[_dt.date, str, _dt.datetime]]:
    """All shift candidates up to days_back, ordered most-recently-closed first."""
    today = local_now.date()
    cands: list[tuple[_dt.date, str, _dt.datetime]] = []
    for back in range(days_back, -1, -1):
        d = today - _dt.timedelta(days=back)
        cands.append((d, M.MORNING, _closes_at(d, M.MORNING,
                                               ctx.shift_morning_start,
                                               ctx.shift_evening_start)))
        cands.append((d, M.EVENING, _closes_at(d, M.EVENING,
                                               ctx.shift_morning_start,
                                               ctx.shift_evening_start)))
    cands.sort(key=lambda c: c[2], reverse=True)
    return [c for c in cands if c[2] <= local_now]


def select_shift(local_now: _dt.datetime, ctx: TenantContext,
                 existing: set[tuple[_dt.date, str]],
                 force_shift: str | None = None,
                 shift_date: _dt.date | None = None
                 ) -> tuple[_dt.date, str] | None:
    """
    Deterministic shift selection.

    Default rule: the most recent CLOSED shift that has no report row yet.
    Never "is it exactly 07:00 now" — a run 20 minutes late (or a day late)
    must still produce the right report.

    force_shift / shift_date override the rule for manual runs:
      shift_date + force_shift  -> exactly that shift (explicit test action,
                                   even if its window has not closed yet)
      shift_date only           -> most recently closed shift of that date
      force_shift only          -> most recent closed occurrence of that
                                   shift (today, else yesterday)
    """
    if shift_date is not None:
        if force_shift is not None:
            return (shift_date, force_shift) if (shift_date, force_shift) not in existing else None
        for d, name, _ in _closed_shifts(local_now, ctx, 1):
            if d == shift_date and (d, name) not in existing:
                return (d, name)
        # the requested date may be a past date with no closed candidate in
        # the last two days — fall back to scanning from that date
        start, end = ctx.shift_morning_start, ctx.shift_evening_start
        for name in (M.EVENING, M.MORNING):
            closes = _closes_at(shift_date, name, start, end)
            if closes <= local_now and (shift_date, name) not in existing:
                return (shift_date, name)
        return None
    if force_shift is not None:
        today = local_now.date()
        for d in (today, today - _dt.timedelta(days=1)):
            if (d, force_shift) not in existing and \
               _closes_at(d, force_shift, ctx.shift_morning_start,
                          ctx.shift_evening_start) <= local_now:
                return (d, force_shift)
        return None
    for d, name, _ in _closed_shifts(local_now, ctx, MAX_SHIFT_LOOKBACK_DAYS):
        if (d, name) not in existing:
            return (d, name)
    return None


# ════════════════════════════════════════════════════════════════
# pure: recipient gate
# ════════════════════════════════════════════════════════════════

def eligible_recipients(ctx: TenantContext) -> list[Recipient]:
    """
    Gate order, per the Task 2 decisions and the Phase 2 prompt:
      1. active (never bypassed)
      2. go_live_at — notify_before_golive bypasses THIS gate and only this
         one. It is a developer testing bypass; it can never open the door
         for the owner (whose flag stays false).
    alert_settings.notify is applied to events separately by filter_sendable.
    """
    out: list[Recipient] = []
    for r in ctx.recipients:
        if not r.active or r.channel != "telegram":
            continue
        if r.notify_before_golive or ctx.go_live_at is not None:
            out.append(r)
    return out


# ════════════════════════════════════════════════════════════════
# pure: event pipeline
# ════════════════════════════════════════════════════════════════

def _lines_for(lines: Sequence[Line], salids: set[int]) -> dict[int, list[Line]]:
    out: dict[int, list[Line]] = {}
    for ln in lines:
        if ln.salid in salids:
            out.setdefault(ln.salid, []).append(ln)
    return out


def _shift_sales_lookup(obs_invoices: Sequence[Inv]):
    """For detect_cash_diffs: sales total of the shift a count fell in."""
    per_shift: dict[tuple[_dt.date, str], float] = {}
    for inv in obs_invoices:
        if inv.kind != "cash":
            continue
        key = M.resolve_shift(inv.sold_at)
        per_shift[key] = per_shift.get(key, 0.0) + float(inv.total or 0)

    def lookup(counted_at: _dt.datetime) -> float | None:
        return per_shift.get(M.resolve_shift(counted_at))

    return lookup


def _detect_events(now_utc: _dt.datetime, ctx: TenantContext, state: DBState,
                   tz: zoneinfo.ZoneInfo,
                   pos_now: _dt.datetime) -> tuple[list[E.Event],
                                                   list[E.Event],
                                                   list[E.Event],
                                                   list[E.Event]]:
    """(all detected, sendable-and-under-cap, capped overflow, cash events)."""
    obs_from = pos_now - _dt.timedelta(hours=OBSERVATION_HOURS)
    obs_invs = [i for i in state.invoices
                if i.deleted_at is None and i.sold_at >= obs_from]
    obs_ids = {i.salid for i in obs_invs}
    obs_lines = _lines_for(state.lines, obs_ids)
    obs_cash = [c for c in state.cash_counts if c.counted_at >= obs_from]

    detected: list[E.Event] = []
    detected += E.detect_zero_invoices(obs_invs, obs_lines)
    detected += E.detect_refunds(obs_invs)
    cash_events = E.detect_cash_diffs(obs_cash, shift_sales_lookup=_shift_sales_lookup(obs_invs))
    detected += cash_events

    # Deletion is absence. Absence is only evidence while the agent is
    # demonstrably alive AND a rescan has recently refreshed last_seen_at.
    deletion_enabled = (
        state.heartbeat_at is not None
        and state.last_rescan_at is not None
        and not state.restore_suspected
        and state.schema_ok
        and (now_utc - state.heartbeat_at) <= _dt.timedelta(minutes=HEARTBEAT_FRESH_MINUTES)
        and (now_utc - state.last_rescan_at) <= _dt.timedelta(minutes=DELETION_RESCAN_MAX_AGE_MINUTES)
    )
    if deletion_enabled:
        fresh = {i.salid for i in state.invoices
                 if i.last_seen_at is not None
                 and (now_utc - i.last_seen_at) <= _dt.timedelta(minutes=DELETION_SEEN_MAX_AGE_MINUTES)}
        window_known = [i for i in state.invoices
                        if i.deleted_at is None and i.salid >= state.rescan_from_salid]
        detected += E.detect_deleted(window_known, fresh, state.rescan_from_salid, now_utc)

    latest = max((i.sold_at for i in obs_invs), default=None)
    if latest is not None:
        detected += E.detect_no_sales(latest, pos_now)

    # Decision 5: compare with go_live_at only after localizing the naive
    # POS-local timestamp with the tenant's IANA zone. filter_sendable is a
    # locked file, so the localization happens on a copy.
    localized = [E.Event(e.type, e.dedup_key, e.level,
                         e.occurred_at.replace(tzinfo=tz), e.payload, e.internal)
                 for e in detected]
    sendable = E.filter_sendable(localized, ctx.alert_settings, ctx.go_live_at)
    by_key = {(e.type, e.dedup_key): e for e in detected}
    sendable_orig = [by_key[(e.type, e.dedup_key)] for e in sendable]
    to_send, overflow = E.apply_daily_cap(sendable_orig,
                                          state.sent_alerts_today,
                                          cap=DAILY_ALERT_CAP)
    return detected, to_send, overflow, cash_events


def _event_row(ev: E.Event, ctx: TenantContext, status: str) -> dict:
    return {
        "tenant_id": ctx.tenant_id,
        "source_id": ctx.source_id,
        "type": ev.type,
        "dedup_key": ev.dedup_key,
        "level": ev.level,
        "occurred_at": pos_ts(ev.occurred_at),
        "payload": json.dumps(ev.payload, ensure_ascii=False, default=str),
        "status": status,
    }


# ════════════════════════════════════════════════════════════════
# pure: shift report
# ════════════════════════════════════════════════════════════════

def _build_shift_report(out: Plan, ctx: TenantContext, state: DBState,
                        target: tuple[_dt.date, str], detected: Sequence[E.Event],
                        overflow: Sequence[E.Event], cash_events: Sequence[E.Event],
                        tz: zoneinfo.ZoneInfo) -> None:
    shift_date, shift_name = target
    start, end = M.shift_window(shift_date, shift_name)
    win_invs = [i for i in state.invoices
                if i.deleted_at is None and start <= i.sold_at < end]
    win_ids = {i.salid for i in win_invs}
    m = M.compute_shift(
        shift_date, shift_name, win_invs,
        lines_by_salid=_lines_for(state.lines, win_ids),
        user_names=state.users,
        product_is_modifier=state.modifiers,
        top_n=5)

    pw_date = M.previous_week_shift(shift_date)
    pw_start, pw_end = M.shift_window(pw_date, shift_name)
    pw_invs = [i for i in state.invoices
               if i.deleted_at is None and pw_start <= i.sold_at < pw_end]
    prev_total: float | None = None
    prev_count: int | None = None
    if pw_invs:
        pw = M.compute_shift(pw_date, shift_name, pw_invs)
        prev_total, prev_count = pw.grand_total, pw.total_invoices
    comparison = M.compare_to_last_week(m.grand_total, prev_total, prev_count)

    # is_partial: the shift that was in progress when the agent first synced.
    # Recorded and never reported (schema.sql comment).
    is_partial = (state.first_sync_at is not None
                  and start <= state.first_sync_at.astimezone(tz).replace(tzinfo=None) < end)

    cash_diffs = sorted((e for e in cash_events if e.type == "cash_diff"
                         and start <= e.occurred_at < end),
                        key=lambda e: (e.level, e.occurred_at))
    had_no_count = any(e.type == "cash_no_count"
                       and start <= e.occurred_at < end for e in cash_events)
    cash_event = None if had_no_count else (cash_diffs[0] if cash_diffs else None)

    # Report notes: level 2/3 events in this shift (refunds, small diffs)
    # plus whatever the daily cap pushed out of the alert channel.
    in_shift = [e for e in detected
                if start <= e.occurred_at < end and e.level in (2, 3)]
    in_shift += [e for e in overflow if start <= e.occurred_at < end]
    notes = [R.note_line(e) for e in in_shift][:MAX_REPORT_NOTES]

    body = R.build_shift_report(m, comparison, notes=notes,
                                cash_event=cash_event, had_no_count=had_no_count,
                                max_notes=MAX_REPORT_NOTES)
    E.assert_no_accusation(body)

    out.shift_row = {
        "tenant_id": ctx.tenant_id,
        "source_id": ctx.source_id,
        "shift_date": shift_date.isoformat(),
        "shift_name": shift_name,
        "window_start": pos_ts(start),
        "window_end": pos_ts(end),
        "primary_user": m.primary_user.name if m.primary_user else None,
        "sales": m.sales,
        "returns": m.returns,
        "delivery": m.delivery,
        "collections": m.collections,
        "grand_total": m.grand_total,
        "n_cash": m.n_cash,
        "n_return": m.n_return,
        "n_external": m.n_external,
        "is_partial": is_partial,
    }
    if not is_partial:
        dedup = SHIFT_DEDUP.format(shift_date=shift_date.isoformat(),
                                   shift_name=shift_name)
        for rcpt in eligible_recipients(ctx):
            out.envelopes.append(Envelope("shift_report", rcpt.channel,
                                          rcpt.address, body, dedup))


# ════════════════════════════════════════════════════════════════
# pure: monthly top products (day 1 only)
# ════════════════════════════════════════════════════════════════

def _build_monthly(out: Plan, ctx: TenantContext, state: DBState,
                   local_now: _dt.datetime) -> None:
    if local_now.day != 1:
        return
    prev = (local_now.replace(day=1) - _dt.timedelta(days=1)).date()
    mstart = _dt.datetime(prev.year, prev.month, 1)
    nxt = (_dt.datetime(prev.year + 1, 1, 1) if prev.month == 12
           else _dt.datetime(prev.year, prev.month + 1, 1))
    deleted = {i.salid for i in state.month_invoices if i.deleted_at is not None}
    month_lines = [ln for ln in state.month_lines
                   if ln.sold_at is not None and mstart <= ln.sold_at < nxt
                   and ln.salid not in deleted]
    items = M.top_items(month_lines, product_is_modifier=state.modifiers, top_n=5)
    body = R.build_monthly_products(prev, items)
    E.assert_no_accusation(body)
    dedup = MONTHLY_DEDUP.format(year=prev.year, month=prev.month)
    for rcpt in eligible_recipients(ctx):
        out.envelopes.append(Envelope("monthly_products", rcpt.channel,
                                      rcpt.address, body, dedup))


# ════════════════════════════════════════════════════════════════
# pure: internal anomalies (ours, never the owner's)
# ════════════════════════════════════════════════════════════════

def _parse_note(note: str | None) -> dict | None:
    if not note:
        return None
    try:
        raw = json.loads(note)
        if isinstance(raw, dict) and raw.get("kind"):
            return raw
        return None
    except (ValueError, AttributeError):
        return None


def _build_anomalies(out: Plan, ctx: TenantContext, state: DBState,
                     now_utc: _dt.datetime) -> None:
    # dead_man — the Phase 1 handoff's mandated "alert on heartbeat silence".
    if state.heartbeat_at is None or \
       (now_utc - state.heartbeat_at) > _dt.timedelta(minutes=HEARTBEAT_DEAD_MINUTES):
        if "dead_man" not in state.open_anomaly_kinds:
            out.anomalies.append({
                "tenant_id": ctx.tenant_id,
                "source_id": ctx.source_id,
                "kind": "dead_man",
                "detail": json.dumps({
                    "last_heartbeat_at": (state.heartbeat_at.isoformat()
                                          if state.heartbeat_at else None),
                    "minutes_since": (int((now_utc - state.heartbeat_at).total_seconds() // 60)
                                      if state.heartbeat_at else None),
                }, ensure_ascii=False, default=str),
            })
    elif "dead_man" in state.open_anomaly_kinds:
        # the agent is alive again — close the open alert so a later
        # outage can raise a fresh one (resolved_at is our "re-armed" flag)
        out.resolve_dead_man = True

    # Mirror every unmirrored ok=false heartbeat note (the agent's only
    # anomaly channel; keyed on heartbeat id so re-runs cannot duplicate).
    for hb in state.failure_heartbeats:
        hb_id = hb.get("id")
        if hb_id is None or hb_id in state.mirrored_heartbeat_ids:
            continue
        if not hb.get("note"):
            continue                        # nothing to mirror
        parsed = _parse_note(hb.get("note"))
        kind = parsed["kind"] if parsed else "unparseable_note"
        detail = dict(parsed.get("detail", {})) if parsed else {"note": hb.get("note")}
        detail["heartbeat_id"] = hb_id
        out.anomalies.append({
            "tenant_id": ctx.tenant_id,
            "source_id": ctx.source_id,
            "kind": kind,
            "detail": json.dumps(detail, ensure_ascii=False, default=str),
        })


# ════════════════════════════════════════════════════════════════
# pure: the whole decision
# ════════════════════════════════════════════════════════════════

def plan(now_utc: _dt.datetime, ctx: TenantContext, state: DBState,
         force_shift: str | None = None,
         shift_date: _dt.date | None = None) -> Plan:
    """
    Pure: (now, tenant config, database state) -> what to enqueue.
    Nothing here reads the clock, the network, or the disk.
    """
    tz = zoneinfo.ZoneInfo(ctx.timezone)
    local_now = now_utc.astimezone(tz)
    pos_now = local_now.replace(tzinfo=None)
    out = Plan()

    target = select_shift(local_now.replace(tzinfo=None), ctx,
                          state.existing_reports,
                          force_shift=force_shift, shift_date=shift_date)

    detected, to_send, overflow, cash_events = _detect_events(
        now_utc, ctx, state, tz, pos_now)

    # Event rows: only actually-sent alerts stay 'detected' (then 'queued'
    # after the enqueue lands); everything else is 'suppressed' — notify
    # off, pre-go-live, daily-capped, or a level 2/3 note for the report.
    sent_keys = {(e.type, e.dedup_key) for e in to_send}
    out.event_rows = [
        _event_row(e, ctx, "detected" if (e.type, e.dedup_key) in sent_keys
                   else "suppressed")
        for e in detected
    ]

    for e in to_send:
        body = R.build_alert(e, _user_name(state, e))
        E.assert_no_accusation(body)
        dedup = ALERT_DEDUP.format(type=e.type, dedup_key=e.dedup_key)
        for rcpt in eligible_recipients(ctx):
            out.envelopes.append(Envelope("alert", rcpt.channel, rcpt.address,
                                          body, dedup))
        out.queue_keys.append((e.type, e.dedup_key))

    if target is not None:
        _build_shift_report(out, ctx, state, target, detected, overflow,
                            cash_events, tz)

    _build_monthly(out, ctx, state, local_now)

    _build_anomalies(out, ctx, state, now_utc)

    return out


def _user_name(state: DBState, ev: E.Event) -> str:
    uid = (ev.payload or {}).get("user_uid")
    if uid is not None and uid in state.users:
        return state.users[uid]
    return "غير محدد"


# ════════════════════════════════════════════════════════════════
# runner: fetch, decide, apply
# ════════════════════════════════════════════════════════════════

def _parse_dt(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _load_context(client: supa.Supa, tenant_id: str,
                  source_id: str) -> TenantContext:
    t_rows = client.select("tenants", {"id": f"eq.{tenant_id}",
                                       "select": "timezone,shift_morning_start,shift_evening_start,go_live_at"},
                           paginate=False)
    if not t_rows:
        raise supa.SupaError(f"no tenants row for id {tenant_id}")
    t = t_rows[0]

    recipients = [
        Recipient(address=r["address"], label=r.get("label"),
                  active=bool(r.get("active", True)),
                  notify_before_golive=bool(r.get("notify_before_golive", False)),
                  channel=r.get("channel") or "telegram")
        for r in client.select("recipients",
                               {"tenant_id": f"eq.{tenant_id}",
                                "select": "channel,address,label,active,notify_before_golive"})
    ]

    settings: dict[str, dict] = {}
    for row in client.select("alert_settings",
                             {"tenant_id": f"eq.{tenant_id}",
                              "select": "alert_type,detect,notify"}):
        settings[row["alert_type"]] = {"detect": bool(row.get("detect", True)),
                                       "notify": bool(row.get("notify", False))}

    return TenantContext(
        tenant_id=tenant_id, source_id=source_id,
        timezone=t.get("timezone") or "Africa/Cairo",
        shift_morning_start=_dt.time.fromisoformat((t.get("shift_morning_start") or "07:00")[:8]),
        shift_evening_start=_dt.time.fromisoformat((t.get("shift_evening_start") or "19:00")[:8]),
        go_live_at=_parse_dt(t.get("go_live_at")),
        recipients=recipients,
        alert_settings=settings,
    )


def _load_state(client: supa.Supa, ctx: TenantContext,
                now_utc: _dt.datetime) -> DBState:
    tid, sid = ctx.tenant_id, ctx.source_id
    scope = {"tenant_id": f"eq.{tid}", "source_id": f"eq.{sid}"}
    tz = zoneinfo.ZoneInfo(ctx.timezone)
    local_now = now_utc.astimezone(tz)

    sync = client.select("sync_state", {**scope,
                                        "select": "rescan_from_salid,last_rescan_at,restore_suspected,schema_ok"},
                         paginate=False)
    sync_row = sync[0] if sync else {}
    if not sync:
        raise supa.SupaError(
            "no sync_state row for this tenant/source — a provisioning "
            "problem, not an empty history")

    beats_latest = client.select("heartbeats", {**scope, "select": "at",
                                                "order": "at.desc", "limit": "1"},
                                 paginate=False)
    beats_first = client.select("heartbeats", {**scope, "select": "at",
                                               "order": "at.asc", "limit": "1"},
                                paginate=False)

    failure_beats = client.select("heartbeats",
                                  {**scope, "select": "id,note", "ok": "eq.false",
                                   "order": "at.desc", "limit": "50"},
                                  paginate=False)

    existing = {( _dt.date.fromisoformat(r["shift_date"]), r["shift_name"])
                for r in client.select("shift_reports",
                                       {**scope, "select": "shift_date,shift_name"})}

    local_midnight = _dt.datetime.combine(local_now.date(), _dt.time.min,
                                          tzinfo=tz).astimezone(_dt.timezone.utc)
    sent_alerts = client.count("outbox", {
        "tenant_id": f"eq.{tid}",
        "kind": "eq.alert",
        "status": "eq.sent",
        "created_at": f"gte.{local_midnight.isoformat(timespec='seconds')}"})

    cutoff8 = (local_now - _dt.timedelta(days=8)).replace(tzinfo=None).isoformat(timespec="seconds")
    inv_rows = client.select("invoices", {
        **scope, "sold_at": f"gte.{cutoff8}",
        "select": ("salid,receipt_num,sold_at,total,delivery_cost,user_uid,"
                   "kind,first_seen_at,last_seen_at,deleted_at")})
    # Deletion detection operates on the salid-based rescan window, which a
    # quiet shop can push older than the 8-day sold_at cutoff. Merge in the
    # window by salid — only once a rescan has completed, and only then, or
    # a pre-rescan run would drag the entire history.
    invoices: dict[int, Inv] = {_row_inv(r).salid: _row_inv(r) for r in inv_rows}
    if sync_row.get("last_rescan_at"):
        for r in client.select("invoices", {
                **scope, "salid": f"gte.{int(sync_row.get('rescan_from_salid') or 0)}",
                "select": ("salid,receipt_num,sold_at,total,delivery_cost,user_uid,"
                           "kind,first_seen_at,last_seen_at,deleted_at")}):
            inv = _row_inv(r)
            invoices[inv.salid] = inv
    invoices_list = list(invoices.values())

    line_rows = client.select("invoice_lines", {
        **scope, "or": f"(sold_at.gte.{cutoff8},sold_at.is.null)",
        "select": ("saledeid,salid,itid,item_name,list_price,qty,line_total,sold_at")})
    lines = [_row_line(r) for r in line_rows]

    cash_rows = client.select("cash_counts", {
        **scope, "counted_at": f"gte.{cutoff8}",
        "select": "srid,counted_at,user_uid,user_value,app_value,kind"})
    cash = [_row_cash(r) for r in cash_rows]

    users = {int(r["uid"]): r.get("name") for r in client.select(
        "pos_users", {**scope, "select": "uid,name"}) if r.get("uid") is not None}
    modifiers = {int(r["itid"]): bool(r.get("is_modifier", False))
                 for r in client.select("pos_products",
                                        {**scope, "select": "itid,is_modifier"})
                 if r.get("itid") is not None}

    # day 1 of the local month: the previous month's data for the monthly report
    month_invoices: list[Inv] = []
    month_lines: list[Line] = []
    if local_now.day == 1:
        prev = (local_now.replace(day=1) - _dt.timedelta(days=1)).date()
        mstart = _dt.datetime(prev.year, prev.month, 1)
        nxt = (_dt.datetime(prev.year + 1, 1, 1) if prev.month == 12
               else _dt.datetime(prev.year, prev.month + 1, 1))
        band = (f"and=(sold_at.gte.{mstart.isoformat(timespec='seconds')},"
                f"sold_at.lt.{nxt.isoformat(timespec='seconds')})")
        month_invoices = [_row_inv(r) for r in client.select("invoices", {
            **scope, "and": band,
            "select": ("salid,receipt_num,sold_at,total,delivery_cost,user_uid,"
                       "kind,first_seen_at,last_seen_at,deleted_at")})]
        month_lines = [_row_line(r) for r in client.select("invoice_lines", {
            **scope, "and": band,
            "select": ("saledeid,salid,itid,item_name,list_price,qty,line_total,sold_at")})]

    # internal_anomalies: which dead-man is already open, and which
    # heartbeat ids have already been mirrored.
    anomaly_rows = client.select("internal_anomalies",
                                 {**scope, "select": "kind,detail,resolved_at"})
    open_kinds = {r["kind"] for r in anomaly_rows if r.get("resolved_at") is None}
    mirrored: set[int] = set()
    for r in anomaly_rows:
        try:
            d = json.loads(r.get("detail") or "{}")
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("heartbeat_id") is not None:
            mirrored.add(int(d["heartbeat_id"]))

    return DBState(
        existing_reports=existing,
        sent_alerts_today=sent_alerts,
        invoices=invoices_list,
        lines=lines,
        cash_counts=cash,
        users=users,
        modifiers=modifiers,
        first_sync_at=_parse_dt(beats_first[0]["at"]) if beats_first else None,
        heartbeat_at=_parse_dt(beats_latest[0]["at"]) if beats_latest else None,
        last_rescan_at=_parse_dt(sync_row.get("last_rescan_at")),
        rescan_from_salid=int(sync_row.get("rescan_from_salid") or 0),
        restore_suspected=bool(sync_row.get("restore_suspected", False)),
        schema_ok=bool(sync_row.get("schema_ok", True)),
        month_invoices=month_invoices,
        month_lines=month_lines,
        mirrored_heartbeat_ids=mirrored,
        failure_heartbeats=[{"id": b.get("id"), "note": b.get("note")}
                            for b in failure_beats],
        open_anomaly_kinds=open_kinds,
    )


def _row_inv(r: dict) -> Inv:
    return Inv(salid=int(r["salid"]),
               receipt_num=(int(r["receipt_num"]) if r.get("receipt_num") is not None else None),
               sold_at=_dt.datetime.fromisoformat(r["sold_at"]),
               total=float(r.get("total") or 0),
               delivery_cost=float(r.get("delivery_cost") or 0),
               user_uid=(int(r["user_uid"]) if r.get("user_uid") is not None else None),
               kind=r.get("kind") or "other",
               first_seen_at=_parse_dt(r.get("first_seen_at")),
               last_seen_at=_parse_dt(r.get("last_seen_at")),
               deleted_at=_parse_dt(r.get("deleted_at")))


def _row_line(r: dict) -> Line:
    return Line(saledeid=int(r["saledeid"]),
                salid=int(r["salid"]),
                itid=(int(r["itid"]) if r.get("itid") is not None else None),
                item_name=r.get("item_name"),
                list_price=(float(r["list_price"]) if r.get("list_price") is not None else None),
                qty=float(r.get("qty") or 0),
                line_total=float(r.get("line_total") or 0),
                sold_at=(_dt.datetime.fromisoformat(r["sold_at"])
                         if r.get("sold_at") else None))


def _row_cash(r: dict) -> Cash:
    return Cash(srid=int(r["srid"]),
                counted_at=_dt.datetime.fromisoformat(r["counted_at"]),
                user_uid=(int(r["user_uid"]) if r.get("user_uid") is not None else None),
                user_value=float(r.get("user_value") or 0),
                app_value=float(r.get("app_value") or 0),
                kind=r.get("kind") or "no_count")


def _outbox_row(e: Envelope, tenant_id: str) -> dict:
    row: dict[str, Any] = {
        "tenant_id": tenant_id,
        "channel": e.channel,
        "recipient": e.recipient,
        "kind": e.kind,
        "body": e.body,
        "dedup_key": e.dedup_key,
        "status": "pending",
        "attempts": 0,
    }
    if e.event_id is not None:
        row["event_id"] = e.event_id
    return row


def apply(client: supa.Supa, ctx: TenantContext, plan: Plan,
          now_utc: _dt.datetime | None = None) -> None:
    """
    The database's UNIQUE constraints are the idempotency authority. Every
    write is conflict-ignoring; nothing here reads-then-writes. If two runs
    overlap, both attempt the same INSERT and the constraint decides.
    """
    if plan.event_rows:
        client.insert_ignore("events", plan.event_rows,
                             on_conflict="tenant_id,source_id,type,dedup_key")
    if plan.shift_row is not None:
        client.insert_ignore("shift_reports", [plan.shift_row],
                             on_conflict="tenant_id,source_id,shift_date,shift_name")
    if plan.envelopes:
        client.insert_ignore("outbox",
                             [_outbox_row(e, ctx.tenant_id) for e in plan.envelopes],
                             on_conflict="tenant_id,channel,recipient,dedup_key")
    # 'detected' -> 'queued' only after the outbox rows have been attempted.
    # If the enqueue failed, the event stays 'detected' and the next run
    # re-attempts; if the update fails, the event is 'detected' but the
    # outbox row already exists, and outbox dedup prevents a duplicate send.
    for typ, dkey in plan.queue_keys:
        client.update("events",
                      {"tenant_id": f"eq.{ctx.tenant_id}",
                       "source_id": f"eq.{ctx.source_id}",
                       "type": f"eq.{typ}",
                       "dedup_key": f"eq.{dkey}",
                       "status": "eq.detected"},
                      {"status": "queued"}, returning=False)
    if plan.anomalies:
        client.insert("internal_anomalies", plan.anomalies, returning=False)
    if plan.resolve_dead_man:
        # the agent reports again: close the open dead_man so the next
        # outage can raise a fresh one
        now = (now_utc or _dt.datetime.now(_dt.timezone.utc)).isoformat(timespec="seconds")
        client.update("internal_anomalies",
                      {"tenant_id": f"eq.{ctx.tenant_id}",
                       "source_id": f"eq.{ctx.source_id}",
                       "kind": "eq.dead_man",
                       "resolved_at": "is.null"},
                      {"resolved_at": now}, returning=False)


def run(client: supa.Supa, *, tenant_id: str, source_id: str,
        now_utc: _dt.datetime | None = None,
        force_shift: str | None = None,
        shift_date: _dt.date | None = None) -> Plan:
    """Load, decide, apply. One orchestrator pass. Raises loudly on failure."""
    now_utc = now_utc or _dt.datetime.now(_dt.timezone.utc)
    ctx = _load_context(client, tenant_id, source_id)
    state = _load_state(client, ctx, now_utc)
    decided = plan(now_utc, ctx, state, force_shift=force_shift,
                   shift_date=shift_date)
    apply(client, ctx, decided, now_utc)
    return decided


# ════════════════════════════════════════════════════════════════
# CLI — for local runs and the GitHub Actions workflow
# ════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="POSentine delivery orchestrator (cloud-side only).")
    ap.add_argument("--tenant-id", default=os.environ.get("TENANT_ID"))
    ap.add_argument("--source-id", default=os.environ.get("SOURCE_ID"))
    ap.add_argument("--force-shift", choices=(M.MORNING, M.EVENING), default=None)
    ap.add_argument("--shift-date", type=_dt.date.fromisoformat, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="build and print the full report; enqueue and send nothing")
    args = ap.parse_args(argv)

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (args.tenant_id and args.source_id):
        ap.error("--tenant-id and --source-id are required (or TENANT_ID/SOURCE_ID env)")
    if not (url and key):
        ap.error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")

    # The gateway admits any valid project JWT in the apikey header; the
    # role that matters travels in Authorization. service_role bypasses RLS,
    # which is exactly what the delivery side needs, and it lives only in
    # GitHub Secrets — never in a file or a log.
    client = supa.Supa(url, anon_key=key, token=key)
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    if args.dry_run:
        # read-only: build the plan from live state, print it, write nothing
        ctx = _load_context(client, args.tenant_id, args.source_id)
        state = _load_state(client, ctx, now_utc)
        out = plan(now_utc, ctx, state, force_shift=args.force_shift,
                   shift_date=args.shift_date)
        _print_plan(out)
        return 0
    run(client, tenant_id=args.tenant_id, source_id=args.source_id,
        now_utc=now_utc, force_shift=args.force_shift,
        shift_date=args.shift_date)
    return 0


def _print_plan(out: Plan) -> None:
    """dry-run output: the full Arabic report bodies, no secrets anywhere."""
    print("=" * 62)
    print("POSentine orchestrator — DRY RUN (nothing enqueued, nothing sent)")
    print("=" * 62)
    if out.shift_row is not None:
        sr = out.shift_row
        print(f"  shift report    {sr['shift_date']} {sr['shift_name']} "
              f"grand_total={sr['grand_total']} is_partial={sr['is_partial']}")
    for e in out.envelopes:
        print("-" * 62)
        print(f"  [{e.kind}] recipient={e.recipient} dedup={e.dedup_key}")
        print()
        for line in e.body.splitlines():
            print(f"    {line}")
        print()
    if not out.envelopes:
        print("  (no messages to enqueue)")
    print("-" * 62)
    print(f"  events to record : {len(out.event_rows)} "
          f"(queued: {len(out.queue_keys)})")
    print(f"  internal anomalies: {len(out.anomalies)}")
    print("=" * 62)


if __name__ == "__main__":
    raise SystemExit(main())
