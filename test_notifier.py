# -*- coding: utf-8 -*-
"""
test_notifier.py — Phase 2 notifier contract tests.

No network. run() is driven through a fake Supa client whose outbox store
honours the real schema's status lifecycle (pending|failed = claimable,
sending = in flight, sent/dead = terminal), and through a scripted fake
Telegram session that returns canned status codes or raises transport
errors, recording every call and sleep.

The failure shape this project keeps hitting is "a check that shares the
fault it is meant to detect". The no-pyodbc closure test therefore has a
paired falsifier that proves the walker can fail before the main test
proves the tree is clean.
"""

from __future__ import annotations

import ast
import datetime as _dt
import json
from pathlib import Path

import pytest
import requests

import notifier.telegram as TG
import orchestrator as ORCH
import supa

TID = "57b61b47-a590-49fe-803c-0c174a07b7ec"
REPO = Path(__file__).resolve().parent

# A fake test token. It must never be a real credential — and the tests
# prove the code never lets even this one reach a log or the DB.
TOKEN = "1234567890:FAKE-TEST-TOKEN"


def utc(y, mo, d, h, mi=0):
    return _dt.datetime(y, mo, d, h, mi, tzinfo=_dt.timezone.utc)


# ════════════════════════════════════════════════════════════════
# fakes
# ════════════════════════════════════════════════════════════════

def _eq_clause(row: dict, clause: str) -> bool:
    parts = clause.split(".", 2)          # status.eq.pending
    if len(parts) < 3:
        return False
    key, op, val = parts
    if op == "eq":
        return str(row.get(key)) == val
    if op == "is":
        return row.get(key) is None
    return False


def _match(row: dict, filters: dict) -> bool:
    for k, v in filters.items():
        if k == "order":
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


class FakeClient:
    """Serves canned reads/counts; outbox writes land in a status-store."""

    def __init__(self, tables=None, counts=None):
        self.reads = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.counts = dict(counts or {})
        self.outbox = {}
        self.anomalies = []
        self.calls = []
        for r in (tables or {}).get("outbox", []):
            self.outbox[r["id"]] = dict(r)

    def select(self, table, params=None, paginate=True):
        self.calls.append(("select", table))
        params = params or {}
        rows = [dict(r) for r in self.reads.get(table, [])
                if _match(r, params)]
        if params.get("order") == "created_at.asc":
            rows.sort(key=lambda r: r.get("created_at", ""))
        return rows

    def count(self, table, params=None):
        self.calls.append(("count", table))
        return self.counts.get(table, 0)

    def insert(self, table, rows, returning=True):
        self.calls.append(("insert", table))
        if table == "internal_anomalies":
            self.anomalies.extend(dict(r) for r in rows)
            return []
        return [dict(r) for r in rows] if returning else []

    def update(self, table, filters, patch, returning=True):
        self.calls.append(("update", table))
        matched = [r for r in self.outbox.values() if _match(r, filters)]
        if filters.get("order") == "created_at.asc":
            matched.sort(key=lambda r: r.get("created_at", ""))
        for r in matched:
            r.update(patch)
        return [dict(r) for r in matched] if returning else []


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)
        self.headers = {}

    def json(self):
        return self._body


def _telegram_ok():
    return _Resp(200, {"ok": True, "result": {"message_id": 42}})


def _telegram_429(retry_after=7):
    return _Resp(429, {"ok": False, "error_code": 429,
                       "description": "Too Many Requests",
                       "parameters": {"retry_after": retry_after}})


def _telegram_400():
    return _Resp(400, {"ok": False, "error_code": 400,
                       "description": "Bad Request: chat not found"})


def _telegram_403():
    return _Resp(403, {"ok": False, "error_code": 403,
                       "description": "Forbidden: bot was blocked by the user"})


def _telegram_500():
    return _Resp(500, {"ok": False, "error_code": 500,
                       "description": "Internal Server Error"})


class FakeTelegram:
    """Scripted sendMessage responses; records calls and sleeps."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.sleeps = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, dict(json) if json else None))
        if not self.responses:
            return _telegram_ok()
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def sleep(self, seconds):
        self.sleeps.append(seconds)


# ════════════════════════════════════════════════════════════════
# builders
# ════════════════════════════════════════════════════════════════

def obox(id, kind="shift_report", recipient="111",
         dedup="shift_report:2026-06-30:evening", body="تقرير الوردية",
         status="pending", attempts=0, created_at="2026-06-30T20:00:00+00:00",
         last_error=None):
    return {"id": id, "tenant_id": TID, "channel": "telegram",
            "recipient": recipient, "kind": kind, "body": body,
            "dedup_key": dedup, "status": status, "attempts": attempts,
            "created_at": created_at, "last_error": last_error}


def dev_recipient(**kw):
    kw.setdefault("channel", "telegram")
    kw.setdefault("address", "111")
    kw.setdefault("label", "dev")
    kw.setdefault("active", True)
    kw.setdefault("notify_before_golive", True)
    return kw


def owner_recipient(**kw):
    kw.setdefault("channel", "telegram")
    kw.setdefault("address", "222")
    kw.setdefault("label", "owner")
    kw.setdefault("active", True)
    kw.setdefault("notify_before_golive", False)
    return kw


def alert_settings(notify=True):
    return [{"alert_type": t, "detect": True, "notify": notify}
            for t in ("zero_invoice", "refund", "cash_diff",
                      "deleted_invoice", "no_sales")]


def tables(go_live=None, recipients=None, settings=None, outbox=None):
    """Canned context. Rows carry their real key columns so the fake
    client's filter-honouring select() matches them like PostgREST would."""
    rcpts = recipients if recipients is not None else [dev_recipient()]
    sett = settings if settings is not None else alert_settings()
    return {
        "tenants": [{"id": TID, "timezone": "Africa/Cairo",
                     "shift_morning_start": "07:00:00",
                     "shift_evening_start": "19:00:00", "go_live_at": go_live}],
        "recipients": [{**r, "tenant_id": TID} for r in rcpts],
        "alert_settings": [{**s, "tenant_id": TID} for s in sett],
        "outbox": outbox or [],
    }


def deliver(tbl=None, responses=None, counts=None, now=None, **run_kw):
    """One run() over canned state with a scripted Telegram session."""
    session = FakeTelegram(responses or [])
    client = FakeClient(tables=tbl, counts=counts or {})
    summary = TG.run(client, tenant_id=TID, token=TOKEN,
                     now_utc=now or utc(2026, 7, 1, 4, 10),
                     session=session, sleep=session.sleep, **run_kw)
    return summary, session, client


# ════════════════════════════════════════════════════════════════
# masking, redaction, the 4096 policy
# ════════════════════════════════════════════════════════════════

def test_mask_chat_id_hides_the_middle():
    assert TG.mask_chat_id("9876543210") == "98…10"
    assert "9876543210" not in TG.mask_chat_id("9876543210")


def test_redact_removes_the_token_everywhere():
    text = f"connection failed for bot{TOKEN}/sendMessage token={TOKEN}"
    assert TOKEN not in TG.redact(text, TOKEN)
    assert "***" in TG.redact(text, TOKEN)


def test_short_body_is_untouched_by_4096_policy():
    body = "تقرير" * 100                       # ~300 units, well under
    out, truncated = TG.apply_4096_policy(body)
    assert out == body
    assert truncated is False


def test_long_body_is_truncated_with_marker_within_limit():
    body = "أ" * 5000                          # 5000 units > 4096
    out, truncated = TG.apply_4096_policy(body)
    assert truncated is True
    assert TG._utf16_units(out) <= 4096
    assert TG.TRUNCATION_MARKER in out
    assert out != body


def test_emoji_heavy_body_counts_utf16_units_not_code_points():
    body = "😀" * 3000                          # 3000 code points, 6000 units
    out, truncated = TG.apply_4096_policy(body)
    assert truncated is True
    assert TG._utf16_units(out) <= 4096


# ════════════════════════════════════════════════════════════════
# claim semantics — claim before send, so a crash cannot re-send
# ════════════════════════════════════════════════════════════════

def test_claim_touches_only_pending_and_failed():
    tbl = tables(outbox=[
        obox(1, status="pending"),
        obox(2, status="failed", attempts=1),
        obox(3, status="sending"),             # crashed mid-send last run
        obox(4, status="sent"),
        obox(5, status="dead", attempts=3),
    ])
    summary, session, client = deliver(tbl, responses=[_telegram_ok(),
                                                       _telegram_ok()])
    assert summary.claimed == 2                # only rows 1 and 2
    assert summary.sent == 2
    assert client.outbox[1]["status"] == "sent"
    assert client.outbox[2]["status"] == "sent"
    # the in-flight / terminal rows were never touched by the claim
    assert client.outbox[3]["status"] == "sending"
    assert client.outbox[4]["status"] == "sent"
    assert client.outbox[5]["status"] == "dead"
    assert client.outbox[5]["attempts"] == 3


def test_sending_row_is_never_reclaimed_and_never_resends():
    tbl = tables(outbox=[obox(1, status="sending")])   # crash left it in flight
    summary, session, client = deliver(tbl)
    assert summary.claimed == 0
    assert summary.sent == 0
    assert session.calls == []
    assert client.outbox[1]["status"] == "sending"


def test_success_marks_sent_with_sent_at():
    now = utc(2026, 7, 1, 4, 10)
    tbl = tables(outbox=[obox(1)])
    summary, session, client = deliver(tbl, responses=[_telegram_ok()], now=now)
    assert summary.sent == 1
    assert client.outbox[1]["status"] == "sent"
    assert client.outbox[1]["sent_at"] == "2026-07-01T04:10:00+00:00"
    assert session.calls[0][1]["chat_id"] == "111"
    assert session.calls[0][1]["text"] == "تقرير الوردية"


# ════════════════════════════════════════════════════════════════
# gates — active → go_live (bypassed by notify_before_golive) → notify
# ════════════════════════════════════════════════════════════════

def test_inactive_recipient_row_is_blocked():
    tbl = tables(recipients=[dev_recipient(active=False)],
                 outbox=[obox(1)])
    summary, session, client = deliver(tbl)
    assert summary.gate_blocked == 1
    assert summary.sent == 0
    assert session.calls == []
    assert client.outbox[1]["status"] == "failed"
    assert "inactive" in client.outbox[1]["last_error"]


def test_owner_blocked_pre_go_live_while_dev_bypass_sends():
    tbl = tables(go_live=None,
                 recipients=[dev_recipient(), owner_recipient()],
                 outbox=[obox(1, recipient="111"),
                         obox(2, recipient="222",
                              dedup="shift_report:2026-06-30:morning")])
    summary, session, client = deliver(tbl, responses=[_telegram_ok()])
    assert summary.sent == 1                    # only the dev
    assert summary.gate_blocked == 1
    assert session.calls[0][1]["chat_id"] == "111"
    assert "pre-go-live" in client.outbox[2]["last_error"]


def test_recipient_row_missing_is_blocked():
    tbl = tables(outbox=[obox(1, recipient="999")])
    summary, session, client = deliver(tbl)
    assert summary.gate_blocked == 1
    assert session.calls == []


def test_alert_notify_off_blocks_alert_row():
    tbl = tables(settings=alert_settings(notify=False),
                 outbox=[obox(1, kind="alert", dedup="alert:refund:0",
                              body="تنبيه")])
    summary, session, client = deliver(tbl)
    assert summary.gate_blocked == 1
    assert session.calls == []
    assert "notify" in client.outbox[1]["last_error"]


def test_alert_with_notify_on_is_sent():
    tbl = tables(outbox=[obox(1, kind="alert", dedup="alert:refund:0",
                              body="تنبيه مرتجع")])
    summary, session, client = deliver(tbl, responses=[_telegram_ok()])
    assert summary.sent == 1
    assert session.calls[0][1]["text"] == "تنبيه مرتجع"


# ════════════════════════════════════════════════════════════════
# retry and classification — 429/5xx retried, 400/403 never
# ════════════════════════════════════════════════════════════════

def test_429_honours_retry_after_then_sends():
    tbl = tables(outbox=[obox(1)])
    summary, session, client = deliver(tbl, responses=[_telegram_429(retry_after=7),
                                                       _telegram_ok()])
    assert summary.sent == 1
    assert session.sleeps == [7.0]              # honoured retry_after exactly
    assert len(session.calls) == 2


def test_5xx_backs_off_then_sends():
    tbl = tables(outbox=[obox(1)])
    summary, session, client = deliver(tbl, responses=[_telegram_500(),
                                                       _telegram_ok()])
    assert summary.sent == 1
    assert session.sleeps == [0.5]              # first backoff step
    assert len(session.calls) == 2


def test_400_is_permanent_and_never_retried():
    tbl = tables(outbox=[obox(1)])
    summary, session, client = deliver(tbl, responses=[_telegram_400()])
    assert summary.failed == 1
    assert len(session.calls) == 1              # exactly one call — no retry
    assert client.outbox[1]["attempts"] == 1
    assert client.outbox[1]["status"] == "failed"
    assert "400" in client.outbox[1]["last_error"]


def test_403_is_permanent_distinct_and_records_anomaly():
    tbl = tables(outbox=[obox(1)])
    client = FakeClient(tables=tbl)
    session = FakeTelegram([_telegram_403()])
    summary = TG.run(client, tenant_id=TID, token=TOKEN,
                     now_utc=utc(2026, 7, 1, 4, 10),
                     session=session, sleep=session.sleep)
    assert summary.telegram_403 == 1
    assert summary.failed == 1
    assert len(session.calls) == 1              # never retried
    assert len(client.anomalies) == 1
    a = client.anomalies[0]
    assert a["kind"] == "telegram_403"
    assert a["tenant_id"] == TID
    detail = json.loads(a["detail"])
    assert detail["recipient"] == "****"        # chat id never in full
    assert "111" not in json.dumps(detail)
    assert "blocked" in detail["error"]


def test_403_and_transport_take_distinct_paths_through_run():
    """403 and a network failure must never be confused end to end."""
    # 403: exactly one call (never retried), telegram_403 anomaly recorded.
    c1 = FakeClient(tables=tables(outbox=[obox(1)]))
    s1 = FakeTelegram([_telegram_403()])
    r1 = TG.run(c1, tenant_id=TID, token=TOKEN,
                now_utc=utc(2026, 7, 1, 4, 10),
                session=s1, sleep=s1.sleep)
    # transport: bounded retries inside the run, no anomaly.
    conn = requests.exceptions.ConnectionError("network down")
    c2 = FakeClient(tables=tables(outbox=[obox(1)]))
    s2 = FakeTelegram([conn, conn, conn])
    r2 = TG.run(c2, tenant_id=TID, token=TOKEN,
                now_utc=utc(2026, 7, 1, 4, 10),
                session=s2, sleep=s2.sleep)
    assert r1.telegram_403 == 1 and len(s1.calls) == 1   # 403: distinct
    assert r2.telegram_403 == 0 and len(s2.calls) == 3   # transport: distinct
    assert len(c1.anomalies) == 1 and len(c2.anomalies) == 0


def test_transport_error_retries_within_run_then_fails():
    tbl = tables(outbox=[obox(1)])
    conn = requests.exceptions.ConnectionError(
        f"Connection refused to https://api.telegram.org/bot{TOKEN}/sendMessage")
    summary, session, client = deliver(tbl, responses=[conn, conn, conn])
    assert summary.failed == 1
    assert len(session.calls) == 3              # bounded retries inside run
    assert session.sleeps == [0.5, 1.0]         # backoff 0.5, 1.0
    assert client.outbox[1]["status"] == "failed"
    assert client.outbox[1]["attempts"] == 1    # the whole run = 1 attempt
    assert TOKEN not in (client.outbox[1]["last_error"] or "")  # redacted
    assert TOKEN not in "\n".join(summary.lines)


def test_attempts_reach_max_then_dead_and_never_claimed_again():
    tbl = tables(outbox=[obox(1)])
    statuses: list[str] = []
    dead_counts: list[int] = []
    for _ in range(4):
        client = FakeClient(tables=tbl)
        session = FakeTelegram([_telegram_400()])
        summary = TG.run(client, tenant_id=TID, token=TOKEN,
                         now_utc=utc(2026, 7, 1, 4, 10),
                         session=session, sleep=session.sleep)
        statuses.append(client.outbox[1]["status"])
        dead_counts.append(summary.dead)
        tbl["outbox"] = [client.outbox[1]]       # persist state between runs
    assert statuses == ["failed", "failed", "dead", "dead"]
    assert dead_counts == [0, 0, 1, 0]           # dead recorded only in run 3
    assert summary.claimed == 0                  # the dead row was never claimed


# ════════════════════════════════════════════════════════════════
# daily cap backlog — the sender, not the orchestrator, is the last line
# ════════════════════════════════════════════════════════════════

def test_cap_exhausted_defers_alerts_but_shift_report_still_sends():
    alerts = [obox(i, kind="alert", dedup=f"alert:refund:{i}",
                   created_at=f"2026-06-30T2{i}:00:00+00:00")
              for i in range(1, 4)]
    tbl = tables(outbox=alerts + [obox(9, kind="shift_report")])
    summary, session, client = deliver(tbl, counts={"outbox": 3},
                                       responses=[_telegram_ok()])
    assert summary.sent == 1                    # only the shift report
    assert summary.cap_deferred == 3
    for i in range(3):
        assert client.outbox[i + 1]["status"] == "pending"   # deferred, not failed
        assert client.outbox[i + 1]["attempts"] == 0


def test_cap_room_sends_oldest_alerts_first():
    alerts = [obox(1, kind="alert", dedup="alert:refund:0",
                   created_at="2026-06-30T20:00:00+00:00"),
              obox(2, kind="alert", dedup="alert:refund:1",
                   created_at="2026-06-30T21:00:00+00:00"),
              obox(3, kind="alert", dedup="alert:refund:2",
                   created_at="2026-06-30T22:00:00+00:00")]
    tbl = tables(outbox=alerts)
    summary, session, client = deliver(tbl, counts={"outbox": 1},
                                       responses=[_telegram_ok(), _telegram_ok()])
    assert summary.sent == 2
    assert summary.cap_deferred == 1
    assert client.outbox[1]["status"] == "sent"             # oldest two sent
    assert client.outbox[2]["status"] == "sent"
    assert client.outbox[3]["status"] == "pending"          # newest deferred


def test_cap_room_is_zero_when_over_cap():
    tbl = tables(outbox=[obox(1, kind="alert", dedup="alert:refund:0")])
    summary, session, client = deliver(tbl, counts={"outbox": 5})
    assert summary.sent == 0
    assert summary.cap_deferred == 1


# ════════════════════════════════════════════════════════════════
# dry run — build and print, claim/send nothing
# ════════════════════════════════════════════════════════════════

def test_dry_run_claims_and_sends_nothing():
    tbl = tables(outbox=[obox(1),
                         obox(2, kind="alert", dedup="alert:refund:0")])
    summary, session, client = deliver(tbl, dry_run=True)
    assert summary.dry_run is True
    assert summary.claimed == 2
    assert summary.sent == 0
    assert session.calls == []                  # Telegram never contacted
    assert all(r["status"] == "pending" for r in client.outbox.values())
    assert any("would-send" in line for line in summary.lines)


def test_dry_run_shows_gate_blocked_rows_without_sending():
    tbl = tables(recipients=[dev_recipient(active=False)],
                 outbox=[obox(1)])
    summary, session, client = deliver(tbl, dry_run=True)
    assert session.calls == []
    assert any("blocked" in line for line in summary.lines)


# ════════════════════════════════════════════════════════════════
# fail loudly
# ════════════════════════════════════════════════════════════════

def test_run_raises_when_tenant_row_is_missing():
    client = FakeClient(tables={"tenants": [], "recipients": [],
                                "alert_settings": []})
    session = FakeTelegram([])
    with pytest.raises(supa.SupaError):
        TG.run(client, tenant_id=TID, token=TOKEN,
               now_utc=utc(2026, 7, 1, 4, 10),
               session=session, sleep=session.sleep)


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


def test_no_pyodbc_in_notifier_import_closure():
    forbidden = {"pyodbc", "adapter_hdsoft", "agent", "fake_adapter", "sqlguard"}
    mods = _transitive_imports(REPO / "notifier" / "telegram.py")
    assert not (forbidden & mods), f"notifier closure imports {forbidden & mods}"
    # the delivery closure must stay cloud-side; the orchestrator's closure
    # is itself tested by test_orchestrator.py — gates come from one source
    assert "orchestrator" in mods


# ════════════════════════════════════════════════════════════════
# full pass against a stateful store (run-level integration)
# ════════════════════════════════════════════════════════════════

def test_full_pass_pipeline():
    tbl = tables(outbox=[
        obox(1, kind="shift_report"),
        obox(2, kind="alert", dedup="alert:refund:0"),
        obox(3, kind="monthly_products", dedup="monthly:2026-06"),
    ])
    summary, session, client = deliver(tbl, responses=[_telegram_ok()] * 3)
    assert summary.claimed == 3
    assert summary.sent == 3
    assert summary.failed == 0
    assert summary.cap_deferred == 0
    assert all(r["status"] == "sent" for r in client.outbox.values())
    assert [c[1]["chat_id"] for c in session.calls] == ["111"] * 3   # FIFO


def test_orchestrator_enqueues_then_notifier_delivers():
    """The Task 2 → Task 3 seam, end to end through the same fake store."""
    import test_orchestrator as TO

    client = TO.FakeClient(tables=TO._canned_morning(),
                           counts={"outbox": 0})
    ORCH.run(client, tenant_id=TO.TID, source_id=TO.SID,
             now_utc=utc(2026, 7, 1, 4, 10))
    # July 1 is day 1 of the local month, so the orchestrator also enqueues
    # the monthly_products row alongside the shift report.
    pending = next(r for r in client.store.outbox.values()
                   if r["kind"] == "shift_report")
    pending = {**pending, "id": 1}             # the real outbox has a bigserial id
    nclient = FakeClient(tables={
        "tenants": [{"id": TO.TID, "timezone": "Africa/Cairo",
                     "shift_morning_start": "07:00:00",
                     "shift_evening_start": "19:00:00", "go_live_at": None}],
        "recipients": [{**dev_recipient(), "tenant_id": TO.TID}],
        "alert_settings": [{**s, "tenant_id": TO.TID}
                            for s in alert_settings()],
        "outbox": [pending],
    }, counts={"outbox": 0})
    session = FakeTelegram([_telegram_ok()])
    summary = TG.run(nclient, tenant_id=TO.TID, token=TOKEN,
                     now_utc=utc(2026, 7, 1, 4, 10),
                     session=session, sleep=session.sleep)
    assert summary.sent == 1
    assert nclient.outbox[pending["id"]]["status"] == "sent"
    assert session.calls[0][1]["chat_id"] == "111"
    assert "تقرير نهاية الوردية" in session.calls[0][1]["text"]
