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

### On a first install

Expect a block headed `FIRST RUN (nothing is written, anywhere)` telling you which
`watermark_salid` a real run would adopt.

**This is correct.** A fresh install adopts the current `MAX(salid)` and reads nothing
behind it. History is not backfilled by design — reading it would drag the whole sales
table across during service, and we never report on data recorded before we started
watching.

**STOP IF** you instead see `invoices to upload` in the **thousands** on a first
install. That means the first-run guard did not fire and the agent is about to pull the
entire history. Call.

Run it once more after step 6 to see the normal block below.

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
.\install\install_agent.ps1
Get-ScheduledTask -TaskName thirdeyev | Get-ScheduledTaskInfo
```

Expect `LastTaskResult : 0`.

Wait three minutes, then:

```powershell
Get-ScheduledTask -TaskName thirdeyev | Get-ScheduledTaskInfo
python agent.py --confirm
```

Expect `LastRunTime` inside the last three minutes, `LastTaskResult : 0`, and a **newer**
heartbeat than the one from step 6.

**STOP IF** `LastTaskResult` is not 0, or no new heartbeat appeared. The task is
registered but not running, and nothing will arrive after you leave.

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
.\install\uninstall_agent.ps1
```

Removes the task and stops the agent. Uploaded data is untouched, and the POS database
was never written to at any point.

## If you get stuck

```powershell
Get-Content C:\thirdeyev\agent.log -Tail 100
```

Send that, the step number, and the exact message. The log is UTF-8 and contains no
passwords or tokens.
