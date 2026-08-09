# -*- coding: utf-8 -*-
"""
test_audit_privileges.py — the guard for a condition we cannot close.

supabase_admin's default ACL grants anon and authenticated full DML on any
table *it* creates. Our tables are created by postgres, so that path is
dormant — but we cannot alter that role's defaults from the Management API,
so it stays a live condition rather than a fixed one.

The audit runs monthly and fails loudly the first time a table appears with
privileges nobody granted. The checking logic is pure so it can be pinned
without touching a database.
"""

from __future__ import annotations

import audit_privileges as audit

AGENT_TABLES = ["sync_state", "heartbeats", "pos_users", "pos_products",
                "invoices", "invoice_lines", "cash_counts"]


def healthy():
    rows = [{"grantee": "authenticated", "table_name": t,
             "privs": "INSERT,SELECT,UPDATE"} for t in AGENT_TABLES]
    rows += [{"grantee": "service_role", "table_name": t,
              "privs": "DELETE,INSERT,SELECT,UPDATE"}
             for t in AGENT_TABLES + ["tenants", "events", "outbox"]]
    return rows


def test_healthy_state_reports_nothing():
    assert audit.check(healthy()) == []


def test_anon_on_any_table_is_a_violation():
    """The headline check: anon must appear zero times."""
    rows = healthy() + [{"grantee": "anon", "table_name": "invoices",
                         "privs": "SELECT"}]
    problems = audit.check(rows)
    assert len(problems) == 1
    assert "anon" in problems[0] and "invoices" in problems[0]


def test_anon_with_only_truncate_is_still_a_violation():
    """This is the exact shape the inherited default ACL produced."""
    rows = healthy() + [{"grantee": "anon", "table_name": "tenants",
                         "privs": "REFERENCES,TRIGGER,TRUNCATE"}]
    assert audit.check(rows), "TRUNCATE bypasses RLS — never acceptable"


def test_agent_gaining_delete_is_a_violation():
    rows = healthy()
    rows[0] = {**rows[0], "privs": "DELETE,INSERT,SELECT,UPDATE"}
    problems = audit.check(rows)
    assert any("DELETE" in p for p in problems)


def test_agent_gaining_truncate_is_a_violation():
    rows = healthy()
    rows[0] = {**rows[0], "privs": "INSERT,SELECT,TRUNCATE,UPDATE"}
    assert audit.check(rows)


def test_agent_reaching_an_orchestrator_table_is_a_violation():
    rows = healthy() + [{"grantee": "authenticated", "table_name": "outbox",
                         "privs": "SELECT"}]
    problems = audit.check(rows)
    assert any("outbox" in p for p in problems)


def test_agent_losing_a_table_it_needs_is_a_violation():
    """A missing grant breaks the sync silently — the agent just 403s forever."""
    rows = [r for r in healthy()
            if not (r["grantee"] == "authenticated" and r["table_name"] == "invoices")]
    problems = audit.check(rows)
    assert any("invoices" in p for p in problems)


def test_empty_result_is_a_violation_not_a_pass():
    """
    An empty result means the query failed or the schema vanished. Reading
    that as 'no violations' is exactly the silent pass this product cannot
    afford.
    """
    assert audit.check([]) != []
