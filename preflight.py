# -*- coding: utf-8 -*-
"""
preflight.py — VERIFY.md steps 1–4, in one run
================================================================
The operator is standing in a working restaurant with a queue behind
them. Every command typed by hand on that machine is a chance to mistype
something we cannot afford to get wrong, and the four checks that decide
whether this install is safe are exactly the four that are pure reads.

So they are bundled. Steps 1–4 write nothing to the POS and nothing to
the cloud, which is what makes a single double-click carry no risk.

Steps 5 onward stay manual. Those involve decisions, and a script should
not make them.

Two rules govern everything below:

  • **Stop at the first failure.** Never continue past a bad result. A
    later step that "passes" on top of a broken earlier one produces a
    confident, wrong conclusion — which is the failure mode this whole
    product exists to avoid.

  • **Never invent a verdict.** Step 4's block is printed exactly as the
    agent produced it. This file classifies it; it never rewrites it,
    and anything it cannot classify is a failure, not a pass.

Called by preflight.bat. Runnable directly:

    python preflight.py                     # steps 1–4
    python preflight.py --skip-install      # steps 1–4, no pip

Audience note: this speaks English, like VERIFY.md, because its reader is
the person doing the install. The Arabic-only rule governs what reaches
the owner — the report and alert text in report.py.
================================================================
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

MIN_PYTHON = (3, 11)
GOLDEN_TEST_COUNT = 31          # VERIFY.md step 3: "31 passed", not "0 failed"
MANIFEST_NAME = "MANIFEST.txt"

RULE = "─" * 66

STEP_1 = "1 — console and Python"
STEP_2 = "2 — dependencies"
STEP_3 = "3 — config and token"
STEP_3B = "3b — read-only proof (attempts to write, requires refusal)"
STEP_4 = "4 — dry run (reads only, writes nothing anywhere)"

# The block headings agent.py prints. Matched literally: a heading that
# changed shape must fail loudly here rather than be quietly reinterpreted.
FIRST_RUN_MARK = "FIRST RUN (nothing is written, anywhere)"
DRY_RUN_MARK = "DRY RUN (nothing is written, anywhere)"

# Markers agent.py / adapter_hdsoft emit on the failures VERIFY.md step 4
# tables. Mapped to the same instruction the operator would have read there.
STEP_4_SYMPTOMS: tuple[tuple[str, str], ...] = (
    ("مفيش ODBC driver مناسب",
     "No usable ODBC driver. Go back to step 2 and install\n"
     "    \"Microsoft ODBC Driver 17 for SQL Server\"."),
    ("pyodbc مش متسطّب",
     "pyodbc is not installed. Go back to step 2."),
    ("Login failed for user",
     "Wrong monitor_ro password, or SQL Server Mixed Mode auth is off.\n"
     "    STOP. Call."),
    ("أعمدة ناقصة بعد تحديث",
     "HD Soft updated and a column we rely on changed. Numbers would be\n"
     "    wrong rather than absent. STOP. Call."),
    ("restore suspected",
     "The database was restored from a backup; salid went backwards.\n"
     "    STOP. Call."),
    ("no sync_state row",
     "This tenant/source has never been provisioned in the cloud.\n"
     "    STOP. Call — do not treat it as a first install."),
)


class Stop(Exception):
    """A failed check. Carries the VERIFY.md step, what broke, and what to do."""

    def __init__(self, step: str, what: str, do: str) -> None:
        super().__init__(what)
        self.step = step
        self.what = what
        self.do = do


# ════════════════════════════════════════════════════════════════
# output
# ════════════════════════════════════════════════════════════════

def configure_output() -> None:
    """Arabic reaches this console on the step-4 failure paths. A crash
    while printing an error message is the worst possible moment for one."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def heading(text: str) -> None:
    print()
    print(RULE)
    print(f"  VERIFY.md step {text}")
    print(RULE)


def ok(text: str) -> None:
    print(f"  [ OK ] {text}")


def note(text: str) -> None:
    print(f"         {text}" if text else "")


def child_env() -> dict[str, str]:
    """Children print Arabic into a pipe. Without this they pick cp1252 and
    die on the first item name."""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run(argv: list[str], echo: bool = True) -> tuple[int, str]:
    """Run a child, stream it to the console as it arrives, and keep a copy.

    stderr is merged into stdout on purpose: agent.py logs through logging,
    which writes to stderr, and an error the operator cannot see is an error
    that gets scrolled past.
    """
    proc = subprocess.Popen(
        argv, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=child_env(),
    )
    captured: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        captured.append(line)
        if echo:
            print(line)
    proc.wait()
    return proc.returncode, "\n".join(captured)


# ════════════════════════════════════════════════════════════════
# step 0 — is this the code we verified?
# ════════════════════════════════════════════════════════════════

def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def read_manifest(path: Path) -> dict[str, str]:
    """`<sha256>  <relative path>` per line; blank lines and # comments skipped."""
    entries: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise ValueError(f"unreadable MANIFEST.txt line: {raw!r}")
        entries[parts[1].strip()] = parts[0].strip().lower()
    if not entries:
        raise ValueError("MANIFEST.txt lists no files")
    return entries


def verify_manifest(root: Path) -> str:
    """Check every shipped file against its recorded hash.

    Extra files are not an error here — config.json, state.json and agent.log
    are all created on the customer machine after packaging. Missing or
    altered ones are.

    Returns a one-line status for the closing summary. A missing manifest is
    reported as NOT VERIFIED rather than passed over: "nothing was checked"
    and "everything checked out" must never look the same.
    """
    path = root / MANIFEST_NAME
    if not path.exists():
        return "code integrity   NOT VERIFIED — no MANIFEST.txt in this folder"

    try:
        entries = read_manifest(path)
    except (OSError, ValueError) as exc:
        raise Stop("0 — code integrity", f"{MANIFEST_NAME} is unreadable: {exc}",
                   "This folder was not produced by make_ship.py, or it was\n"
                   "    edited. Re-copy the ship folder and start again.")

    missing = [name for name in entries if not (root / name).exists()]
    if missing:
        raise Stop("0 — code integrity",
                   f"files listed in {MANIFEST_NAME} are missing: {missing}",
                   "The copy onto this machine was incomplete. Re-copy the\n"
                   "    whole ship folder and start again.")

    altered = [name for name, want in entries.items()
               if sha256_of(root / name) != want]
    if altered:
        raise Stop("0 — code integrity",
                   f"files differ from the versions we verified: {altered}",
                   "This machine is not running the code the tests were run\n"
                   "    against. Re-copy the ship folder. Do not edit files here.")

    return f"code integrity   OK — {len(entries)} files match {MANIFEST_NAME}"


# ════════════════════════════════════════════════════════════════
# step 1 — console and Python
# ════════════════════════════════════════════════════════════════

def step_1_python() -> None:
    heading(STEP_1)
    version = ".".join(str(n) for n in sys.version_info[:3])
    if sys.version_info < MIN_PYTHON:
        raise Stop(STEP_1,
                   f"Python {version} is below the minimum "
                   f"{'.'.join(str(n) for n in MIN_PYTHON)}",
                   "Install Python 3.11 or 3.12 from python.org, tick\n"
                   "    \"Add python.exe to PATH\", then run this again.")
    ok(f"Python {version}")
    note(sys.executable)

    encoding = (sys.stdout.encoding or "").lower().replace("-", "")
    if encoding not in ("utf8", "cp65001"):
        # Not fatal: the agent forces UTF-8 on its own output. But Arabic on
        # this screen will be unreadable, and step 4 is read off this screen.
        note(f"⚠ console encoding is {sys.stdout.encoding!r}, not UTF-8 — "
             "Arabic will be unreadable")
    else:
        ok(f"console encoding {sys.stdout.encoding}")


# ════════════════════════════════════════════════════════════════
# step 2 — dependencies
# ════════════════════════════════════════════════════════════════

DRIVER_PROBE = (
    "import adapter_hdsoft as a, pyodbc;"
    "print('pyodbc', pyodbc.version);"
    "print('drivers', pyodbc.drivers());"
    "print('picked', a.pick_driver())"
)


def step_2_dependencies(skip_install: bool = False) -> None:
    heading(STEP_2)
    requirements = HERE / "requirements.txt"
    if not requirements.exists():
        raise Stop(STEP_2, "requirements.txt is not in this folder",
                   "The copy onto this machine was incomplete. Re-copy the\n"
                   "    whole ship folder.")

    if skip_install:
        note("--skip-install: pip was not run")
    else:
        note("installing requirements (this needs internet the first time)")
        code, _ = run([sys.executable, "-m", "pip", "install",
                       "--disable-pip-version-check", "-r", "requirements.txt"])
        if code != 0:
            raise Stop(STEP_2, f"pip install failed (exit {code})",
                       "If it tried to compile pyodbc and failed, install\n"
                       "    \"Microsoft ODBC Driver 17 for SQL Server\" and retry.\n"
                       "    If it could not reach the network, connect this machine\n"
                       "    to the internet once and retry.")
        ok("requirements installed")

    # pyodbc is the one dependency never exercised on our own machines, and
    # pick_driver() is asked here rather than re-implemented, so this checks
    # what the agent will actually do rather than something adjacent to it.
    code, out = run([sys.executable, "-c", DRIVER_PROBE], echo=False)
    if code != 0:
        raise Stop(STEP_2, f"pyodbc / ODBC driver check failed:\n{out}",
                   "If pyodbc is missing, the install above did not take.\n"
                   "    If the driver list is empty, this machine has no ODBC\n"
                   "    driver at all — install \"Microsoft ODBC Driver 17 for\n"
                   "    SQL Server\". The agent cannot connect without one.")
    for line in out.splitlines():
        if line.startswith("picked"):
            ok(line)
        else:
            note(line)


# ════════════════════════════════════════════════════════════════
# step 3 — config and token
# ════════════════════════════════════════════════════════════════

SQL_KEYS = ("server", "database", "user", "password")

# Config.load's placeholder guard only inspects top-level strings, so the
# `sql` block — where "<monitor_ro password>" actually gets left behind —
# passes it and fails later at connect(), during step 4, looking like a
# network problem. Checked here, where it reads as what it is.
def check_sql_block(sql: dict) -> None:
    missing = [k for k in SQL_KEYS if not sql.get(k)]
    if missing:
        raise Stop(STEP_3, f"config.json sql block is missing: {missing}",
                   "Fill every field in the sql block of config.json. Copy the\n"
                   "    shape from config.example.json.")
    placeholders = [k for k in SQL_KEYS
                    if isinstance(sql[k], str) and sql[k].startswith("<")]
    if placeholders:
        raise Stop(STEP_3,
                   f"config.json sql block still has placeholders: {placeholders}",
                   "Replace the <angle bracket> values with the real ones.")


def _token_guidance(message: str) -> str:
    """VERIFY.md step 3 already answers each of these. Same words here."""
    if "role=" in message and "service_role" in message:
        return ("A key that bypasses every access rule is on this machine.\n"
                "    DO NOT run the agent. Remove it and call.")
    if "is for tenant" in message:
        return ("The token authenticates fine and matches nothing — every\n"
                "    upload would silently affect zero rows. Re-mint it for the\n"
                "    tenant_id in config.json.")
    if "no tenant_id claim" in message:
        return ("Same outcome as a wrong tenant: zero rows, forever.\n"
                "    Re-mint the token with mint_agent_token.py.")
    if "not a readable JWT" in message:
        return ("The token was truncated or mangled on the way into\n"
                "    config.json. Paste it again, whole, on one line.")
    if "placeholder" in message:
        return ("config.json was copied from config.example.json and not\n"
                "    filled in. Replace the <angle bracket> values.")
    if "missing required keys" in message:
        return "Add the missing keys. config.example.json has the full shape."
    return "Fix config.json and run this again."


def step_3_config(config_name: str = "config.json") -> None:
    heading(STEP_3)
    config_path = HERE / config_name
    if not config_path.exists():
        raise Stop(STEP_3, f"{config_name} is not in this folder",
                   "config.json is placed separately and is never bundled with\n"
                   "    the code. Copy config.example.json to config.json, fill it\n"
                   "    in, and put it next to agent.py.")

    # Imported here, not at the top: supa needs requests, which step 2 installs.
    importlib.invalidate_caches()
    try:
        agent = importlib.import_module("agent")
        mint = importlib.import_module("mint_agent_token")
        config_load = agent.Config.load
    except (ImportError, AttributeError, SyntaxError) as exc:
        # A traceback here would be the operator's first sight of this tool.
        # AttributeError is included deliberately: an agent.py that imports
        # but has no Config is not our agent.py, and that is worth saying
        # in words rather than in a stack trace.
        raise Stop(STEP_3, f"cannot use the agent modules in this folder: {exc}",
                   "Either step 2 did not install what it reported, or the\n"
                   "    files in this folder are not the ones we shipped.\n"
                   "    Re-copy the ship folder and run this again.")

    # Config.load is the agent's own validation — required keys, unfilled
    # placeholders, and the decoded token. Called rather than re-implemented
    # so this check can never drift away from what the agent enforces.
    try:
        cfg = config_load(config_path)
    except (ValueError, json.JSONDecodeError) as exc:
        raise Stop(STEP_3, str(exc), _token_guidance(str(exc)))
    except OSError as exc:
        raise Stop(STEP_3, f"cannot read {config_name}: {exc}",
                   "Check the file is readable and is valid JSON.")

    claims = mint.decode_claims(cfg.supabase_agent_token)
    print("  decoded token claims")
    for line in json.dumps(claims, indent=2, sort_keys=True).splitlines():
        note(line)
    exp = claims.get("exp")
    if exp:
        expires = _dt.datetime.fromtimestamp(int(exp), _dt.timezone.utc)
        note(f"expires {expires.isoformat()}")
    ok("TOKEN OK — role is 'authenticated' and tenant_id matches config")

    check_sql_block(cfg.sql)
    ok(f"sql block complete — {cfg.sql['user']}@{cfg.sql['server']}/"
       f"{cfg.sql['database']}")

    step_3_golden()
    return cfg


def step_3_golden() -> None:
    note("")
    note(f"running the golden baseline — must print {GOLDEN_TEST_COUNT} passed")
    # --color=no: the output is read off a restaurant counter, and raw ANSI
    # escapes on a console that does not render them turn "31 passed" into
    # noise. -p no:cacheprovider: a production folder should not accumulate
    # a .pytest_cache directory.
    code, out = run([sys.executable, "-m", "pytest", "-q", "--color=no",
                     "-p", "no:cacheprovider", "test_golden.py"])
    expected = f"{GOLDEN_TEST_COUNT} passed"
    if code != 0:
        raise Stop(STEP_3, f"test_golden.py failed (exit {code})",
                   "This machine has different code than we verified, or a\n"
                   "    dependency is missing. Do not continue. Send the output\n"
                   "    above and call.")
    if expected not in out:
        # Exit 0 is not enough. A collection that found 30 of 31 tests also
        # exits 0, and the missing one is exactly the one that would have
        # caught the problem.
        raise Stop(STEP_3,
                   f"the golden baseline did not report '{expected}'",
                   "The test file on this machine is not the one we verified.\n"
                   "    Re-copy the ship folder. Do not continue.")
    ok(f"golden baseline: {expected}")


# ════════════════════════════════════════════════════════════════
# step 3b — the read-only proof
# ════════════════════════════════════════════════════════════════

def step_3b_readonly_proof(cfg):
    """Attempt to write to the POS, and require every attempt to be refused.

    This is the first thing that touches the customer's database, and it is
    deliberately not a read. We told this customer their POS will not be
    written to; that promise is worth exactly as much as the last time we
    checked it, so it is checked on every install rather than once.

    It cannot run any earlier than this: connecting needs pyodbc from step 2
    and the credentials from step 3. It runs before step 4 — before the
    agent reads a single invoice — and it aborts the whole install if any
    write is permitted.

    The probes change nothing even if they are wrongly allowed: every one
    carries `WHERE 1 = 0`. The verbs with no harmless form — TRUNCATE, ALTER
    — are asked about rather than attempted. See readonly_probe.py.
    """
    heading(STEP_3B)
    note("attempting UPDATE, DELETE and INSERT against the POS database.")
    note("every one must be refused. nothing is changed either way:")
    note("every probe carries WHERE 1 = 0.")
    print()

    importlib.invalidate_caches()
    try:
        adapter = importlib.import_module("adapter_hdsoft")
        probe = importlib.import_module("readonly_probe")
    except ImportError as exc:
        raise Stop(STEP_3B, f"cannot load the probe modules: {exc}",
                   "Re-copy the ship folder and run this again.")

    try:
        cn = adapter.connect(**cfg.sql)
    except Exception as exc:                            # noqa: BLE001
        # Same symptom table as step 4 — a connection failure here reads
        # exactly like a connection failure there.
        guidance = next((do for mark, do in STEP_4_SYMPTOMS
                         if mark in str(exc)), None)
        raise Stop(STEP_3B, f"could not connect to the POS database: {exc}",
                   guidance or ("Check the sql block in config.json against the\n"
                                "    machine's actual SQL Server instance name.\n"
                                "    Photograph this and call."))

    try:
        report = probe.run(cn)
    finally:
        try:
            cn.close()
        except Exception:                               # pragma: no cover
            pass

    # Printed whole, pass or fail. This block is the evidence we show the
    # customer, so it belongs in the transcript either way.
    print(probe.format_report(report))

    if not report.passed:
        raise Stop(
            STEP_3B,
            "this login can write to the POS database"
            if report.permitted else
            "could not establish that the POS refuses our writes",
            "STOP. Do not install. These credentials are not read-only, and\n"
            "    read-only on the POS is the one promise this product makes\n"
            "    that is not negotiable.\n"
            "    Photograph the block above - it names exactly which\n"
            "    permission is wrong - change nothing, and call.")

    ok("every write was refused by the POS; no altering permission is held")
    if not report.guard_wired:
        # Loud rather than absent: the choke point is a two-line diff into a
        # locked file, and "nobody applied it yet" must not look the same as
        # "it is protecting you".
        note("")
        note("⚠ the sqlguard choke point is NOT wired into adapter_hdsoft.py.")
        note("  The server still refuses us - that is what the block above")
        note("  proves - but our own code is not yet refusing itself.")
        note("  See READONLY_GUARANTEE.md, layer 3.")
    return report


# ════════════════════════════════════════════════════════════════
# step 4 — dry run
# ════════════════════════════════════════════════════════════════

def _field(text: str, label: str) -> int | None:
    """Read `  <label>   <number>` out of the block. None when absent."""
    match = re.search(rf"^\s*{re.escape(label)}\s+(\d+)\s*$", text, re.MULTILINE)
    return int(match.group(1)) if match else None


def classify_dry_run(text: str) -> tuple[bool, str, str]:
    """Turn the agent's own block into a pass/fail. Returns (passed, what, do).

    This never rewrites the block and never softens it. Anything it cannot
    recognise is a failure: an unreadable dry run and a clean one must not
    produce the same outcome.
    """
    if FIRST_RUN_MARK in text:
        # Matched as a field line, not as a substring: the first-run block
        # explains itself with the sentence "Expected here: 'invoices to
        # upload' is 0 on a first install", and reading that as a reported
        # count would fail every legitimate first install.
        if _field(text, "invoices to upload") is not None:
            return (False,
                    "the FIRST RUN block also reported 'invoices to upload'",
                    "That combination should be impossible. The agent is not\n"
                    "    behaving as verified. Photograph this and call.")
        return (True,
                "FIRST RUN — this machine has no history and would adopt the\n"
                "         current MAX(salid), reading nothing behind it",
                "Expected on a first install. History is not backfilled by\n"
                "    design. Continue with VERIFY.md step 6.")

    if DRY_RUN_MARK not in text:
        return (False, "the dry run printed no recognisable block",
                "Something failed before the agent could report. Read the\n"
                "    output above, photograph it, and call.")

    watermark = _field(text, "watermark_salid")
    invoices = _field(text, "invoices to upload")
    if watermark is None or invoices is None:
        return (False,
                "could not read 'watermark_salid' / 'invoices to upload' "
                "out of the block",
                "The block is not the shape we verified. Do not draw a\n"
                "    conclusion from it. Photograph it and call.")

    # The failure the step-5 verdict cannot catch. On a watermark of 0 the
    # cross-check compares the whole table against the whole table, agrees
    # with itself, and prints PASS — two identical wrong answers. A resuming
    # agent never sits at watermark 0 unless the POS is genuinely empty, so
    # a zero watermark with invoices behind it is always wrong.
    if watermark == 0 and invoices > 0:
        return (False,
                f"watermark_salid is 0 and the agent would read {invoices} "
                "invoice(s)",
                "First-run initialisation did not take, and the agent is about\n"
                "    to pull the entire history during service. The VERDICT line\n"
                "    below cannot see this — at watermark 0 it compares the whole\n"
                "    table against the whole table and agrees with itself.\n"
                "    Most likely a state.json left behind by an earlier attempt.\n"
                "    STOP. Do not run agent.py. Photograph the block and call.")

    if "VERDICT: ABORT" in text:
        return (False, "the agent's own cross-check returned ABORT",
                "We are reading their data wrong. Every number after this\n"
                "    point would be confidently incorrect. Change nothing,\n"
                "    install nothing. Photograph the block and call.")
    if "VERDICT: CAPPED" in text:
        return (False, "the agent's own cross-check returned CAPPED",
                "A backlog is draining. Expected after an outage; on a fresh\n"
                "    install it means first-run initialisation did not run.\n"
                "    Photograph the block and call before continuing.")
    if "VERDICT: PASS" in text:
        return (True, f"VERDICT: PASS — watermark {watermark}, "
                      f"{invoices} invoice(s) to upload",
                "Write down the values from the block above, then continue\n"
                "    with VERIFY.md step 6. That step is the first write.")

    return (False, "the block carried no VERDICT line",
            "The cross-check did not run or did not finish. Do not treat\n"
            "    this as a pass. Photograph the block and call.")


def step_4_dry_run() -> str:
    """Returns the captured block, so a caller can tell a first install from
    a resuming one without running the agent a second time."""
    heading(STEP_4)
    note("running: python agent.py --dry-run")
    note("this reads the POS and the cloud. It writes to neither.")
    print()

    code, out = run([sys.executable, "agent.py", "--dry-run"])

    if code != 0:
        # agent.py exits non-zero when it could not complete. Match the
        # symptom to the instruction VERIFY.md step 4 would have given.
        guidance = next((do for mark, do in STEP_4_SYMPTOMS if mark in out), None)
        raise Stop(STEP_4, f"agent.py --dry-run exited {code}",
                   guidance or ("Read the error above. Photograph it and call.\n"
                                "    Change nothing on this machine."))

    # Exit 0 is not the verdict. agent.py returns 0 after printing an ABORT
    # block too — the block is the result, so the block is what is judged.
    passed, what, do = classify_dry_run(out)
    print()
    if not passed:
        raise Stop(STEP_4, what, do)
    first, *rest = what.splitlines()
    ok(first)
    for line in rest:
        note(line.strip())
    note("")
    for line in do.splitlines():
        note(line.strip())
    return out


# ════════════════════════════════════════════════════════════════
# driver
# ════════════════════════════════════════════════════════════════

def report_stop(stop: Stop) -> None:
    print()
    print("=" * 66)
    print(f"  STOPPED at VERIFY.md step {stop.step}")
    print("=" * 66)
    print()
    print("  What failed:")
    for line in str(stop.what).splitlines():
        print(f"    {line.strip()}" if line.strip() else "")
    print()
    print("  What to do:")
    for line in stop.do.splitlines():
        print(f"    {line.strip()}" if line.strip() else "")
    print()
    print("  Nothing was written to the POS or to the cloud.")
    print("  Do not continue to VERIFY.md step 5 until this is resolved.")
    print("=" * 66)


class PhaseA:
    """What steps 0–4 established, for whoever runs them.

    `installer.py` needs two facts out of preflight that a person reading
    the screen gets for free: whether this is a first install, and whether
    the read-only proof passed. Returning them here is what stops the
    installer from re-deriving either — a second implementation of the
    gate is a second thing that can disagree with the first.
    """

    def __init__(self, integrity: str, dry_run: str, readonly_report,
                 config=None) -> None:
        self.integrity = integrity
        self.dry_run = dry_run
        self.readonly_report = readonly_report
        self.config = config

    @property
    def is_first_install(self) -> bool:
        return FIRST_RUN_MARK in self.dry_run


def run_steps_0_to_4(config_name: str = "config.json",
                     skip_install: bool = False) -> PhaseA:
    """Steps 0–4, in order, stopping at the first failure.

    One implementation, two callers: `preflight.main` for our own use and
    `installer.py` Phase A for the one-click path. They cannot drift.
    """
    integrity = verify_manifest(HERE)
    print(f"  {integrity}")

    step_1_python()
    step_2_dependencies(skip_install)
    cfg = step_3_config(config_name)
    report = step_3b_readonly_proof(cfg)
    return PhaseA(integrity, step_4_dry_run(), report, cfg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run VERIFY.md steps 1–4. Reads only; writes nothing "
                    "to the POS or the cloud.")
    ap.add_argument("--config", default="config.json",
                    help="config file name, inside this folder")
    ap.add_argument("--skip-install", action="store_true",
                    help="do not run pip (offline machine, already installed)")
    args = ap.parse_args(argv)

    configure_output()
    print("=" * 66)
    print("  POSentine preflight — VERIFY.md steps 1 to 4")
    print("=" * 66)
    print("  Nothing here writes to the POS or to the cloud. Step 3b does")
    print("  attempt to write to the POS on purpose, and requires every")
    print("  attempt to be refused; each probe carries WHERE 1 = 0.")
    print("  It stops at the first failure and tells you which step failed.")
    print(f"  Folder: {HERE}")

    integrity = "code integrity   NOT VERIFIED — preflight stopped early"
    try:
        integrity = run_steps_0_to_4(args.config, args.skip_install).integrity
    except Stop as stop:
        report_stop(stop)
        return 1

    print()
    print("=" * 66)
    print("  VERIFY.md steps 1–4 PASSED")
    print("=" * 66)
    print(f"  {integrity}")
    print("  read-only proof  OK — the POS refused every write we attempted")
    print()
    print("  Steps 5 onward are manual, on purpose — they involve decisions.")
    print("  Step 5 is the verdict you just read. Step 6 is the first write.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
