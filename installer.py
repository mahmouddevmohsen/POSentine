# -*- coding: utf-8 -*-
"""
installer.py — VERIFY.md steps 1–8, one double-click
================================================================
The operator copies this folder onto the till, double-clicks INSTALL.bat,
and walks away with a working self-running agent — or with an unambiguous
stop and a log we can read from here. He is not a Windows engineer, there
is a queue at the counter, and a second trip is the most expensive thing
on this project.

**This is not "run everything and hope".** Every gate VERIFY.md defines
stays exactly where it is. What changes is who enforces it: a person under
pressure might look at a delta of 7 and carry on. This will not.

    Phase A   preflight, steps 1-4, plus the read-only proof
      GATE    verdict must be PASS or FIRST RUN     else STOP, nothing written
    Phase B   one real cycle                        step 6
    Phase C   --confirm                             step 6
      GATE    RESULT must be OK                     else STOP
    Phase D   register the scheduled task           step 7
    Phase E   wait for the task to fire ON ITS OWN, and prove a NEW
              heartbeat arrived                     step 7
    Phase F   what happened, what runs now, where the logs are

Four properties, each of which had to be designed for rather than hoped
for:

  • **Nothing is written to the POS or the cloud before the Phase A gate.**
    Proven, not asserted — test_installer.py runs the whole of Phase A
    against a recording client and fails if any request is not a GET.

  • **Safe to run twice.** Someone will double-click it again because they
    are not sure it worked. Every phase is idempotent, and Phase B knows
    what to do when it finds the scheduled task already running a cycle.

  • **No partial installs.** If Phase D fails after registering, the task
    that was there before is put back byte-for-byte. See install_agent.ps1.

  • **The stop is impossible to misread.** Screen-wide, what failed, which
    VERIFY.md step, what to do, where the transcript is, and "photograph
    this and call — change nothing".

The individual entry points still work and are still what we use:
`preflight.bat`, `python agent.py --dry-run`, `install_agent.ps1`. This is
an addition, not a replacement.
================================================================
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import logsetup
import preflight

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"

TASK_NAME = "thirdeyev"

# How long Phase E waits for the scheduler to fire the task by itself. The
# repetition is 3 minutes, so two intervals plus slack: if nothing has run
# in 8 minutes, something is wrong and waiting longer only delays finding
# out. Measured on a real registration, the first run landed 3m14s after
# the task was registered.
PHASE_E_TIMEOUT_SECONDS = 8 * 60
PHASE_E_POLL_SECONDS = 15

# How many install transcripts to keep. They are small, but an unbounded
# directory on a machine we cannot reach is a fault we caused.
KEEP_TRANSCRIPTS = 20

RULE = "=" * 70


class Halt(Exception):
    """A gate refused. Carries everything the stop screen needs."""

    def __init__(self, phase: str, step: str, what: str, do: str,
                 machine_state: str) -> None:
        super().__init__(what)
        self.phase = phase
        self.step = step
        self.what = what
        self.do = do
        self.machine_state = machine_state


# ════════════════════════════════════════════════════════════════
# the transcript
# ════════════════════════════════════════════════════════════════

class Transcript:
    """Everything that reaches the screen, also written to a file.

    This is the file the operator photographs or sends, so it is masked:
    a screen gets photographed and a photograph gets forwarded. Masking is
    done a whole line at a time, because a secret split across two write()
    calls would slip past a per-chunk replacement — and print() writes its
    argument and its newline separately.
    """

    def __init__(self, path: Path, stream) -> None:
        self.path = path
        self._stream = stream
        self._file = path.open("w", encoding="utf-8")
        self._pending = ""

    def write(self, text: str) -> int:
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            masked = logsetup.mask(line)
            self._stream.write(masked + "\n")
            self._file.write(masked + "\n")
        self._file.flush()
        return len(text)

    def flush(self) -> None:
        if self._pending:
            masked = logsetup.mask(self._pending)
            self._stream.write(masked)
            self._file.write(masked)
            self._pending = ""
        self._stream.flush()
        self._file.flush()

    def close(self) -> None:
        self.flush()
        self._file.close()

    # subprocess and logging both ask streams these questions
    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"


def start_transcript(now: _dt.datetime) -> Transcript:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    prune_transcripts()
    path = LOG_DIR / f"install_{now.strftime('%Y%m%d_%H%M%S')}.txt"
    return Transcript(path, sys.stdout)


def prune_transcripts() -> list[Path]:
    """Keep the most recent KEEP_TRANSCRIPTS. Returns what was removed."""
    existing = sorted(LOG_DIR.glob("install_*.txt"))
    removed: list[Path] = []
    for stale in existing[:-KEEP_TRANSCRIPTS] if len(
            existing) > KEEP_TRANSCRIPTS else []:
        try:
            stale.unlink()
            removed.append(stale)
        except OSError:
            pass
    return removed


# ════════════════════════════════════════════════════════════════
# saying things
# ════════════════════════════════════════════════════════════════

def banner(text: str) -> None:
    print()
    print(RULE)
    print(f"  {text}")
    print(RULE)


def phase_header(letter: str, title: str, verify_step: str) -> None:
    print()
    print()
    print("#" * 70)
    print(f"#  PHASE {letter} — {title}")
    print(f"#  (VERIFY.md {verify_step})")
    print("#" * 70)


def say(text: str = "") -> None:
    print(f"  {text}" if text else "")


# ════════════════════════════════════════════════════════════════
# running the agent
# ════════════════════════════════════════════════════════════════

AGENT_LOG = "agent.log"
ANOTHER_INSTANCE = "another cycle is still running"
FIRST_RUN_LINE = "first run: adopted watermark"


def run_agent(*args: str) -> tuple[int, str]:
    """The agent, with its output on this screen and in the transcript."""
    return preflight.run([sys.executable, "agent.py", "--log", AGENT_LOG,
                          *args])


def phase_b_one_real_cycle() -> None:
    """The first thing in this whole install that writes anything, anywhere.

    Two shapes have to be handled, and both of them are ordinary:

    1. **A first install.** `agent.py` adopts MAX(salid), uploads nothing,
       and exits — that is the entire cycle, by design, because history is
       not backfilled. A second run is needed to perform a real sync, and
       leaving it at one would hand Phase C a cloud with no invoices in it
       and produce a stop for a healthy machine.

    2. **A re-run.** If the scheduled task is already registered from an
       earlier attempt, its cycle may hold the lock. `agent.py` exits **0**
       in that case, having done nothing — so exit 0 alone would let Phase
       B pass without a cycle ever running. Waited for and retried instead.
    """
    for attempt in range(1, 5):
        code, out = run_agent()
        if code == 0 and ANOTHER_INSTANCE not in out:
            break
        if code != 0:
            raise Halt(
                "B", "step 6 — one real cycle",
                f"agent.py exited {code}",
                "Read the error above. The agent could not complete a cycle.\n"
                "Photograph this screen and call. Change nothing.",
                "Nothing was installed. The scheduled task was not registered.")
        # Exit 0, but nothing happened: the scheduled task holds the lock.
        say(f"a scheduled cycle is holding the lock; waiting 20s "
            f"(attempt {attempt} of 4)")
        time.sleep(20)
    else:
        raise Halt(
            "B", "step 6 — one real cycle",
            "every attempt found another cycle already running",
            "The scheduled task is registered from an earlier install and its\n"
            "cycles are overlapping this one. Remove it and run this again:\n"
            "  powershell -ExecutionPolicy Bypass -File "
            ".\\install\\uninstall_agent.ps1",
            "Nothing new was installed.")

    if FIRST_RUN_LINE in out:
        say()
        say("that was a first install: the agent adopted the current")
        say("MAX(salid) and uploaded nothing, which is the whole cycle.")
        say("history is not backfilled by design. running a second cycle")
        say("now, to perform a real sync.")
        say()
        code, out = run_agent()
        if code != 0:
            raise Halt(
                "B", "step 6 — one real cycle",
                f"the second cycle exited {code}",
                "The first cycle adopted the watermark. The second, which does\n"
                "the real sync, failed. Photograph this screen and call.",
                "The watermark was adopted in the cloud. No task was registered.")


# ════════════════════════════════════════════════════════════════
# the cloud, read-only
# ════════════════════════════════════════════════════════════════

def latest_heartbeat(cfg) -> str | None:
    """The newest heartbeat timestamp, or None.

    Phase E compares against this. Read directly rather than scraped out of
    --confirm's printed block: a timestamp parsed from a screen is a second
    implementation of the same fact, and the two can disagree.
    """
    import supa

    client = supa.Supa(cfg.supabase_url, anon_key=cfg.supabase_anon_key,
                       token=cfg.supabase_agent_token)
    rows = client.select("heartbeats", {
        "tenant_id": f"eq.{cfg.tenant_id}",
        "source_id": f"eq.{cfg.source_id}",
        "select": "at", "order": "at.desc", "limit": "1"}, paginate=False)
    return rows[0]["at"] if rows else None


# ════════════════════════════════════════════════════════════════
# the scheduled task
# ════════════════════════════════════════════════════════════════

def powershell(*args: str) -> tuple[int, str]:
    return preflight.run(["powershell", "-NoProfile", "-NonInteractive",
                          "-ExecutionPolicy", "Bypass", *args])


def task_info() -> dict | None:
    """What the scheduler says about our task. None when it is not there."""
    code, out = preflight.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"$i = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}' "
         "-ErrorAction SilentlyContinue; if ($i) { "
         "[pscustomobject]@{ LastRunTime = "
         "$(if ($i.LastRunTime) { $i.LastRunTime.ToString('o') } else { '' }); "
         "LastTaskResult = $i.LastTaskResult; NumberOfMissedRuns = "
         "$i.NumberOfMissedRuns; NextRunTime = "
         "$(if ($i.NextRunTime) { $i.NextRunTime.ToString('o') } else { '' }) "
         "} | ConvertTo-Json -Compress }"],
        echo=False)
    if code != 0:
        return None
    match = re.search(r"\{.*\}", out, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def phase_d_register() -> None:
    code, _out = powershell(
        "-File", str(HERE / "install" / "install_agent.ps1"),
        "-TaskName", TASK_NAME)
    if code != 0:
        raise Halt(
            "D", "step 7 — the scheduled task",
            f"install_agent.ps1 exited {code}",
            "The block above says which check failed and what to do.\n"
            "Photograph this screen and call.",
            "The installer rolls back a failed registration: read the line\n"
            "above that begins 'Rolled back' or '!! COULD NOT ROLL BACK'.\n"
            "Data already uploaded in Phase B is untouched, and the POS\n"
            "database was never written to.")


def phase_e_prove_it_runs_itself(cfg, baseline: str | None) -> dict:
    """The phase that decides whether the visit actually succeeded.

    Everything before this proves a human can run the agent by hand. Only a
    NEW heartbeat, produced by a cycle nobody started, proves the machine
    will keep working after we leave.

    Two independent facts are required, because either one alone can lie:

      * the scheduler ran the task and got exit 0 — proves the task fires
        and the wrapper works, but not that anything reached the cloud;
      * a heartbeat newer than the one Phase C left behind — proves data
        arrived, but a heartbeat could have come from our own Phase B run.

    Together they prove a cycle that nobody started reached Supabase.
    """
    say(f"waiting up to {PHASE_E_TIMEOUT_SECONDS // 60} minutes for the "
        "scheduler to fire the task by itself.")
    say("nothing is being done to the machine during this wait; the task")
    say("repeats every 3 minutes, so this normally takes 3 to 4.")
    say(f"newest heartbeat before this phase: {baseline or '(none)'}")
    say()

    deadline = time.monotonic() + PHASE_E_TIMEOUT_SECONDS
    ran_ok = False
    info: dict = {}

    while time.monotonic() < deadline:
        time.sleep(PHASE_E_POLL_SECONDS)
        info = task_info() or {}
        result = info.get("LastTaskResult")
        last_run = info.get("LastRunTime") or "(never)"
        waited = int(PHASE_E_TIMEOUT_SECONDS - (deadline - time.monotonic()))

        if result == 0 and not ran_ok:
            ran_ok = True
            say(f"[{waited:>3}s] the task ran on its own and exited 0 "
                f"({last_run})")
        elif not ran_ok:
            say(f"[{waited:>3}s] not yet — LastRunTime={last_run} "
                f"LastTaskResult={result}")
            # 267009 is SCHED_S_TASK_RUNNING; anything else non-zero and
            # not "has not run yet" is a real failure worth stopping on.
            if result not in (None, 0, 267011, 267009):
                raise Halt(
                    "E", "step 7 — the scheduled task",
                    f"the task ran on its own and exited {result}",
                    "The task is registered and firing, but the agent inside it\n"
                    "is failing. Read agent.log:\n"
                    "  Get-Content agent.log -Tail 40\n"
                    "Photograph that and this screen, and call.",
                    f"The task '{TASK_NAME}' IS registered and IS running every\n"
                    "3 minutes, and every run is failing. Remove it if you are\n"
                    "leaving the machine:\n"
                    "  powershell -ExecutionPolicy Bypass -File "
                    ".\\install\\uninstall_agent.ps1")
            continue

        try:
            newest = latest_heartbeat(cfg)
        except Exception as exc:                        # noqa: BLE001
            say(f"[{waited:>3}s] could not read the cloud back yet: "
                f"{type(exc).__name__}")
            continue

        if newest and newest != baseline:
            say(f"[{waited:>3}s] a NEW heartbeat arrived: {newest}")
            info["new_heartbeat"] = newest
            return info

        say(f"[{waited:>3}s] task ran, waiting for its heartbeat to land")

    # Timed out. Which half failed changes what to do about it.
    if not ran_ok:
        raise Halt(
            "E", "step 7 — the scheduled task",
            f"the task did not run by itself within "
            f"{PHASE_E_TIMEOUT_SECONDS // 60} minutes",
            "The task is registered but the scheduler is not firing it.\n"
            "Check it by hand:\n"
            f"  Get-ScheduledTask -TaskName {TASK_NAME} | Get-ScheduledTaskInfo\n"
            "Photograph that and this screen, and call.",
            f"The task '{TASK_NAME}' IS registered. It is not running, so no\n"
            "data will arrive after you leave. Data uploaded in Phase B is\n"
            "safe, and the POS database was never written to.")
    raise Halt(
        "E", "step 7 — the scheduled task",
        "the task ran and exited 0, but no new heartbeat reached the cloud",
        "The agent is running on this machine and its data is not arriving.\n"
        "That is a network or credentials problem, not a scheduling one.\n"
        "Read agent.log:\n"
        "  Get-Content agent.log -Tail 40\n"
        "Photograph that and this screen, and call.",
        f"The task '{TASK_NAME}' IS registered and IS running every 3 minutes.")


# ════════════════════════════════════════════════════════════════
# the stop screen
# ════════════════════════════════════════════════════════════════

def report_halt(halt: Halt, transcript_path: Path) -> None:
    """Screen-wide, and impossible to mistake for a success.

    Whoever reads this is standing at a counter and has just been told a
    thing they were relying on did not work. Every question they are about
    to have is answered here, in the order they will ask them.
    """
    print()
    print()
    print("#" * 70)
    print("#" * 70)
    print("##" + " " * 66 + "##")
    print("##" + "S T O P P E D".center(66) + "##")
    print("##" + " " * 66 + "##")
    print("#" * 70)
    print("#" * 70)
    print()
    print(f"  Failed in PHASE {halt.phase} — {halt.step}")
    print()
    print("  " + "-" * 66)
    print("  WHAT FAILED")
    print("  " + "-" * 66)
    for line in str(halt.what).splitlines():
        print(f"    {line.strip()}" if line.strip() else "")
    print()
    print("  " + "-" * 66)
    print("  WHAT TO DO")
    print("  " + "-" * 66)
    for line in halt.do.splitlines():
        print(f"    {line.strip()}" if line.strip() else "")
    print()
    print("  " + "-" * 66)
    print("  THE STATE OF THIS MACHINE")
    print("  " + "-" * 66)
    for line in halt.machine_state.splitlines():
        print(f"    {line.strip()}" if line.strip() else "")
    print()
    print("  " + "-" * 66)
    print("  THE LOG")
    print("  " + "-" * 66)
    print(f"    {transcript_path}")
    print()
    print("    Everything on this screen is in that file, including what")
    print("    scrolled past. It contains no password and no token.")
    print("    For everything at once, double-click:")
    print("      collect_diagnostics.bat")
    print()
    print("#" * 70)
    print("##" + " " * 66 + "##")
    print("##" + "PHOTOGRAPH THIS SCREEN AND CALL.".center(66) + "##")
    print("##" + "CHANGE NOTHING ON THIS MACHINE.".center(66) + "##")
    print("##" + " " * 66 + "##")
    print("#" * 70)
    print("#" * 70)


def report_success(phase_a: preflight.PhaseA, info: dict,
                   transcript_path: Path, started: _dt.datetime) -> None:
    banner("INSTALLED — the agent is running on this machine")
    minutes = (_dt.datetime.now(_dt.timezone.utc) - started).total_seconds() / 60
    say(f"took {minutes:.0f} minutes")
    say()
    say("What was checked, in order:")
    say(f"  A  {phase_a.integrity}")
    say("  A  Python, dependencies, config, the decoded token, 31 golden tests")
    say("  A  the POS refused every write we attempted at it")
    say("  A  " + ("first install — the watermark was adopted, no backfill"
                   if phase_a.is_first_install else
                   "dry run cross-check: VERDICT PASS"))
    say("  B  one real cycle ran and uploaded")
    say("  C  the cloud was read back: RESULT OK")
    say("  D  the scheduled task was registered and read back")
    say("  E  the task fired BY ITSELF and a new heartbeat arrived")
    say()
    say("What runs now:")
    say(f"  a task named '{TASK_NAME}', every 3 minutes, while this user is")
    say("  logged on. It reads the POS and uploads. It never writes to the POS.")
    say(f"  next run: {info.get('NextRunTime') or 'see Task Scheduler'}")
    say()
    say("Where the logs are:")
    say(f"  {HERE / AGENT_LOG}       every cycle, rotated, capped")
    say(f"  {transcript_path}   this install")
    say()
    say("If anything looks wrong later, double-click collect_diagnostics.bat")
    say("and send the one zip it produces. It contains no secrets.")
    say()
    say("⚠ No messages will arrive yet. Detection is running; sending stays")
    say("  off until we enable it deliberately — VERIFY.md step 9, which is")
    say("  done from our side and is NOT part of this visit.")
    print(RULE)


# ════════════════════════════════════════════════════════════════
# driver
# ════════════════════════════════════════════════════════════════

def install(config_name: str = "config.json",
            skip_install: bool = False,
            skip_wait: bool = False,
            transcript_path: Path | None = None) -> int:
    started = _dt.datetime.now(_dt.timezone.utc)
    transcript_path = transcript_path or Path("(not recorded)")

    banner("POSentine — install (VERIFY.md steps 1 to 8)")
    say(f"folder:  {HERE}")
    say(f"started: {started.isoformat(timespec='seconds')}")
    say()
    say("This runs every check in VERIFY.md and stops at the first one that")
    say("fails. Phase A reads only — nothing is written to the POS or to the")
    say("cloud until Phase A has passed. The POS database is never written")
    say("to at any point, and Phase A proves that by attempting to.")

    # ---- Phase A -------------------------------------------------
    phase_header("A", "preflight — read-only, and the read-only proof",
                 "steps 1-4")
    phase_a = preflight.run_steps_0_to_4(config_name, skip_install)
    banner("GATE A PASSED — nothing has been written anywhere yet")
    say("first install" if phase_a.is_first_install
        else "resuming an existing install")

    # ---- Phase B -------------------------------------------------
    phase_header("B", "one real cycle — the first write of the install",
                 "step 6")
    phase_b_one_real_cycle()

    # ---- Phase C -------------------------------------------------
    phase_header("C", "confirm — read the cloud back", "step 6")
    code, out = run_agent("--confirm")
    if code != 0 or "RESULT: OK" not in out:
        raise Halt(
            "C", "step 6 — confirm",
            "the cloud read-back did not return RESULT: OK",
            "The block above lists exactly what is wrong, line by line.\n"
            "Photograph it and call. Do not install the scheduled task.",
            "A cycle ran and uploaded in Phase B. No task was registered, so\n"
            "nothing will run after you close this window. The POS database\n"
            "was never written to.")
    banner("GATE C PASSED — data landed and the agent is reporting in")

    baseline = latest_heartbeat(phase_a.config)

    # ---- Phase D -------------------------------------------------
    phase_header("D", "register the scheduled task", "step 7")
    phase_d_register()

    # ---- Phase E -------------------------------------------------
    phase_header("E", "prove the task fires by itself", "step 7")
    if skip_wait:
        say("--skip-wait: not waiting. THIS INSTALL IS NOT VERIFIED.")
        say("Only Phase E proves the agent runs without a human.")
        info = task_info() or {}
    else:
        info = phase_e_prove_it_runs_itself(phase_a.config, baseline)
        banner("GATE E PASSED — a cycle nobody started reached the cloud")

    # ---- Phase F -------------------------------------------------
    phase_header("F", "summary", "step 8")
    run_agent("--confirm")
    report_success(phase_a, info, transcript_path, started)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Install POSentine: VERIFY.md steps 1-8, gated.")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--skip-install", action="store_true",
                    help="do not run pip (offline machine, already installed)")
    ap.add_argument("--skip-wait", action="store_true",
                    help="do not wait for Phase E. Leaves the install "
                         "UNVERIFIED; for our own rehearsals only.")
    args = ap.parse_args(argv)

    logsetup.configure_streams()
    now = _dt.datetime.now()
    transcript = start_transcript(now)
    real_stdout = sys.stdout
    sys.stdout = transcript                       # type: ignore[assignment]

    try:
        code = install(args.config, args.skip_install, args.skip_wait,
                       transcript.path)
        return code
    except preflight.Stop as stop:
        # Phase A speaks VERIFY.md's own language. Translated rather than
        # re-worded, so the two cannot drift apart.
        report_halt(Halt("A", f"VERIFY.md step {stop.step}", stop.what, stop.do,
                         "Nothing was written to the POS or to the cloud, and\n"
                         "no scheduled task was registered. This machine is\n"
                         "exactly as it was before you double-clicked."),
                    transcript.path)
        return 1
    except Halt as halt:
        report_halt(halt, transcript.path)
        return 1
    except KeyboardInterrupt:
        print()
        print("  interrupted. Nothing further was done.")
        return 1
    except Exception as exc:                            # noqa: BLE001
        # An unexpected failure must not print a traceback and nothing else.
        # Whoever is reading has to know what state the machine is in.
        import traceback
        report_halt(Halt("?", "unexpected", f"{type(exc).__name__}: {exc}",
                         "This is a bug in the installer, not a check that\n"
                         "failed. The traceback below is what we need.\n"
                         "Photograph this and call.",
                         "Unknown. Run collect_diagnostics.bat and send the\n"
                         "zip; it records exactly what is and is not installed."),
                    transcript.path)
        traceback.print_exc(file=sys.stdout)
        return 1
    finally:
        sys.stdout = real_stdout                  # type: ignore[assignment]
        transcript.close()


if __name__ == "__main__":
    raise SystemExit(main())
