# -*- coding: utf-8 -*-
"""
sqlguard.py — the POS connection cannot carry a write
================================================================
"Read-only on the POS" has been a rule people keep. This makes it a code
path that raises.

Every statement bound for the POS passes through `assert_read_only`.
Wiring is at the *connection*, not at the call sites, deliberately: a
guard you have to remember to call is the same kind of promise we already
had. `guard(cn)` returns a connection whose every cursor refuses anything
that is not a read, including statements written months from now by
someone who never read this file.

Two rules, and the first one is the real control:

  1. **Allowlist the leading verb.** A statement must begin with SELECT
     or WITH. Everything else — EXEC, a bare `sp_who`, SET, BEGIN TRAN —
     is refused for not being on the list, rather than for being on a
     list of things we thought of.

  2. **Denylist inside the statement**, for the write verbs that can hide
     after a legal opening: `WITH x AS (...) DELETE FROM x`, a `SELECT
     ... INTO`, a batch with a second statement behind a semicolon.

Comments and string literals are removed before either rule is applied,
so `SELECT '--'` is read correctly and `/* DELETE */ SELECT 1` is not
refused for a word in a comment — and, the direction that matters,
`SELECT 1 -- \n ; DROP TABLE x` cannot hide a statement inside one.

⚠️ This module is the one place in the repository whose source contains
   write keywords on purpose. `test_readonly.py` knows that, and checks
   every other POS-facing module against the same list.
================================================================
"""

from __future__ import annotations

import re
from typing import Any

# Verbs a statement may begin with. Short on purpose: the allowlist is the
# control, and every query this product sends is a SELECT.
#
#   SELECT  every read in adapter_hdsoft.py and agent.py
#   WITH    a common table expression, should one ever be needed
ALLOWED_LEADING = frozenset({"SELECT", "WITH"})

# Words that may not appear anywhere in a statement, after comments and
# string literals have been stripped. The reason is recorded where it is
# not obvious, because the next person to edit this list needs to know
# which of these are load-bearing.
FORBIDDEN = frozenset({
    # ── plain writes ────────────────────────────────────────────
    "INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE",
    "WRITETEXT", "UPDATETEXT",
    # ── schema ──────────────────────────────────────────────────
    "CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME", "DISABLE", "ENABLE",
    # SELECT ... INTO creates a table. There is no read that needs INTO.
    "INTO",
    # ── running someone else's code ─────────────────────────────
    # EXEC is the sharpest one: a stored procedure owned by the same
    # owner as the tables runs under ownership chaining, and the
    # permission check on the tables it writes is SKIPPED. db_denydatawriter
    # does not save us there. Refusing EXEC outright is what does.
    "EXEC", "EXECUTE", "CALL",
    # ── permissions ─────────────────────────────────────────────
    "GRANT", "REVOKE", "DENY", "SETUSER", "IMPERSONATE", "REVERT",
    # ── server-level ────────────────────────────────────────────
    "BACKUP", "RESTORE", "RECONFIGURE", "SHUTDOWN", "KILL", "DBCC",
    "CHECKPOINT",
    # ── bulk and remote paths, all of which can write ───────────
    "BULK", "OPENROWSET", "OPENDATASOURCE", "OPENQUERY", "OPENXML",
    # ── transactions and session state ──────────────────────────
    # Not writes in themselves, but a statement that opens a transaction
    # or changes session state is not a read, and a held transaction on
    # dbo.Sales during service is its own incident.
    "BEGIN", "COMMIT", "ROLLBACK", "SAVE", "TRAN", "TRANSACTION", "SET",
    # ── denial of service ───────────────────────────────────────
    # WAITFOR DELAY '23:59:59' writes nothing and stops the till's cycle
    # for a day.
    "WAITFOR",
})

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class WriteAttempt(RuntimeError):
    """A statement that is not a read tried to reach the POS database."""


# ════════════════════════════════════════════════════════════════
# normalising — comments and literals out, in one pass
# ════════════════════════════════════════════════════════════════

def normalise(sql: str) -> str:
    """Strip comments, blank out string literals, neutralise quoted
    identifiers. Returns SQL whose words are only real words.

    Refuses an unterminated literal or comment rather than guessing where
    it ends: that is precisely the shape an injection takes, and a guard
    that guesses is not a guard.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]

        if sql.startswith("/*", i):
            depth, i = 1, i + 2          # T-SQL block comments nest
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise WriteAttempt(
                    "unterminated /* block comment — refusing to guess where "
                    "the statement resumes")
            out.append(" ")
            continue

        if sql.startswith("--", i):
            newline = sql.find("\n", i)
            i = n if newline < 0 else newline
            out.append(" ")
            continue

        if ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2                     # '' is an escaped quote
                        continue
                    break
                i += 1
            if i >= n:
                raise WriteAttempt(
                    "unterminated string literal — refusing to guess where "
                    "the statement resumes")
            out.append("''")
            i += 1
            continue

        for opener, closer in (("[", "]"), ('"', '"')):
            if ch == opener:
                end = sql.find(closer, i + 1)
                if end < 0:
                    raise WriteAttempt(
                        f"unterminated {opener}quoted identifier{closer} — "
                        "refusing to guess where the statement resumes")
                # A column legitimately called [Delete] must not read as the
                # verb, and must not smuggle one either.
                out.append(" _identifier_ ")
                i = end + 1
                break
        else:
            out.append(ch)
            i += 1

    return "".join(out)


def statements(sql: str) -> list[str]:
    """Split a batch on semicolons. Empty fragments are dropped, so a
    trailing `;` is not treated as a second statement."""
    return [part for part in normalise(sql).split(";") if part.strip()]


# ════════════════════════════════════════════════════════════════
# the check
# ════════════════════════════════════════════════════════════════

def assert_read_only(sql: Any) -> None:
    """Raise WriteAttempt unless this is a single, plain read."""
    if not isinstance(sql, str) or not sql.strip():
        raise WriteAttempt(
            f"refusing a statement that is not a non-empty string: {sql!r}")

    parts = statements(sql)
    if not parts:
        raise WriteAttempt(f"refusing a statement with no content: {sql!r}")
    if len(parts) > 1:
        # `SELECT 1; DROP TABLE x` — the first half is innocent and the
        # batch is not.
        raise WriteAttempt(
            f"refusing a batch of {len(parts)} statements; the POS "
            f"connection sends one read at a time. Statement: {sql.strip()!r}")

    words = [w.upper() for w in _WORD.findall(parts[0])]
    if not words:
        raise WriteAttempt(f"refusing a statement with no keywords: {sql!r}")

    leading = words[0]
    if leading not in ALLOWED_LEADING:
        raise WriteAttempt(
            f"refusing a statement beginning with {leading!r}. The POS "
            f"connection may only send {sorted(ALLOWED_LEADING)}. "
            f"Statement: {sql.strip()!r}")

    offenders = sorted(set(words) & FORBIDDEN)
    if offenders:
        raise WriteAttempt(
            f"refusing a statement containing {offenders} — that is not a "
            f"read, and this product never writes to the POS database. "
            f"Statement: {sql.strip()!r}")


def is_read_only(sql: Any) -> bool:
    """Non-raising form, for reporting rather than for enforcing."""
    try:
        assert_read_only(sql)
        return True
    except WriteAttempt:
        return False


# ════════════════════════════════════════════════════════════════
# the wrapper — where the guard actually bites
# ════════════════════════════════════════════════════════════════

class GuardedCursor:
    """A pyodbc cursor that will not execute anything but a read.

    Everything else is delegated, so callers keep using `fetchall`,
    `fetchone`, row attribute access and iteration exactly as before.
    """

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def execute(self, sql: Any, *params: Any, **kwargs: Any) -> "GuardedCursor":
        assert_read_only(sql)
        self._cursor.execute(sql, *params, **kwargs)
        # pyodbc returns the cursor so that .execute(...).fetchall() works.
        return self

    def executemany(self, sql: Any, *params: Any, **kwargs: Any) -> "GuardedCursor":
        assert_read_only(sql)
        self._cursor.executemany(sql, *params, **kwargs)
        return self

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __iter__(self):
        return iter(self._cursor)

    def __enter__(self) -> "GuardedCursor":
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._cursor.__exit__(*exc)


class GuardedConnection:
    """A pyodbc connection that hands out guarded cursors only."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @property
    def raw(self) -> Any:
        """The unguarded connection.

        Exactly one caller is meant to use this: `readonly_probe.py`, whose
        entire job is to send writes and require the *server* to refuse
        them. If our own guard refused them first, the probe would prove
        our code works and say nothing about the customer's credentials —
        which is the only thing it exists to establish.
        """
        return self._connection

    def cursor(self) -> GuardedCursor:
        return GuardedCursor(self._connection.cursor())

    def execute(self, sql: Any, *params: Any, **kwargs: Any) -> GuardedCursor:
        assert_read_only(sql)
        return GuardedCursor(self._connection.execute(sql, *params, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._connection, name, value)

    def __enter__(self) -> "GuardedConnection":
        self._connection.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._connection.__exit__(*exc)


def guard(connection: Any) -> GuardedConnection:
    """Wrap a live connection. Idempotent — guarding twice is harmless."""
    if isinstance(connection, GuardedConnection):
        return connection
    return GuardedConnection(connection)


def is_guarded(connection: Any) -> bool:
    """Whether the choke point is actually wired in.

    preflight reports this on the install transcript. `adapter_hdsoft.py`
    is locked, so the two-line diff that calls `guard()` is applied by the
    architect rather than by us — and an unapplied diff must be visible on
    the screen rather than assumed.
    """
    return isinstance(connection, GuardedConnection)
