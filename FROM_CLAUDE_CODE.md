# Replies to the architect

Newest section at the top.

---

# 2026-08-09 21:05 UTC — Priority Zero done. And the scheduled task never ran.

**302 passed** (was 209), `test_golden.py` still exactly **31**, no locked file
touched. `ship/` is 24 files + `MANIFEST.txt`. Commit `2fafad5`.

Read the second section first if you only read one. Priority Zero is done and I
believe it is right; **the task defect is the one that would have cost the visit.**

---

## 🔴 Before anything else: the scheduled task we shipped would never have run

I set out to write Phase E, which has to wait for the task to fire on its own.
Before writing the wait I checked what it would be waiting for. It would have
waited forever.

**A `LogonTrigger` fires on a logon *event*.** Registering it while the till user
is already logged on does not produce one — and the repetition hung off that
trigger, so nothing repeated either. Two probe tasks, registered side by side on
this machine, already logged on, no logoff:

```
registered at 18:31:00 - user already logged on, no logoff will happen

t+1m  LogonTrigger fired 0 time(s)   TimeTrigger fired 1 time(s)
t+2m  LogonTrigger fired 0 time(s)   TimeTrigger fired 2 time(s)
t+3m  LogonTrigger fired 0 time(s)   TimeTrigger fired 3 time(s)
t+4m  LogonTrigger fired 0 time(s)   TimeTrigger fired 4 time(s)

pos_probe_logon  LastRunTime=11/30/1999 12:00:00 AM  LastTaskResult=267011  NextRunTime=
pos_probe_time   LastRunTime=8/9/2026 6:35:35 PM     LastTaskResult=0       NextRunTime=8/9/2026 6:35:35 PM
```

`11/30/1999` is the scheduler's "never ran" sentinel. `267011` is
`SCHED_S_TASK_HAS_NOT_RUN`. **`NextRunTime` is empty — it was not merely waiting,
it had no run scheduled at all.**

On a till that stays logged in for weeks, the agent would have run **zero times**
after we left, with a task sitting in the scheduler looking perfectly correct.

Worse than that: it would have *eventually* worked. The next reboot or logon
would start it, so a later check might find it healthy and nobody would ever know
why the first week was empty.

**Why our own evidence missed it.** Last session I registered the task, ran it
with `Start-ScheduledTask`, and got the exit code back through the wrapper. That
proved the chain — scheduler → hidden PowerShell → wrapper → env → python → exit
code — and I reported it as such. It could not have caught this, because
`Start-ScheduledTask` is me starting it. **I proved the task *can* run and read it
as proof that it *will*.** Same shape as the manifest hashes: the check and the
thing it checked shared a source.

**The fix.** The repetition moves to a `TimeTrigger` whose `StartBoundary` is the
install time, so the task is due the moment it is registered. The `LogonTrigger`
stays, *without* a repetition, purely so a reboot starts a cycle promptly instead
of waiting up to three minutes. One repetition, one job each, nothing competing.

Verified end to end, for real, on a staged folder — registered, fired by itself,
rolled back, uninstalled:

```
  trigger MSFT_TaskTimeTrigger       interval='PT3M' duration=''
  trigger MSFT_TaskLogonTrigger      interval=''     duration=''
  NextRunTime    = 8/9/2026 6:42:42 PM

  t+1m  agent ran 0 time(s)  LastRunTime=11/30/1999   LastTaskResult=267011
  t+2m  agent ran 0 time(s)  LastRunTime=11/30/1999   LastTaskResult=267011
  t+3m  agent ran 1 time(s)  LastRunTime=6:42:42 PM   LastTaskResult=0
  t+4m  agent ran 1 time(s)  LastRunTime=6:42:42 PM   LastTaskResult=0

  ran.txt:
    18:42:01 argv=--log ...\stage\agent.log
```

Three tests now pin it, including one that fails if the repetition is ever moved
back onto the logon trigger, with the measurement above in its docstring.

**And the partial-install window you asked about was real.** `install_agent.ps1`
registered first and checked afterwards, so a failed read-back left the new task
in place. It now exports the existing definition before registering and restores
it byte-for-byte on any failure. Forced with an injected post-registration
failure plus a changed interval, so a failed rollback would be visible as `PT9M`:

```
  What failed:
    INJECTED post-registration failure
    repetition interval is 'PT9M', expected PT3M

  Rolled back: the task that was here before has been put back exactly
  as it was. Nothing was written to the POS or the cloud.

  prior task restored byte-for-byte: True
  interval now: 'PT3M'  (PT3M = restored, PT9M = rollback FAILED)
```

---

## 🔴 Priority Zero — prove read-only

### 1. The layer audit, honestly

| # | Layer | Rated | What it actually stops |
|---|---|---|---|
| 1 | `db_denydatawriter` | **Enforced** — SQL Server | `INSERT`/`UPDATE`/`DELETE`/`MERGE` on every table and view |
| 2 | Absence of any other grant | **Enforced, weakly** | DDL, `TRUNCATE`, `SELECT…INTO`, `BACKUP` — **this is where the risk lives** |
| 3 | `sqlguard.assert_read_only` | **Enforced** — our code raises | Anything that is not a `SELECT`, before it reaches the network |
| 4 | `pyodbc readonly=True` | **Convention. Counts for nothing.** | **Nothing.** |
| 5 | Source scan in the suite | **Enforced** — CI refuses | A future edit that adds a write |
| 6 | The on-site probe | **Enforced** — install aborts | Credentials that are not read-only, on this machine, today |
| 7 | Disk | **Enforced** — audit hook | Any file opened for writing outside our folder |

**What `db_denydatawriter` does not cover.** It denies exactly three permissions.
Everything else is blocked only because nobody granted it — and an absence can be
handed out by a helpful administrator without anyone touching the `DENY`:

- **`TRUNCATE TABLE`** — needs `ALTER` on the table. Not covered.
- **DDL and `SELECT … INTO`** — need DDL / `CREATE TABLE`. Not covered.
- **`BACKUP DATABASE`** — not covered.
- **`EXEC` of a stored procedure — the sharpest gap.** Under ownership chaining,
  a procedure sharing an owner with the tables it writes runs with the permission
  check on those tables **skipped entirely**. The `DENY` is never evaluated. This
  is the one path that defeats layer 1.
- **`sp_executesql`** — counter-intuitively *safe*: dynamic SQL runs under the
  caller's permissions, so the `DENY` still applies.

And the condition that voids layers 1 and 2 completely: if the login is
**`sysadmin`**, permission checks are skipped altogether; if **`db_owner`**, it
can remove the `DENY` itself. The probe reads both on every install and aborts.

**`pyodbc.connect(readonly=True)` — you were right, and I have downgraded it to
zero.** It sets `SQL_ATTR_ACCESS_MODE`, which the ODBC specification defines as a
*hint* a driver may ignore, and the SQL Server driver does: SQL Server has no
read-only session mode (`ApplicationIntent=ReadOnly` is Availability-Group
routing, not enforcement). **I could not test this here** — no SQL Server on this
machine — so I have not counted it. The on-site probe settles it and its error
code says which layer refused: SQL Server `229` means the server did.

**The audit finding I did not expect.** `monitor_ro`'s permissions had **no
committed definition anywhere.** They existed only as something typed into a
management tool once — unreproducible, unreviewable, un-re-assertable. That is
now `monitor_ro.sql`: idempotent, refuses to run against `master`, and its most
important line is `DENY EXECUTE ON SCHEMA::dbo`, which converts the
ownership-chaining hole from an absence into a refusal. Applying it is the
customer's DBA's call.

### 2. The choke point

`sqlguard.py`. **Allowlist first** — a statement must begin with `SELECT` or
`WITH`; `EXEC`, a bare `sp_who`, `SET`, `BEGIN TRAN` are refused for not being on
the list rather than for being on a list of things I thought of. **Then a
denylist** for writes that hide behind a legal opening. Comments and string
literals are stripped first, and an unterminated one is refused rather than
guessed at.

Wired at the **connection**, so it covers statements written months from now by
someone who never reads it. That is `sqlguard_wiring.patch` — two lines into
`adapter_hdsoft.py`, for you to apply:

```diff
+import sqlguard
@@
     cn.autocommit = True
-    return cn
+    return sqlguard.guard(cn)
```

I applied it temporarily to test it — **252 passed, `test_golden.py` 31** — then
reverted and confirmed the file is byte-identical
(`f75ef36e…e708d2`). Until you apply it, **the install transcript says
`sqlguard choke point  NOT WIRED` in words**, every time. An unapplied diff must
not look the same as a working guard.

### 3. The source scan

Reads the source, not the intent. Proven by injecting a write into `agent.py`:

```
E   AssertionError: write SQL outside the probe:
E       agent.py: 'UPDATE dbo.Sales SET saltot = 0 WHERE salid = 1'
FAILED test_readonly.py::test_no_pos_facing_module_contains_a_write_statement[agent.py]
FAILED test_readonly.py::test_write_sql_lives_in_exactly_one_file
```

Exactly one file may contain write SQL — `readonly_probe.py` — and a separate
test fails if a second one ever does.

The disk claim is proven the same way, with a `sys.addaudithook` recording every
file the interpreter opens for writing during a real cycle. It has its own
falsifier: a deliberate stray write the test must catch, so it cannot pass for
the wrong reason. **It found a bug in itself** — the `open` audit event carries an
*int* when a file is opened from a descriptor, and treating that as a path
invented a file called `3`.

### 4. 🎯 The empirical proof — with one change I want you to overrule or accept

**I did not implement the TRUNCATE probe, and I do not think we should.**

Your instruction was that every probe affect zero rows so that a wrongly
permitted one still changes nothing. `UPDATE`/`DELETE`/`INSERT` all take
`WHERE 1 = 0` and that works. **`TRUNCATE TABLE` takes no `WHERE` clause.** There
is no zero-row version. A probe that is wrongly permitted empties the customer's
sales history. Same for `ALTER TABLE … ADD`, which permanently changes their live
table. Wrapping either in a transaction and rolling back would take a
schema-modification lock on `dbo.Sales` **during service**, which blocks the POS
itself.

So those are **asked, never attempted**, with `HAS_PERMS_BY_NAME` — which is a
`SELECT`, accounts for `DENY`, role membership and ownership, and answers exactly
the same question: **`TRUNCATE` requires `ALTER` on the table**, so "can this
login `ALTER dbo.Sales`" *is* "can this login `TRUNCATE dbo.Sales`", with no risk
attached. I also enumerate `fn_my_permissions`, which is exhaustive rather than a
list of things we thought to ask about.

Your own verification point — **I checked the assumption before relying on it.**
SQL Server checks permissions at compile time, before touching rows, so a denied
statement raises. The `WHERE 1 = 0` is belt-and-braces, not the primary reason
these are safe. Two independent reasons is the right number when the target is a
working restaurant's sales table.

Nine writes are attempted (three shapes × three tables — a login denied on
`Sales` but not on `Items` would otherwise pass). Rendered against a simulated
correctly-configured server:

```
  READ-ONLY PROOF - attempting to write to the POS, and requiring
  every attempt to be refused
==================================================================
  login              monitor_ro
  sysadmin           no
  db_denydatawriter  yes
  sqlguard choke point  ACTIVE

  ATTEMPTED - each of these was actually sent to the POS
    REFUSED       UPDATE dbo.Sales
      UPDATE dbo.Sales SET salid = salid WHERE 1 = 0
      -> [42000] [Microsoft][ODBC Driver 17 for SQL Server][SQ...
    REFUSED       DELETE dbo.Sales
      DELETE FROM dbo.Sales WHERE 1 = 0
      ...  (9 probes, 3 tables)

  ASKED - never attempted; there is no harmless version of these
    not held      ALTER dbo.Sales  (ALTER)
    not held      CONTROL dbo.Sales  (CONTROL)
    not held      CREATE TABLE in the database  (CREATE TABLE)
    not held      BACKUP the database  (BACKUP DATABASE)
    not held      CONTROL the server  (CONTROL SERVER)

  Everything this login may do to dbo.Sales, per the server:
    SELECT

  VERDICT: READ-ONLY CONFIRMED
```

**An inconclusive answer is not a pass.** A probe that fails for a reason other
than permissions, or a `HAS_PERMS_BY_NAME` that returns NULL, stops the install.
"We could not tell" and "it is refused" must never produce the same outcome.

It runs as **preflight step 3b**, on every install. It cannot run earlier than
that — connecting needs pyodbc from step 2 and the credentials from step 3 — but
it runs before step 4, before the agent reads a single invoice.

The block is ASCII-only on purpose: it is evidence we show the customer, and a
cp1252 console would otherwise turn the em-dashes into noise.

### 5. What the installer touches

Complete list, in `READONLY_GUARANTEE.md`. Our own folder, and one scheduled
task. Two things I am naming because the list is supposed to be complete:

- Registering a task makes **Windows itself** write `C:\Windows\System32\Tasks\`
  and the scheduler's registry keys. Unregistering reverses it.
- **`pip install` writes into the machine's Python installation, and the
  uninstall does not reverse that** — removing shared packages could break
  anything else on that machine using Python. Named rather than hidden.

Not touched: `PATH`, environment variables outside our process, file
associations, services, startup folder, firewall, any HD Soft file.

### 6. `READONLY_GUARANTEE.md`

Written to be shown to the customer. Includes **what is not guaranteed** — six
items, the sharpest being that we do not control the `monitor_ro` login and can
only guarantee we *check* it on every install, and that read-only means we do not
change their database, not that nothing leaves it.

There is a **draft Arabic summary at the end, clearly marked not-for-use**. The
owner reads Arabic and this is meant to be showable to him, but Arabic
customer-facing text is reviewed wording in this project and I am not going to
quietly introduce unreviewed prose. Review it or tell me to drop it.

---

## Priority 1 — one click

`INSTALL.bat` → `installer.py`. Phases A–F exactly as you specified.

**Phase A's gate is preflight's own**, called rather than reimplemented —
`run_steps_0_to_4()` is the single implementation and both entry points use it. A
test fails if the installer ever starts re-deriving the step-4 verdict itself. A
second implementation of a gate is a second thing that can disagree with the
first.

**On "nothing is written anywhere before the Phase A gate" — I have to correct
the claim slightly, because it is not quite true and I would rather say so.**
Phase A writes three things: `pip install` writes into site-packages, and step 3b
sends nine zero-row statements to the POS which the server refuses. Nothing is
written to the **cloud**, nothing to the agent's own state, and nothing to the POS
*data*. The wording in `INSTALL.bat` and `VERIFY.md` now says exactly that rather
than the broader claim.

**Two things I found designing Phase B, both of which would have bitten:**

1. On a first install, `agent.py` adopts the watermark, uploads nothing, and
   exits — that is the whole cycle by design. Stopping there hands Phase C a
   cloud with no invoices and stops a *healthy* machine. Phase B detects it and
   runs a second cycle.
2. **`agent.py` exits `0` when another instance holds the lock, having done
   nothing.** On a second double-click the task is already registered and its
   cycles overlap, so exit 0 alone would let Phase B pass **without a cycle ever
   having run**. It now waits and retries, and gives up with instructions rather
   than looping.

**Phase E requires two independent facts**, because either alone can lie: the
scheduler reporting `LastTaskResult 0` (the task fires, but that says nothing
about data arriving) *and* a heartbeat newer than the one Phase C left behind (a
heartbeat could be left over from our own manual Phase B run). Together they
prove a cycle nobody started reached Supabase. The baseline is read straight from
`heartbeats`, not scraped from `--confirm`'s printed block.

The stop screen, rendered for real by running the installer against a folder with
no dependencies installed:

```
######################################################################
##                          S T O P P E D                           ##
######################################################################

  Failed in PHASE A — VERIFY.md step 2 — dependencies

  WHAT FAILED / WHAT TO DO / THE STATE OF THIS MACHINE / THE LOG
    ...
    Nothing was written to the POS or to the cloud, and
    no scheduled task was registered. This machine is
    exactly as it was before you double-clicked.
    ...
    C:\...\smoke\logs\install_20260809_233952.txt

##                 PHOTOGRAPH THIS SCREEN AND CALL.                 ##
##                 CHANGE NOTHING ON THIS MACHINE.                  ##
######################################################################
```

**`--skip-wait` exists for our rehearsals and prints `THIS INSTALL IS NOT
VERIFIED`.** A test fails if that sentence is ever removed. That is the specific
thing you warned about — one click quietly becoming one click that skips a check.

---

## Priority 2 — logs

**Rotation.** 2 MiB × 6 files = a 12 MiB ceiling. Proven by writing ~400 KB
through a scaled-down handler and measuring what survived: exactly 4 files, total
under `(backups+1) × (max + one record)`, and the **newest** records kept — a cap
that discarded the newest lines would be worse than no cap.

**One honest caveat.** The stale-lock takeover deliberately allows two processes
to overlap, and on Windows a rename fails while another process holds the file. A
failed rollover is written into the log and skipped rather than taking down a
cycle to tidy a log file; the next cycle rotates. So the ceiling is enforced on
the next successful write, not on that one. Tested, and stated in the module
rather than implied.

**Secrets are masked at the formatter**, not at the call sites — which means
`LOG.exception` tracebacks are covered too, and that is exactly where a
connection string turns up unannounced. Registration happens in `Config.load`
before any validation, so even a message *about* a malformed config cannot carry
the value it is complaining about. A secret too short to mask safely is reported
loudly rather than skipped silently.

**Truncated secrets are masked too.** Error text is cut at 500 characters on its
way to the cloud, and half a token is exactly as leaked as a whole one. I also
reordered `_beat_failure` to mask *before* truncating — truncating first can cut a
token in half and leave the half unmatched.

**The test you asked for by name** runs a real cycle that fails in the ways most
likely to leak (a 401 whose message embeds both keys, and a connection-string
exception carrying the SQL password), then greps every produced file for every
secret in `config.json`, including leading fragments. It has a falsifier: with
masking disabled the same run *must* leak, or the test is not exercising the path
it claims to.

**`collect_diagnostics.bat`** produces one zip. `config.json` is never copied — a
redacted version is, keeping every key and replacing each secret with its length
and a sha256 prefix, so "is this the token we issued?" is answerable without
disclosure. It re-runs the read-only proof rather than copying the install
transcript's, because the question three weeks later is whether the POS still
refuses us *today*. It fails soft: the machine whose network is broken is exactly
the machine whose diagnostics we most need.

---

## Priority 3 — the failure-mode review, and two defects I fixed

I read the code rather than the comments. **Two of the eleven were not handled,
and both were the silent kind.**

### 🔴 Not handled #1 — `--confirm` never judged the clock

`VERIFY.md` step 6 has always carried a row telling the operator that a drift
beyond ±300 is listed by `--confirm` and what to do about it. **It was not.**
`confirm()` printed `drift=` and appended no problem for it. A till whose clock
was hours out printed **`RESULT: OK`**.

Shift boundaries are wall-clock 07:00/19:00 local. A wrong clock puts invoices in
the wrong shift, and every total is then confidently incorrect rather than
absent — the exact failure this product exists to prevent, and the document
claimed we already caught it. Fixed, with the threshold as a named constant and
three tests, including one that refuses to pass a heartbeat carrying **no** clock
reading — missing and good must not look the same.

### 🔴 Not handled #2 — a corrupt `state.json` wedged the agent forever

`State.load` was called outside any `try` in `main()`. A torn or hand-edited
`state.json` raised `JSONDecodeError` out of `main`, **every three minutes,
forever.** No cycle would ever run again, and the file that causes it is one the
agent writes itself.

Fixed: the bad file is quarantined to `state.json.corrupt` (kept — it is the only
copy of the evidence), and the agent starts from an empty local state. That is
safe *because* of the work you had already done: `reconcile_with_cloud` treats the
cloud as authoritative and may only initialise when nothing has ever synced on
either side, so an empty local state resumes rather than re-adopting `MAX(salid)`.
Both halves are tested, including the dangerous one.

### The other nine

| # | Scenario | Loud? | Data loss? | Self-recovers? | Diagnosable? |
|---|---|---|---|---|---|
| 1 | Network drops between invoice and line batch | yes | **no** | yes | yes |
| 2 | Supabase 500/429 for an hour | yes | no | yes | yes |
| 3 | Token expires or is revoked | yes **locally** | no | **no** | **locally only** |
| 4 | Disk fills | yes | no | yes | partly |
| 5 | Clock jumps | **was silent** | — | n/a | now yes |
| 6 | `config.json` edited mid-run | yes | no | yes | yes |
| 7 | SQL Server restarts mid-query | yes | no | yes | yes |
| 8 | Two cycles overlap after takeover | yes | no | yes | yes |
| 9 | Killed mid-upload | yes | **no** | yes | yes |
| 10 | HD Soft changes a column | yes | no | **no, by design** | yes |
| 11 | Supabase paused / free tier full | yes | no | yes | yes |

**1, 9 — the watermark is correct.** `upload()` raises on the first failing table,
`advance_sync_state` is never reached, and the same range is re-read next cycle.
Every upload is an idempotent upsert. Covered by
`test_failed_line_upload_holds_the_watermark`. I added per-table logging so
"invoices went up and lines did not" is now visible in the log rather than
inferred.

**2 — retries 429/500/502/503/504** five times with exponential backoff, honouring
`Retry-After`. Beyond that the cycle fails, the watermark holds, and the next tick
tries again. An hour of outage costs an hour of latency, not data.

**3 — the one I want to flag.** A 401 is deliberately *not* retried (correct — RLS
refusal is an answer, not a hiccup). But `_beat_failure` then tries to record the
failure as a heartbeat, **and that insert gets 401 too**. So a revoked token is
loud in `agent.log` on the till and **completely silent in the cloud.** Our only
signal is the *absence* of heartbeats. Not fixed — the fix belongs in the
orchestrator (alert on heartbeat silence), which is Priority 3 work. **Naming it
so it does not get lost.**

**4 — disk full.** `State.save` writes to `.tmp` and only then `os.replace`, so a
failed write leaves the good file intact. There is no `fsync`, so a power cut
could lose the last state write — but the cloud is authoritative and
`reconcile_with_cloud` takes the higher watermark, so the impact is nil. Logging
degrades without crashing. Partly diagnosable: the log is the thing that fills.

**8 — overlap after takeover.** Safe by construction: idempotent upserts, and
every `sync_state` write is `lt.`-guarded so an older cycle cannot rewind a newer
one. That matters because a watermark moving backwards is the exact signature
`RestoreSuspected` exists to detect, and it must never fire for a benign reason.
Covered by `test_a_hung_process_cannot_rewind_the_watermark`. I added a warning
log when a takeover happens — it used to be inferable only from timing.

**10 — halts on purpose** and stays halted. Correct: numbers would be wrong rather
than absent.

**One thing I could not determine:** whether the customer's SQL Server behaves as
documented for any of this. There is no SQL Server on this machine. Everything
above is traced through our code; step 3b is what closes the gap, on site, before
anything else runs.

---

## Priority 4 — the two time-savers

**I have not evaluated either properly and I am not going to pretend otherwise.**
I dispatched both as parallel investigations — a real PyInstaller build with
measurements, and a config-prompt analysis — and both were killed by a session
limit before returning anything. I would rather hand you nothing than an opinion
dressed as a finding.

What I can say without measuring, as a starting position for when I do:

**PyInstaller — my prior is against, and the reason is not antivirus.** It is that
`test_golden.py` currently runs **on the customer machine** as acceptance
evidence, and `MANIFEST.txt` checks 24 files individually. One opaque binary
replaces a per-file integrity check with a single hash, and 31 pytest tests
running inside a frozen exe is a different and less convincing claim than 31
tests running against the files that will execute. The AV concern is real and
adds to it. **I will measure it properly and report.**

**Config from a prompt — my prior is also against**, because it moves the risk
rather than removing it: pasting a 219-character JWT into a Windows console is
its own failure mode, and the existing guards already catch every typo class
(`Config.load` decodes the token and checks `role` and `tenant_id`; preflight
checks the `sql` block that `Config.load` misses). A typo today is *caught, at the
counter, with an instruction*. That is not obviously worse than *impossible*.

**Two things I would propose instead, unprompted:**

1. **Apply `monitor_ro.sql`.** The read-only guarantee currently rests on a login
   with no committed definition. That is the weakest link in Priority Zero and it
   is one file to fix.
2. **Alert on heartbeat silence in the orchestrator.** Failure mode 3 above means
   a revoked token produces *nothing* — and "nothing" is what a healthy quiet
   shop also produces if we only look at error heartbeats.

---

## What is still unproven

- **An actual dry run against the real POS.** Unchanged, and everything is
  downstream of it.
- **The read-only probe against a real SQL Server.** The logic is tested against
  a simulated server, both compliant and dangerously misconfigured. The error
  codes it classifies (`229`/`230`/`262`) are from documentation, not from that
  machine. If they differ, step 3b returns `INCONCLUSIVE` and **stops the
  install** — which is the safe direction, but it would stop a good install, and
  you should know that is the failure mode.
- **Windows older than 11.** Still schema 1.2, still no
  `UseUnifiedSchedulingEngine`. The `TimeTrigger` change does not alter that.
- **Phase E against a real POS.** Verified with a stand-in agent on this machine;
  a `LastTaskResult 0` from a real cycle still needs a POS that answers.

---

# 2026-08-09 08:52 UTC — the manifest hashes you verified were wrong. Fixed.

Correction to the section below, found while committing. It is the same
failure shape as the watermark-0 trap, so it is worth your attention.

**Your check `ship/ sha256 vs repo — 8/8 identical` passed, and it could not have
failed.** Both sides read the same working tree. The working tree was not what a
clean checkout produces.

Git warned on commit:

```
warning: in the working copy of 'make_ship.py', CRLF will be replaced by LF
```

Three shipped files had picked up CRLF in the working copy — a tool on this machine
wrote them through Windows newline translation at some point. `.gitattributes` pins
`*.py` to `eol=lf`, and every committed blob is LF (checked: no tracked blob contains
CRLF), so a fresh clone produces different bytes than what I hashed. After
`git checkout -- .`:

```
file                     before         after clean-checkout bytes
adapter_hdsoft.py        f75ef36e6d34   f75ef36e6d34
agent.py                 9b7ba54265ae   48cdcba4e691    <-- CHANGED
config.example.json      b3920bc9c8f3   572aa1da6e50    <-- CHANGED
events.py                f563acca496e   f563acca496e
metrics.py               3672f30e9889   3672f30e9889
mint_agent_token.py      a1cc7d331827   a1cc7d331827
report.py                fad3785f92d9   fad3785f92d9
rows.py                  49a1764a5c56   49a1764a5c56
supa.py                  49ae9f26c9f1   c72382c0f3c3    <-- CHANGED
test_golden.py           8752bc77dfcc   8752bc77dfcc
```

**Every locked file is unaffected** — they were already LF, which is why
`git diff HEAD~2 HEAD` on them came back empty for you and stays empty. Nothing in
them changed. The three that moved are `agent.py`, `supa.py`, `config.example.json`,
and only their line endings.

**Consequence if it had shipped:** the manifest would have been correct about my
machine and wrong about the repository. Nobody would have noticed until someone
rebuilt `ship/` from a clean clone and got a different `MANIFEST.txt` for identical
source — or worse, until a "code integrity" stop on site for a file nobody had
touched.

**Guard added,** because I do not want this decided by whoever last opened a file in
which editor. `make_ship.py` now refuses to build when a shipped file's line endings
disagree with `.gitattributes`, before it hashes anything:

```
### 1. correct tree - must build ###
  built D:\New folder (2)\New folder\ship

### 2. reintroduce the exact fault (agent.py -> CRLF) ###
error: line endings do not match .gitattributes:
  agent.py: expected LF, found CRLF

       The bytes here are not the bytes a clean checkout
       produces, so every sha256 below would describe this
       machine rather than the repository. Fix with:
           git checkout -- <file>
--- exit=1 ---
```

`MANIFEST.txt` is also stamped with the revision it was built from, so a folder on a
customer machine traces back to a commit rather than to a memory:

```
# built from: 58a0788089a798fc4ab56811f4270b204853cc80 +uncommitted changes
```

Three tests cover it: the guard passes on the current tree, `CRLF_SUFFIXES` still
matches what `.gitattributes` pins, and the revision stamp resolves.

**209 passed**, `test_golden.py` still **31**. The hashes in the section below are
superseded by the ones above; the current `ship/MANIFEST.txt` is authoritative.

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
