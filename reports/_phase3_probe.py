# -*- coding: utf-8 -*-
"""PHASE 3 — READ-ONLY live Supabase permission probe.

GET only. Never writes, never deletes, never runs DDL. Masks every secret
in output. Purpose: establish the CURRENT effective access state of the
tables the dashboard needs, with real evidence, before the architecture
decision is made.

Loads credentials from Docs/config.json (gitignored). Prints the table,
the HTTP status, the row-count header, and (for readable tables) the
shape of one row with PII fields truncated.
"""
import json
import sys

import requests

CFG_PATH = "Docs/config.json"
try:
    with open(CFG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
except FileNotFoundError:
    print(f"FATAL: {CFG_PATH} not found — cannot probe without credentials")
    sys.exit(2)

BASE = cfg["supabase_url"].rstrip("/")
ANON = cfg.get("supabase_anon_key", "")
TOKEN = cfg["supabase_agent_token"]
TENANT = cfg["tenant_id"]
SOURCE = cfg["source_id"]


def mask(text: str) -> str:
    for s in (ANON, TOKEN):
        if s and s in text:
            text = text.replace(s, "***")
    return text


def get(table: str, params: dict | None = None, headers: dict | None = None):
    h = {"apikey": ANON, "Authorization": "Bearer " + TOKEN,
         "Accept": "application/json", "Range-Unit": "items", "Range": "0-0"}
    if headers:
        h.update(headers)
    try:
        r = requests.get(BASE + "/rest/v1/" + table, headers=h,
                         params=params, timeout=30)
    except Exception as exc:
        return f"EXCEPTION: {mask(str(exc))[:200]}"
    return r


def probe(table: str, extra_params: dict | None = None, show_row: bool = True):
    r = get(table, params=extra_params, headers={"Prefer": "count=exact"})
    if isinstance(r, str):
        print(f"  {table:22s} {r}")
        return
    total = (r.headers.get("Content-Range", "") if hasattr(r, "headers") else "")
    total = total.rsplit("/", 1)[-1] if total else "?"
    body = mask(r.text)[:600] if r.text else ""
    status = r.status_code
    print(f"  {table:22s} HTTP {status}  rows={total}")
    if status == 200 and show_row and body and body != "[]":
        try:
            row = json.loads(r.text)[0]
            # truncate long/PII-prone values for display
            for k, v in list(row.items()):
                if isinstance(v, str) and len(v) > 24:
                    row[k] = v[:24] + "…"
            print(f"     shape: {json.dumps(row, ensure_ascii=False)[:400]}")
        except Exception:
            print(f"     body: {body[:200]}")
    elif status >= 400:
        print(f"     body: {body[:200]}")


print("=" * 78)
print(f"PHASE 3 READ-ONLY PROBE — tenant {TENANT[:8]}… source {SOURCE[:8]}…")
print(f"identity: agent token (role=authenticated, tenant-scoped)")
print("=" * 78)

print("\n-- dashboard-critical tables (blocked twice per prior audit) --")
for t in ("shift_reports", "events", "tenants", "internal_anomalies"):
    probe(t)

print("\n-- detail tables (audit says authenticated has some access) --")
for t in ("withdrawals", "heartbeats", "cash_counts",
          "pos_users", "pos_products", "sync_state"):
    probe(t)

print("\n-- invoice tables (raw ingredients) --")
probe("invoices", {"select": "salid,total,kind,sold_at", "order": "salid.desc"})
probe("invoice_lines", {"select": "saledeid,salid,item_name,qty,line_total",
                        "order": "saledeid.desc"})

print("\n-- real-data window: invoices min/max + kind totals (last 30 days) --")
r = get("invoices", params={
    "select": "kind,total,sold_at",
    "sold_at": "gte.2026-07-15T00:00:00",
    "order": "sold_at.desc",
}, headers={"Range-Unit": "items", "Range": "0-9999"})
if isinstance(r, str):
    print("  " + r)
elif r.status_code == 200:
    rows = json.loads(r.text)
    print(f"  invoices fetched: {len(rows)}")
    if rows:
        kinds: dict[str, list] = {}
        dates = [x["sold_at"] for x in rows if x.get("sold_at")]
        for x in rows:
            kinds.setdefault(x.get("kind"), []).append(x.get("total") or 0)
        print(f"  sold_at range: {min(dates)[:10]} … {max(dates)[:10]}")
        for k, v in sorted(kinds.items()):
            print(f"  kind={k:10s} n={len(v):5d} sum={sum(v):,.2f}")
else:
    print(f"  HTTP {r.status_code} {mask(r.text)[:200]}")

print("\nDONE (read-only — nothing written)")
