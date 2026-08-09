# -*- coding: utf-8 -*-
"""
test_rows.py — the boundary between POS objects and Supabase payloads.

Two clocks meet here and must never mix:

    sold_at, counted_at   POS local wall time, `timestamp`, stored as-is
    last_seen_at, at      our time, `timestamptz`, aware UTC

Writing an aware datetime into a POS column means a UTC conversion happened
somewhere, which silently shifts every invoice by two or three hours and
lands them in the wrong shift. Writing a naive datetime into one of ours
means an unanchored timestamp. Both raise rather than guess.
"""

from __future__ import annotations

import datetime as dt

import pytest

import rows as R

TENANT = "57b61b47-a590-49fe-803c-0c174a07b7ec"
SOURCE = "93f8d146-ba68-4d58-8eda-f797f3e28bd4"
NAIVE = dt.datetime(2026, 8, 8, 19, 30, 15)
AWARE = dt.datetime(2026, 8, 8, 16, 30, 15, tzinfo=dt.timezone.utc)


class Inv:
    salid = 1001
    receipt_num = 5
    sold_at = NAIVE
    total = 145.5
    delivery_cost = 10.0
    user_uid = 2
    cust_code = 7
    sal_t = 1
    sal_type = 0
    sale_rtype = 0
    kind = "cash"


class Line:
    saledeid = 9001
    salid = 1001
    itid = 42
    qty = 2.0
    line_total = 30.0
    sold_at = NAIVE


class Cash:
    srid = 3
    counted_at = NAIVE
    user_uid = 8
    user_value = 0.0
    app_value = 1032.0
    diff_value = -1032.0
    pc_name = "CASHIER-PC"
    kind = "no_count"


# ════════════════════════════════════════════════════════════════
# the two clocks
# ════════════════════════════════════════════════════════════════

def test_pos_timestamp_is_written_without_a_zone():
    assert R.pos_ts(NAIVE) == "2026-08-08T19:30:15"


def test_pos_timestamp_refuses_an_aware_datetime():
    """An aware value here means someone converted POS time to UTC."""
    with pytest.raises(ValueError):
        R.pos_ts(AWARE)


def test_our_timestamp_keeps_its_zone():
    assert R.utc_ts(AWARE).startswith("2026-08-08T16:30:15")
    assert "+00:00" in R.utc_ts(AWARE)


def test_our_timestamp_refuses_a_naive_datetime():
    with pytest.raises(ValueError):
        R.utc_ts(NAIVE)


# ════════════════════════════════════════════════════════════════
# payloads
# ════════════════════════════════════════════════════════════════

def test_invoice_payload_never_carries_server_owned_columns():
    """first_seen_at anchors the deletion guard; deleted_at is the cloud's."""
    p = R.invoice_payload(Inv(), TENANT, SOURCE, AWARE)
    assert "first_seen_at" not in p
    assert "deleted_at" not in p
    assert p["last_seen_at"] == R.utc_ts(AWARE)
    assert p["sold_at"] == "2026-08-08T19:30:15"
    assert p["kind"] == "cash" and p["salid"] == 1001


def test_product_payload_omits_the_generated_column():
    """pos_products.is_modifier is GENERATED ALWAYS — sending it is 428C9."""
    p = R.product_payload({"itid": 42, "itcode": "A1", "itname": "طعمية",
                           "list_price": 15.0, "main_cat": 1, "sub_cat": 2},
                          TENANT, SOURCE)
    assert "is_modifier" not in p
    assert p["itname"] == "طعمية"


def test_line_payload_freezes_name_and_price_from_the_snapshot():
    """
    Frozen on purpose: if the menu price changes later, historical
    zero-invoice detection must not change with it.
    """
    p = R.line_payload(Line(), TENANT, SOURCE,
                       {"itname": "حمص صغير", "list_price": 25.0})
    assert p["item_name"] == "حمص صغير"
    assert p["list_price"] == 25.0
    assert "first_seen_at" not in p


def test_line_payload_with_unknown_product_is_null_not_zero():
    """
    list_price is what detect_zero_invoices keys on. Coercing an unknown
    item to 0 would make it look like a kitchen note and silently disappear
    from the highest-value detection in the product.
    """
    p = R.line_payload(Line(), TENANT, SOURCE, None)
    assert p["list_price"] is None
    assert p["item_name"] is None


def test_cash_payload_keeps_no_count_distinct_from_zero():
    p = R.cash_payload(Cash(), TENANT, SOURCE)
    assert p["kind"] == "no_count"
    assert p["user_value"] == 0.0
    assert p["counted_at"] == "2026-08-08T19:30:15"


# ════════════════════════════════════════════════════════════════
# the anomaly channel
# ════════════════════════════════════════════════════════════════

def test_anomaly_note_is_structured_json():
    """
    The agent cannot write internal_anomalies (no RLS policy). It reports
    through heartbeats.note, which the orchestrator parses and mirrors.
    """
    import json
    note = R.anomaly_note("unknown_item", {"itid": 77}, "1.0.0")
    parsed = json.loads(note)
    assert parsed["kind"] == "unknown_item"
    assert parsed["detail"] == {"itid": 77}
    assert parsed["agent_version"] == "1.0.0"


def test_heartbeat_payload_carries_pos_clock_and_drift():
    p = R.heartbeat_payload(TENANT, SOURCE, agent_version="1.0.0",
                            pos_clock=NAIVE, drift_seconds=-12,
                            rows_pulled=37, ok=True, note=None)
    assert p["pos_clock"] == "2026-08-08T19:30:15"   # POS clock, no zone
    assert p["drift_seconds"] == -12
    assert p["ok"] is True
    assert "id" not in p, "bigserial is the server's"
