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

## 0 — Steps 1–4 in one click

```
double-click preflight.bat
```

Steps 1 to 4 are the read-only ones, and they are the ones with the most typing
in them. `preflight.bat` runs all four: console to UTF-8, Python, dependencies,
config and the **decoded** token, the golden baseline, then `agent.py --dry-run`.

It **stops at the first failure**, names the step, and says what to do. It writes
nothing to the POS and nothing to the cloud, so one click carries no risk. It ends
by printing the dry-run block exactly as the agent produced it, and its own verdict
under it.

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
cd C:\thirdeyev
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

Place `config.json` in `C:\thirdeyev`, copied from `config.example.json` and filled in.
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

Expect `LastTaskResult : 0`.

**STOP IF** it refuses because `config.json` is missing. That is deliberate: a task
installed before step 6 fails every three minutes into a log nobody is reading yet.

Wait three minutes, then:

```powershell
Get-ScheduledTask -TaskName thirdeyev | Get-ScheduledTaskInfo
python agent.py --confirm
```

Expect `LastRunTime` inside the last three minutes, `LastTaskResult : 0`, and a **newer**
heartbeat than the one from step 6.

**STOP IF** `LastTaskResult` is not 0, or no new heartbeat appeared. The task is
registered but not running, and nothing will arrive after you leave.

> **Tell the owner:** the task runs **at logon and only while the till user is logged
> on**. Running while logged off needs either a stored password or an administrator to
> grant a logon right, and this account has neither — so this is the correct choice,
> not a shortcut. If the machine is ever logged out, cycles stop until someone logs
> back in. The 3-minute repetition then resumes by itself; nothing is lost, because
> the watermark only ever moves forward.

---

## 8 — Before you leave

```powershell
Get-Content C:\thirdeyev\agent.log -Tail 20
```

Expect `cycle ok` lines and no tracebacks.

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
Get-Content C:\thirdeyev\agent.log -Tail 100
```

Send that, the step number, and the exact message. The log is UTF-8 and contains no
passwords or tokens.
