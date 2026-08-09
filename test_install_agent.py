# -*- coding: utf-8 -*-
"""
test_install_agent.py — the task we would register, checked before we do
================================================================
`install_agent.ps1 -ShowXml` prints the exact XML it would hand to the
scheduler and registers nothing. That is what makes step 7 testable on a
machine that is not the POS: the properties below are the ones that
silently change behaviour if they are wrong, and every one of them is
read out of the real script's real output.

What a wrong value costs, in order of how quietly it fails:

  * a <Duration> on the repetition — the agent stops after that long and
    the heartbeat simply goes quiet
  * RunLevel HighestAvailable — needs an administrator; the till account
    is not one, so the task never runs
  * a missing PYTHONUTF8 — Arabic item names crash the cycle on a cp1252
    console, but only once a receipt contains one
  * python.exe as the action — a console window on the till every three
    minutes, during service

None of this proves the scheduler accepts it. That is proven separately,
by registering it, and finally on site.
================================================================
"""

from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
INSTALL = HERE / "install" / "install_agent.ps1"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

# Windows-only by nature: there is no Task Scheduler to install into
# anywhere else. Skipped loudly rather than quietly passing.
powershell = shutil.which("powershell") or shutil.which("pwsh")
requires_powershell = pytest.mark.skipif(
    powershell is None,
    reason="needs Windows PowerShell — install_agent.ps1 targets the POS machine",
)


@pytest.fixture(scope="module")
def task_xml() -> ET.Element:
    """The XML the real script would register, parsed."""
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(INSTALL), "-ShowXml"],
        cwd=str(HERE), capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    start = result.stdout.index("<?xml")
    return ET.fromstring(result.stdout[start:])


@pytest.fixture(scope="module")
def arguments(task_xml) -> str:
    node = task_xml.find(".//t:Actions/t:Exec/t:Arguments", NS)
    assert node is not None and node.text
    return node.text


# ════════════════════════════════════════════════════════════════
# the trigger
# ════════════════════════════════════════════════════════════════

@requires_powershell
def test_it_repeats_every_three_minutes(task_xml):
    interval = task_xml.find(".//t:TimeTrigger/t:Repetition/t:Interval", NS)
    assert interval is not None
    assert interval.text == "PT3M"


@requires_powershell
def test_the_repetition_never_ends(task_xml):
    """A <Duration> is how "every 3 minutes" quietly becomes "for a day".
    Absent means indefinite, by definition of the schema."""
    repetition = task_xml.find(".//t:TimeTrigger/t:Repetition", NS)
    assert repetition is not None
    assert repetition.find("t:Duration", NS) is None
    assert repetition.find("t:StopAtDurationEnd", NS).text == "false"


@requires_powershell
def test_the_repetition_is_not_on_the_logon_trigger(task_xml):
    """🔴 The one that was wrong, and cost nothing only because it was
    measured before the visit.

    A logon trigger fires on a logon *event*. Registering it while the till
    user is already logged on does not produce one, and a repetition hangs
    off its trigger — so the task sits there, enabled and correct-looking,
    and runs zero times. Measured on 2026-08-09, no logoff, over 4 minutes:

        LogonTrigger fired 0 times      NextRunTime = (empty)
        TimeTrigger  fired 4 times      NextRunTime = 18:42:42

    LastTaskResult was 267011 — SCHED_S_TASK_HAS_NOT_RUN. On a till that
    stays logged in for weeks, the agent would never have run at all.
    """
    logon = task_xml.find(".//t:Triggers/t:LogonTrigger", NS)
    assert logon is not None, "a reboot should still start a cycle promptly"
    assert logon.find("t:Repetition", NS) is None, (
        "the repetition is on the logon trigger again — that task never runs "
        "while the user is already logged on")


@requires_powershell
def test_it_starts_without_waiting_for_a_logon(task_xml):
    """The start boundary is in the past, so the task is due the moment it
    is registered. Phase E of the installer waits for a run; without this
    it would be waiting for someone to log out and back in."""
    import datetime as dt

    start = task_xml.find(".//t:TimeTrigger/t:StartBoundary", NS)
    assert start is not None and start.text
    boundary = dt.datetime.strptime(start.text, "%Y-%m-%dT%H:%M:%S")
    assert boundary <= dt.datetime.now() + dt.timedelta(seconds=90), (
        f"start boundary {boundary} is in the future; the first run would wait")


@requires_powershell
def test_exactly_one_trigger_repeats(task_xml):
    """Two repeating triggers would fire at different offsets and compete."""
    repeating = [t for t in task_xml.findall(".//t:Triggers/*", NS)
                 if t.find("t:Repetition", NS) is not None]
    assert len(repeating) == 1, [t.tag for t in repeating]


# ════════════════════════════════════════════════════════════════
# the principal — user level, never SYSTEM
# ════════════════════════════════════════════════════════════════

@requires_powershell
def test_it_runs_as_the_user_at_least_privilege(task_xml):
    principal = task_xml.find(".//t:Principals/t:Principal", NS)
    assert principal.find("t:RunLevel", NS).text == "LeastPrivilege"
    assert principal.find("t:LogonType", NS).text == "InteractiveToken"


@requires_powershell
def test_it_never_runs_as_a_service_account(task_xml):
    """S-1-5-18/19/20 and the *SERVICE names are the ways a task ends up
    with more rights than the till account has."""
    principal = task_xml.find(".//t:Principals/t:Principal", NS)
    assert principal.find("t:GroupId", NS) is None
    user = principal.find("t:UserId", NS).text.upper()
    for forbidden in ("SYSTEM", "S-1-5-18", "S-1-5-19", "S-1-5-20",
                      "LOCALSERVICE", "NETWORKSERVICE"):
        assert forbidden not in user


# ════════════════════════════════════════════════════════════════
# the action
# ════════════════════════════════════════════════════════════════

@requires_powershell
def test_the_window_is_hidden(arguments, task_xml):
    """python.exe as the action puts a console window on the till every
    three minutes. The hidden PowerShell wrapper is the whole reason the
    action is not python.exe."""
    assert task_xml.find(".//t:Actions/t:Exec/t:Command", NS).text == "powershell.exe"
    assert "-WindowStyle Hidden" in arguments


@requires_powershell
def test_it_runs_the_wrapper_and_not_the_agent_directly(arguments):
    assert "run_agent.ps1" in arguments
    assert "agent.py" not in arguments


@requires_powershell
def test_the_wrapper_sets_both_utf8_variables():
    """The variables live in run_agent.ps1 because a Scheduled Task action
    has no environment block. If they move, this test moves with them."""
    wrapper = (HERE / "install" / "run_agent.ps1").read_text(encoding="utf-8")
    assert re.search(r"\$env:PYTHONUTF8\s*=\s*'1'", wrapper)
    assert re.search(r"\$env:PYTHONIOENCODING\s*=\s*'utf-8'", wrapper)


@requires_powershell
def test_the_wrapper_passes_the_agents_exit_code_through():
    """LastTaskResult is what VERIFY.md step 7 reads. A wrapper that always
    exits 0 makes a failing agent look healthy for as long as nobody opens
    the log."""
    wrapper = (HERE / "install" / "run_agent.ps1").read_text(encoding="utf-8")
    assert "exit $LASTEXITCODE" in wrapper
    assert "$null -eq $LASTEXITCODE" in wrapper


@requires_powershell
def test_it_runs_from_the_install_folder(task_xml):
    working = task_xml.find(".//t:Actions/t:Exec/t:WorkingDirectory", NS)
    assert working is not None
    assert working.text == str(HERE)


@requires_powershell
def test_execution_policy_cannot_block_the_wrapper(arguments):
    """A machine left on the default Restricted policy would otherwise
    have a task that runs and does nothing, every three minutes."""
    assert "-ExecutionPolicy Bypass" in arguments
    assert "-NoProfile" in arguments


# ════════════════════════════════════════════════════════════════
# settings that decide whether it keeps running
# ════════════════════════════════════════════════════════════════

@requires_powershell
def test_battery_does_not_stop_it(task_xml):
    """Both default to true. On a till behind a UPS, the defaults stop the
    agent on the first power blip and nothing says so."""
    settings = task_xml.find(".//t:Settings", NS)
    assert settings.find("t:DisallowStartIfOnBatteries", NS).text == "false"
    assert settings.find("t:StopIfGoingOnBatteries", NS).text == "false"


@requires_powershell
def test_cycles_never_overlap(task_xml):
    settings = task_xml.find(".//t:Settings", NS)
    assert settings.find("t:MultipleInstancesPolicy", NS).text == "IgnoreNew"


@requires_powershell
def test_a_wedged_cycle_is_killed_before_it_blocks_the_next_forever(task_xml):
    """IgnoreNew means one hung cycle blocks every later one. The limit is
    what bounds that, and it matches agent.py's own LOCK_STALE_SECONDS so
    the task and the agent cannot disagree about when a cycle is dead."""
    settings = task_xml.find(".//t:Settings", NS)
    assert settings.find("t:ExecutionTimeLimit", NS).text == "PT15M"

    agent = (HERE / "agent.py").read_text(encoding="utf-8")
    match = re.search(r"LOCK_STALE_SECONDS\s*=\s*(\d+)\s*\*\s*60", agent)
    assert match, "LOCK_STALE_SECONDS moved; the task limit must follow it"
    assert int(match.group(1)) == 15


@requires_powershell
def test_the_task_is_visible_to_whoever_looks(task_xml):
    """A hidden task is one nobody finds when the agent misbehaves."""
    assert task_xml.find(".//t:Settings/t:Hidden", NS).text == "false"


@requires_powershell
def test_missed_runs_are_caught_up_after_a_reboot(task_xml):
    assert task_xml.find(".//t:Settings/t:StartWhenAvailable", NS).text == "true"


# ════════════════════════════════════════════════════════════════
# the script itself
# ════════════════════════════════════════════════════════════════

@requires_powershell
def test_show_xml_registers_nothing(task_xml):
    """Proven by the task not existing after the fixture ran -ShowXml."""
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command",
         "if (Get-ScheduledTask -TaskName thirdeyev -ErrorAction SilentlyContinue) "
         "{ 'PRESENT' } else { 'ABSENT' }"],
        capture_output=True, text=True)
    assert "ABSENT" in result.stdout, result.stdout


@requires_powershell
def test_it_refuses_to_install_without_a_config(tmp_path):
    """A task installed before config.json exists fails every three
    minutes, into a log nobody is reading yet."""
    staged = tmp_path / "thirdeyev"
    (staged / "install").mkdir(parents=True)
    (staged / "agent.py").write_text("# stand-in\n", encoding="utf-8")
    for name in ("install_agent.ps1", "run_agent.ps1"):
        shutil.copy2(HERE / "install" / name, staged / "install" / name)

    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(staged / "install" / "install_agent.ps1"),
         "-TaskName", "thirdeyev-should-never-exist"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 1
    assert "config.json is not in" in result.stdout
    assert "No task was registered" in result.stdout


@requires_powershell
def test_both_scripts_are_shipped():
    import make_ship as S
    shipped = {name for name, _ in S.SHIPPED}
    for required in ("install/install_agent.ps1", "install/uninstall_agent.ps1",
                     "install/run_agent.ps1"):
        assert required in shipped
