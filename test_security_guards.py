# -*- coding: utf-8 -*-
"""
test_security_guards.py — H7: the repository's secret/credential guards.

Phase 2 history: working correspondence under Docs/ has carried the
monitor_ro SQL password and, in ksjud7.md, the Supabase JWT secret and
service_role key. `data UN/` holds real customer POS exports (sales,
cash counts, employees). None of it may ever become trackable — and one
`git add -A` or an accidental .gitignore edit would publish all of it.

These tests pin the exclusions AND scan the actual tracked set, so the
guard cannot silently regress. Every predicate has a falsifier that
proves it can fire (project discipline: a check that shares the fault it
is meant to detect is not a check).

The scans cover value shapes, never names: the string "service_role" is
legitimate documentation; a real bot token, JWT, or private key is not.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent
GITIGNORE = REPO / ".gitignore"

# Secret-VALUE patterns (a real credential shape, never a name).
_SECRET_PATTERNS = [
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35,}\b"),            # Telegram bot token
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),         # private key block
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),                       # GitHub PAT
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),               # Slack token
    re.compile(r"sk_live_[A-Za-z0-9]{16,}"),                   # Stripe key
]


def _gitignore_lines() -> list[str]:
    assert GITIGNORE.exists(), "missing .gitignore — the guard itself is gone"
    return GITIGNORE.read_text(encoding="utf-8").splitlines()


def _has_exact_line(line: str, lines: list[str] | None = None) -> bool:
    return any(l.strip() == line
               for l in (lines if lines is not None else _gitignore_lines()))


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO,
                         capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


# ════════════════════════════════════════════════════════════════
# the exclusions — one forgotten line = one published credential
# ════════════════════════════════════════════════════════════════

def test_docs_never_trackable():
    """Docs/ carried live credentials twice (monitor_ro password, JWT +
    service_role key). The exclusion must exist and hold zero files."""
    assert _has_exact_line("Docs/"), ".gitignore no longer excludes Docs/"
    assert not any(f.startswith("Docs/") for f in _tracked_files())


def test_customer_data_dir_never_trackable():
    """`data UN` is a real directory of customer POS exports on disk; a
    single `git add -A` would publish all of it."""
    assert _has_exact_line("data UN/"), \
        ".gitignore no longer excludes the customer-data directory"
    assert not any(f.startswith("data UN/") for f in _tracked_files())


def test_build_artefacts_and_diagnostics_never_trackable():
    assert _has_exact_line("ship/"), ".gitignore lost ship/"
    assert any(l.strip().startswith("diagnostics_") for l in _gitignore_lines()), \
        ".gitignore lost the diagnostics_* exclusion"
    # .gitignore excludes diagnostics_*/ DIRECTORIES and diagnostics_*.zip
    # bundles — a committed python module named diagnostics_forensic.py is
    # not a bundle and is legitimately tracked.
    flagged = [f for f in _tracked_files()
               if f.startswith("ship/")
               or (f.startswith("diagnostics_")
                   and ("/" in f or f.endswith(".zip")))]
    assert not flagged


def test_machine_secrets_never_trackable():
    for line in ("config.json", ".env", "*.token", "*.pem", "*.key"):
        assert _has_exact_line(line), f".gitignore lost {line}"


# ════════════════════════════════════════════════════════════════
# the tracked-set scan — what actually made it into the repository
# ════════════════════════════════════════════════════════════════

def test_no_tracked_file_contains_a_secret_value():
    for path in _tracked_files():
        p = REPO / path
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        hits = [rx.pattern for rx in _SECRET_PATTERNS if rx.search(text)]
        assert not hits, f"possible secret value in {path}: {hits}"


# ════════════════════════════════════════════════════════════════
# falsifiers — every check must be able to fire
# ════════════════════════════════════════════════════════════════

def test_secret_scan_can_fire():
    """The value patterns really match real-shaped credentials (a short,
    fake-shaped token must NOT match — the scan must not be vacuous).

    Built by runtime concatenation, not as one literal: GitHub's own push
    protection scans raw source text and (correctly, from its point of
    view) treats an unbroken real-shaped token as a possible live secret,
    even a synthetic one meant only to prove a regex fires. Splitting each
    fake token across separate string literals keeps the *evaluated*
    value — which is all `_SECRET_PATTERNS` ever sees — exactly
    real-shaped, while no single line of source text contains a
    contiguous match for anything's scanner.
    """
    real_shaped = ("bot " + "1234567890:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabc"
                   "defghijklmnopqrstuvwxyz0123456789AB"
                   " key=" + "ghp_" + "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"
                   " " + "xoxb-" + "1234567890-abcdefghijklmnopqr")
    hits = [rx.pattern for rx in _SECRET_PATTERNS if rx.search(real_shaped)]
    assert len(hits) >= 3
    assert not any(rx.search("token 1234567890:FAKE-DELIVERY-TOKEN")
                   for rx in _SECRET_PATTERNS)


def test_gitignore_guards_can_fire():
    """Removing a guard line must be caught by the SAME predicate the
    real checks use — the falsifier mutates the parsed lines and proves
    the predicate fires on the mutation."""
    lines = _gitignore_lines()
    for removed in ("Docs/", "data UN/"):
        assert _has_exact_line(removed, lines)          # present for real
        mutated = [l for l in lines if l.strip() != removed]
        assert not _has_exact_line(removed, mutated)    # and the check fires
