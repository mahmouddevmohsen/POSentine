# VERIFY — on-site acceptance for the POSentine agent

For whoever is standing at the shop. Work top to bottom. **Every step has an
expected result; if what you see does not match, stop at that step** and use the
"if it does not match" note. Do not continue past a mismatch — the whole point of
this product is that wrong numbers are worse than no numbers.

Total time if nothing goes wrong: about 15 minutes.

---

## 0 — Before you start

On the POS machine, in `C:\thirdeyev`:

```powershell
chcp 65001
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
```

**Why:** the reports are Arabic. A default Windows console is cp1252 and will mangle
or crash on them. `chcp 65001` makes the output readable. The agent forces UTF-8 on
its own streams too, so it will not crash without this — but you will not be able to
read what it prints.

Confirm you have `config.json` (not `config.example.json`) and that it contains a
`supabase_agent_token`, **not** a service_role key. A service_role key on this machine
is a stop-everything problem, not a configuration detail.

```powershell
python -c "import json;c=json.load(open('config.json'));print('role in token:', 'service_role' in c['supabase_agent_token'])"
```

**Expected:** `role in token: False`
**If it does not match:** stop. Do not run the agent. Call before doing anything else.

---

## 1 — The tests still pass here

```powershell
python -m pytest -q test_golden.py
```

**Expected:** `31 passed`
**If it does not match:** stop. The machine has a different copy of the code than we
verified. Do not proceed.

---

## 2 — Dry run: what would be read

```powershell
python agent.py --dry-run
```

**Expected:** a block headed `DRY RUN (nothing is written, anywhere)` showing the ODBC
driver, `schema check OK`, the watermark, counts, the `sold_at` range, and a kind
breakdown.

Write down these two numbers:

- `watermark_salid` = ________
- `invoices to upload` = ________

**This step writes nothing** — no heartbeat, no watermark, no state file. Running it
twice is safe, and running it while the scheduled task is running is safe.

### If it does not match

| What you see | What it means | Do |
|---|---|---|
| `مفيش ODBC driver مناسب` | No SQL Server ODBC driver installed | Install "ODBC Driver 17 for SQL Server", retry |
| `Login failed for user 'monitor_ro'` | Wrong password, or Mixed Mode auth is off | Stop; call |
| `أعمدة ناقصة بعد تحديث محتمل لـHD Soft` | HD Soft was updated and a column we rely on changed | **Stop. Do not proceed.** Send the full message |
| `restore suspected` | The database was restored from a backup; `salid` moved backwards | **Stop. Do not proceed.** Manual review required |
| `⚠ items missing from the snapshot` | New menu items we have not seen | Not a blocker — note the IDs and continue |

---

## 3 — Compare against the POS itself

This is the acceptance check. In SQL Server Management Studio, or `sqlcmd`, run —
substituting the watermark you wrote down:

```sql
SELECT COUNT(*) FROM Sales WITH (NOLOCK) WHERE salid > <watermark_salid>;
```

**Expected:** a number **greater than or equal to** `invoices to upload`, and larger by
at most a handful.

**Why not exactly equal:** the agent deliberately ignores invoices created in the last
30 seconds. `NOLOCK` can read a row that is still being written and later rolled back,
which would otherwise produce a "deleted invoice" alert for a receipt that never
existed. A busy shop will show a difference of one or two. A quiet one, zero.

**If the difference is large** (more than about five, or the agent's number is higher):
stop and call. That is not a timing artefact.

---

## 4 — One real cycle

```powershell
python agent.py
```

**Expected:** `cycle ok — N invoices, M lines, watermark now <number>`, exit code 0.

Then in Supabase → SQL Editor:

```sql
select
  (select count(*) from invoices)                                    as invoices,
  (select count(*) from invoice_lines)                               as lines,
  (select count(*) from invoice_lines where list_price is null)      as lines_missing_price,
  (select count(*) from cash_counts)                                 as cash_counts,
  (select count(*) from pos_products)                                as products,
  (select watermark_salid from sync_state limit 1)                   as watermark,
  (select last_rescan_at   from sync_state limit 1)                  as last_rescan_at;

select at, ok, agent_version, drift_seconds, rows_pulled, note
from heartbeats order by at desc limit 10;
```

**Expected:**

- `invoices` and `lines` match what the dry run said it would upload
- `watermark` equals the POS `MAX(salid)`
- `last_rescan_at` is not null (the first cycle is always a rescan)
- the newest heartbeat has `ok = true` and `note = null`
- `drift_seconds` is within ±300

**If `lines_missing_price` is greater than 0:** the snapshot is missing menu items.
Zero-invoice detection cannot see those lines. Look for `note` rows with
`"kind": "unknown_item"` and send us the item IDs. Not a stop, but tell us.

**If `drift_seconds` is beyond ±300:** the POS clock is wrong. Every shift boundary
depends on it. Fix the machine clock before go-live.

**If a heartbeat has `ok = false`:** read its `note` — it is JSON and names the problem.
`restore_suspected` and `schema_drift` are stop-and-call.

---

## 5 — Let it run, then check a real shift

Install the scheduled task and leave it for one full shift.

```powershell
Get-ScheduledTask -TaskName thirdeyev | Get-ScheduledTaskInfo
```

**Expected:** `LastTaskResult` is `0`, and `LastRunTime` is within the last 3 minutes.

After a shift boundary (07:00 or 19:00), open HD Soft's **يومية الخزينة** screen for
that shift and compare against:

```sql
select shift_date, shift_name, sales, returns, delivery, collections, grand_total
from shift_reports order by shift_date desc, shift_name desc limit 4;
```

**Expected:** `grand_total` equals the total on their screen, exactly.

The formula is `sales + collections − returns − delivery`. If any single line differs,
stop and send us the screen and the row. **Do not adjust anything to make them match.**

---

## 6 — Go-live: enabling notifications

⚠️ **These two changes must happen in the same statement.** Not the same session — the
same statement.

During the silent period the system detects events and stores them with
`notify = false`. Nothing is sent. But `filter_sendable` suppresses an event only when
it is older than `go_live_at`, and `go_live_at` is null until now. The moment
`notify` becomes true with `go_live_at` still null, **every event accumulated since
installation becomes sendable at once** — and the owner's first-ever contact from this
system would be dozens of alerts about things that happened last week.

Setting `go_live_at = now()` at the same instant is what suppresses that backlog.

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

Then confirm the backlog really is suppressed **before** anyone is expecting messages:

```sql
select count(*) as would_have_been_sent
from events e
join tenants t on t.id = e.tenant_id
where t.slug = 'sobh_onthefast'
  and e.occurred_at < t.go_live_at
  and e.status = 'detected';
```

**Expected:** whatever the number, none of these will be sent — they are older than
`go_live_at`. A large number here is normal and is exactly what the guard is for.

**If `go_live_at` was already set** before you ran this, the first `update` leaves it
alone (`and go_live_at is null`), which is deliberate: re-running this must never move
the line forward and re-suppress events that were legitimately sendable.

---

## 7 — Uninstall, if it comes to that

```powershell
.\install\uninstall_agent.ps1
```

Removes the scheduled task and stops the agent. It does not delete uploaded data, and
it does not touch the POS database — nothing in this product ever writes there.

---

## What to send us if you get stuck

```powershell
Get-Content C:\thirdeyev\agent.log -Tail 100
```

Plus the step number you stopped at and the exact message. The log is UTF-8 and
contains no passwords or tokens.
