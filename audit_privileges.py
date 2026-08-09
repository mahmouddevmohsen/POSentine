# -*- coding: utf-8 -*-
"""
audit_privileges.py — monthly guard on database privileges
================================================================
Runs from the keepalive workflow. Fails loudly if the privilege surface
drifts from what schema_v3_revoke_inherited.sql established.

Why this exists rather than a one-time fix:

supabase_admin's default ACL grants anon and authenticated the full
arwdDxtm on any table **it** creates. Our tables are created by postgres,
where we closed the inheritance — but we cannot alter supabase_admin's
defaults from the Management API's role. So the condition stays live.

Anything that cannot be closed gets made visible instead. The first table
created in public by the wrong role will hand anon full DML on it, and
this check is what says so.

  python audit_privileges.py          # needs SUPABASE_ACCESS_TOKEN
  exit 0 = clean, exit 1 = drift

check() is pure so the rules can be pinned without a database.
================================================================
"""

from __future__ import annotations

import os
import sys
from typing import Any, Iterable, Mapping

import requests

PROJECT_REF = os.environ.get("SUPABASE_PROJECT_REF", "mwwjfeporhfhcekmektg")
ENV_TOKEN = "SUPABASE_ACCESS_TOKEN"

# Exactly the tables the agent's token may touch, and exactly how.
AGENT_TABLES = frozenset({
    "sync_state", "heartbeats", "pos_users", "pos_products",
    "invoices", "invoice_lines", "cash_counts",
})
AGENT_PRIVS = frozenset({"INSERT", "SELECT", "UPDATE"})

# TRUNCATE and DELETE are singled out in the message because they destroy
# rows, and TRUNCATE is not filtered by RLS at all — a table-level verb
# makes every row-level guarantee in this system irrelevant.
DESTRUCTIVE = frozenset({"DELETE", "TRUNCATE"})

PRIVILEGE_QUERY = """
select grantee, table_name,
       string_agg(privilege_type, ',' order by privilege_type) as privs
from information_schema.role_table_grants
where table_schema = 'public'
  and grantee in ('anon','authenticated','service_role')
group by grantee, table_name
order by grantee, table_name;
"""


def check(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return a list of violations. Empty means the surface is as designed."""
    rows = list(rows)
    if not rows:
        # A failed query and a clean database look identical from here.
        # Refusing to call that a pass is the whole point.
        return ["privilege query returned no rows at all — the query failed "
                "or the schema is gone; this is not a clean result"]

    problems: list[str] = []
    agent_seen: set[str] = set()

    for row in rows:
        grantee = row["grantee"]
        table = row["table_name"]
        privs = frozenset(p.strip() for p in row["privs"].split(",") if p.strip())

        if grantee == "anon":
            problems.append(
                f"anon holds {sorted(privs)} on public.{table} — anon must "
                "hold nothing; the anon key is public and TRUNCATE bypasses RLS"
            )
            continue

        if grantee != "authenticated":
            continue

        if table not in AGENT_TABLES:
            problems.append(
                f"authenticated (the agent token) can reach public.{table} "
                f"with {sorted(privs)} — that table is the orchestrator's"
            )
            continue

        agent_seen.add(table)
        extra = privs - AGENT_PRIVS
        if extra:
            harm = sorted(extra & DESTRUCTIVE) or sorted(extra)
            problems.append(
                f"authenticated holds {harm} on public.{table} — the agent "
                f"may only {sorted(AGENT_PRIVS)}"
            )
        missing = AGENT_PRIVS - privs
        if missing:
            problems.append(
                f"authenticated is missing {sorted(missing)} on public.{table} "
                "— the agent's sync will 403 and stall"
            )

    for table in sorted(AGENT_TABLES - agent_seen):
        problems.append(
            f"authenticated has no grant on public.{table} — the agent needs "
            "it and will fail every cycle"
        )

    return problems


def fetch(token: str) -> list[dict[str, Any]]:
    resp = requests.post(
        f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"query": PRIVILEGE_QUERY},
        timeout=60,
    )
    if resp.status_code >= 300:
        raise SystemExit(
            f"privilege query failed: HTTP {resp.status_code} "
            f"{(resp.text or '')[:200]}"
        )
    return resp.json()


def main() -> int:
    token = os.environ.get(ENV_TOKEN, "").strip()
    if not token:
        print(f"error: {ENV_TOKEN} is not set", file=sys.stderr)
        return 2

    problems = check(fetch(token))
    if not problems:
        print("privilege audit: clean — anon holds nothing, agent token "
              f"limited to {len(AGENT_TABLES)} tables")
        return 0

    print("privilege audit FAILED", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
