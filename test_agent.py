# -*- coding: utf-8 -*-
"""
test_agent.py — the ordering rules that make the sync safe.

No network, no SQL Server. A fake Supabase records every call and can be
told to fail on a chosen table, which is how the partial-upload cases get
tested at all.

The tests worth reading are the ones about what must NOT happen:
the watermark that must not advance, the last_rescan_at that must not
advance, the price that must not become zero, and the dry run that must
not write.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest

import agent as A
import fake_adapter
import supa

NOW = dt.datetime(2026, 8, 9, 3, 0, tzinfo=dt.timezone.utc)

CFG = A.Config(
    tenant_id="57b61b47-a590-49fe-803c-0c174a07b7ec",
    source_id="93f8d146-ba68-4d58-8eda-f797f3e28bd4",
    supabase_url="https://example.supabase.co",
    supabase_anon_key="anon",
    supabase_agent_token="tok",
    sql={"server": "x", "database": "y", "user": "z", "password": "p"},
)


class FakeSupa:
    """Records writes; can be told to fail on one table."""

    def __init__(self, fail_on: str | None = None):
        self.fail_on = fail_on
        self.upserts: list[tuple[str, int]] = []
        self.updates: list[tuple[str, dict]] = []
        self.update_filters: list[tuple[str, dict]] = []
        self.inserts: list[tuple[str, list]] = []

    def upsert(self, table, rows, on_conflict):
        if table == self.fail_on:
            raise supa.SupaError(f"{table}: 0 of {len(rows)} rows landed")
        self.upserts.append((table, len(rows)))
        return len(rows)

    def update(self, table, filters, patch, returning=True):
        if table == self.fail_on:
            raise supa.SupaError(f"{table} failed")
        self.updates.append((table, patch))
        self.update_filters.append((table, dict(filters)))
        return [patch]

    def insert(self, table, rows, returning=True):
        self.inserts.append((table, list(rows)))
        return []

    def tables_written(self):
        return {t for t, _ in self.upserts}


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "state.json"


def fresh_state():
    """An agent that is already installed. Day Zero has its own tests."""
    return A.State(initialised=True)


# ════════════════════════════════════════════════════════════════
# 1) 🔴 --dry-run writes nothing, anywhere
# ════════════════════════════════════════════════════════════════

def test_dry_run_writes_absolutely_nothing(paths, capsys):
    client = FakeSupa()
    state = fresh_state()
    code = A.run_once(CFG, state, paths, fake_adapter, client, NOW, dry_run=True)

    assert code == A.EXIT_OK
    assert client.upserts == [] and client.updates == [] and client.inserts == []
    assert not paths.exists(), "state.json must not be created by a dry run"
    assert state.cycle_index == 0, "not even the local cycle counter advances"
    assert state.watermark_salid == 0


def test_dry_run_reports_what_it_would_upload(paths, capsys):
    A.run_once(CFG, fresh_state(), paths, fake_adapter, FakeSupa(), NOW,
               dry_run=True)
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "nothing is written" in out
    assert "invoices to upload" in out
    assert "SELECT COUNT(*) FROM dbo.Sales" in out
    assert "cash" in out and "external" in out and "return" in out


def test_dry_run_prints_arabic_without_crashing(paths, capsys):
    """cp1252 on a Windows console would raise UnicodeEncodeError here."""
    A.run_once(CFG, fresh_state(), paths, fake_adapter, FakeSupa(), NOW,
               dry_run=True)
    assert capsys.readouterr().out


# ════════════════════════════════════════════════════════════════
# 2) 🔴 the watermark advances only on a complete upload
# ════════════════════════════════════════════════════════════════

def test_successful_cycle_advances_the_watermark(paths):
    client, state = FakeSupa(), fresh_state()
    assert A.run_once(CFG, state, paths, fake_adapter, client, NOW, False) == A.EXIT_OK
    assert state.watermark_salid == 1104
    assert state.cycle_index == 1
    assert paths.exists()
    assert {"invoices", "invoice_lines", "cash_counts",
            "pos_products", "pos_users"} <= client.tables_written()


def test_failed_line_upload_holds_the_watermark(paths):
    """
    Invoices land, lines fail. Advancing here would skip those invoices
    forever — nothing re-reads a range below the watermark.
    """
    client, state = FakeSupa(fail_on="invoice_lines"), fresh_state()
    code = A.run_once(CFG, state, paths, fake_adapter, client, NOW, False)

    assert code == A.EXIT_ERROR
    assert state.watermark_salid == 0
    assert state.cycle_index == 0
    assert client.updates == [], "sync_state must not be touched"
    assert not paths.exists()


def test_failure_still_records_a_heartbeat(paths):
    """Silence and success must not look the same from the cloud."""
    client = FakeSupa(fail_on="invoices")
    A.run_once(CFG, fresh_state(), paths, fake_adapter, client, NOW, False)
    beats = [r for t, rowset in client.inserts if t == "heartbeats" for r in rowset]
    assert beats and beats[0]["ok"] is False
    assert json.loads(beats[0]["note"])["kind"] == "upload_failed"


# ════════════════════════════════════════════════════════════════
# 3) 🔴 last_rescan_at is the deletion trigger — never on a partial upload
# ════════════════════════════════════════════════════════════════

def _merged_patches(client) -> dict:
    """sync_state is written as several independently-guarded PATCHes."""
    merged = {}
    for table, patch in client.updates:
        if table == "sync_state":
            merged.update(patch)
    return merged


def test_rescan_cycle_stamps_last_rescan_at(paths):
    client, state = FakeSupa(), fresh_state()      # cycle_index 0 -> rescan
    A.run_once(CFG, state, paths, fake_adapter, client, NOW, False)
    patch = _merged_patches(client)
    assert patch["last_rescan_at"] == "2026-08-09T03:00:00+00:00"
    assert patch["rescan_from_salid"] == 1000


def test_last_rescan_write_is_guarded_by_lt_or_null(paths):
    client, state = FakeSupa(), fresh_state()
    A.run_once(CFG, state, paths, fake_adapter, client, NOW, False)
    guards = [f for t, f in client.update_filters if t == "sync_state"]
    rescan_guard = [g for g in guards if "last_rescan_at" in g.get("or", "")]
    assert rescan_guard, "the rescan stamp must be written monotonically"
    assert "lt." in rescan_guard[0]["or"] and "is.null" in rescan_guard[0]["or"]


def test_non_rescan_cycle_never_touches_last_rescan_at(paths):
    client, state = FakeSupa(), fresh_state()
    state.cycle_index = 1                          # not a multiple of 5
    state.products = {i: {"itname": n, "list_price": p}
                      for i, n, p in fake_adapter.MENU}
    A.run_once(CFG, state, paths, fake_adapter, client, NOW, False)
    patch = _merged_patches(client)
    assert "last_rescan_at" not in patch
    assert "rescan_from_salid" not in patch


def test_partial_rescan_upload_does_not_stamp_last_rescan_at(paths):
    """
    The most dangerous failure in the feature. If last_rescan_at advanced
    after a half-written rescan, every invoice that did not make it would
    read as absent, and the cloud would report hundreds of deletions.
    """
    client, state = FakeSupa(fail_on="invoice_lines"), fresh_state()
    A.run_once(CFG, state, paths, fake_adapter, client, NOW, False)
    assert client.updates == []
    assert state.rescan_from_salid == 0


# ════════════════════════════════════════════════════════════════
# 3b) 🔴 sync_state must be monotonic — upserts do not protect it
# ════════════════════════════════════════════════════════════════

class StatefulSyncState:
    """
    A sync_state row that actually enforces PostgREST filters, so the
    monotonicity can be tested as a property rather than as a string in a
    request. Only the filters the agent uses are implemented.
    """

    def __init__(self):
        self.row = {"watermark_salid": 0, "watermark_saledeid": 0,
                    "rescan_from_salid": 0, "last_sync_at": None,
                    "last_rescan_at": None, "pos_max_salid": 0}
        self.rejected = 0

    def upsert(self, table, rows, on_conflict):
        return len(rows)

    def insert(self, table, rows, returning=True):
        return []

    def update(self, table, filters, patch, returning=True):
        if table != "sync_state":
            return []
        if not self._matches(filters):
            self.rejected += 1
            return []                      # PostgREST: zero rows matched
        self.row.update(patch)
        return [dict(self.row)]

    def _matches(self, filters) -> bool:
        for key, expr in filters.items():
            if key in ("tenant_id", "source_id"):
                continue
            if key == "or":
                inner = expr.strip("()").split(",")
                if not any(self._one(c) for c in inner):
                    return False
            elif not self._one(f"{key}.{expr}"):
                return False
        return True

    def _one(self, clause: str) -> bool:
        col, op, val = clause.split(".", 2)
        current = self.row.get(col)
        if op == "is" and val == "null":
            return current is None
        if op == "lt":
            if current is None:
                return False
            return str(current) < val if isinstance(current, str) else current < int(val)
        raise AssertionError(f"unsupported filter in test: {clause}")


def test_watermark_write_is_guarded_against_going_backwards(paths):
    client, state = StatefulSyncState(), fresh_state()
    A.run_once(CFG, state, paths, fake_adapter, client, NOW, False)
    assert client.row["watermark_salid"] == 1104


def test_a_hung_process_cannot_rewind_the_watermark(paths):
    """
    The takeover case. A wedged cycle resumes after a newer one finished
    and tries to write its own, older watermark. PostgreSQL must reject it
    — a watermark moving backwards is the exact signature RestoreSuspected
    exists to catch, and it must never fire for a benign reason.
    """
    client = StatefulSyncState()
    client.row.update({"watermark_salid": 5000, "watermark_saledeid": 90000})

    A.run_once(CFG, fresh_state(), paths, fake_adapter, client, NOW, False)

    assert client.row["watermark_salid"] == 5000, "the older write must not apply"
    assert client.rejected >= 1


def test_an_older_rescan_stamp_cannot_overwrite_a_newer_one(paths):
    client = StatefulSyncState()
    client.row["last_rescan_at"] = "2026-08-09T04:00:00+00:00"   # newer

    A.run_once(CFG, fresh_state(), paths, fake_adapter, client,
               dt.datetime(2026, 8, 9, 3, 0, tzinfo=dt.timezone.utc), False)

    assert client.row["last_rescan_at"] == "2026-08-09T04:00:00+00:00"


def test_first_ever_rescan_applies_when_last_rescan_at_is_null(paths):
    client = StatefulSyncState()
    assert client.row["last_rescan_at"] is None
    A.run_once(CFG, fresh_state(), paths, fake_adapter, client, NOW, False)
    assert client.row["last_rescan_at"] == "2026-08-09T03:00:00+00:00"


# ════════════════════════════════════════════════════════════════
# 4) 🔴 an unknown item is NULL and loud, never 0 and quiet
# ════════════════════════════════════════════════════════════════

def test_unknown_item_uploads_null_price_and_raises_an_anomaly(paths):
    client, state = FakeSupa(), fresh_state()
    A.run_once(CFG, state, paths, fake_adapter, client, NOW, False)

    beats = [r for t, rowset in client.inserts if t == "heartbeats" for r in rowset]
    notes = [json.loads(b["note"]) for b in beats if b["note"]]
    unknown = [n for n in notes if n["kind"] == "unknown_item"]
    assert unknown, "an item missing from the snapshot must be reported"
    assert unknown[0]["detail"]["itid"] == fake_adapter.UNKNOWN_ITID
    assert all(b["ok"] is False for b in beats if b["note"])


def test_known_items_are_frozen_with_name_and_price():
    result = A.build_cycle(fake_adapter, None, CFG, fresh_state(), NOW)
    priced = [ln for ln in result.lines if ln["itid"] == 1]
    assert priced and priced[0]["item_name"] == "طعمية"
    assert priced[0]["list_price"] == 15.0
    unknown = [ln for ln in result.lines
               if ln["itid"] == fake_adapter.UNKNOWN_ITID]
    assert unknown and unknown[0]["list_price"] is None


# ════════════════════════════════════════════════════════════════
# 5) halt conditions stop the cycle rather than reporting zeros
# ════════════════════════════════════════════════════════════════

def test_restore_suspected_state_halts_before_connecting(paths):
    state = fresh_state()
    state.restore_suspected = True
    client = FakeSupa()
    assert A.run_once(CFG, state, paths, fake_adapter, client, NOW,
                      False) == A.EXIT_HALTED
    assert client.upserts == []


def test_schema_not_ok_halts(paths):
    state = fresh_state()
    state.schema_ok = False
    assert A.run_once(CFG, state, paths, fake_adapter, FakeSupa(), NOW,
                      False) == A.EXIT_HALTED


def test_restore_detected_mid_cycle_sets_the_flag_and_stops(paths):
    """salid moved backwards: the watermark now matches nothing, forever."""
    state = fresh_state()
    state.watermark_salid = 999_999_999          # higher than the fixture max
    client = FakeSupa()
    code = A.run_once(CFG, state, paths, fake_adapter, client, NOW, False)

    assert code == A.EXIT_HALTED
    assert state.restore_suspected is True
    assert client.updates[0][1]["restore_suspected"] is True
    beats = [r for t, rowset in client.inserts if t == "heartbeats" for r in rowset]
    assert json.loads(beats[0]["note"])["kind"] == "restore_suspected"


# ════════════════════════════════════════════════════════════════
# 6) state file
# ════════════════════════════════════════════════════════════════

def test_state_round_trips_with_integer_item_keys(tmp_path):
    p = tmp_path / "state.json"
    s = A.State(watermark_salid=42, products={7: {"itname": "x", "list_price": 1}})
    s.save(p)
    back = A.State.load(p)
    assert back.watermark_salid == 42
    assert 7 in back.products, "JSON string keys must come back as ints"


def test_state_write_is_atomic(tmp_path):
    p = tmp_path / "state.json"
    A.State(watermark_salid=1).save(p)
    A.State(watermark_salid=2).save(p)
    assert json.loads(p.read_text(encoding="utf-8"))["watermark_salid"] == 2
    assert not p.with_suffix(".json.tmp").exists()


# ════════════════════════════════════════════════════════════════
# 7) single instance
# ════════════════════════════════════════════════════════════════

def test_second_instance_does_not_run(tmp_path):
    lock = tmp_path / "a.lock"
    with A.SingleInstanceLock(lock) as first:
        assert first.may_run
        with A.SingleInstanceLock(lock) as second:
            assert not second.may_run, "two cycles must not overlap"


def test_lock_is_released_on_exit(tmp_path):
    lock = tmp_path / "a.lock"
    with A.SingleInstanceLock(lock) as first:
        assert first.may_run
    with A.SingleInstanceLock(lock) as again:
        assert again.may_run


def test_a_hung_holder_is_taken_over_rather_than_stalling_forever(tmp_path):
    """One wedged process must not stop the product permanently."""
    lock = tmp_path / "a.lock"
    with A.SingleInstanceLock(lock):
        import os
        old = 1_000_000
        os.utime(lock, (old, old))
        with A.SingleInstanceLock(lock, stale_after=60) as second:
            assert second.may_run and second.took_over


# ════════════════════════════════════════════════════════════════
# 7b) --confirm reads the cloud back and judges it on this screen
# ════════════════════════════════════════════════════════════════

class ReadbackSupa:
    def __init__(self, counts=None, state=None, beats=None):
        self.counts = counts or {"invoices": 45, "invoice_lines": 129,
                                 "cash_counts": 2, "pos_products": 9,
                                 "pos_users": 1}
        self.null_price = 0
        self.state = state if state is not None else [{
            "watermark_salid": 1104, "last_sync_at": "2026-08-09T03:00:00+00:00",
            "last_rescan_at": "2026-08-09T03:00:00+00:00",
            "restore_suspected": False, "schema_ok": True}]
        self.beats = beats if beats is not None else [
            {"at": "2026-08-09T03:00:00+00:00", "ok": True, "drift_seconds": -3,
             "rows_pulled": 174, "note": None}]

    def count(self, table, params=None):
        if (params or {}).get("list_price") == "is.null":
            return self.null_price
        return self.counts[table]

    def select(self, table, params=None, paginate=True):
        return self.state if table == "sync_state" else self.beats


def test_confirm_passes_on_a_healthy_upload(capsys):
    assert A.confirm(ReadbackSupa(), CFG) == A.EXIT_OK
    out = capsys.readouterr().out
    assert "RESULT: OK" in out and "watermark_salid      1104" in out


def test_confirm_flags_lines_without_a_price(capsys):
    client = ReadbackSupa()
    client.null_price = 3
    assert A.confirm(client, CFG) == A.EXIT_ERROR
    assert "Zero-invoice detection" in capsys.readouterr().out


def test_confirm_flags_a_halted_sync(capsys):
    client = ReadbackSupa(state=[{"watermark_salid": 0, "restore_suspected": True,
                                  "schema_ok": True, "last_rescan_at": None}])
    assert A.confirm(client, CFG) == A.EXIT_ERROR
    out = capsys.readouterr().out
    assert "restore_suspected is true" in out


def test_confirm_flags_silence(capsys):
    """No heartbeats and no data must never read as a clean install."""
    client = ReadbackSupa(beats=[])
    client.counts = dict(client.counts, invoices=0)
    assert A.confirm(client, CFG) == A.EXIT_ERROR
    out = capsys.readouterr().out
    assert "no heartbeats at all" in out and "no invoices landed" in out


# ════════════════════════════════════════════════════════════════
# 8) config
# ════════════════════════════════════════════════════════════════

def _write_config(tmp_path, token, tenant="57b61b47-a590-49fe-803c-0c174a07b7ec"):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "tenant_id": tenant, "source_id": "s",
        "supabase_url": "https://x.supabase.co",
        "supabase_anon_key": "anon", "supabase_agent_token": token,
        "sql": {"server": "x"}}), encoding="utf-8")
    return p


def _token(role="authenticated", tenant="57b61b47-a590-49fe-803c-0c174a07b7ec"):
    import base64
    import hashlib
    import hmac
    import mint_agent_token as m
    head = m._segment({"alg": "HS256", "typ": "JWT"})
    body = m._segment({"role": role, "tenant_id": tenant, "aud": "authenticated"})
    sig = base64.urlsafe_b64encode(
        hmac.new(b"k", f"{head}.{body}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{head}.{body}.{sig}"


# ════════════════════════════════════════════════════════════════
# 8b) 🔴 the agent token must be decoded, not string-matched
# ════════════════════════════════════════════════════════════════

def test_service_role_token_is_rejected_by_decoding_the_claim(tmp_path):
    """
    A JWT is base64: the literal text 'service_role' never appears in it,
    so `'service_role' in token` returns False for an actual service_role
    key. A check that always passes is worse than no check — it
    manufactures confidence. The claim must be decoded and read.
    """
    token = _token(role="service_role")
    assert "service_role" not in token, "the premise: the string is not there"

    with pytest.raises(ValueError) as exc:
        A.Config.load(_write_config(tmp_path, token))
    assert "service_role" in str(exc.value)
    assert "authenticated" in str(exc.value)


def test_token_for_a_different_tenant_is_rejected(tmp_path):
    """Authenticates perfectly, matches no rows, uploads into nothing."""
    other = "00000000-0000-4000-8000-000000000001"
    with pytest.raises(ValueError) as exc:
        A.Config.load(_write_config(tmp_path, _token(tenant=other)))
    assert "tenant" in str(exc.value).lower()


def test_a_correct_agent_token_loads(tmp_path):
    cfg = A.Config.load(_write_config(tmp_path, _token()))
    assert cfg.tenant_id == "57b61b47-a590-49fe-803c-0c174a07b7ec"


def test_a_malformed_token_is_rejected_not_ignored(tmp_path):
    with pytest.raises(ValueError):
        A.Config.load(_write_config(tmp_path, "not.a.jwt"))


# ════════════════════════════════════════════════════════════════
# 9) 🔴 Day Zero — a fresh install must not pull 218,207 invoices
# ════════════════════════════════════════════════════════════════

def test_first_run_sets_the_watermark_and_pulls_nothing(paths):
    """
    watermark_salid defaults to 0, and 'no backfill' has to mean something
    in practice: on a fresh install we adopt MAX(salid) and read nothing
    behind it. Otherwise the first cycle drags the whole history across
    during service.
    """
    client, state = FakeSupa(), A.State()
    assert state.initialised is False

    code = A.run_once(CFG, state, paths, fake_adapter, client, NOW, False)

    assert code == A.EXIT_OK
    assert state.initialised is True
    assert state.watermark_salid == 1104          # the fixture's MAX(salid)
    assert client.upserts == [], "nothing may be uploaded on the first run"


def test_first_run_dry_run_reports_but_sets_nothing(paths, capsys):
    state = A.State()
    A.run_once(CFG, state, paths, fake_adapter, FakeSupa(), NOW, dry_run=True)
    out = capsys.readouterr().out
    assert "FIRST RUN" in out
    assert "1104" in out
    assert state.initialised is False and state.watermark_salid == 0
    assert not paths.exists()


def test_second_run_after_initialisation_pulls_normally(paths):
    client = FakeSupa()
    state = A.State(initialised=True)
    A.run_once(CFG, state, paths, fake_adapter, client, NOW, False)
    assert "invoices" in client.tables_written()


# ════════════════════════════════════════════════════════════════
# 9b) 🔴 a per-cycle ceiling, and a watermark that never overshoots
# ════════════════════════════════════════════════════════════════

def test_the_cap_is_applied_by_the_server_and_flagged(paths):
    """
    The ceiling is now enforced in the SQL (SELECT TOP), so it bounds the
    POS's work rather than only our upload. The flag matters because
    'capped at N' and 'found exactly N' are otherwise indistinguishable,
    and a persistent backlog would drain quietly forever.
    """
    cfg = replace(CFG, max_new_invoices_per_cycle=10)
    result = A.build_cycle(fake_adapter, None, cfg, fresh_state(), NOW)

    assert result.new_invoice_count == 10, "exactly the cap, not more"
    assert result.capped is True
    assert result.pull.new_capped is True


def test_an_uncapped_cycle_is_not_flagged(paths):
    cfg = replace(CFG, max_new_invoices_per_cycle=5000)
    result = A.build_cycle(fake_adapter, None, cfg, fresh_state(), NOW)
    assert result.capped is False
    assert result.pull.new_capped is False


def test_the_backlog_drains_one_cap_at_a_time(paths):
    """
    The watermark advances by exactly what was pulled, so the remainder is
    still above it next cycle. Without this the capped remainder would be
    skipped permanently.
    """
    cfg = replace(CFG, max_new_invoices_per_cycle=10)
    state = fresh_state()

    A.run_once(cfg, state, paths, fake_adapter, FakeSupa(), NOW, False)
    first = state.watermark_salid
    assert first == 1009

    A.run_once(cfg, state, paths, fake_adapter, FakeSupa(), NOW, False)
    assert state.watermark_salid == 1019, "the next ten, not a re-read"
    assert state.watermark_salid > first


def test_a_capped_cycle_reports_a_backlog_anomaly(paths):
    """--confirm has no other way to see that a backlog exists."""
    cfg = replace(CFG, max_new_invoices_per_cycle=10)
    client = FakeSupa()
    A.run_once(cfg, fresh_state(), paths, fake_adapter, client, NOW, False)

    beats = [r for t, rowset in client.inserts if t == "heartbeats" for r in rowset]
    kinds = [json.loads(b["note"])["kind"] for b in beats if b["note"]]
    assert "backlog" in kinds


def test_confirm_names_a_backlog_explicitly(capsys):
    client = ReadbackSupa(beats=[{
        "at": "2026-08-09T03:00:00+00:00", "ok": False, "drift_seconds": 0,
        "rows_pulled": 0,
        "note": json.dumps({"kind": "backlog", "detail": {"remaining": 900}})}])
    assert A.confirm(client, CFG) == A.EXIT_ERROR
    out = capsys.readouterr().out
    assert "per-cycle ceiling" in out, "must name it, not just echo the note"
    assert "still draining" in out


def test_confirm_flags_an_unparseable_note_rather_than_skipping_it(capsys):
    """A note we cannot read is itself a finding."""
    client = ReadbackSupa(beats=[{
        "at": "2026-08-09T03:00:00+00:00", "ok": True, "drift_seconds": 0,
        "rows_pulled": 1, "note": "{not json"}])
    assert A._note_kind("{not json") == "unparseable_note"
    A.confirm(client, CFG)


def test_dry_run_shows_the_ceiling_was_hit(paths, capsys):
    cfg = replace(CFG, max_new_invoices_per_cycle=10)
    A.run_once(cfg, fresh_state(), paths, fake_adapter, FakeSupa(), NOW,
               dry_run=True)
    out = capsys.readouterr().out
    assert "CEILING HIT" in out
    assert "CAPPED" in out
    assert "ceiling 10" in out, "must print the configured cap, not the default"
    assert "ceiling 5000" not in out


def test_cycle_is_capped_and_the_cap_is_logged(paths, caplog):
    caplog.set_level("WARNING")
    cfg = replace(CFG, max_new_invoices_per_cycle=10)
    A.run_once(cfg, fresh_state(), paths, fake_adapter, FakeSupa(), NOW, False)
    assert any("backlog" in r.message.lower() for r in caplog.records),         "a persistent backlog must be visible in the log"


def test_watermark_never_passes_an_invoice_we_did_not_read(paths):
    """
    pull() returns MAX(salid) over the whole table, which includes rows the
    30-second dirty-read guard deliberately held back. Advancing to that
    number would step over invoices we never read — the rescan would catch
    them within 15 minutes, but a report built in between would be short.
    The watermark follows what was actually pulled.
    """
    cfg = replace(CFG, max_new_invoices_per_cycle=10)
    state = fresh_state()
    A.run_once(cfg, state, paths, fake_adapter, FakeSupa(), NOW, False)

    assert state.watermark_salid < 1104, "must not skip the uncapped remainder"
    assert state.watermark_salid == 1009


# ════════════════════════════════════════════════════════════════
# 9c) 🔴 the acceptance comparison runs inside --dry-run
# ════════════════════════════════════════════════════════════════

def test_dry_run_compares_against_a_bare_count_and_says_pass(paths, capsys):
    """Neither SSMS nor sqlcmd exists on that machine."""
    state = fresh_state()
    A.run_once(CFG, state, paths, fake_adapter, FakeSupa(), NOW, dry_run=True)
    out = capsys.readouterr().out
    assert "SELECT COUNT(*) FROM dbo.Sales" in out
    assert "VERDICT" in out and "PASS" in out


def test_dry_run_aborts_when_the_agent_claims_more_than_the_pos_has(paths,
                                                                    capsys,
                                                                    monkeypatch):
    state = fresh_state()
    monkeypatch.setattr(fake_adapter, "raw_counts", lambda cn, w: (3, 3))
    A.run_once(CFG, state, paths, fake_adapter, FakeSupa(), NOW, dry_run=True)
    out = capsys.readouterr().out
    assert "ABORT" in out


def test_config_rejects_unfilled_placeholders(tmp_path):
    """
    config.example.json ships '<from mint_agent_token.py>'. Copied over
    unedited, that reaches the HTTP layer and dies as UnicodeEncodeError on
    an em-dash — an error that points at encoding, not at the config nobody
    filled in. Name the real problem instead.
    """
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "tenant_id": "t", "source_id": "s", "supabase_url": "https://x",
        "supabase_anon_key": "real-anon",
        "supabase_agent_token": "<from mint_agent_token.py>",
        "sql": {"server": "x"}}), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        A.Config.load(p)
    assert "supabase_agent_token" in str(exc.value)
    assert "placeholder" in str(exc.value).lower()


def test_config_rejects_a_missing_anon_key(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"tenant_id": "t", "source_id": "s",
                             "supabase_url": "https://x", "sql": {}}),
                 encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        A.Config.load(p)
    assert "supabase_anon_key" in str(exc.value)
