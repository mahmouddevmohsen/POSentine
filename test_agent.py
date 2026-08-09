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
        return []

    def insert(self, table, rows, returning=True):
        self.inserts.append((table, list(rows)))
        return []

    def tables_written(self):
        return {t for t, _ in self.upserts}


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "state.json"


def fresh_state():
    return A.State()


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
    assert "SELECT COUNT(*) FROM Sales" in out
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

def test_rescan_cycle_stamps_last_rescan_at(paths):
    client, state = FakeSupa(), fresh_state()      # cycle_index 0 -> rescan
    A.run_once(CFG, state, paths, fake_adapter, client, NOW, False)
    patch = dict(client.updates[0][1])
    assert patch["last_rescan_at"] == "2026-08-09T03:00:00+00:00"
    assert patch["rescan_from_salid"] == 1000


def test_non_rescan_cycle_never_touches_last_rescan_at(paths):
    client, state = FakeSupa(), fresh_state()
    state.cycle_index = 1                          # not a multiple of 5
    state.products = {i: {"itname": n, "list_price": p}
                      for i, n, p in fake_adapter.MENU}
    A.run_once(CFG, state, paths, fake_adapter, client, NOW, False)
    patch = dict(client.updates[0][1])
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
# 8) config
# ════════════════════════════════════════════════════════════════

def test_config_rejects_a_missing_anon_key(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"tenant_id": "t", "source_id": "s",
                             "supabase_url": "https://x", "sql": {}}),
                 encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        A.Config.load(p)
    assert "supabase_anon_key" in str(exc.value)
