# -*- coding: utf-8 -*-
"""
rows.py — POS objects → Supabase payloads
================================================================
One place owns the column mapping, so the agent and the orchestrator
cannot drift apart on what a column means.

🕐 Two clocks live here and must never mix:

    pos_ts()   sold_at, counted_at, pos_clock
               POS local wall time. Column type `timestamp`, no zone.
               Stored exactly as the POS recorded it — no UTC conversion.
               Shift boundaries are wall-clock (07:00/19:00 local), so a
               converted timestamp lands invoices in the wrong shift and
               nothing anywhere reports an error.

    utc_ts()   last_seen_at, heartbeats.at
               Our time. Column type `timestamptz`, aware UTC.

Passing the wrong kind to either raises. Both directions are silent
corruption otherwise, and the deletion logic depends on being able to
compare `first_seen_at` against an aware `now`.

❄️ item_name and list_price are frozen into invoice_lines at sync time.
   If the menu price changes later, historical detection must not change
   with it — a receipt that was free in August stays free in December.
================================================================
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Mapping


def pos_ts(value: _dt.datetime | None) -> str | None:
    """POS local wall time. Refuses anything carrying a zone."""
    if value is None:
        return None
    if value.tzinfo is not None:
        raise ValueError(
            "POS timestamps are stored as-is and must be naive; got an "
            f"aware datetime ({value!r}). A zone here means a UTC conversion "
            "happened, which shifts invoices into the wrong shift."
        )
    return value.isoformat(timespec="seconds")


def utc_ts(value: _dt.datetime) -> str:
    """Our own time. Refuses a naive datetime."""
    if value.tzinfo is None:
        raise ValueError(
            f"our timestamps must be timezone-aware; got {value!r}"
        )
    return value.isoformat(timespec="seconds")


def _f(value: Any) -> float:
    return float(value or 0)


def _i(value: Any) -> int | None:
    return None if value is None else int(value)


# ════════════════════════════════════════════════════════════════
# ingestion payloads
# ════════════════════════════════════════════════════════════════

def invoice_payload(inv, tenant_id: str, source_id: str,
                    last_seen_at: _dt.datetime) -> dict[str, Any]:
    """
    first_seen_at is deliberately absent: it defaults on insert and must
    survive every later update, because detect_deleted uses it as the
    30-minute stability guard. Rewriting it each rescan would make a real
    deletion permanently invisible.

    deleted_at is absent for the same class of reason — it belongs to the
    orchestrator, which is the only side that can observe absence.
    """
    return {
        "tenant_id": tenant_id,
        "source_id": source_id,
        "salid": int(inv.salid),
        "receipt_num": _i(inv.receipt_num),
        "sold_at": pos_ts(inv.sold_at),
        "total": _f(inv.total),
        "delivery_cost": _f(inv.delivery_cost),
        "user_uid": _i(inv.user_uid),
        "cust_code": _i(inv.cust_code),
        "sal_t": _i(inv.sal_t),
        "sal_type": _i(inv.sal_type),
        "sale_rtype": _i(inv.sale_rtype),
        "kind": inv.kind,
        "last_seen_at": utc_ts(last_seen_at),
    }


def line_payload(line, tenant_id: str, source_id: str,
                 product: Mapping[str, Any] | None) -> dict[str, Any]:
    """
    product is the snapshot entry for this itid, or None when we have never
    seen the item. An unknown item yields NULL — never 0. list_price is what
    detect_zero_invoices keys on, and 0 there means 'kitchen note, ignore',
    so coercing an unknown item to 0 would delete it from the detection
    quietly. The caller records an anomaly when this returns NULLs.
    """
    return {
        "tenant_id": tenant_id,
        "source_id": source_id,
        "saledeid": int(line.saledeid),
        "salid": int(line.salid),
        "itid": _i(line.itid),
        "item_name": (product or {}).get("itname"),
        "list_price": (product or {}).get("list_price"),
        "qty": _f(line.qty),
        "line_total": _f(line.line_total),
        "sold_at": pos_ts(line.sold_at),
    }


def cash_payload(count, tenant_id: str, source_id: str) -> dict[str, Any]:
    """kind is already classified by the adapter: SrUserval = 0 is
    'no_count' (the cashier entered nothing), never a shortage."""
    return {
        "tenant_id": tenant_id,
        "source_id": source_id,
        "srid": int(count.srid),
        "counted_at": pos_ts(count.counted_at),
        "user_uid": _i(count.user_uid),
        "user_value": _f(count.user_value),
        "app_value": _f(count.app_value),
        "diff_value": _f(count.diff_value),
        "pc_name": count.pc_name,
        "kind": count.kind,
    }


def product_payload(product: Mapping[str, Any], tenant_id: str,
                    source_id: str) -> dict[str, Any]:
    """is_modifier is GENERATED ALWAYS AS (...) STORED — never sent."""
    return {
        "tenant_id": tenant_id,
        "source_id": source_id,
        "itid": int(product["itid"]),
        "itcode": product.get("itcode"),
        "itname": product.get("itname"),
        "list_price": product.get("list_price"),
        "main_cat": product.get("main_cat"),
        "sub_cat": product.get("sub_cat"),
    }


def user_payload(user: Mapping[str, Any], tenant_id: str,
                 source_id: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "source_id": source_id,
        "uid": int(user["uid"]),
        "name": user.get("name"),
    }


# ════════════════════════════════════════════════════════════════
# telemetry — and the agent's only anomaly channel
# ════════════════════════════════════════════════════════════════

def heartbeat_payload(tenant_id: str, source_id: str, agent_version: str,
                      pos_clock: _dt.datetime | None, drift_seconds: int | None,
                      rows_pulled: int, ok: bool,
                      note: str | None) -> dict[str, Any]:
    """`at` and `id` are the server's; we never send them."""
    return {
        "tenant_id": tenant_id,
        "source_id": source_id,
        "agent_version": agent_version,
        "pos_clock": pos_ts(pos_clock),
        "drift_seconds": drift_seconds,
        "rows_pulled": rows_pulled,
        "ok": ok,
        "note": note,
    }


def anomaly_note(kind: str, detail: Mapping[str, Any],
                 agent_version: str) -> str:
    """
    RLS gives the agent no access to internal_anomalies, so anomalies ride
    out on heartbeats.note as structured JSON. The orchestrator parses this
    and mirrors it into the real table under service_role; if parsing ever
    fails it still records the unparseable note rather than dropping it.
    """
    return json.dumps(
        {"kind": kind, "detail": dict(detail), "agent_version": agent_version},
        ensure_ascii=False, default=str, sort_keys=True,
    )
