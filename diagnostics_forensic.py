# -*- coding: utf-8 -*-
"""
diagnostics_forensic.py — ONE-OFF, READ-ONLY forensic dump for the Aug 9/10
Telegram-vs-POS discrepancy audit (2026-08-11).

Not part of the delivery closure. Never imported by orchestrator.py,
notifier/telegram.py, or delivery.py. Issues SELECT/count only — no insert,
update, or delete anywhere in this file. Meant to be run once via
.github/workflows/diagnostics-oneoff.yml and then deleted.
"""

from __future__ import annotations

import datetime as _dt
import os

import supa


def _mask(v):
    s = str(v)
    return s if len(s) <= 4 else f"{s[:2]}…{s[-2:]}"


def main() -> int:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    tenant_id = os.environ["TENANT_ID"]
    source_id = os.environ["SOURCE_ID"]
    client = supa.Supa(url, anon_key=key, token=key)
    scope = {"tenant_id": f"eq.{tenant_id}", "source_id": f"eq.{source_id}"}

    print("=" * 70)
    print("FORENSIC DIAGNOSTIC — READ ONLY — no writes anywhere in this run")
    print("=" * 70)

    print("\n--- tenants row ---")
    for r in client.select("tenants", {"id": f"eq.{tenant_id}",
                                       "select": "timezone,shift_morning_start,"
                                                 "shift_evening_start,go_live_at,created_at"}):
        print(r)

    print("\n--- sync_state row ---")
    for r in client.select("sync_state", {**scope}):
        print(r)

    print("\n--- ALL heartbeats, ascending (earliest first) ---")
    beats = client.select("heartbeats", {**scope,
                                         "select": "id,at,pos_clock,drift_seconds,rows_pulled,ok,note",
                                         "order": "at.asc"})
    print(f"total heartbeats: {len(beats)}")
    for r in beats[:15]:
        print(" ", r)
    if len(beats) > 15:
        print(f"  ... ({len(beats) - 15} more) ...")
        print(" LAST 5:")
        for r in beats[-5:]:
            print(" ", r)

    print("\n--- shift_reports rows (all) ---")
    for r in client.select("shift_reports", {**scope, "order": "shift_date.asc,shift_name.asc"}):
        print(" ", r)

    print("\n--- events rows for Aug 9-11 (all types) ---")
    for r in client.select("events", {**scope,
                                      "occurred_at": "gte.2026-08-09T00:00:00",
                                      "select": "type,dedup_key,level,occurred_at,status,payload",
                                      "order": "occurred_at.asc"}):
        print(" ", r)

    print("\n--- outbox rows (masked recipient) ---")
    for r in client.select("outbox", {"tenant_id": f"eq.{tenant_id}",
                                      "select": "channel,recipient,kind,dedup_key,status,"
                                                "attempts,created_at,sent_at",
                                      "order": "created_at.asc"}):
        r = dict(r)
        r["recipient"] = _mask(r.get("recipient"))
        print(" ", r)

    print("\n--- invoices: min/max sold_at, count, sum(total) for Aug 9 00:00 - Aug 11 00:00 ---")
    inv = client.select("invoices", {
        **scope,
        "sold_at": "gte.2026-08-09T00:00:00",
        "select": "salid,sold_at,total,delivery_cost,kind,first_seen_at,last_seen_at,deleted_at"})
    print(f"total invoices in window: {len(inv)}")
    if inv:
        sold_ats = sorted(r["sold_at"] for r in inv)
        first_seens = sorted((r.get("first_seen_at") or "") for r in inv)
        print(f"  sold_at range: {sold_ats[0]}  ..  {sold_ats[-1]}")
        print(f"  first_seen_at range: {first_seens[0]}  ..  {first_seens[-1]}")
        by_kind: dict[str, list[float]] = {}
        for r in inv:
            by_kind.setdefault(r.get("kind") or "?", []).append(float(r.get("total") or 0))
        for k, vals in sorted(by_kind.items()):
            print(f"  kind={k:10s} count={len(vals):4d} sum={sum(vals):,.2f}")
        deleted = [r for r in inv if r.get("deleted_at")]
        print(f"  deleted_at set on: {len(deleted)} rows")

        # Hourly bucket by sold_at (naive POS-local string prefix, cheap and
        # exact enough for an hour bucket)
        print("\n  hourly buckets (sold_at hour -> count, sum total):")
        buckets: dict[str, list[float]] = {}
        for r in inv:
            hour = r["sold_at"][:13]  # 'YYYY-MM-DDTHH'
            buckets.setdefault(hour, []).append(float(r.get("total") or 0))
        for h in sorted(buckets):
            vals = buckets[h]
            print(f"    {h}:00  count={len(vals):4d}  sum={sum(vals):,.2f}")

    print("\n--- DONE ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
