# -*- coding: utf-8 -*-
"""
test_delivery.py — Phase 2 Task 4: the cloud execution layer contract.

delivery.py is exactly what GitHub Actions runs (one process: orchestrator
then notifier). These tests pin:

  - the import closure stays cloud-only — no pyodbc, no adapter_hdsoft, no
    agent, no sqlguard — with the paired falsifier that proves the walker
    can fail (the project's recurring failure shape: a check that shares
    the fault it is meant to detect);
  - missing configuration fails LOUDLY (a delivery system that silently
    succeeds without its configuration is worse than one that does not
    exist);
  - --dry-run builds and prints the decided plan and the notifier's
    would-send view, and writes NOTHING (no enqueue, no claim, no send);
  - live mode runs orchestrator → notifier against one fake store and the
    outbox row goes pending → sending → sent with sent_at;
  - --force-shift / --shift-date pass through to the orchestrator.

No network anywhere: delivery.main accepts injectable client / session /
sleep / now_utc (the same convention as orchestrator.run and notifier.run);
production (the workflow) passes nothing and uses the real environment.
"""

from __future__ import annotations

import ast
import datetime as _dt
import json
import re
from pathlib import Path

import pytest

import delivery
import orchestrator as ORCH

TID = "57b61b47-a590-49fe-803c-0c174a07b7ec"
SID = "93f8d146-ba68-4d58-8eda-f797f3e28bd4"
REPO = Path(__file__).resolve().parent

# Fake configuration values — never real credentials. Tests prove the code
# reads them from the environment by name only.
FAKE_URL = "https://example-project.supabase.co"
FAKE_KEY = "fake-service-role-key"
FAKE_TOKEN = "1234567890:FAKE-DELIVERY-TOKEN"


def utc(y, mo, d, h, mi=0):
    return _dt.datetime(y, mo, d, h, mi, tzinfo=_dt.timezone.utc)


def set_fake_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", FAKE_URL)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", FAKE_KEY)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TENANT_ID", TID)
    monkeypatch.setenv("SOURCE_ID", SID)


# ════════════════════════════════════════════════════════════════
# one in-memory Supabase for one delivery.py process
# ════════════════════════════════════════════════════════════════

def _eq_clause(row: dict, clause: str) -> bool:
    parts = clause.split(".", 2)                 # status.eq.pending
    if len(parts) < 3:
        return False
    key, op, val = parts
    if op == "eq":
        return str(row.get(key)) == val
    if op == "is":
        return (row.get(key) is None) == (val == "null")
    return False


def _match(row: dict, filters: dict) -> bool:
    for k, v in filters.items():
        if k in ("order", "limit", "select"):
            continue
        if k == "or":
            clauses = v.strip("()").split(",")
            if not any(_eq_clause(row, c) for c in clauses):
                return False
        elif isinstance(v, str) and v.startswith("eq."):
            if str(row.get(k)) != v[3:]:
                return False
        elif isinstance(v, str) and v.startswith("gte."):
            if str(row.get(k)) < v[4:]:
                return False
    return True


class FakeCloud:
    """One fake store for the whole process, like the real project.

    Serves the full supa.Supa surface delivery.py exercises: canned reads,
    constraint-honoring insert_ignore (the database is the idempotency
    arbiter — a duplicate insert is ignored, exactly like on_conflict
    do nothing), outbox claim/mark updates, and event status updates.
    The orchestrator writes pending outbox rows, the notifier claims and
    marks them — in the same store, in the same process.
    """

    def __init__(self, tables=None, counts=None,
                 now_iso="2026-07-01T04:10:00+00:00"):
        self.reads = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.counts = dict(counts or {})
        self.stores = {"outbox": {}, "shift_reports": {}, "events": {}}
        self.anomalies = []
        self.calls = []
        self._seq = 0
        self._now_iso = now_iso
        for r in self.reads.get("outbox", []):
            self.stores["outbox"][r["id"]] = dict(r)
        for r in self.reads.get("shift_reports", []):
            self.stores["shift_reports"][(r["shift_date"], r["shift_name"])] = dict(r)
        for r in self.reads.get("events", []):
            self.stores["events"][(r["type"], r["dedup_key"])] = dict(r)

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def select(self, table, params=None, paginate=True):
        self.calls.append(("select", table))
        params = params or {}
        if table in self.stores:
            rows = [dict(r) for r in self.stores[table].values()]
        else:
            rows = [dict(r) for r in self.reads.get(table, [])]
        rows = [r for r in rows if _match(r, params)]
        order = params.get("order")
        if order == "created_at.asc":
            rows.sort(key=lambda r: str(r.get("created_at", "")))
        elif order == "at.desc":
            rows.sort(key=lambda r: str(r.get("at", "")), reverse=True)
        elif order == "at.asc":
            rows.sort(key=lambda r: str(r.get("at", "")))
        if params.get("limit") is not None:
            rows = rows[: int(params["limit"])]
        return rows

    def count(self, table, params=None):
        self.calls.append(("count", table))
        return self.counts.get(table, 0)

    def insert_ignore(self, table, rows, on_conflict):
        self.calls.append(("insert_ignore", table, on_conflict))
        store = self.stores[table]
        for row in rows:
            key = self._key(table, row)
            if key in store:
                continue
            stored = dict(row)
            if table == "outbox":                       # server-owned columns
                stored.setdefault("id", self._next_id())
                stored.setdefault("created_at", self._now_iso)
            store[key] = stored

    def insert(self, table, rows, returning=True):
        self.calls.append(("insert", table))
        if table == "internal_anomalies":
            self.anomalies.extend(dict(r) for r in rows)
            return []
        return [dict(r) for r in rows] if returning else []

    def update(self, table, filters, patch, returning=True):
        self.calls.append(("update", table))
        if table not in self.stores:
            return []
        matched = [r for r in self.stores[table].values() if _match(r, filters)]
        if filters.get("order") == "created_at.asc":
            matched.sort(key=lambda r: str(r.get("created_at", "")))
        for r in matched:
            r.update(patch)
        return [dict(r) for r in matched] if returning else []

    @staticmethod
    def _key(table, row):
        if table == "events":
            return (row["tenant_id"], row["source_id"], row["type"], row["dedup_key"])
        if table == "outbox":
            return (row["tenant_id"], row["channel"], row["recipient"], row["dedup_key"])
        if table == "shift_reports":
            return (row["tenant_id"], row["source_id"], row["shift_date"], row["shift_name"])
        return None


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)
        self.headers = {}

    def json(self):
        return self._body


def _tg_ok(message_id=42):
    return _Resp(200, {"ok": True, "result": {"message_id": message_id}})


class FakeTelegram:
    """Scripted sendMessage responses; records calls and sleeps."""

    def __init__(self, responses=None):
        self.responses = list(responses) if responses else []
        self.calls = []
        self.sleeps = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, dict(json) if json else None))
        if not self.responses:
            return _tg_ok()
        return self.responses.pop(0)

    def sleep(self, seconds):
        self.sleeps.append(seconds)


# ════════════════════════════════════════════════════════════════
# canned live state — July 1 07:10 Cairo, one evening-shift invoice
# ════════════════════════════════════════════════════════════════

def _canned(now=None, outbox=None, reports=None):
    now = now or utc(2026, 7, 1, 4, 10)
    base = {
        "tenants": [{"id": TID, "timezone": "Africa/Cairo",
                     "shift_morning_start": "07:00:00",
                     "shift_evening_start": "19:00:00", "go_live_at": None}],
        "recipients": [{"tenant_id": TID, "channel": "telegram", "address": "111",
                        "label": "dev", "active": True, "notify_before_golive": True}],
        "alert_settings": [{"tenant_id": TID, "alert_type": "refund",
                            "detect": True, "notify": False}],
        "sync_state": [{"tenant_id": TID, "source_id": SID, "rescan_from_salid": 0,
                        "last_rescan_at": None, "restore_suspected": False,
                        "schema_ok": True}],
        "heartbeats": [
            {"id": 1, "tenant_id": TID, "source_id": SID, "ok": True, "note": None,
             "at": (now - _dt.timedelta(hours=14)).isoformat()},
            {"id": 2, "tenant_id": TID, "source_id": SID, "ok": True, "note": None,
             "at": (now - _dt.timedelta(minutes=1)).isoformat()},
        ],
        "shift_reports": [],
        "invoices": [{
            "tenant_id": TID, "source_id": SID, "salid": 1, "receipt_num": 1,
            "sold_at": "2026-06-30T20:00:00", "total": 100.0, "delivery_cost": 0.0,
            "user_uid": 1, "kind": "cash",
            "first_seen_at": (now - _dt.timedelta(hours=2)).isoformat(),
            "last_seen_at": (now - _dt.timedelta(minutes=1)).isoformat(),
            "deleted_at": None}],
        "invoice_lines": [{
            "tenant_id": TID, "source_id": SID, "saledeid": 1, "salid": 1,
            "itid": 1, "item_name": "طعمية", "list_price": 50.0, "qty": 2.0,
            "line_total": 100.0, "sold_at": "2026-06-30T20:00:00"}],
        "cash_counts": [],
        "pos_users": [{"tenant_id": TID, "source_id": SID, "uid": 1, "name": "أحمد"}],
        "pos_products": [{"tenant_id": TID, "source_id": SID,
                          "itid": 1, "is_modifier": False}],
        "internal_anomalies": [],
    }
    if reports is not None:
        base["shift_reports"] = reports
    if outbox is not None:
        base["outbox"] = outbox
    return base


def obox(id, kind="shift_report", recipient="111",
         dedup="shift_report:2026-06-30:evening", body="تقرير الوردية",
         status="pending", attempts=0, created_at="2026-06-30T20:00:00+00:00"):
    return {"id": id, "tenant_id": TID, "channel": "telegram",
            "recipient": recipient, "kind": kind, "body": body,
            "dedup_key": dedup, "status": status, "attempts": attempts,
            "created_at": created_at}


# ════════════════════════════════════════════════════════════════
# loud configuration failures
# ════════════════════════════════════════════════════════════════

def test_missing_supabase_url_is_loud(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        delivery.main(["--tenant-id", TID, "--source-id", SID])
    assert "SUPABASE_URL" in str(exc.value)
    assert "GitHub Secret" in str(exc.value)


def test_missing_telegram_token_is_loud_even_in_dry_run(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", FAKE_URL)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", FAKE_KEY)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        delivery.main(["--tenant-id", TID, "--source-id", SID, "--dry-run"],
                      client=FakeCloud(tables=_canned()))
    assert "TELEGRAM_BOT_TOKEN" in str(exc.value)


def test_missing_tenant_id_is_loud(monkeypatch):
    set_fake_env(monkeypatch)
    monkeypatch.delenv("TENANT_ID", raising=False)
    with pytest.raises(SystemExit) as exc:
        delivery.main(["--source-id", SID], client=FakeCloud(tables=_canned()))
    assert "TENANT_ID" in str(exc.value)


# ════════════════════════════════════════════════════════════════
# dry run — build and print, write nothing
# ════════════════════════════════════════════════════════════════

def test_dry_run_builds_and_prints_but_writes_nothing(monkeypatch, capsys):
    set_fake_env(monkeypatch)
    now = utc(2026, 7, 1, 4, 10)
    client = FakeCloud(tables=_canned(now, outbox=[obox(1)]))
    rc = delivery.main(["--tenant-id", TID, "--source-id", SID, "--dry-run"],
                       client=client, now_utc=now)
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out
    assert "NOTHING is enqueued" in out
    assert "shift report" in out and "2026-06-30 evening" in out
    assert "grand_total=100.0" in out
    assert "تقرير نهاية الوردية" in out          # the full Arabic report body
    assert "would-send" in out                  # notifier dry-run view
    # nothing was written anywhere — not even an outbox claim
    writes = [c for c in client.calls if c[0] in ("insert_ignore", "insert", "update")]
    assert writes == []
    assert client.stores["outbox"][1]["status"] == "pending"
    assert client.anomalies == []


def test_dry_run_never_contacts_telegram(monkeypatch):
    set_fake_env(monkeypatch)
    now = utc(2026, 7, 1, 4, 10)
    client = FakeCloud(tables=_canned(now, outbox=[obox(1)]))
    session = FakeTelegram()
    delivery.main(["--tenant-id", TID, "--source-id", SID, "--dry-run"],
                  client=client, now_utc=now, session=session, sleep=session.sleep)
    assert session.calls == []                  # Telegram never contacted


# ════════════════════════════════════════════════════════════════
# live mode — orchestrator enqueues, notifier claims and sends
# ════════════════════════════════════════════════════════════════

def test_live_run_orchestrator_then_notifier_end_to_end(monkeypatch, capsys):
    set_fake_env(monkeypatch)
    now = utc(2026, 7, 1, 4, 10)
    client = FakeCloud(tables=_canned(now), counts={"outbox": 0})
    session = FakeTelegram([_tg_ok(101), _tg_ok(102)])
    rc = delivery.main(["--tenant-id", TID, "--source-id", SID],
                       client=client, now_utc=now, session=session,
                       sleep=session.sleep)
    assert rc == 0
    assert "LIVE" in capsys.readouterr().out

    # orchestrator side: exactly one shift_report row, constraint-keyed
    assert len(client.stores["shift_reports"]) == 1
    sr = list(client.stores["shift_reports"].values())[0]
    assert sr["shift_name"] == "evening"
    assert sr["grand_total"] == 100.0

    # notifier side: the enqueued rows were claimed and marked sent
    shift_rows = [r for r in client.stores["outbox"].values()
                  if r["kind"] == "shift_report"]
    assert len(shift_rows) == 1
    assert shift_rows[0]["status"] == "sent"
    assert shift_rows[0]["sent_at"] == "2026-07-01T04:10:00+00:00"
    # July 1 is day 1 of the local month → the monthly products report is
    # enqueued alongside the shift report, and it is sent too
    monthly = [r for r in client.stores["outbox"].values()
               if r["kind"] == "monthly_products"]
    assert len(monthly) == 1
    assert monthly[0]["status"] == "sent"
    # Telegram: both deliveries went to the dev chat (the only eligible
    # recipient — the owner is not in this canned tenant)
    assert len(session.calls) == 2
    assert all(c[1]["chat_id"] == "111" for c in session.calls)
    assert "تقرير نهاية الوردية" in session.calls[0][1]["text"]


def test_live_double_run_is_idempotent(monkeypatch):
    """Two delivery.py passes → no duplicate report and no re-send.

    Run 1 reports the evening shift. Every OTHER closed shift in the 14-day
    lookback is already reported, so run 2 can build nothing new; the
    monthly row is blocked by the outbox UNIQUE (already sent), and the
    notifier claims nothing. The database constraints are the arbiter —
    this is the same claim test_orchestrator makes at the row level,
    here proven through the real cloud entry point.
    """
    set_fake_env(monkeypatch)
    now = utc(2026, 7, 1, 4, 10)
    d0 = _dt.date(2026, 6, 17)                     # 14-day lookback start
    seeded = []
    d = d0
    while d <= _dt.date(2026, 7, 1):
        for name in ("morning", "evening"):
            if (d, name) != (_dt.date(2026, 6, 30), "evening"):
                seeded.append({"tenant_id": TID, "source_id": SID,
                               "shift_date": d.isoformat(),
                               "shift_name": name})
        d += _dt.timedelta(days=1)
    client = FakeCloud(tables=_canned(now, reports=seeded),
                       counts={"outbox": 0})
    # The fake store is seeded with every already-reported shift in the
    # 14-day lookback, so "one new report" is a DELTA against that seeded
    # baseline — never a magic 1. FakeCloud copies seeded reads into
    # stores, so the baseline is captured here, before either run.
    n_reports_seeded = len(client.stores["shift_reports"])
    assert n_reports_seeded >= 1          # the seed really is populated
    session1 = FakeTelegram([_tg_ok(), _tg_ok()])
    delivery.main(["--tenant-id", TID, "--source-id", SID],
                  client=client, now_utc=now, session=session1,
                  sleep=session1.sleep)
    # run 1 produced exactly ONE new shift_report row (baseline + 1)
    assert len(client.stores["shift_reports"]) == n_reports_seeded + 1
    session2 = FakeTelegram()
    delivery.main(["--tenant-id", TID, "--source-id", SID],
                  client=client, now_utc=now, session=session2,
                  sleep=session2.sleep)
    # run 2 added nothing: the row count did not increase, the only
    # shift_report outbox row is run 1's, the monthly row is the outbox
    # UNIQUE's single survivor, and the second pass claimed and sent
    # nothing — the DB constraints are the arbiter, exactly as in the
    # real system. The same report was never produced twice.
    assert len(client.stores["shift_reports"]) == n_reports_seeded + 1
    shift_rows = [r for r in client.stores["outbox"].values()
                  if r["kind"] == "shift_report"]
    assert len(shift_rows) == 1
    assert shift_rows[0]["dedup_key"] == "shift_report:2026-06-30:evening"
    assert shift_rows[0]["status"] == "sent"
    monthly_rows = [r for r in client.stores["outbox"].values()
                    if r["kind"] == "monthly_products"]
    assert len(monthly_rows) == 1
    assert monthly_rows[0]["status"] == "sent"
    # the second pass claimed and sent nothing
    assert session1.calls and session2.calls == []


# ════════════════════════════════════════════════════════════════
# flag passthrough
# ════════════════════════════════════════════════════════════════

def test_force_shift_and_shift_date_reach_the_orchestrator(monkeypatch, capsys):
    set_fake_env(monkeypatch)
    now = utc(2026, 7, 1, 4, 10)
    client = FakeCloud(tables=_canned(now))
    delivery.main(["--tenant-id", TID, "--source-id", SID, "--dry-run",
                   "--force-shift", "morning", "--shift-date", "2026-07-02"],
                  client=client, now_utc=now)
    out = capsys.readouterr().out
    assert "2026-07-02 morning" in out


def test_invalid_shift_date_is_loud(monkeypatch):
    with pytest.raises(SystemExit):
        delivery.main(["--tenant-id", TID, "--source-id", SID, "--dry-run",
                       "--shift-date", "2026/07/02"])


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


def _local_modules(name: str) -> list[Path]:
    """Resolve a top-level import name to repo source files (module/package)."""
    mod = REPO / f"{name}.py"
    if mod.exists():
        return [mod]
    pkg = REPO / name
    if (pkg / "__init__.py").exists():
        return sorted(pkg.glob("*.py"))
    return []


def _transitive_imports(entry: Path) -> set[str]:
    seen: set[Path] = set()
    stack = [entry]
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in _module_names(path):
            for p in _local_modules(name):
                if p not in seen:
                    stack.append(p)
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
    mods = _transitive_imports(REPO / "delivery.py")
    assert not (forbidden & mods), f"delivery closure imports {forbidden & mods}"
    # the composition root really does wire the two halves
    assert "orchestrator" in mods
    assert "notifier" in mods


# ════════════════════════════════════════════════════════════════
# workflow YAML — mechanical enforcement of the Task 4 spec
#
# GitHub's own parser is the ultimate syntax check and cannot run
# locally, so these tests pin the CONTENT the spec demands, with no
# dependency: cron */15, workflow_dispatch inputs (dry_run default
# true, force_shift none|morning|evening, shift_date validated), the
# five secrets referenced by NAME ONLY, and the fail-loudly guard.
# ════════════════════════════════════════════════════════════════

WORKFLOW = REPO / ".github" / "workflows" / "delivery.yml"
REQUIRED_SECRETS = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY",
                    "TELEGRAM_BOT_TOKEN", "TENANT_ID", "SOURCE_ID")


def _workflow_text() -> str:
    assert WORKFLOW.exists(), f"missing {WORKFLOW.relative_to(REPO)}"
    return WORKFLOW.read_text(encoding="utf-8")


def _block(text: str, start: str, end: str | None = None) -> str:
    """Lines from the one containing `start` up to (not incl.) `end`.

    Scopes assertions to a YAML input block so a later sibling input
    cannot make them pass vacuously.
    """
    out: list[str] = []
    seen = False
    for line in text.splitlines():
        if not seen:
            if start in line:
                seen = True
                out.append(line)
            continue
        if end is not None and end in line:
            break
        out.append(line)
    return "\n".join(out)


def _assert_cron_spec(text: str) -> None:
    assert "on:" in text and "schedule:" in text and "cron:" in text
    assert "'*/15 * * * *'" in text


def _assert_dry_run_spec(text: str) -> None:
    # scoped to the dry_run input block: a later sibling boolean input
    # with default true must not make these assertions pass vacuously
    assert "workflow_dispatch:" in text and "inputs:" in text
    dry = _block(text, "dry_run:", "force_shift:")
    assert "type: boolean" in dry
    assert "default: true" in dry


def _assert_force_shift_spec(text: str) -> None:
    # scoped to the force_shift block — the run step's "!= none" is not
    # evidence that the input options exist
    fs = _block(text, "force_shift:", "shift_date:")
    assert "type: choice" in fs
    assert "- none" in fs and "morning" in fs and "evening" in fs
    # the job validates shift_date before it reaches delivery.py
    assert re.search(r"shift_date must be YYYY-MM-DD", text)


def _assert_no_hardcoded_env_values(text: str) -> None:
    """Every uppercase env assignment is one of the five secrets (assigned
    exactly its secret) or an `inputs` expression — a literal credential
    under ANY env name fails here, not just the five known names."""
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Z][A-Z0-9_]*):\s*(.*)$", line)
        if m:
            name, value = m.group(1), m.group(2)
            if name in REQUIRED_SECRETS:
                assert value == f"${{{{ secrets.{name} }}}}", \
                    f"{name} assigned something other than its secret: {value!r}"
            else:
                assert value.startswith("${{ inputs."), \
                    f"{name} carries a non-secret, non-input value: {value!r}"


def test_workflow_schedules_every_15_minutes():
    _assert_cron_spec(_workflow_text())


def test_workflow_dispatch_inputs_dry_run_defaults_true():
    _assert_dry_run_spec(_workflow_text())


def test_workflow_force_shift_and_shift_date_inputs():
    _assert_force_shift_spec(_workflow_text())


def test_workflow_secrets_by_name_only():
    text = _workflow_text()
    # every secret reference is an expression on the exact five names
    refs = re.findall(r"\$\{\{\s*secrets\.(\w+)\s*\}\}", text)
    assert set(refs) == set(REQUIRED_SECRETS), f"unexpected secret names: {refs}"
    assert len(refs) == len(REQUIRED_SECRETS) * 2, \
        f"expected both env blocks to carry all 5 secrets, got {len(refs)} refs"
    # and no env line anywhere carries a literal value
    _assert_no_hardcoded_env_values(text)


def test_workflow_fails_loudly_and_is_isolated():
    text = _workflow_text()
    # a delivery system that fails silently is worse than one that does
    # not exist — the run steps must not swallow errors
    assert "set -euo pipefail" in text
    assert "permissions:" in text and "contents: read" in text
    assert "concurrency:" in text
    assert "cancel-in-progress: false" in text
    # the standard job skeleton is present: checkout, python 3.12,
    # cloud deps, the delivery command
    assert "actions/checkout@v4" in text
    assert "actions/setup-python@v5" in text
    assert "pip install -r requirements-cloud.txt" in text
    assert "python delivery.py" in text


def test_workflow_spec_checks_can_fire():
    """Falsifier: the same predicates must FAIL on a corrupted copy.

    Every proof needs a paired falsifier (project discipline) — these
    checks only mean something if they can actually fire. The real file
    passes; each mutated copy must trip its predicate.
    """
    text = _workflow_text()
    _assert_cron_spec(text)
    _assert_dry_run_spec(text)
    _assert_force_shift_spec(text)
    _assert_no_hardcoded_env_values(text)
    # wrong cron
    with pytest.raises(AssertionError):
        _assert_cron_spec(text.replace("'*/15 * * * *'", "'*/30 * * * *'"))
    # dry_run default flipped (first occurrence is the dry_run block)
    with pytest.raises(AssertionError):
        _assert_dry_run_spec(text.replace("default: true", "default: false", 1))
    # force_shift options corrupted
    with pytest.raises(AssertionError):
        _assert_force_shift_spec(text.replace("morning", "midday", 1))
    # a hardcoded credential injected under a NEW env name
    with pytest.raises(AssertionError):
        _assert_no_hardcoded_env_values(
            text.replace("SHIFT_DATE: ${{ inputs.shift_date }}",
                         "SHIFT_DATE: ${{ inputs.shift_date }}\n"
                         "          MY_TOKEN: abc-123"))
