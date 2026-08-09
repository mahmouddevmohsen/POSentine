# -*- coding: utf-8 -*-
"""
make_ship.py — assemble the folder that goes on the customer machine
================================================================
The handover should be "copy this folder", not "copy these files and not
those". So this builds `ship/` from one explicit list.

`ship/` is generated, never hand-maintained, and never committed. A second
committed copy of agent.py is exactly the failure this project keeps
running into: the repository's tests pass, the customer's machine runs a
file that drifted from it months ago, and nothing anywhere says so.

What it produces:

  ship/                 the runtime, the golden baseline, preflight, VERIFY.md
  ship/MANIFEST.txt     sha256 of every file above

MANIFEST.txt is not decoration. preflight.py checks it before anything
else, so "this machine is running the code we verified" becomes a fact
that is tested rather than assumed.

`config.json` is never bundled. It is placed on the machine separately,
by hand, and this refuses to build if it ever appears in the list.

    python make_ship.py
================================================================
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

# Same reason as agent.py and preflight.py: a default Windows console is
# cp1252 and dies on the first non-Latin character. This script prints a
# warning sign, and a build tool that crashes while printing a warning is
# a build tool that appears to have succeeded.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
SHIP = HERE / "ship"
MANIFEST_NAME = "MANIFEST.txt"

# Everything the customer machine needs, and nothing else. The reason is
# recorded next to each file because "why is this here?" is the question
# that gets asked when someone trims the list later.
SHIPPED: tuple[tuple[str, str], ...] = (
    # ── the agent itself ────────────────────────────────────────
    ("agent.py", "entry point; one cycle per invocation"),
    ("adapter_hdsoft.py", "SQL Server reads and invoice classification"),
    ("rows.py", "POS rows to upload payloads"),
    ("supa.py", "Supabase REST client"),
    ("mint_agent_token.py", "imported by agent.py to decode and check the token"),

    # ── the baseline, run on site by VERIFY.md step 3 ───────────
    ("test_golden.py", "31 golden tests — proves this machine's code is ours"),
    ("metrics.py", "imported by test_golden.py"),
    ("events.py", "imported by test_golden.py"),
    ("report.py", "imported by test_golden.py"),

    # ── what the operator touches ───────────────────────────────
    ("preflight.bat", "double-click: VERIFY.md steps 1-4"),
    ("preflight.py", "the checks preflight.bat runs"),
    ("VERIFY.md", "the acceptance procedure, steps 1-10"),
    ("requirements.txt", "pyodbc, requests, pytest"),
    ("config.example.json", "the shape of config.json, with no secrets in it"),

    # ── the scheduled task, VERIFY.md step 7 ────────────────────
    ("install/install_agent.ps1", "registers the task; -ShowXml to inspect first"),
    ("install/uninstall_agent.ps1", "removes the task; leaves data untouched"),
    ("install/run_agent.ps1", "what the task executes; the task's environment block"),
)

# Deliberately absent, and named so their absence is a decision rather than
# an oversight. Anyone reading ship/ should be able to tell the difference.
EXCLUDED: tuple[tuple[str, str], ...] = (
    ("fake_adapter.py", "synthetic data has no place on a production machine"),
    ("schema.sql / schema_v2 / schema_v3", "cloud side; already applied"),
    ("audit_privileges.py", "runs in the monthly workflow, not here"),
    ("requirements-cloud.txt", "the Actions runner's, not this machine's"),
    ("test_agent.py and the other tests", "our baseline is test_golden.py"),
    ("README.md", "build documentation, not install documentation"),
    ("config.json", "placed separately, by hand. NEVER bundled."),
)

# Needed by VERIFY.md but not built yet. Printed loudly at the end: a ship
# folder that is silently incomplete is worse than one that says so.
NOT_YET_BUILT: tuple[tuple[str, str], ...] = ()

FORBIDDEN = ("config.json", "state.json", ".env")

# Line endings, per .gitattributes: Windows runs the scripts, so those are
# CRLF; everything else is LF. Checked before hashing, because a manifest
# is only meaningful if the bytes it describes are the bytes a checkout
# produces. See check_line_endings().
CRLF_SUFFIXES = (".ps1", ".bat")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_line_endings() -> None:
    """Refuse to hash a working tree whose bytes a checkout would not produce.

    This exists because it happened. A build ran against a working copy that
    had picked up CRLF in agent.py, supa.py and config.example.json — a tool
    on this machine had written them with Windows newline translation. The
    manifest was correct about that working copy and wrong about the
    repository, and nothing noticed: preflight compared ship/ against the
    same working tree and agreed with itself.

    That is the same shape as the watermark-0 trap — two answers from one
    wrong source, agreeing. So it is checked here rather than trusted.
    """
    wrong: list[str] = []
    for name, _why in SHIPPED:
        raw = (HERE / name).read_bytes()
        wants_crlf = Path(name).suffix in CRLF_SUFFIXES
        has_crlf = b"\r\n" in raw
        has_lone_lf = raw.replace(b"\r\n", b"") .count(b"\n") > 0
        if wants_crlf and has_lone_lf:
            wrong.append(f"{name}: expected CRLF throughout, found bare LF")
        elif not wants_crlf and has_crlf:
            wrong.append(f"{name}: expected LF, found CRLF")
    if wrong:
        raise SystemExit(
            "error: line endings do not match .gitattributes:\n  "
            + "\n  ".join(wrong)
            + "\n\n       The bytes here are not the bytes a clean checkout"
              "\n       produces, so every sha256 below would describe this"
              "\n       machine rather than the repository. Fix with:"
              "\n           git checkout -- <file>"
        )


def git_revision() -> str:
    """Stamp the manifest with what it was built from, so a folder on a
    customer machine can be traced back to a commit rather than to a date."""
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(HERE),
                              capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(HERE),
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown (git not available)"
    if head.returncode != 0:
        return "unknown (not a git checkout)"
    revision = head.stdout.strip()
    return revision + (" +uncommitted changes" if dirty.stdout.strip() else "")


def check_list() -> None:
    """Refuse to build a list that carries a secret or a duplicate."""
    names = [name for name, _ in SHIPPED]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise SystemExit(f"error: duplicate entries in SHIPPED: {sorted(duplicates)}")
    leaked = [n for n in names if n in FORBIDDEN]
    if leaked:
        raise SystemExit(
            f"error: {leaked} must never be bundled. config.json holds the "
            "SQL password and the agent token; it is placed on the machine "
            "by hand, separately."
        )
    missing = [n for n in names if not (HERE / n).exists()]
    if missing:
        raise SystemExit(f"error: listed but not in the repository: {missing}")


def clear_ship() -> None:
    """Rebuild from scratch, but never delete anything we did not create.

    A leftover file in ship/ from an earlier build is exactly the thing
    "copy this folder" would carry onto the customer machine unnoticed.
    """
    if not SHIP.exists():
        return
    manifest = SHIP / MANIFEST_NAME
    known = {MANIFEST_NAME}
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                # Only the top-level name matters here: iterdir() sees the
                # directory, not the files inside it.
                known.add(line.split(None, 1)[1].strip().split("/")[0])

    # Our own tooling's droppings — running the golden baseline inside ship/
    # to check it stands alone leaves both of these behind.
    generated = {"__pycache__", ".pytest_cache"}
    strangers = sorted(p.name for p in SHIP.iterdir()
                       if p.name not in known and p.name not in generated)
    if strangers:
        raise SystemExit(
            f"error: ship/ holds files this script did not create: {strangers}\n"
            "       Refusing to delete them. Move them somewhere safe, or "
            "delete ship/ yourself, then run this again."
        )
    shutil.rmtree(SHIP)


def build() -> int:
    check_list()
    check_line_endings()
    clear_ship()
    SHIP.mkdir(parents=True)

    entries: list[tuple[str, str]] = []
    for name, _why in SHIPPED:
        source = HERE / name
        target = SHIP / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        # Copied, then compared. shutil.copy2 does not verify, and a
        # truncated copy that nobody checked is how the wrong code ends up
        # on the machine that handles the money.
        digest = sha256_of(target)
        if digest != sha256_of(source):
            raise SystemExit(f"error: {name} differs after copying")
        entries.append((digest, name))

    lines = [
        "# POSentine — ship manifest",
        "# sha256 of every file in this folder, as built from the repository.",
        "# preflight.py checks these before it checks anything else.",
        "#",
        f"# built from: {git_revision()}",
        "#",
        "# config.json is NOT here and must never be. It is placed on the",
        "# machine separately and holds the SQL password and the agent token.",
        "#",
    ]
    lines += [f"# not yet built: {name} — {why}" for name, why in NOT_YET_BUILT]
    lines.append("")
    lines += [f"{digest}  {name}" for digest, name in sorted(entries,
                                                             key=lambda e: e[1])]
    (SHIP / MANIFEST_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")

    for forbidden in FORBIDDEN:
        if (SHIP / forbidden).exists():
            raise SystemExit(f"error: {forbidden} ended up in ship/. Remove it.")

    print("=" * 66)
    print(f"  built {SHIP}")
    print("=" * 66)
    for digest, name in sorted(entries, key=lambda e: e[1]):
        print(f"  {digest[:12]}  {name}")
    print(f"  {'':12}  {MANIFEST_NAME}")
    print()
    print(f"  {len(entries)} files + {MANIFEST_NAME}")
    print()
    print("  Not included, on purpose:")
    for name, why in EXCLUDED:
        print(f"    {name:<34} {why}")
    print()
    if NOT_YET_BUILT:
        print("  ⚠ NOT COMPLETE YET — these are required by VERIFY.md and do")
        print("    not exist in the repository:")
        for name, why in NOT_YET_BUILT:
            print(f"      {name:<32} {why}")
        print()
        print("    A visit run from this folder will stop at the step that")
        print("    needs them.")
    else:
        print("  ✔ Complete for VERIFY.md steps 1–8: preflight, the agent,")
        print("    the golden baseline, and the scheduled task.")
        print("    Step 9 (go-live) is two SQL statements run from our side,")
        print("    not on this machine, and is deliberately not in here.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(build())
