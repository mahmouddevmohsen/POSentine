# VERIFY — POSentine install and acceptance, one visit

You are standing at the counter. There is a queue. Work down the list.

**Every step has an expected result printed next to it. If what you see does not
match, STOP at that step.** Do not adjust anything to make it match. A wrong number
that gets believed is worse than no number.

Steps 1–5 write nothing to the POS or the cloud. The first write is step 6.
**Nothing in this product ever writes to the POS database — not one statement.**

Everything runs through Python. No SSMS, no `sqlcmd`, no browser, no second device.

Time if nothing goes wrong: ~20 minutes. Budget 45.

---

## 0 — The whole thing in one click

> ## What to download
>
> **Release `v1.0.1`**, from:
>
> ```
> https://github.com/mahmouddevmohsen/POSentine/releases/latest
> ```
>
> Take the single **`posentine-<commit>.zip`** asset attached to that
> release. The filename carries the commit it was built from, so it changes
> with every release — that is deliberate, and it is why this document names
> the *release* rather than a filename that would be out of date the moment
> it shipped. Inside the zip, `MANIFEST.txt` records the same commit.
>
> **Do NOT use the green `Code → Download ZIP` button** on the repository
> page. A repository ZIP has no `.git` and no `MANIFEST.txt`, so **nothing
> can confirm this machine is running the code we tested** — step 0 says
> `NOT VERIFIED` and makes you type an acknowledgement before it continues.
>
> This is not hypothetical. The 2026-08-10 install ran from a repository
> ZIP, with no integrity check at all.
>
> `git clone` is equally fine — a clean checkout is verified against its
> commit.

```
double-click INSTALL.bat
```

This runs **steps 1 to 8** as gated phases and stops at the first one that
fails. Every gate below stays exactly where it is; what changes is who
enforces it. A person under pressure might look at a delta of 7 and carry
on. `INSTALL.bat` will not.

| Phase | What | Gate |
| --- | --- | --- |
| A | preflight, steps 1–4, plus the read-only proof | verdict must be **PASS** or **FIRST RUN** |
| B | one real cycle (step 6) | |
| C | `--confirm` (step 6) | **RESULT: OK** |
| D | register the scheduled task (step 7) | rolls back on failure |
| E | wait for the task to fire **by itself**, prove a **new** heartbeat | |
| F | summary: what happened, what runs now, where the logs are |

It takes about 10 minutes, most of it Phase E waiting. **That wait is the
point.** Everything before it proves a human can run the agent by hand;
only a new heartbeat from a cycle nobody started proves the machine keeps
working after you leave.

**Safe to run twice.** Run it again if you are not sure it worked.

If it stops, the screen is unmissable and says what failed, which step,
what to do, what state the machine is in, and where the log is. Photograph
it and call.

The rest of this document is what it runs, and how to run any step by
hand. **Read on when it stops.**

---

## 0b — Steps 1–4 only

```
double-click preflight.bat
```

`preflight.bat` runs steps 1 to 4 plus **3b**: console to UTF-8, Python,
dependencies, config and the **decoded** token, the golden baseline, the
read-only proof, then `agent.py --dry-run`.

It **stops at the first failure**, names the step, and says what to do. It ends
by printing the dry-run block exactly as the agent produced it, and its own verdict
under it.

Nothing here writes to the cloud, and nothing writes to the POS *data*. Step 3b
does deliberately **send writes** to the POS and require the server to refuse them
— every probe carries `WHERE 1 = 0`, so a probe that is wrongly permitted still
changes nothing. Two other things are written to the machine and named here so the
claim is exact: `pip install` writes into the Python installation, and this
transcript is written to `logs\`.

Safe to run at any time, including months later, to re-check that the POS still
refuses us.

Before anything else it checks every file against `MANIFEST.txt`, so "this machine
is running the code we verified" is a fact rather than an assumption.

It also stops on one thing this document can only ask you to notice by eye: a dry
run whose `watermark_salid` is **0** with invoices behind it. The agent prints
`VERDICT: PASS` on that — see step 5 — and it is wrong. See step 4a.

**Steps 1–4 below are what it runs.** Read them when it stops, or to run a step by
hand. **Steps 5 onward stay manual** — those involve decisions, and a script should
not make them.

---

## 1 — Console and Python

```powershell
cd C:\Users\Techno\Downloads\posentine
chcp 65001
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
python --version
```

Expect `Active code page: 65001`, then `Python 3.11.x` or `3.12.x`.

`chcp 65001` is so you can *read* the Arabic. The agent forces UTF-8 on its own
output and will not crash without it, but the screen will be unreadable.

**STOP IF:** Python is missing or below 3.11.

---

## 2 — Dependencies

```powershell
python -m pip install -r requirements.txt
python -c "import pyodbc; print('pyodbc', pyodbc.version); print(pyodbc.drivers())"
```

Expect a version number, then a list containing an `ODBC Driver ... for SQL Server`.

`pyodbc` is the one dependency never exercised on this machine. It ships as a wheel
and should not need to compile.

**STOP IF:** the install tries to compile and fails → install "Microsoft ODBC Driver 17
for SQL Server", then retry.

**STOP IF:** the driver list is `[]` → no ODBC driver at all. The agent cannot connect.

---

## 3 — Config and token

Place `config.json` in `C:\Users\Techno\Downloads\posentine`, copied from `config.example.json` and filled in.
Never paste a token into a terminal, and never commit this file.

```powershell
python -c "import json, mint_agent_token as m; c=json.load(open('config.json')); print(m.decode_claims(c['supabase_agent_token'])); m.assert_is_agent_token(c['supabase_agent_token'], c['tenant_id']); print('TOKEN OK')"
python -m pytest -q test_golden.py
```

Expect the claims printed, then:

```text
TOKEN OK
31 passed
```

This **decodes** the token and reads its `role` and `tenant_id` claims. Do not test for
the text `service_role` inside the token — a JWT is base64, the literal string never
appears, and that check returns "safe" for an actual service_role key.

**STOP IF:** the token check raises. It will say which claim is wrong:

| Message | Meaning |
| --- | --- |
| `role='service_role', must be 'authenticated'` | A key that bypasses every access rule is on this machine. **Do not run the agent. Call.** |
| `token is for tenant X, but config says Y` | It authenticates fine and matches nothing. Every upload would silently affect zero rows. |
| `carries no tenant_id claim` | Same outcome. Re-mint the token. |
| `not a readable JWT` | The field was truncated or mangled on the way in. |

**STOP IF:** not `31 passed` → this machine has different code than we verified.

---

## 3b — The read-only proof (attempts to write, and requires refusal)

This is the first thing that touches the POS database, and it is
deliberately **not** a read.

We have told this customer their POS will not be written to. That promise
is worth exactly as much as the last time we checked it, so it is checked
on **every install** — permissions drift, and someone helpful "fixes" a
login.

`preflight.bat` and `INSTALL.bat` both run it. By hand:

```powershell
python preflight.py --skip-install
```

It attempts `UPDATE`, `DELETE` and `INSERT` against `dbo.Sales`,
`dbo.SalesDe` and `dbo.Items`, and requires **every one** to be refused.
Every probe carries `WHERE 1 = 0`, so a probe that is wrongly permitted
still changes nothing.

`TRUNCATE` and `ALTER` are **asked about, never attempted** — `TRUNCATE`
takes no `WHERE` clause, so a permitted probe would empty the customer's
sales history. `TRUNCATE` requires `ALTER` on the table, so asking "can
this login `ALTER dbo.Sales`" is the same question with no risk attached.

Expect the block to end:

```text
  VERDICT: READ-ONLY CONFIRMED
```

**STOP IF anything else.** The block names exactly which permission is
wrong. These credentials can change the customer's database, which is the
one promise this product makes that is not negotiable.

| Line | Meaning | Do |
| --- | --- | --- |
| `PERMITTED` on any probe | The login can write to the POS | **STOP. Do not install. Call.** |
| `HELD !!` on `ALTER` | The login could empty a table with `TRUNCATE` | **STOP. Call.** |
| `member of sysadmin` | Permission checks are skipped entirely; every other line above is meaningless | **STOP. Call.** |
| `INCONCLUSIVE` / `UNKNOWN` | We could not establish refusal. Not a pass. | **STOP. Call.** |
| `PROBE DEFECT` / `CANNOT VERIFY — OUR PROBE IS AT FAULT` | **Our tool is wrong, not this machine.** It could not build a valid write for this schema. | **STOP, but nothing is wrong here.** Send the block and the diagnostics zip; we fix it from our side. |

> ### On `PROBE DEFECT` — this happened once, on 2026-08-10
>
> The three `UPDATE` probes used to target the primary keys, and on the
> customer's schema all three were **IDENTITY** columns. SQL Server refuses
> to update an identity column whatever permissions you hold (`Msg 8102`),
> so the probe could not tell *denied* from *impossible*, and the install
> was blocked on a machine whose login was perfectly read-only.
>
> Blocking was correct — the gate did its job. The probe was wrong.
> It now asks the server which column it may legally target, and a
> structural refusal is reported as **our** defect rather than as a finding
> about the customer's credentials.
>
> Every non-refusal now prints the **SQL Server error number** and a `why:`
> line. `Msg 229` = permission denied (good). `Msg 8102` = identity refusal
> (our bug).

The full block goes into the install transcript and the diagnostics zip.
**That transcript is our evidence to the customer.** See
`READONLY_GUARANTEE.md`.

---

## 4 — Dry run (reads only, writes nothing anywhere)

```powershell
python agent.py --dry-run
```

### 4a — On a first install (no `state.json` yet)

Expect a block headed **`FIRST RUN (nothing is written, anywhere)`**, naming the
`watermark_salid` a real run would adopt. It should **not** print an
`invoices to upload` line at all.

That is correct. A fresh install adopts the current `MAX(salid)` and reads nothing
behind it. History is not backfilled by design: reading it would drag the whole sales
table across during service, and we never report on data recorded before we started
watching.

> ### 🛑 The one failure the step-5 verdict cannot catch
>
> **On a first install, the expected number of invoices to read is `0`.**
>
> If you see a `DRY RUN` block instead of a `FIRST RUN` block, and
> `invoices to upload` shows **hundreds or thousands** (the real database holds about
> **218,000** invoices and **481,000** lines), then first-run initialisation did not
> take, and the agent is about to pull the entire history during service.
>
> **The step-5 verdict will say PASS.** It compares what the agent would read against a
> bare `COUNT(*)`, and on a fresh install with `watermark_salid = 0` both numbers are
> the whole table. They agree, and they are both wrong. `delta 0` here is not a pass —
> it is two identical wrong answers.
>
> **The number to check is not the delta. It is `invoices to upload`, and on a first
> install it must be `0`.**
>
> **STOP. Do not run `python agent.py`. Photograph the block and call.**
>
> Most likely cause: a `state.json` left behind from a previous attempt with
> `"initialised": true` but `"watermark_salid": 0`. Do not edit it yourself.
>
> `preflight.bat` stops on this by itself: a `watermark_salid` of 0 with any
> invoices behind it fails, whatever the VERDICT line says. It is the only check
> in this document that overrides the agent's own verdict.

Run this step again after step 6 to see the normal block below.

### On an already-installed agent

Expect a block headed `DRY RUN (nothing is written, anywhere)`. Write these down:

| Field | Value |
| --- | --- |
| `watermark_salid` | |
| `invoices to upload` | |
| `sold_at range` | |
| cash / external / return / other | |

Sanity: the `sold_at` range should sit inside the hours the shop was open, and `cash`
should be the large majority. If `other` is not 0, note the number.

| If you see | Meaning | Do |
| --- | --- | --- |
| `مفيش ODBC driver مناسب` | No driver | Back to step 2 |
| `Login failed for user 'monitor_ro'` | Wrong password, or Mixed Mode auth off | **STOP. Call.** |
| `أعمدة ناقصة بعد تحديث` | HD Soft updated; a column we rely on changed | **STOP. Call.** |
| `restore suspected` | Database restored from backup; `salid` went backwards | **STOP. Call.** |
| `items missing from the snapshot` | New menu items | Note the IDs, continue |

---

## 5 — Read the verdict

The same `--dry-run` output ends with a cross-check the agent runs itself: a bare
`COUNT(*)` against `dbo.Sales` and `dbo.SalesDe`, no joins and no classification,
compared against what the adapter produced. Different code path, same source.

```text
    bare COUNT(*) invoices   1043
    agent would read         1041
    delta                    2
    bare COUNT(*) lines      2310

  VERDICT: PASS — delta 2, within the tolerance of 5.
```

Expect **`VERDICT: PASS`**. The only acceptable difference is invoices created in the
last 30 seconds, which the agent holds back on purpose: `NOLOCK` can read a row still
being written that is later rolled back, which would otherwise raise a "deleted
invoice" alert for a receipt that never existed. A busy counter shows 1–3, a quiet one 0.

### 🛑 ABORT — any verdict other than PASS

| Verdict | Meaning |
| --- | --- |
| `ABORT — the agent claims N more invoice(s) than the POS has` | We are reading their data wrong. Not a timing artefact. |
| `ABORT — delta N exceeds the tolerance` | Same. Something other than the 30-second guard is dropping rows. |
| `CAPPED — N of M this cycle` | A backlog is draining. Expected after an outage; on a fresh install it means step 4's first-run guard did not run. |

**Then:** photograph the whole dry-run block. **Change nothing. Install nothing. Call.**

Every number after this point would be confidently incorrect.

---

## 6 — One real cycle, then confirm it

```powershell
python agent.py
```

On a first install this prints `first run: adopted watermark N` and uploads nothing —
that is the whole cycle. Run `python agent.py` again to perform a real sync, then:

```powershell
python agent.py --confirm
```

Expect the last line to be:

```text
  RESULT: OK — data landed, watermark advanced, agent reporting in
```

and above it: counts matching step 4, `lines w/o price` at `0`, `last_rescan_at` not
null, and the newest heartbeat marked `ok` with `drift` within ±300.

`--confirm` reads the cloud back with the agent's own token and prints the verdict here.
It also proves the token that ships is the token that works.

**If `RESULT: NEEDS ATTENTION`,** it lists exactly what is wrong:

| Line | Meaning | Do |
| --- | --- | --- |
| `lines w/o price` > 0 | New menu items; zero-invoice detection is blind to them | Not a stop. Send us the `unknown_item` notes |
| `drift` beyond ±300 | POS clock is wrong; every shift boundary depends on it | Fix the machine clock, re-run |
| `restore_suspected is true` | `salid` went backwards | **STOP. Call.** |
| `no invoices landed at all` | Upload failed upstream | **STOP. Call.** |

---

## 7 — Install the scheduled task

```powershell
powershell -ExecutionPolicy Bypass -File .\install\install_agent.ps1
Get-ScheduledTask -TaskName thirdeyev | Get-ScheduledTaskInfo
```

The `-ExecutionPolicy Bypass` is not optional caution — a machine left on the Windows
default (`Restricted`) refuses to run any `.ps1`, and `.\install\install_agent.ps1`
fails before it prints anything.

To see exactly what would be registered without registering it:

```powershell
powershell -ExecutionPolicy Bypass -File .\install\install_agent.ps1 -ShowXml
```

The script is **idempotent** — running it twice replaces the task, never adds a second.
It registers **user-level, LeastPrivilege, never SYSTEM**, reads the task back from the
scheduler and checks it, and **stops** if registration fails on privileges rather than
falling back to something weaker.

It also **rolls back**. The task that was there before is exported first, and if any
read-back check fails it is restored byte-for-byte. A failed step 7 leaves the machine
exactly as it was, never half-installed.

Right after registering, expect:

```text
NextRunTime    : <a time within the next 3 minutes>
LastTaskResult : 267011
```

> ### 🛑 `267011` here is correct, and `0` would be suspicious
>
> `267011` is `SCHED_S_TASK_HAS_NOT_RUN`. The task has just been created and has not
> run yet — that is the only honest value.
>
> **The field that matters right now is `NextRunTime`.** If it is **empty**, the task
> is registered but *not scheduled*, and it will never run. That is exactly the fault
> this step used to have: the repetition was attached to a logon trigger, which does
> not fire while the till user is already logged on, so the agent would have run zero
> times after you left with every check still green.
>
> **STOP IF `NextRunTime` is empty.**

**STOP IF** it refuses because `config.json` is missing. That is deliberate: a task
installed before step 6 fails every three minutes into a log nobody is reading yet.

Now wait **three minutes and do nothing** — do not use `Start-ScheduledTask`. Starting
it yourself proves the task *can* run and says nothing about whether it *will*, which
is the whole point of this wait.

```powershell
Get-ScheduledTask -TaskName thirdeyev | Get-ScheduledTaskInfo
python agent.py --confirm
```

Now expect `LastRunTime` inside the last three minutes, `LastTaskResult : 0`, and a
**newer** heartbeat than the one from step 6.

**STOP IF** `LastTaskResult` is not 0, or no new heartbeat appeared. The task is
registered but not working, and nothing will arrive after you leave.

> **Tell the owner:** the task repeats **every 3 minutes, but only while the till user
> is logged on**. Running while logged off needs either a stored password or an
> administrator to grant a logon right, and this account has neither — so this is the
> correct choice, not a shortcut. If the machine is ever logged out, cycles stop until
> someone logs back in, and then resume by themselves; nothing is lost, because the
> watermark only ever moves forward.

---

## 8 — Before you leave

```powershell
Get-Content C:\Users\Techno\Downloads\posentine\agent.log -Tail 20
```

Expect `cycle ok` lines and no tracebacks.

The log is size-capped and rotated — `agent.log` plus up to five older
copies, 12 MB in total, so it can never fill the till's disk. It contains
no password and no token: every record passes through a masking formatter
on its way out, rather than depending on whoever wrote the line.

### If anything looks wrong, now or in three weeks

```
double-click collect_diagnostics.bat
```

One zip, next to the agent: install transcripts, agent logs, versions,
ODBC drivers, the scheduled task, `state.json`, what is in the cloud, the
manifest check, and a freshly re-run read-only proof. **No secrets** —
`config.json` is not included, only a redacted copy.

Send that one file. It replaces the conversation.

Tell the owner **no messages will arrive yet**. Detection is running; sending stays off
until we enable it deliberately in step 9, which is not done on this visit.

---

## 9 — Go-live: turning notifications on (NOT on this visit)

⚠️ **Both statements, one transaction.** Not the same session — the same transaction.

During the silent period events accumulate with `notify = false`. Nothing is sent. But
an event is suppressed only when it is older than `go_live_at`, and `go_live_at` is null
until now. The moment `notify` becomes true with `go_live_at` still null, **every event
since installation becomes sendable at once** — the owner's first ever contact from this
system would be dozens of alerts about last week.

```sql
begin;

update tenants
   set go_live_at = now()
 where slug = 'sobh_onthefast'
   and go_live_at is null;

update alert_settings
   set notify = true
 where tenant_id = (select id from tenants where slug = 'sobh_onthefast')
   and alert_type in ('zero_invoice','refund','cash_diff','deleted_invoice','no_sales');

commit;
```

Then confirm the backlog is suppressed, before anyone expects messages:

```sql
select count(*) as suppressed_backlog
from events e
join tenants t on t.id = e.tenant_id
where t.slug = 'sobh_onthefast'
  and e.occurred_at < t.go_live_at;
```

A large number here is normal — it is exactly what the guard is for.

`and go_live_at is null` on the first update is deliberate: re-running this must never
move the line forward and re-suppress events that had legitimately become sendable.

---

## 10 — Before a shift report is trusted

After a shift boundary (07:00 or 19:00), open HD Soft's **يومية الخزينة** for that shift
and compare against `shift_reports.grand_total`.

Expect exactly equal. The formula is `sales + collections − returns − delivery`.

If any line differs, photograph the screen and send the row. **Do not adjust anything.**

---

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File .\install\uninstall_agent.ps1
```

Removes the task and stops the agent. Uploaded data is untouched, and the POS database
was never written to at any point.

## If you get stuck

```powershell
Get-Content C:\Users\Techno\Downloads\posentine\agent.log -Tail 100
```

Send that, the step number, and the exact message. The log is UTF-8 and contains no
passwords or tokens.
