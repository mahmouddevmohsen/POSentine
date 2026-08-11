# -*- coding: utf-8 -*-
"""
test_schema_migrations.py — structural guards on the hardening-phase
migration files (schema_v5, schema_v6).

The CHECK constraint behavior itself is explicitly NOT pytest-testable: no
test in this codebase exercises a real Postgres constraint (all tests use
in-memory fakes), and the manual verification SQL is documented inside
schema_v6_grand_total_check.sql for a one-time run in the Supabase SQL
editor. What IS testable here is that the files keep the properties the
owner depends on when applying them by hand: additive, idempotent, the
exact verified formula, and a documented rollback. A future session
"fixing" a migration could silently break the owner's apply step; these
guards fire on that.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent

MIGRATIONS = ("schema_v5_outbox_claimed_at.sql",
              "schema_v6_grand_total_check.sql")


def _read(name: str) -> str:
    path = REPO / name
    assert path.exists(), f"missing {name}"
    return path.read_text(encoding="utf-8")


def test_v5_outbox_claimed_at_is_additive_and_idempotent():
    text = _read("schema_v5_outbox_claimed_at.sql")
    assert "alter table outbox" in text
    assert "add column if not exists claimed_at timestamptz" in text
    # the ACTIVE apply statement is additive-only; the drop appears solely
    # as a documented rollback inside a comment block
    apply_lines = [ln for ln in text.splitlines()
                   if ln.strip() and not ln.strip().startswith("--")]
    assert not any("drop" in ln.lower() for ln in apply_lines)


def test_v5_documents_verification_and_rollback():
    text = _read("schema_v5_outbox_claimed_at.sql")
    assert "select id, status, claimed_at from outbox" in text
    assert "drop column claimed_at" in text


def test_v6_grand_total_check_has_the_exact_verified_formula():
    text = _read("schema_v6_grand_total_check.sql")
    assert "grand_total = sales + collections - returns - delivery" in text
    # the verified daybook formula must never drift into a plus-sign variant
    assert "grand_total = sales + collections + returns - delivery" not in text


def test_v6_is_idempotent_guarded_and_rollbackable():
    text = _read("schema_v6_grand_total_check.sql")
    assert "pg_constraint" in text and "conname" in text
    assert "if not exists" in text
    assert "drop constraint shift_reports_grand_total_formula" in text
    # the manual check-violation verification is documented, not executed
    assert "23514" in text


def test_migrations_never_create_tables_or_touch_schema_sql():
    """schema.sql is a locked Phase-1 file — these are separate additive
    files, and nothing in them may instruct editing schema.sql in place."""
    for name in MIGRATIONS:
        text = _read(name)
        assert "create table" not in text, f"{name} creates a table"
        assert "alter table recipients" not in text
