# Replies to the architect

Newest section at the top.

---

# 2026-08-09 08:35 UTC — Priority 1 done. Priority 2 I am pushing back on.

`install_agent.ps1`, `uninstall_agent.ps1` and `run_agent.ps1` are built, tested,
and in `ship/`. **206 passed** (was 184), `test_golden.py` still exactly **31**,
no locked file touched.

Priority 2 I have not done, and I do not think it should be done. Evidence below;
say the word and it is a five-minute change.

---

## 🔴 Priority 1 — `install_agent.ps1`

### What I built

| File | Role |
|---|---|
| `install/install_agent.ps1` | Registers the task. `-ShowXml` prints exactly what it would register and touches nothing. `-TaskName`, `-Python` overrides. |
| `install/uninstall_agent.ps1` | Removes it. Safe to run when nothing is installed. |
| `install/run_agent.ps1` | What the task executes — the task's environment block. |
| `test_install_agent.py` | 19 tests, driven off the real script's real `-ShowXml` output. |

### Four design calls you should check

**1. Explicit task XML, not `New-ScheduledTaskTrigger`.** An indefinite repetition
built through those cmdlets depends on `[TimeSpan]::MaxValue` surviving a round trip
into task XML — version-dependent, with a known failure mode. XML with an
`<Interval>` and no `<Duration>` means forever, on every version. It also gave me
`-ShowXml`, which is what makes step 7 testable off-site at all.

**2. The action is `powershell.exe -WindowStyle Hidden`, not `python.exe`.** A console
app launched by the scheduler shows its window. A black window on the till every three
minutes during service is not acceptable.

**3. Not `pythonw.exe`,** which would have removed the console entirely. Under
`pythonw`, `sys.stderr` is `None`; `agent.py`'s logging `StreamHandler` then fails on
every record and logging swallows the failure. That trades a visible window for a
silent one. Rejected.

**4. `run_agent.ps1` exists because a Scheduled Task action has no environment block** —
only a command, arguments and a working directory. The wrapper is that block. The
python path is **passed in as an argument**, not written into the wrapper at install
time: `run_agent.ps1` is hashed in `MANIFEST.txt`, and a script that rewrites itself
fails the integrity check `preflight.bat` runs first.

Two settings worth naming because their defaults are wrong for a till:

- `DisallowStartIfOnBatteries` / `StopIfGoingOnBatteries` both default to **true**.
  On a POS behind a UPS the defaults stop the agent on the first power blip, silently.
  Both forced to `false`.
- `ExecutionTimeLimit` is `PT15M`. `MultipleInstancesPolicy` is `IgnoreNew`, so one
  hung cycle blocks every later one forever. 15 minutes is not arbitrary — it is
  `agent.py`'s own `LOCK_STALE_SECONDS`, so the task and the agent cannot disagree
  about when a cycle is dead. `test_a_wedged_cycle_is_killed_before_it_blocks_the_next_forever`
  reads the constant out of `agent.py` and fails if it moves.

### Evidence — the task XML, from the real script

```
> powershell -ExecutionPolicy Bypass -File .\install\install_agent.ps1 -ShowXml

  [ OK ] python      3.11.15
  [ OK ] principal   KOMA\mahmo (user-level, LeastPrivilege - not SYSTEM)

  -ShowXml: printing the task XML. Nothing was registered.
------------------------------------------------------------------
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>KOMA\mahmo</UserId>
      <Repetition>
        <Interval>PT3M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>KOMA\mahmo</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File &quot;...\install\run_agent.ps1&quot; -Python &quot;...\python.exe&quot;</Arguments>
      <WorkingDirectory>D:\New folder (2)\New folder</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
=== EXIT = 0 ===
```

Note `<Repetition>` has **no `<Duration>`**. That is the whole point of the XML route.

### Evidence — registered for real, run, and removed

Not a mock. Registered on this machine as a non-administrator, run, inspected,
then removed.

```
### BEFORE: does 'thirdeyev' exist? ###
ABSENT

### INSTALL (run 1) ###
  [ OK ] python      3.11.15
  [ OK ] principal   KOMA\mahmo (user-level, LeastPrivilege - not SYSTEM)
  [ OK ] registered  1 task named 'thirdeyev' (read back and checked)
--- exit = 0 ---

### INSTALL (run 2) - idempotency ###
  [ .. ] a task named 'thirdeyev' already exists - replacing it
  [ OK ] registered  1 task named 'thirdeyev' (read back and checked)
--- exit = 0 ---

### how many tasks named thirdeyev? ###
1
```

What the scheduler actually stored — read back from the scheduler, not from my XML:

```
Principal.UserId    : mahmo
Principal.LogonType : Interactive
Principal.RunLevel  : Limited
Trigger             : MSFT_TaskLogonTrigger
Repetition.Interval : PT3M
Repetition.Duration : ''  (empty = indefinite)
Action.Execute      : powershell.exe
Action.WorkingDir   : C:\Users\mahmo\.claude\jobs\e26f7f1b\tmp\task
Settings.MultipleInstances : IgnoreNew
Settings.ExecutionTimeLimit: PT15M
Settings.DisallowStartIfOnBatteries: False
```

Then `Start-ScheduledTask`:

```
TaskName           : thirdeyev
LastRunTime        : 8/9/2026 8:26:26 AM
LastTaskResult     : 1
NumberOfMissedRuns : 0

### did the wrapper actually launch python? ###
2026-08-09 08:26:30,153 ERROR   cycle failed before upload: ('08001', '[08001]
[Microsoft][ODBC SQL Server Driver][DBNETLIB]SQL Server does not exist or access
denied. (17) (SQLDriverConnect); ...
```

**`LastTaskResult : 1` is the correct result here, and it is the interesting one.**
There is no SQL Server on this machine, so the agent failed at `connect()` and exited
1 — and that 1 came back through `run_agent.ps1` into `LastTaskResult`. The whole
chain is proven: scheduler → hidden PowerShell → wrapper → env → `python agent.py
--log` → exit code back out. `agent.log` was created, which is what VERIFY.md step 8
reads. On site, with a real POS, the same chain gives `0`.

Uninstall, and uninstall again:

```
  [ OK ] removed 'thirdeyev' (checked: it is gone)
--- exit = 0 ---
### is it gone? ###
ABSENT - clean

### UNINSTALL again (nothing to remove) ###
  [ OK ] no task named 'thirdeyev' is registered - nothing to remove
--- exit = 0 ---
```

### Evidence — the environment actually reaches python

The tests only grep `run_agent.ps1` for the two variables, which proves the source and
not the behaviour. So: both variables **explicitly unset**, started from `C:\Windows`,
with a stand-in `agent.py` that reports what it received and exits `7`.

```
### this shell, before: ###
PYTHONUTF8=[] PYTHONIOENCODING=[] cwd=C:\Windows

### what run_agent.ps1 hands to python: ###
cwd                = C:\Users\mahmo\.claude\jobs\e26f7f1b\tmp\envproof
PYTHONUTF8         = 1
PYTHONIOENCODING   = utf-8
sys.stdout.encoding= utf-8
argv               = ['--log', '...\\envproof\\agent.log']
--- wrapper exit = 7   (agent returned 7; must come back as 7) ---
```

Working directory forced, both variables set, `--log` passed, exit code preserved.

### Evidence — tests

```
> python -m pytest -q test_install_agent.py
19 passed in 2.05s

> python -m pytest -q
206 passed in 2.42s

> python -m pytest -q test_golden.py
31 passed in 0.05s
```

The 19 read properties out of the real `-ShowXml` output: `PT3M`, no `<Duration>`,
`LeastPrivilege` / `InteractiveToken`, no `SYSTEM`/`S-1-5-18`/`LOCALSERVICE` anywhere
in the principal, `-WindowStyle Hidden`, the wrapper and not `agent.py` directly,
`-ExecutionPolicy Bypass`, both battery settings `false`, `IgnoreNew`, `PT15M` tied to
`LOCK_STALE_SECONDS`, `Hidden=false`, `StartWhenAvailable=true`. Plus: `-ShowXml`
registers nothing, and the script refuses to install without `config.json`.

Windows-only, so they `skipif` when there is no PowerShell — pytest reports the skip
rather than passing quietly.

### A bug my own test caught, worth recording

The first version of `install_agent.ps1` would not parse at all:

```
The ampersand (&) character is not allowed...
Write-Host '  POSentine â€” install the scheduled task...
Unexpected token 'POSentine' in expression or statement.
```

**Windows PowerShell 5.1 reads a BOM-less `.ps1` as the system ANSI code page.** One
em-dash in a comment turns into `â€"` and shreds the parse, at lines that look fine.
All three scripts are now **ASCII-only and saved with a UTF-8 BOM**.

Second one, same session: PowerShell strips inner double quotes when building a native
command line, so `python -c 'print("%d.%d" % ...)'` reached Python as
`print(%d.%d % ...)` and died of a `SyntaxError` that reads like a broken interpreter.
The version probe now contains no double quotes.

Neither would have shown up before the visit. Both were found by running the file.

### Two things I changed outside the scripts

- **`VERIFY.md` step 7 now says `powershell -ExecutionPolicy Bypass -File ...`.**
  `.\install\install_agent.ps1` fails on a machine left at the Windows default
  (`Restricted`), before printing anything. Same for the uninstall line.
- **The logon-trigger trade-off is written down** rather than worked around: the task
  runs at logon and only while the till user is logged on. Running while logged off
  needs a stored password or an admin-granted logon right, and this account has
  neither. If the till is logged out, cycles stop and resume on logon; nothing is
  lost, because the watermark only moves forward. It is in step 7 and the install
  script prints it.

### What is still unproven

The scheduler accepted the XML, ran the task, and propagated the exit code **on this
machine**. What has not been proven anywhere: a `LastTaskResult : 0`, which needs a
POS that answers. And this machine is Windows 11; the customer's runs SQL Server 2014
Express and may be older. I kept the task to schema **1.2** (Windows 7+) and left out
`UseUnifiedSchedulingEngine` (Windows 8+) for that reason, but I cannot prove the
older path from here.

---

## 🟡 Priority 2 — I am not removing `mint_agent_token.py`, and here is why

The reasoning in your note is right about the *minting*: the customer machine never
mints a token, and that CLI is never used there. But the file is not only a minter.
`agent.py` imports it at module level and `Config.load` calls it:

```
agent.py:48:import mint_agent_token
agent.py:142:        mint_agent_token.assert_is_agent_token(raw["supabase_agent_token"],
```

`assert_is_agent_token` **is the check that refuses a service_role key** — the one
your own review called out as load-bearing. Removing the file from `ship/` does this:

```
### ship/ with mint_agent_token.py removed, as Priority 2 asks: ###
Traceback (most recent call last):
  File "...\trimtest\agent.py", line 48, in <module>
    import mint_agent_token
ModuleNotFoundError: No module named 'mint_agent_token'
```

That is the agent failing to start on the till, at install time.

**On the surface that genuinely is unused** — `mint()`, `_read_secret()`, `main()`:
they are inert on that machine. `mint()` cannot produce a token without
`SUPABASE_JWT_SECRET`, which by rule 5 is never there. And anyone who has the machine
already has the agent token in `config.json`; they do not need a minter.

I considered splitting the file — `token_check.py` (decode + assert, shipped) and
`mint_agent_token.py` (mint + CLI, not shipped). That achieves your intent exactly.
I did not do it because it means editing `agent.py`'s imports and `Config.load`'s call
path days before a site visit, to remove code that cannot be executed. That is the same
trade you agreed with on the exit code, pointing the same way — and there the change
would at least have fixed a real defect.

**What I did instead,** so this is decided mechanically rather than by review next
time — a test that derives the requirement from the source:

```python
@pytest.mark.parametrize("entry", ["agent.py", "preflight.py", "test_golden.py"])
def test_the_ship_list_is_closed_under_import(entry):
```

It walks each entry point's **module-level** imports and fails if any repository module
is missing from `SHIPPED`. Proof it bites — I removed the line from `make_ship.py` and
ran it:

```
E  AssertionError: agent.py imports ['mint_agent_token.py'], which ship/ does not
   contain. The agent would raise ImportError on the customer machine.
1 failed, 2 passed
make_ship.py restored
```

Module-level only, deliberately: `agent.py` imports `fake_adapter` *inside* `main()`
under `--fake`, and that one stays excluded — synthetic data has no place on a
production machine. The first draft of this test walked every import and correctly
flagged `fake_adapter`, which is how I noticed the distinction mattered.

**If you still want it gone after reading this, say so and I will do the split.** It
is about five minutes and I will re-run everything.

---

## `ship/` now

17 files + `MANIFEST.txt`. `make_ship.py` no longer prints the incomplete banner:

```
ship/adapter_hdsoft.py          ship/preflight.bat
ship/agent.py                   ship/preflight.py
ship/config.example.json        ship/report.py
ship/events.py                  ship/requirements.txt
ship/install/install_agent.ps1  ship/rows.py
ship/install/run_agent.ps1      ship/supa.py
ship/install/uninstall_agent.ps1  ship/test_golden.py
ship/metrics.py                 ship/VERIFY.md
ship/mint_agent_token.py        ship/MANIFEST.txt

  ✔ Complete for VERIFY.md steps 1–8: preflight, the agent,
    the golden baseline, and the scheduled task.
    Step 9 (go-live) is two SQL statements run from our side,
    not on this machine, and is deliberately not in here.
```

`MANIFEST.txt` now carries subdirectory paths, which step 0 had never seen. Checked
both ways:

```
### step 0 against a manifest with subdirectories ###
code integrity   OK - 17 files match MANIFEST.txt

### now corrupt a file INSIDE install/ ###
STOP: files differ from the versions we verified: ['install/run_agent.ps1']
```

Shipped `.ps1` encoding, verified after the copy:

```
install_agent.ps1      BOM=True  CRLF=317  ascii=True
run_agent.ps1          BOM=True  CRLF=60   ascii=True
uninstall_agent.ps1    BOM=True  CRLF=65   ascii=True
```

---

## Priority 3 — starting next

`orchestrator.py`, `notifier/telegram.py`, both workflows with `audit_privileges.py`
wired into keepalive. Agreed none of it needs the POS. Same gate discipline.

One thing I want to flag before I start, because it changes the shape of the work:
**`report.py` and `metrics.py` are locked**, and the orchestrator is the thing that
calls them. So the orchestrator has to fit their existing signatures exactly rather
than the other way round. I will read both closely and build to what they already
expect; if I find a place where a locked signature makes the orchestrator wrong rather
than merely awkward, I will stop and say so here instead of working around it.
