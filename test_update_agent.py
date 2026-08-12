# -*- coding: utf-8 -*-
"""
test_update_agent.py — the one-click updater, rehearsed before it ships
================================================================
update_agent.ps1 is the only thing the till operator will ever run
again: double-click UPDATE_POSENTINE.bat, read the final screen. So it
is tested the way the installer is — by running the REAL script against
a REAL ship folder, in a throwaway sandbox that stands in for the till.

The sandbox pre-update install IS ship/ (that is exactly what a clean
till runs), plus a config.json, state.json and agent.log created the
way the live install has them. A release zip — built from the same
ship/, with the posentine/ prefix make_ship.make_zip uses — is placed
in the sandbox's Downloads folder as the "new release". Everything is
sandbox-local: the repo-root posentine-<commit>.zip is deliberately NOT
used, because test_preflight.py builds and then deletes that file on
every full-suite run and the updater tests must not depend on it.

The updater is run with -SkipTaskOps -SkipMonitor (there is no Task
Scheduler in a pytest sandbox, and a live 3-minute wait would be absurd
in CI) and with the Phase 5/8 outputs read from fixtures, so the
verdict-parsing paths run for real.

What is exercised for real:
  * Phase 1  — live-install file checks, artifact selection, SHA-256,
               disk space, the checksum-pin failure path
  * Phase 2  — backup: config.json sha256 sidecar (never the file),
               stateful files + every code file + MANIFEST
  * Phase 4  — staging extraction, protected-name refusal, code copy,
               protected files verified byte-identical, MANIFEST +
               report.py hash verification
  * the 02:16 regression — agent.log genuinely locked by another process:
               transient lock absorbed by the 5x500ms retry, persistent
               lock fails closed with nothing half-updated
  * the concurrent-safe helpers (dot-sourced with -SkipRun): shared
               reads, UTF-8 round-trip, growth-tolerant prefix hashing
  * Phase 5/8 — the verdict-parsing functions (via fixtures)
  * rollback — the automatic restore of previous code + MANIFEST when
               a gate fails, with config/state/agent.log preserved

What is NOT exercised here (and must be proven on site):
  * Task Scheduler registration/removal (SkipTaskOps)
  * the natural-cycle monitor (SkipMonitor — the 3-minute trigger)
  * the real POS read-only proof and dry run (they need the till's
    SQL Server; preflight.py itself has the sanctioned tests for the
    verdict classification)

The rehearsal tests skip loudly when ship/ is not present (a fresh
checkout without `python make_ship.py`). The static tests run
everywhere.
================================================================
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
UPDATER = HERE / "install" / "update_agent.ps1"
BAT = HERE / "UPDATE_POSENTINE.bat"
# The release artifact is ship/ zipped with a posentine/ prefix
# (make_ship.make_zip). ship/ is gitignored and is (re)built by
# test_preflight.py on every full run — and that test also creates and
# then deletes the repo-root zip — so these rehearsals build their own
# release zip from ship/ inside the sandbox instead.
SHIP = HERE / "ship"
SHIP_MANIFEST = SHIP / "MANIFEST.txt"

# Windows-only by nature: this is a PowerShell updater for a Windows
# till. Skipped loudly, like test_install_agent.py does.
powershell = shutil.which("powershell") or shutil.which("pwsh")
requires_powershell = pytest.mark.skipif(
    powershell is None,
    reason="needs Windows PowerShell — the updater targets the POS machine",
)

needs_ship = pytest.mark.skipif(
    not SHIP_MANIFEST.exists(),
    reason="needs the built ship folder (python make_ship.py) in the repo root",
)

# The exact success markers preflight.py prints. The en-dash in
# "steps 1–4 PASSED" is deliberate: the updater matches it through a
# '?' wildcard, and this fixture must be able to satisfy that.
PREFLIGHT_PASS = (
    "[ OK ] golden baseline: 31 passed\n"
    "VERIFY.md steps 1\u20134 PASSED\n"
    "read-only proof  OK \u2014 the POS refused every write we attempted\n"
    "[ OK ] VERDICT: PASS \u2014 watermark 958907, 0 invoice(s) to upload\n"
)
CONFIRM_PASS = "RESULT: OK \u2014 cloud confirmation\n"


# ════════════════════════════════════════════════════════════════
# sandbox
# ════════════════════════════════════════════════════════════════

def build_release_zip(ship: Path, zip_path: Path) -> None:
    """Mirror make_ship.make_zip: every file under ship/, prefixed with
    posentine/, so the updater's nested-payload detection runs for real."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(ship.rglob("*")):
            if path.is_file():
                zf.write(path, Path("posentine") / path.relative_to(ship))


def manifest_commit(install: Path) -> str:
    line = next(line for line in install.joinpath("MANIFEST.txt")
                .read_text(encoding="utf-8").splitlines()
                if line.startswith("# built from:"))
    return line.split(":", 1)[1].strip()


def build_sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A realistic pre-update install + a Downloads dir with a release zip.

    Returns (install, downloads, release_zip). The install IS ship/ plus
    the stateful files a machine creates after install; the release zip
    is built from the same ship/ inside the sandbox.
    """
    install = tmp_path / "posentine"
    shutil.copytree(SHIP, install)
    if not (install / "config.json").exists():
        shutil.copy2(HERE / "config.example.json", install / "config.json")
    (install / "state.json").write_text(
        '{"watermark_salid": 958907}\n', encoding="utf-8")
    (install / "agent.log").write_text(
        "2026-08-11 20:00:00 cycle ok\n2026-08-11 20:03:00 cycle ok\n",
        encoding="utf-8")
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    release = downloads / f"posentine-{manifest_commit(install)[:12]}.zip"
    build_release_zip(SHIP, release)
    return install, downloads, release


def run_updater(install: Path, downloads: Path, *extra: str,
                timeout: int = 180) -> subprocess.CompletedProcess:
    cmd = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
           str(UPDATER), "-InstallRoot", str(install),
           "-DownloadsDir", str(downloads), *extra]
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def write_fixture(path: Path, text: str) -> Path:
    """UTF-8 with BOM: Windows PowerShell 5.1 reads a BOM-less file as
    ANSI and would mangle the en-dash, and the fixture must be read the
    way the real preflight output arrives."""
    path.write_bytes(text.encode("utf-8-sig"))
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ════════════════════════════════════════════════════════════════
# static invariants — run everywhere
# ════════════════════════════════════════════════════════════════

def test_updater_ps1_has_utf8_bom_and_crlf():
    """Same convention as install_agent.ps1: BOM so PowerShell 5.1 does
    not misread the file, CRLF because it is a Windows script."""
    raw = UPDATER.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_bat_stays_thin_and_calls_the_updater():
    bat = BAT.read_text(encoding="utf-8-sig")
    assert "install\\update_agent.ps1" in bat
    assert "powershell" in bat
    # The phase logic lives in the ps1, not in cmd. The bat may SAY
    # "backup" to the operator, but it must not do any of the work.
    for forbidden in ("Expand-Archive", "Get-ScheduledTask",
                      "uninstall_agent", "install_agent", "agent.py"):
        assert forbidden not in bat, forbidden


def test_bat_never_uses_git():
    assert "git" not in BAT.read_text(encoding="utf-8-sig").lower()


requires_cmd = pytest.mark.skipif(
    os.name != "nt",
    reason="needs cmd.exe - the bat precheck runs under Windows cmd",
)


@requires_cmd
def test_bat_stops_cleanly_when_the_updater_is_not_next_to_it(tmp_path):
    """The exact failure shape from the till: a loose copy of the bat in
    a folder with no install\\ next to it (the customer's scenario). It
    must stop with a clear message and exit 1, and must never reach the
    PowerShell invocation."""
    work = tmp_path / "stray"
    work.mkdir()
    shutil.copy2(BAT, work / "UPDATE_POSENTINE.bat")
    result = subprocess.run(
        ["cmd", "/c", "UPDATE_POSENTINE.bat"],
        cwd=str(work), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=60)
    assert result.returncode == 1, result.stdout
    assert "update_agent.ps1 was not found next to this file" in result.stdout
    assert "Nothing was run and nothing was changed" in result.stdout
    assert "powershell" not in result.stdout.lower()


@requires_cmd
def test_bat_stops_cleanly_in_the_extracted_delivery_folder(tmp_path):
    """A bat double-clicked inside the freshly extracted delivery folder
    (install\\update_agent.ps1 present, config.json absent - config.json
    is never in a release zip) is the delivery-folder trap. The bat must
    name it before PowerShell ever runs."""
    work = tmp_path / "extracted"
    (work / "install").mkdir(parents=True)
    shutil.copy2(BAT, work / "UPDATE_POSENTINE.bat")
    shutil.copy2(UPDATER, work / "install" / "update_agent.ps1")
    result = subprocess.run(
        ["cmd", "/c", "UPDATE_POSENTINE.bat"],
        cwd=str(work), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=60)
    assert result.returncode == 1, result.stdout
    assert "extracted delivery folder" in result.stdout
    assert "Nothing was run and nothing was changed" in result.stdout
    assert "powershell" not in result.stdout.lower()


def test_ps1_never_runs_git():
    """The till is not a git repository and the updater must not need
    one. Comments and docstrings may SAY git; executable code may not
    use it."""
    in_block = False
    for line in UPDATER.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if in_block:
            if s.endswith("#>"):
                in_block = False
            continue
        if s.startswith("<#"):
            in_block = not s.endswith("#>")
            continue
        if s.startswith("#"):
            continue
        assert "git" not in line.lower(), line


def test_protected_names_are_never_touched():
    text = UPDATER.read_text(encoding="utf-8-sig")
    assert "'config.json', 'state.json', 'agent.log'" in text
    assert "'state.lock'" in text and "'logs'" in text and "'_backup'" in text
    for line in text.splitlines():
        s = line.strip()
        if s.lower().startswith(("remove-item", "del ")):
            assert not any(p in s for p in ("config.json", "state.json",
                                            "agent.log")), line


# ════════════════════════════════════════════════════════════════
# rehearsals — against the real ship folder, in a sandbox
# ════════════════════════════════════════════════════════════════

@requires_powershell
@needs_ship
def test_full_update_rehearsal_succeeds_and_preserves_state(tmp_path):
    install, downloads, release = build_sandbox(tmp_path)
    before = {n: sha256(install / n)
              for n in ("config.json", "state.json", "agent.log")}
    pf = write_fixture(tmp_path / "preflight.txt", PREFLIGHT_PASS)
    cf = write_fixture(tmp_path / "confirm.txt", CONFIRM_PASS)

    result = run_updater(install, downloads, "-SkipTaskOps", "-SkipMonitor",
                         "-PreflightTextFile", str(pf),
                         "-ConfirmTextFile", str(cf))
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout

    assert "UPDATE SUCCESS" in out
    for label in ("Preflight:", "Scheduled Task:", "Natural Agent Cycle:",
                  "Cloud Confirmation:"):
        assert label in out
    for label in ("Config:", "State:", "Customer Data:"):
        assert label in out
    assert "PRESERVED" in out and "UNTOUCHED" in out
    # The banner dividers render as 66 '=' characters, not as the tokens
    # '=' * 66 (a command-mode parse trap that once printed literally).
    assert "=" * 66 in out
    assert "= * 66" not in out

    # The code on the machine is now the release's code, and the release
    # is the same code ship/ was built from.
    with zipfile.ZipFile(release) as zf:
        want_report = hashlib.sha256(zf.read("posentine/report.py")).hexdigest()
    assert sha256(install / "report.py") == want_report
    assert sha256(install / "report.py") == sha256(SHIP / "report.py")
    assert manifest_commit(install) == manifest_commit(SHIP)

    # The stateful files are byte-identical to before the update.
    after = {n: sha256(install / n)
             for n in ("config.json", "state.json", "agent.log")}
    assert before == after

    # The backup holds exactly what the procedure promises. Since the
    # F-1 hardening (2026-08-12), config.json is NEVER stored in a
    # backup: only its sha256 is recorded, and the recorded hash must
    # match the live file. Credentials must not sit at rest in _backup\\.
    backups = list((install / "_backup").iterdir())
    assert len(backups) == 1
    b = backups[0]
    assert not (b / "config.json").exists()          # F-1: no plaintext copy
    recorded = (b / "config.json.sha256").read_text(encoding="ascii").strip().lower()
    assert recorded == sha256(install / "config.json")
    for name in ("state.json", "agent.log", "backup_list.txt"):
        assert (b / name).exists(), name
    assert (b / "code" / "MANIFEST.txt").exists()
    assert (b / "code" / "report.py").exists()
    # The backup inventory must not list the config.json FILE itself
    # (config.json.sha256 legitimately contains the substring).
    listed = (b / "backup_list.txt").read_text(encoding="utf-8-sig").splitlines()
    assert "config.json" not in [ln.strip() for ln in listed]

    # updater.log: timestamped, every stage, ending in SUCCESS.
    log = (install / "logs" / "updater.log").read_text(encoding="utf-8-sig")
    assert re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] UPDATE START", log)
    assert "UPDATE START : install=" in log and "downloads=" in log
    for stage in ("BACKUP", "UPDATE", "PREFLIGHT", "CONFIRM", "UPDATE SUCCESS"):
        assert stage in log
    assert "config.json sha256 recorded (file itself not copied)" in log


@requires_powershell
@needs_ship
def test_preflight_failure_rolls_back_previous_code(tmp_path):
    install, downloads, _release = build_sandbox(tmp_path)
    # A distinguishable OLD state: a fake old commit in MANIFEST and a
    # local tweak to report.py — both must survive the failed update.
    manifest = install / "MANIFEST.txt"
    old_manifest = re.sub(r"# built from: \w+",
                          "# built from: " + "f" * 40,  # fake old commit
                          manifest.read_text(encoding="utf-8"))
    manifest.write_text(old_manifest, encoding="utf-8")
    report = install / "report.py"
    old_report = report.read_text(encoding="utf-8")
    report.write_text(old_report + "# local tweak marker\n", encoding="utf-8")

    before = {n: sha256(install / n)
              for n in ("config.json", "state.json", "agent.log")}

    # Phase 5 fails: the preflight fixture carries no PASS markers.
    pf = write_fixture(tmp_path / "preflight.txt", "gate failed: nope\n")
    cf = write_fixture(tmp_path / "confirm.txt", CONFIRM_PASS)
    result = run_updater(install, downloads, "-SkipTaskOps", "-SkipMonitor",
                         "-PreflightTextFile", str(pf),
                         "-ConfirmTextFile", str(cf))
    assert result.returncode == 1, result.stdout
    out = result.stdout
    assert "UPDATE FAILED" in out
    assert "PREFLIGHT" in out
    assert "Rollback:   performed" in out
    assert "=" * 66 in out          # the FAILED banner divider renders

    # Previous code and MANIFEST are back; stateful files untouched.
    assert "ffffffffffffffffffffffffffffffffffffffff" in \
        (install / "MANIFEST.txt").read_text(encoding="utf-8")
    assert (install / "report.py").read_text(encoding="utf-8").endswith(
        "# local tweak marker\n")
    after = {n: sha256(install / n)
             for n in ("config.json", "state.json", "agent.log")}
    assert before == after


@requires_powershell
@needs_ship
def test_checksum_mismatch_stops_before_anything(tmp_path):
    install, downloads, _release = build_sandbox(tmp_path)
    result = run_updater(install, downloads, "-SkipTaskOps",
                         "-ExpectedSha256", "0" * 64)
    assert result.returncode == 1, result.stdout
    out = result.stdout
    assert "checksum mismatch" in out
    assert "UPDATE FAILED" in out
    # Nothing was touched: no backup, agent files intact.
    assert not (install / "_backup").exists()
    assert (install / "config.json").exists()
    assert (install / "state.json").exists()


@requires_powershell
@needs_ship
def test_artifact_carrying_protected_files_is_refused(tmp_path):
    install, downloads, _release = build_sandbox(tmp_path)
    # A defective zip that hides config.json one level deep, under the
    # posentine/ wrapper the real artifact uses. It is newest, so it wins.
    evil = downloads / "posentine-evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("posentine/agent.py", "# stand-in\n")
        zf.writestr("posentine/MANIFEST.txt", "# built from: evil\n")
        zf.writestr("posentine/config.json", '{"hijack": true}\n')
    config_before = sha256(install / "config.json")

    result = run_updater(install, downloads, "-SkipTaskOps", "-SkipMonitor",
                         "-NoRollback")
    assert result.returncode == 1, result.stdout
    assert "contains a protected name" in result.stdout
    # The live config.json was never overwritten.
    assert sha256(install / "config.json") == config_before


@requires_powershell
@needs_ship
def test_precheck_only_modifies_nothing(tmp_path):
    install, downloads, _release = build_sandbox(tmp_path)
    manifest_before = (install / "MANIFEST.txt").read_bytes()
    result = run_updater(install, downloads, "-SkipTaskOps", "-PrecheckOnly")
    assert result.returncode == 0, result.stdout
    assert "PRECHECK OK" in result.stdout
    assert not (install / "_backup").exists()
    assert (install / "MANIFEST.txt").read_bytes() == manifest_before
    assert (install / "config.json").exists()


# ════════════════════════════════════════════════════════════════
# Phase 7 — the natural-cycle verdict, driven by task-info fixtures
# ════════════════════════════════════════════════════════════════
# -MonitorTaskInfoFile stands in for Get-ScheduledTaskInfo. The monitor
# judges its first poll immediately, so a verdict resolves in one pass.

@requires_powershell
@needs_ship
def test_monitor_passes_on_a_clean_natural_cycle(tmp_path):
    install, downloads, _release = build_sandbox(tmp_path)
    pf = write_fixture(tmp_path / "preflight.txt", PREFLIGHT_PASS)
    cf = write_fixture(tmp_path / "confirm.txt", CONFIRM_PASS)
    task = write_fixture(tmp_path / "task.json",
                         '{"Present": true, "LastRunTime": "2099-01-01T00:00:00", '
                         '"LastTaskResult": 0}')
    result = run_updater(install, downloads, "-SkipTaskOps",
                         "-MonitorTaskInfoFile", str(task),
                         "-PreflightTextFile", str(pf),
                         "-ConfirmTextFile", str(cf))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Natural Agent Cycle: PASS" in result.stdout


@requires_powershell
@needs_ship
def test_monitor_fails_on_a_nonzero_task_result(tmp_path):
    install, downloads, _release = build_sandbox(tmp_path)
    pf = write_fixture(tmp_path / "preflight.txt", PREFLIGHT_PASS)
    cf = write_fixture(tmp_path / "confirm.txt", CONFIRM_PASS)
    task = write_fixture(tmp_path / "task.json",
                         '{"Present": true, "LastRunTime": "2099-01-01T00:00:00", '
                         '"LastTaskResult": 42}')
    result = run_updater(install, downloads, "-SkipTaskOps", "-NoRollback",
                         "-MonitorTaskInfoFile", str(task),
                         "-PreflightTextFile", str(pf),
                         "-ConfirmTextFile", str(cf))
    assert result.returncode == 1, result.stdout
    assert "UPDATE FAILED" in result.stdout
    assert "MONITOR" in result.stdout
    assert "LastTaskResult 42" in result.stdout


@requires_powershell
@needs_ship
def test_monitor_fails_when_the_task_disappears(tmp_path):
    install, downloads, _release = build_sandbox(tmp_path)
    pf = write_fixture(tmp_path / "preflight.txt", PREFLIGHT_PASS)
    cf = write_fixture(tmp_path / "confirm.txt", CONFIRM_PASS)
    task = write_fixture(tmp_path / "task.json", "{}")  # no Present property
    result = run_updater(install, downloads, "-SkipTaskOps", "-NoRollback",
                         "-MonitorTaskInfoFile", str(task),
                         "-PreflightTextFile", str(pf),
                         "-ConfirmTextFile", str(cf))
    assert result.returncode == 1, result.stdout
    assert "UPDATE FAILED" in result.stdout
    assert "the task disappeared" in result.stdout


@requires_powershell
@needs_ship
def test_an_unhandled_error_after_backup_still_rolls_back(tmp_path):
    """The outer catch must recover like Fail does. An unexpected error
    AFTER the backup (here: the preflight fixture path does not exist, so
    Get-Content throws) must restore the previous code and MANIFEST, not
    leave a half-updated install with the task stopped."""
    install, downloads, _release = build_sandbox(tmp_path)
    manifest = install / "MANIFEST.txt"
    manifest.write_text(
        re.sub(r"# built from: \w+", "# built from: " + "f" * 40,
               manifest.read_text(encoding="utf-8")), encoding="utf-8")
    report = install / "report.py"
    report.write_text(report.read_text(encoding="utf-8") + "# tweak\n",
                      encoding="utf-8")

    missing = tmp_path / "does-not-exist.txt"  # never created
    result = run_updater(install, downloads, "-SkipTaskOps", "-SkipMonitor",
                         "-PreflightTextFile", str(missing),
                         "-ConfirmTextFile", str(tmp_path / "also-missing.txt"))
    assert result.returncode == 1, result.stdout
    out = result.stdout
    assert "UPDATE FAILED" in out
    assert "UNHANDLED ERROR" in out
    assert "Rollback:   performed" in out
    assert "ffffffffffffffffffffffffffffffffffffffff" in \
        (install / "MANIFEST.txt").read_text(encoding="utf-8")
    assert (install / "report.py").read_text(encoding="utf-8").endswith("# tweak\n")


# ════════════════════════════════════════════════════════════════
# the 02:16 production regression — agent.log held by the agent
# ════════════════════════════════════════════════════════════════
# On 2026-08-12 02:16 the shipped updater died with "being used by
# another process": [IO.File]::OpenRead asks for FileShare.Read, which
# collides with the agent's own read+write handle on agent.log, and the
# old Get-Sha256 had the same flaw through Get-FileHash. The fix opens
# with FileShare::ReadWrite — the sharing the agent itself grants — and
# retries a bounded number of times (5 x 500 ms). These tests hold a
# GENUINE handle on agent.log the way the agent's logger does and prove
# the updater and its helpers survive it.


def hold_agent_log_handle(path: Path):
    """Open agent.log exactly the way the agent's logger does: read+write
    access, sharing read+write. A reader that asks for less sharing (like
    OpenRead's FileShare.Read) is refused — the 02:16 condition."""
    import ctypes
    from ctypes import wintypes
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(path), GENERIC_READ | GENERIC_WRITE,
                                  FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                  OPEN_EXISTING, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError("CreateFileW failed to hold agent.log")
    return kernel32, handle


def write_dotsource_script(tmp_path: Path, body: str) -> tuple[Path, Path]:
    """Write the throwaway dot-source script. Returns (script, sandbox)."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(exist_ok=True)
    script = tmp_path / "dotsource.ps1"
    script.write_text(
        ". '" + str(UPDATER).replace("'", "''") + "' -SkipRun -InstallRoot '"
        + str(sandbox).replace("'", "''") + "'\n" + body + "\n",
        encoding="utf-8-sig")
    return script, sandbox


def run_dotsourced(tmp_path: Path, body: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Dot-source update_agent.ps1 with -SkipRun (its functions become
    callable, nothing runs) inside a throwaway script, then execute
    $body. -InstallRoot is pinned to the sandbox so Write-Log can never
    touch the repository."""
    script, _sandbox = write_dotsource_script(tmp_path, body)
    return subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(script)], capture_output=True, text=True, encoding="utf-8",
         errors="replace", timeout=timeout)


def lock_file_exclusive(path: Path):
    """Open path with FILE_SHARE_NONE - a genuinely exclusive handle that
    refuses every other open (stronger than the agent's shared handle, so
    it also proves the retry budget). Returns (kernel32, handle)."""
    import ctypes
    from ctypes import wintypes
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(path), GENERIC_READ | GENERIC_WRITE, 0,
                                  None, OPEN_EXISTING, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError("CreateFileW failed to lock agent.log exclusively")
    return kernel32, handle


def wait_for_retry_log(sandbox: Path, timeout: float = 60.0) -> None:
    """Block until the updater's Write-Log records a RETRY line."""
    log = sandbox / "logs" / "updater.log"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log.exists() and "RETRY" in log.read_text(encoding="utf-8-sig",
                                                     errors="replace"):
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for a RETRY line in updater.log")


@requires_powershell
@needs_ship
def test_full_update_succeeds_while_agent_log_is_held_open(tmp_path):
    """The 02:16 incident, faithfully: agent.log is held open by another
    process (the agent's exact read+write/shared handle) for the whole
    update. The old OpenRead/Get-FileHash paths failed this; the hardened
    updater must complete and leave every stateful file byte-identical."""
    install, downloads, _release = build_sandbox(tmp_path)
    kernel32, handle = hold_agent_log_handle(install / "agent.log")
    try:
        pf = write_fixture(tmp_path / "preflight.txt", PREFLIGHT_PASS)
        cf = write_fixture(tmp_path / "confirm.txt", CONFIRM_PASS)
        before = {n: sha256(install / n)
                  for n in ("config.json", "state.json", "agent.log")}
        result = run_updater(install, downloads, "-SkipTaskOps", "-SkipMonitor",
                             "-PreflightTextFile", str(pf),
                             "-ConfirmTextFile", str(cf))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "UPDATE SUCCESS" in result.stdout
        after = {n: sha256(install / n)
                 for n in ("config.json", "state.json", "agent.log")}
        assert before == after
        # The backup recorded the config hash, never the file.
        b = next((install / "_backup").iterdir())
        assert not (b / "config.json").exists()
        assert (b / "config.json.sha256").exists()
    finally:
        kernel32.CloseHandle(handle)


@requires_powershell
def test_sha256_reads_agent_log_held_by_another_handle(tmp_path):
    """The core 02:16 fix: hashing the LIVE agent.log succeeds while the
    agent's read+write/shared handle is on the file. Get-FileHash failed
    this; Get-Sha256 with FileShare::ReadWrite must not."""
    log = tmp_path / "agent.log"
    data = b"line one\nline two\n"
    log.write_bytes(data)
    want = hashlib.sha256(data).hexdigest()
    p = str(log).replace("'", "''")
    body = (
        "$fs = New-Object System.IO.FileStream('" + p + "', "
        "[System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, "
        "[System.IO.FileShare]::ReadWrite)\n"
        "try { $h = Get-Sha256 -Path '" + p + "' } finally { $fs.Dispose() }\n"
        "Write-Output $h\n"
    )
    result = run_dotsourced(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().lower() == want


@requires_powershell
def test_sha256_survives_a_transient_lock(tmp_path):
    """A transient exclusive lock (the agent mid-append) must be absorbed
    by the bounded retry: the hash comes back, not an abort. The lock is
    held from the TEST process and released the moment the updater's
    first RETRY is logged - deterministic, no sleeps to guess."""
    log = tmp_path / "agent.log"
    data = b"cycle 1 ok\ncycle 2 ok\n"
    log.write_bytes(data)
    want = hashlib.sha256(data).hexdigest()
    p = str(log).replace("'", "''")
    body = (
        "$h = Get-Sha256 -Path '" + p + "'\n"
        "Write-Output $h\n"
    )
    script, sandbox = write_dotsource_script(tmp_path, body)
    kernel32, handle = lock_file_exclusive(log)
    try:
        proc = subprocess.Popen(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
             text=True, encoding="utf-8", errors="replace")
        try:
            # First attempt fails against the lock; release just after the
            # RETRY is logged so the next attempt (500 ms later) succeeds.
            wait_for_retry_log(sandbox)
        finally:
            kernel32.CloseHandle(handle)
        out, _ = proc.communicate(timeout=60)
    finally:
        kernel32.CloseHandle(handle)
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out
    assert out.strip().splitlines()[-1].strip().lower() == want
    # Prove the retry really engaged before the release.
    updater_log = sandbox / "logs" / "updater.log"
    assert "transient (1/5)" in updater_log.read_text(encoding="utf-8-sig",
                                                      errors="replace")


@requires_powershell
def test_sha256_fails_closed_on_a_persistent_lock(tmp_path):
    """A lock that never releases must exhaust the retry budget and
    re-raise — a real failure stays a failure, never a silent skip."""
    log = tmp_path / "agent.log"
    log.write_bytes(b"held forever\n")
    p = str(log).replace("'", "''")
    body = (
        "$fs = New-Object System.IO.FileStream('" + p + "', "
        "[System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, "
        "[System.IO.FileShare]::None)\n"
        "try {\n"
        "    try { Get-Sha256 -Path '" + p + "' | Out-Null; "
        "'UNEXPECTED SUCCESS' }\n"
        "    catch { 'THREW: ' + $_.Exception.Message }\n"
        "} finally { $fs.Dispose() }\n"
    )
    result = run_dotsourced(tmp_path, body, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "UNEXPECTED SUCCESS" not in result.stdout
    assert "THREW:" in result.stdout
    # The retry budget was actually spent (5 x 500 ms).
    updater_log = tmp_path / "sandbox" / "logs" / "updater.log"
    assert "failed after 5 attempts" in updater_log.read_text(
        encoding="utf-8-sig", errors="replace")


@requires_powershell
def test_read_new_log_bytes_roundtrips_utf8(tmp_path):
    """New log lines come back byte-identical UTF-8 — the F-4 mojibake
    class is gone (Get-Content read the ANSI code page)."""
    log = tmp_path / "agent.log"
    arabic = "\u0628\u064a\u0627\u0646\u0627\u062a \u0647\u0630\u0647 " \
             "\u0627\u0644\u0648\u0631\u062f\u064a\u0629 \u063a\u064a\u0631 " \
             "\u0645\u0643\u062a\u0645\u0644\u0629\n"
    data = ("2026-08-12 07:00:00 cycle ok\n" + arabic).encode("utf-8")
    log.write_bytes(data)
    p = str(log).replace("'", "''")
    offset = len("2026-08-12 07:00:00 cycle ok\n".encode("utf-8"))
    count = len(arabic.encode("utf-8"))
    body = (
        "$s = Read-NewLogBytes -Path '" + p + "' -Offset " + str(offset) +
        " -Count " + str(count) + "\n"
        "Write-Output $s\n"
    )
    result = run_dotsourced(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    # Write-Output appends one newline of its own.
    assert result.stdout.rstrip("\r\n") == arabic.rstrip("\r\n")


@requires_powershell
def test_prefix_hash_tolerates_legitimate_log_growth(tmp_path):
    """A live log that GREW between backup and verify must not be judged
    tampered: the first N bytes (the backup's length) hash identically."""
    log = tmp_path / "agent.log"
    backup = tmp_path / "agent.log.bak"
    first = (b"cycle 1\n" * 100)
    log.write_bytes(first + b"cycle 101 (appended after backup)\n")
    backup.write_bytes(first)
    p = str(log).replace("'", "''")
    b = str(backup).replace("'", "''")
    body = (
        "$ph = Get-PrefixSha256 -Path '" + p + "' -Length " +
        str(len(first)) + "\n"
        "$bh = Get-Sha256 -Path '" + b + "'\n"
        "if ($ph -eq $bh) { 'GROWTH-OK' } else { "
        "'MISMATCH: ' + $ph + ' vs ' + $bh }\n"
    )
    result = run_dotsourced(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GROWTH-OK" in result.stdout


@requires_powershell
def test_prefix_hash_detects_tampering(tmp_path):
    """A log whose backed-up prefix was actually modified must still be
    caught — growth tolerance is not tamper tolerance."""
    log = tmp_path / "agent.log"
    backup = tmp_path / "agent.log.bak"
    first = bytearray(b"trusted baseline\n" * 50)
    backup.write_bytes(bytes(first))
    first[100:106] = b"TAMPER"  # modify INSIDE the backed-up prefix
    log.write_bytes(bytes(first) + b"appended later\n")
    p = str(log).replace("'", "''")
    b = str(backup).replace("'", "''")
    body = (
        "$ph = Get-PrefixSha256 -Path '" + p + "' -Length " +
        str(len(first)) + "\n"
        "$bh = Get-Sha256 -Path '" + b + "'\n"
        "if ($ph -ne $bh) { 'TAMPER-DETECTED' } else { 'TAMPER-MISSED' }\n"
    )
    result = run_dotsourced(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TAMPER-DETECTED" in result.stdout


@requires_powershell
def test_copy_file_with_retry_copies_a_shared_locked_log(tmp_path):
    """The backup copy of agent.log must tolerate the agent's handle
    (Copy-Item does; the retry covers the odd transient)."""
    log = tmp_path / "agent.log"
    data = b"some log content\n" * 20
    log.write_bytes(data)
    dst = tmp_path / "agent.log.bak"
    p = str(log).replace("'", "''")
    d = str(dst).replace("'", "''")
    body = (
        "$fs = New-Object System.IO.FileStream('" + p + "', "
        "[System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, "
        "[System.IO.FileShare]::ReadWrite)\n"
        "try { Copy-FileWithRetry -Source '" + p + "' -Destination '" +
        d + "' -What 'copy probe' } finally { $fs.Dispose() }\n"
        "if (Test-Path -LiteralPath '" + d + "') { 'COPY-OK' } "
        "else { 'COPY-MISSING' }\n"
    )
    result = run_dotsourced(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "COPY-OK" in result.stdout
    assert dst.read_bytes() == data


@requires_powershell
def test_malformed_artifact_without_agent_py_is_refused(tmp_path):
    """A zip that is not a ship artifact (no agent.py at its root) must be
    refused during staging, before anything is copied."""
    install, downloads, _release = build_sandbox(tmp_path)
    bad = downloads / "posentine-notaship.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("posentine/README.md", "# not a ship\n")
        zf.writestr("posentine/MANIFEST.txt", "# built from: nope\n")
    report_before = sha256(install / "report.py")
    result = run_updater(install, downloads, "-SkipTaskOps", "-SkipMonitor",
                         "-NoRollback")
    assert result.returncode == 1, result.stdout
    assert "has no agent.py at its root" in result.stdout
    assert sha256(install / "report.py") == report_before


@requires_powershell
@needs_ship
def test_expected_sha_pin_accepts_the_matching_artifact(tmp_path):
    """The full pin chain works: hash the built artifact, hand the hash
    to the updater, and the same zip passes. (The pin is exactly how
    UPDATE_POSENTINE.bat will behave once EXPECTED_SHA is set.)"""
    install, downloads, release = build_sandbox(tmp_path)
    pf = write_fixture(tmp_path / "preflight.txt", PREFLIGHT_PASS)
    cf = write_fixture(tmp_path / "confirm.txt", CONFIRM_PASS)
    artifact_sha = hashlib.sha256(release.read_bytes()).hexdigest()
    result = run_updater(install, downloads, "-SkipTaskOps", "-SkipMonitor",
                         "-ExpectedSha256", artifact_sha,
                         "-PreflightTextFile", str(pf),
                         "-ConfirmTextFile", str(cf))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "UPDATE SUCCESS" in result.stdout
    assert "sha256 matches the configured expected value" in result.stdout
