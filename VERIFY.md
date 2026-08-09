# VERIFY — POSentine install and acceptance, one visit

You are standing at the counter. There is a queue. Work down the list.

**Every step has an expected result printed next to it. If what you see does not
match, STOP at that step.** Do not adjust anything to make it match. A wrong number
that gets believed is worse than no number.

Steps 1–4 write nothing to the POS or the cloud. The first write is step 5.
**Nothing in this product ever writes to the POS database — not one statement.**

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

**Expect:** `Active code page: 65001`, then `Python 3.11.x` or `3.12.x`.

> `chcp 65001` is so you can *read* the Arabic. The agent forces UTF-8 on its own
> output and will not crash without it, but the screen will be unreadable.

**STOP IF:** Python is missing or below 3.11.

---

## 2 — Dependencies

```powershell
python -m pip install -r requirements.txt
python -c "import pyodbc; print('pyodbc', pyodbc.version); print(pyodbc.drivers())"
```

**Expect:** a version number, then a list containing an `ODBC Driver ... for SQL Server`.

> `pyodbc` is the one dependency never exercised on this machine. It ships as a wheel
> and should not need to compile.

**STOP IF:** the install tries to compile and fails → install "Microsoft ODBC Driver 17
for SQL Server", then retry.
**STOP IF:** the driver list is `[]` → no ODBC driver. Install Driver 17. The agent
cannot connect without one.

---

## 3 — Config

Place `config.json` in `C:\thirdeyev`. Copy it from `config.example.json` and fill in.
**Never** paste a token into a terminal and never commit this file.

```powershell
python -c "import json;c=json.load(open('config.json'));print('keys ok:', all(c.get(k) for k in ['tenant_id','source_id','supabase_url','supabase_anon_key','supabase_agent_token']));print('service_role present:', 'service_role' in c['supabase_agent_token'])"
python -m pytest -q test_golden.py
```

**Expect:**
```
keys ok: True
service_role present: False
31 passed
```

**STOP IF:** `service_role present: True` → that key bypasses every access rule and must
never exist on this machine. Do not run the agent. Call.
**STOP IF:** not `31 passed` → this machine has different code than we verified.

---

## 4 — Dry run (reads only, writes nothing anywhere)

```powershell
python agent.py --dry-run
```

**Expect:** a block headed `DRY RUN (nothing is written, anywhere)`.

**Write these down:**

| | |
|---|---|
| `watermark_salid` | ____________ |
| `invoices to upload` | ____________ |
| `sold_at range` | ____________ → ____________ |
| kinds: cash / external / return / other | ____ / ____ / ____ / ____ |

**Sanity:** the `sold_at range` should sit inside the period the shop was actually open,
and `cash` should be the large majority. If `other` is not 0, note the number.

| If you see | Meaning | Do |
|---|---|---|
| `مفيش ODBC driver مناسب` | No driver | Back to step 2 |
| `Login failed for user 'monitor_ro'` | Wrong password, or Mixed Mode auth off | **STOP. Call.** |
| `أعمدة ناقصة بعد تحديث` | HD Soft updated, a column we rely on changed | **STOP. Call.** |
| `restore suspected` | Database restored from backup; `salid` went backwards | **STOP. Call.** |
| `⚠ items missing from the snapshot` | New menu items | Note the IDs, continue |

---

## 5 — 🔴 The comparison. This is the acceptance check.

In SSMS or `sqlcmd`, using the `watermark_salid` from step 4:

```sql
SELECT COUNT(*) FROM Sales WITH (NOLOCK) WHERE salid > <watermark_salid>;
```

**Expect:** `SQL count − invoices to upload` = **0 to 5**, and never negative.

**The only acceptable difference is invoices created in the last 30 seconds.** The agent
holds those back on purpose: `NOLOCK` can read a row still being written that is later
rolled back, which would otherwise raise a "deleted invoice" alert for a receipt that
never existed. A busy counter shows 1–3. A quiet one shows 0.

### 🛑 ABORT CRITERIA — any of these, stop the visit

- The difference is **negative** (the agent claims more invoices than the POS has)
- The difference is **more than 5**
- The SQL query errors

**Then:** photograph both outputs — the full dry-run block and the SQL result.
**Change nothing. Install nothing. Call.**

A mismatch here means we are reading their data wrong, and every number after this point
would be confidently incorrect.

---

## 6 — One real cycle, then confirm it

```powershell
python agent.py
```

**Expect:** `cycle ok — N invoices, M lines, watermark now <number>` and nothing after it.

```powershell
python agent.py --confirm
```

**Expect:** the last line is

```
  RESULT: OK — data landed, watermark advanced, agent reporting in
```

and in the block above it:

- `invoices` and `invoice_lines` match step 4's numbers
- `lines w/o price` is `0`
- `watermark_salid` matches the POS `MAX(salid)`
- `last_rescan_at` is **not** null
- the newest heartbeat starts `ok ` with `drift=` within ±300

> `--confirm` reads the cloud back with the agent's own token and prints the verdict
> here. No browser, no second device. It also proves the token that ships is the token
> that works.

**If `RESULT: NEEDS ATTENTION`,** it lists exactly what is wrong. Common cases:

| Line | Meaning | Do |
|---|---|---|
| `lines w/o price` > 0 | New menu items we have never seen; zero-invoice detection is blind to them | Not a stop. Send us the `unknown_item` notes |
| `drift` beyond ±300 | The POS clock is wrong; every shift boundary depends on it | Fix the machine clock, re-run step 6 |
| `restore_suspected is true` | `salid` went backwards | **STOP. Call.** |
| `no invoices landed at all` | Upload failed silently upstream | **STOP. Call.** |

---

## 7 — Install the scheduled task

```powershell
.\install\install_agent.ps1
Get-ScheduledTask -TaskName thirdeyev | Get-ScheduledTaskInfo
```

**Expect:** `LastTaskResult : 0`.

Wait three minutes, then:

```powershell
Get-ScheduledTask -TaskName thirdeyev | Get-ScheduledTaskInfo
python agent.py --confirm
```

**Expect:** `LastRunTime` inside the last 3 minutes, `LastTaskResult : 0`, and a **newer**
heartbeat than the one you saw in step 6.

**STOP IF:** `LastTaskResult` is not 0, or no new heartbeat appeared. The task is
registered but not running, and nothing will arrive after you leave.

---

## 8 — Before you leave

```powershell
Get-Content C:\thirdeyev\agent.log -Tail 20
```

**Expect:** `cycle ok` lines, no tracebacks.

Confirm with the owner that **no messages will arrive yet**. Detection is running;
sending is off until we turn it on deliberately (step 9, which is not done on site).

---

## 9 — Go-live: turning notifications on (NOT on this visit)

⚠️ **Both statements, one transaction. Not the same session — the same transaction.**

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

**Expect:** exactly equal. The formula is `sales + collections − returns − delivery`.

**If any line differs:** photograph the screen and send the row. **Do not adjust
anything.**

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
