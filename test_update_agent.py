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
  * Phase 2  — backup: stateful files + every code file + MANIFEST
  * Phase 4  — staging extraction, protected-name refusal, code copy,
               protected files verified byte-identical, MANIFEST +
               report.py hash verification
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
import re
import shutil
import subprocess
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

    # The backup holds exactly what the procedure promises.
    backups = list((install / "_backup").iterdir())
    assert len(backups) == 1
    b = backups[0]
    for name in ("config.json", "state.json", "agent.log", "backup_list.txt"):
        assert (b / name).exists(), name
    assert (b / "code" / "MANIFEST.txt").exists()
    assert (b / "code" / "report.py").exists()

    # updater.log: timestamped, every stage, ending in SUCCESS.
    log = (install / "logs" / "updater.log").read_text(encoding="utf-8-sig")
    assert re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] UPDATE START", log)
    assert "UPDATE START : install=" in log and "downloads=" in log
    for stage in ("BACKUP", "UPDATE", "PREFLIGHT", "CONFIRM", "UPDATE SUCCESS"):
        assert stage in log


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
