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
NOT_YET_BUILT: tuple[tuple[str, str], ...] = (
    ("install/install_agent.ps1", "VERIFY.md step 7 — scheduled task"),
    ("install/uninstall_agent.ps1", "VERIFY.md uninstall"),
)

FORBIDDEN = ("config.json", "state.json", ".env")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
                known.add(line.split(None, 1)[1].strip())

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
    clear_ship()
    SHIP.mkdir(parents=True)

    entries: list[tuple[str, str]] = []
    for name, _why in SHIPPED:
        source = HERE / name
        target = SHIP / name
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
    print("  ⚠ NOT COMPLETE YET — these are required by VERIFY.md and do")
    print("    not exist in the repository:")
    for name, why in NOT_YET_BUILT:
        print(f"      {name:<32} {why}")
    print()
    print("    This folder covers VERIFY.md steps 1–6. Step 7 (the scheduled")
    print("    task) cannot be done from it until those are written.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(build())
