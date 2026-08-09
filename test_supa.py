# -*- coding: utf-8 -*-
"""
test_supa.py — contract tests for the Supabase REST client.

No network. A fake transport records every request and replays canned
responses, so the behaviour we actually depend on is pinned:

  • pagination past PostgREST's 1000-row default
  • a hard raise when a result sits exactly at the page limit un-paginated
  • retry/backoff on 5xx and 429, and *no* retry on other 4xx
  • batching at 500 rows, where a partial batch failure is a failure
  • the two-header pattern, and a token that never leaks into an error

The silent failure these guard against is truncation: a month of
invoice_lines cut to 1000 rows renders a wrong top-5 with no error at all.
"""

from __future__ import annotations

import pytest

import supa


# ════════════════════════════════════════════════════════════════
# fake transport
# ════════════════════════════════════════════════════════════════

class FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None, text=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []
        self.headers = headers or {}
        self.text = text if text is not None else ""

    def json(self):
        return self._json


class FakeSession:
    """Replays queued responses; records every call for assertions."""

    def __init__(self, responses):
        self.queued = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, headers=None, params=None,
                data=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "headers": headers or {}, "params": params or {},
                           "data": data})
        if not self.queued:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        nxt = self.queued.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeClock:
    def __init__(self):
        self.slept: list[float] = []

    def __call__(self, seconds):
        self.slept.append(seconds)


URL = "https://mwwjfeporhfhcekmektg.supabase.co"
ANON = "anon-key-placeholder"
TOKEN = "token-placeholder-do-not-leak"


def make(responses, **kw):
    clock = FakeClock()
    session = FakeSession(responses)
    client = supa.Supa(URL, anon_key=ANON, token=TOKEN,
                       session=session, sleep=clock, **kw)
    return client, session, clock


def rows(n, start=0):
    return [{"salid": start + i} for i in range(n)]


# ════════════════════════════════════════════════════════════════
# 1) transport safety
# ════════════════════════════════════════════════════════════════

def test_plain_http_is_rejected():
    """HTTPS/443 only — direct ports may be blocked on the customer network."""
    with pytest.raises(ValueError):
        supa.Supa("http://mwwjfeporhfhcekmektg.supabase.co",
                  anon_key=ANON, token=TOKEN)


def test_two_header_pattern():
    """apikey admits at the gateway; Bearer carries tenant_id for RLS."""
    client, session, _ = make([FakeResponse(200, [])])
    client.select("heartbeats")
    h = session.calls[0]["headers"]
    assert h["apikey"] == ANON
    assert h["Authorization"] == f"Bearer {TOKEN}"


def test_token_never_leaks_into_errors_or_repr():
    """Rule 9: never log secrets."""
    client, _, _ = make([FakeResponse(400, text=f"boom {TOKEN}")])
    assert TOKEN not in repr(client)
    with pytest.raises(supa.SupaError) as exc:
        client.select("heartbeats")
    assert TOKEN not in str(exc.value)


# ════════════════════════════════════════════════════════════════
# 2) 🔴 pagination — the silent truncation guard
# ════════════════════════════════════════════════════════════════

def test_select_paginates_beyond_one_thousand():
    """A month of invoice_lines is ~35k rows. PostgREST stops at 1000."""
    client, session, _ = make([
        FakeResponse(200, rows(1000, 0)),
        FakeResponse(200, rows(700, 1000)),
    ])
    out = client.select("invoice_lines")
    assert len(out) == 1700
    assert [r["salid"] for r in out[:2]] == [0, 1]
    assert out[-1]["salid"] == 1699
    assert len(session.calls) == 2
    assert session.calls[0]["headers"]["Range"] == "0-999"
    assert session.calls[1]["headers"]["Range"] == "1000-1999"


def test_select_stops_cleanly_on_exact_multiple_of_page():
    """1000 then empty — must terminate, not loop forever."""
    client, session, _ = make([
        FakeResponse(200, rows(1000)),
        FakeResponse(200, []),
    ])
    assert len(client.select("invoices")) == 1000
    assert len(session.calls) == 2


def test_unpaginated_result_at_page_limit_raises():
    """
    The exactly-at-limit assertion. A caller that opts out of pagination
    and receives precisely the page size cannot know whether it saw
    everything — that ambiguity must be loud, never assumed benign.
    """
    client, _, _ = make([FakeResponse(200, rows(1000))])
    with pytest.raises(supa.TruncationError):
        client.select("invoice_lines", paginate=False)


def test_unpaginated_result_below_page_limit_is_fine():
    client, _, _ = make([FakeResponse(200, rows(999))])
    assert len(client.select("invoice_lines", paginate=False)) == 999


# ════════════════════════════════════════════════════════════════
# 3) retry and backoff
# ════════════════════════════════════════════════════════════════

def test_retries_5xx_then_succeeds():
    client, session, clock = make([
        FakeResponse(503),
        FakeResponse(500),
        FakeResponse(200, rows(3)),
    ])
    assert len(client.select("invoices")) == 3
    assert len(session.calls) == 3
    assert len(clock.slept) == 2
    assert clock.slept[1] > clock.slept[0], "backoff must grow"


def test_429_honours_retry_after():
    client, _, clock = make([
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(200, []),
    ])
    client.select("invoices")
    assert clock.slept == [7.0]


def test_other_4xx_is_not_retried():
    """A 403 from RLS is an answer, not a hiccup. Retrying hides it."""
    client, session, _ = make([FakeResponse(403, text="permission denied")])
    with pytest.raises(supa.SupaError):
        client.select("invoices")
    assert len(session.calls) == 1


def test_gives_up_after_max_attempts():
    client, session, _ = make([FakeResponse(500) for _ in range(9)],
                              max_attempts=4)
    with pytest.raises(supa.SupaError):
        client.select("invoices")
    assert len(session.calls) == 4


# ════════════════════════════════════════════════════════════════
# 4) writes — batching, headers, partial failure
# ════════════════════════════════════════════════════════════════

def test_upsert_batches_at_five_hundred():
    client, session, _ = make([FakeResponse(201) for _ in range(3)])
    n = client.upsert("invoices", rows(1200), on_conflict="tenant_id,source_id,salid")
    assert n == 1200
    assert len(session.calls) == 3
    sizes = [len(supa._decode(c["data"])) for c in session.calls]
    assert sizes == [500, 500, 200]


def test_upsert_sends_merge_duplicates_and_on_conflict():
    client, session, _ = make([FakeResponse(201)])
    client.upsert("invoices", rows(1), on_conflict="tenant_id,source_id,salid")
    call = session.calls[0]
    assert "resolution=merge-duplicates" in call["headers"]["Prefer"]
    assert call["params"]["on_conflict"] == "tenant_id,source_id,salid"


def test_insert_ignore_uses_ignore_duplicates():
    """events are inserted 'on conflict do nothing'."""
    client, session, _ = make([FakeResponse(201)])
    client.insert_ignore("events", rows(1),
                         on_conflict="tenant_id,source_id,type,dedup_key")
    assert "resolution=ignore-duplicates" in session.calls[0]["headers"]["Prefer"]


def test_partial_batch_failure_is_a_failure():
    """
    'Uploaded 900 of 1,000 rows' does not pass. The caller must not be
    able to mistake a half-written upload for success — the watermark
    depends on this.
    """
    client, session, _ = make([
        FakeResponse(201),
        FakeResponse(400, text="bad payload"),
    ])
    with pytest.raises(supa.SupaError) as exc:
        client.upsert("invoices", rows(600), on_conflict="salid")
    assert "500" in str(exc.value), "must report how many rows landed"


def test_upsert_of_nothing_makes_no_request():
    client, session, _ = make([])
    assert client.upsert("invoices", [], on_conflict="salid") == 0
    assert session.calls == []


# ════════════════════════════════════════════════════════════════
# 5) columns the client must never send
# ════════════════════════════════════════════════════════════════

def test_generated_column_is_rejected_before_the_request():
    """
    pos_products.is_modifier is GENERATED ALWAYS AS (...) STORED
    (schema.sql:124). Any payload carrying it fails with 428C9 — and it
    would first bite at the agent's product sync, not here. Caught in the
    client so it cannot reach a live table at all.
    """
    client, session, _ = make([])
    with pytest.raises(supa.SupaError) as exc:
        client.upsert("pos_products",
                      [{"itid": 1, "itname": "طعمية", "is_modifier": False}],
                      on_conflict="tenant_id,source_id,itid")
    assert "is_modifier" in str(exc.value)
    assert session.calls == [], "must fail before any request is made"


def test_server_owned_columns_are_rejected():
    """
    first_seen_at must survive updates (it anchors the 30-minute deletion
    guard), and deleted_at belongs to the orchestrator. Sending either from
    a sync would quietly rewrite history.
    """
    client, _, _ = make([])
    for col in ("first_seen_at", "deleted_at"):
        with pytest.raises(supa.SupaError) as exc:
            client.upsert("invoices", [{"salid": 1, col: "2026-08-09T00:00:00Z"}],
                          on_conflict="salid")
        assert col in str(exc.value)


def test_clean_payload_is_untouched():
    client, session, _ = make([FakeResponse(201)])
    client.upsert("pos_products",
                  [{"itid": 1, "itname": "طعمية", "list_price": 15}],
                  on_conflict="tenant_id,source_id,itid")
    assert len(session.calls) == 1
    assert supa._decode(session.calls[0]["data"])[0]["itname"] == "طعمية"


def test_rejection_names_the_row_index():
    """One bad row in 500 must be findable."""
    client, _, _ = make([])
    payload = [{"itid": i} for i in range(5)]
    payload[3]["is_modifier"] = True
    with pytest.raises(supa.SupaError) as exc:
        client.upsert("pos_products", payload, on_conflict="itid")
    assert "3" in str(exc.value)
