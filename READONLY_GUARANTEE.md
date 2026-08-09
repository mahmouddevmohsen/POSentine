# The POS database is never written to

**What we promise:** POSentine reads the cashier machine's database. It does not
write to it, alter it, or delete anything in it — not one statement, ever.

This document says what *enforces* that at each layer, how it is proved on the
machine itself, what would have to go wrong for it to fail, and — at the end —
what is **not** covered. An honest boundary is worth more than a broad claim.

> Every layer below is rated **enforced** (something outside our code refuses us)
> or **convention** (we have been keeping a rule). Only the enforced ones are
> load-bearing. A layer we think exists and does not is worse than a missing one.

---

## The short version

| # | Layer | Rated | What it actually stops |
|---|---|---|---|
| 1 | `monitor_ro` — `db_denydatawriter` | **Enforced** — SQL Server | `INSERT`, `UPDATE`, `DELETE`, `MERGE` on every table and view |
| 2 | `monitor_ro` — absence of any other grant | **Enforced, but weakly** | DDL, `TRUNCATE`, `SELECT … INTO`, `BACKUP` — see §2, this is where the risk lives |
| 3 | `sqlguard.assert_read_only` | **Enforced** — our code raises | Anything that is not a `SELECT`, before it reaches the network |
| 4 | `pyodbc.connect(readonly=True)` | **Convention. Counts for nothing.** | Nothing. See §4 — it is a hint the driver may ignore, and it does |
| 5 | Source scan in the test suite | **Enforced** — CI refuses | A future edit that adds a write to the adapter |
| 6 | The on-site probe | **Enforced** — the install aborts | Credentials that turn out not to be read-only, on this machine, today |
| 7 | Disk | **Enforced** — proven by audit hook | Any file written outside our own folder |

Layers 1 and 3 are the two that carry the promise. Layer 6 is what turns the
promise into something we have *checked* rather than something we *believe*.

---

## 1. `db_denydatawriter` — what it does cover

`monitor_ro` is a member of `db_datareader` (SELECT everywhere) and
`db_denydatawriter` (DENY INSERT, UPDATE, DELETE everywhere).

`DENY` is not the absence of a permission — it is a refusal that **overrides any
`GRANT`**, at every level. If someone later adds `monitor_ro` to `db_datawriter`
by mistake, the `DENY` still wins and writes still fail. That is what makes this
the strongest layer we have.

It covers:

- `INSERT`, `UPDATE`, `DELETE` on every table and view in the database
- `MERGE`, which requires those same three permissions and so is covered
  transitively rather than by name

## 2. `db_denydatawriter` — what it does **not** cover

**This section is the honest part.** `db_denydatawriter` denies exactly three
permissions. Everything below is blocked only because nobody granted it — and an
absence can be handed out by a helpful administrator without anyone touching the
`DENY`.

| Path | Covered by the DENY? | What actually blocks it |
|---|---|---|
| `TRUNCATE TABLE` | **No** | Requires `ALTER` on the table. Blocked by *absence of `ALTER`*. |
| `DROP` / `ALTER` / `CREATE TABLE` | **No** | Requires DDL permission. Blocked by *absence*. |
| `SELECT … INTO` | **No** | Creates a table; requires `CREATE TABLE`. Blocked by *absence*. |
| `BACKUP DATABASE` | **No** | Not a write to the data, but it copies the whole database. Blocked by *absence*. |
| `EXEC` of a stored procedure | **No — and this is the sharpest gap** | See below. |
| `sp_executesql` | **Yes, indirectly** | Dynamic SQL runs under the *caller's* permissions, so the `DENY` still applies. Counter-intuitive, and it is the safe direction. |
| `xp_cmdshell` | **No** | Requires `sysadmin` or a configured proxy, and is disabled by default. Blocked by *absence and server config*. |
| `OPENROWSET` / `OPENDATASOURCE` | **No** | Requires `Ad Hoc Distributed Queries` to be enabled server-wide (off by default) plus permission. Blocked by *absence and server config*. |
| `BULK INSERT` | **No** | Requires `ADMINISTER BULK OPERATIONS`. Blocked by *absence*. |

### The stored-procedure gap, stated plainly

If `monitor_ro` were ever granted `EXECUTE` on a stored procedure that writes,
**`db_denydatawriter` would not stop the write.** Under SQL Server's *ownership
chaining*, when a procedure and the tables it touches share an owner, the
permission check on those tables is **skipped entirely** — so the `DENY` is never
evaluated.

`db_datareader` does not grant `EXECUTE`, so today this path is closed by absence.
Two things close it further:

- **`sqlguard` refuses `EXEC` and `EXECUTE` outright** (layer 3), so our code
  cannot take this path even by accident.
- **`monitor_ro.sql` issues an explicit `DENY EXECUTE ON SCHEMA::dbo`**, which
  converts this from an absence into a refusal. We recommend applying it.

### One condition that voids layers 1 and 2 entirely

If `monitor_ro` is a member of the **`sysadmin`** server role, SQL Server skips
permission checks altogether and nothing above applies. If it is a member of
**`db_owner`**, it can remove the `DENY` itself.

Neither should be true. Because "should" is not a control, the on-site probe
(§6) reads `IS_SRVROLEMEMBER('sysadmin')` and `IS_ROLEMEMBER('db_owner')` on
every install and **aborts if either is true**.

## 3. `sqlguard` — one choke point in our code

Every statement bound for the POS passes through `sqlguard.assert_read_only`.
It is wired at the **connection**, not at the call sites, so it also covers
statements written months from now by someone who never read this file:

```python
cn.autocommit = True
return sqlguard.guard(cn)          # adapter_hdsoft.connect()
```

Two rules, and the first is the real control:

1. **The leading verb is allowlisted** to `SELECT` and `WITH`. `EXEC`, a bare
   `sp_who`, `SET`, `BEGIN TRAN` are refused for not being on the list, rather
   than for being on a list of things we thought of.
2. **A denylist inside the statement**, for writes that can hide behind a legal
   opening: `WITH x AS (…) DELETE FROM x`, `SELECT … INTO`, and a batch with a
   second statement behind a semicolon.

Comments and string literals are removed first, so `SELECT '--'` reads correctly
and `SELECT 1 -- \n ; DROP TABLE x` cannot smuggle a statement past it. An
unterminated literal or comment is refused rather than guessed at.

> **Status:** `adapter_hdsoft.py` is a locked file. The two-line diff that calls
> `guard()` is in `sqlguard_wiring.patch` and is applied by the architect. Until
> it is applied, this layer is **not active** — and the install transcript says
> so in words, every time, rather than leaving it to be assumed.

## 4. `pyodbc.connect(readonly=True)` — this is not protection

`readonly=True` sets the ODBC attribute `SQL_ATTR_ACCESS_MODE` to
`SQL_MODE_READ_ONLY`. **The ODBC specification defines this as a hint.** A driver
is permitted to accept it and ignore it, and the Microsoft ODBC driver for SQL
Server does exactly that: SQL Server has no read-only session mode.
(`ApplicationIntent=ReadOnly` is Availability Group *routing*, not enforcement.)

**We rate this as worth nothing and count it for nothing.** It stays in the
connection string because it is free and correct to state the intent, not because
it defends anything.

We could not test this on the build machine — there is no SQL Server on it. The
on-site probe settles it, and its output tells us *which layer refused*: SQL
Server error **229** (`The UPDATE permission was denied…`) means the server
refused. A driver-level refusal would carry a different SQLSTATE. Either way the
write is refused; the transcript records which layer did it.

## 5. The source cannot grow a write without the suite failing

`test_readonly.py` parses every POS-facing module and fails if any SQL literal
contains a write keyword. This is not a review rule — a future edit that adds a
write to the adapter cannot pass by looking innocent.

Proven by injecting one:

```
E   AssertionError: write SQL outside the probe:
E       agent.py: 'UPDATE dbo.Sales SET saltot = 0 WHERE salid = 1'
FAILED test_readonly.py::test_no_pos_facing_module_contains_a_write_statement[agent.py]
FAILED test_readonly.py::test_write_sql_lives_in_exactly_one_file
```

Exactly one file in the repository may contain write SQL — `readonly_probe.py` —
and a separate test fails if a second one ever does.

## 6. 🎯 The proof that actually matters: we attack the POS on every install

Before the agent reads a single invoice, **preflight step 3b attempts to write to
the POS database with the agent's own credentials and requires every attempt to
be refused.** If any is permitted, the install **aborts**.

It runs on every install, not once, because permissions drift and someone
helpful "fixes" a login.

**Attempted** — actually sent to the customer's SQL Server:

```sql
UPDATE dbo.Sales   SET salid = salid   WHERE 1 = 0;
DELETE FROM dbo.Sales                  WHERE 1 = 0;
INSERT INTO dbo.Sales (salid) SELECT salid FROM dbo.Sales WHERE 1 = 0;
-- and the same three shapes against dbo.SalesDe and dbo.Items
```

Every one carries `WHERE 1 = 0`, so a probe that is wrongly *permitted* still
changes nothing. That is deliberate belt-and-braces: SQL Server checks
permissions when it compiles a statement, before it touches a row, so a denied
statement raises — but we do not rely on that alone when the target is a working
restaurant's sales table.

**Asked, never attempted.** There is no harmless version of these, so they are
interrogated with `HAS_PERMS_BY_NAME`, which is itself a `SELECT` and accounts
for `DENY`, role membership and ownership:

- `TRUNCATE dbo.Sales` takes **no `WHERE` clause**. A probe that is wrongly
  permitted empties the customer's sales history. We do not run it. TRUNCATE
  requires `ALTER` on the table, so *"can this login `ALTER dbo.Sales`"* is the
  same question with no risk attached.
- `ALTER TABLE dbo.Sales ADD …` permanently changes their live table.
- Wrapping either in a transaction and rolling back would take a
  schema-modification lock on `dbo.Sales` **during service**, blocking the POS
  itself. Refused.

Also read and required to be "no": `CONTROL` on each table, `CREATE TABLE` and
`ALTER` at database scope, `BACKUP DATABASE`, `CONTROL SERVER`, and membership of
`sysadmin`, `db_owner`, `db_ddladmin` and `db_datawriter`.

**An inconclusive answer is not a pass.** If the server will not say, the install
stops. "We could not tell" and "it is refused" must never produce the same
outcome — that equivalence is the failure mode this whole product exists to
avoid.

The full block, including the verbatim SQL error for every refusal, is printed on
screen, written into the install transcript, and included in the diagnostics zip.
**That transcript is our evidence to the customer.**

## 7. The disk

Nothing the agent writes goes outside its own folder. This is proven with a
Python audit hook that records every file the interpreter opens for writing
during a real cycle, rather than by reading the code and believing it:

- `state.json`, `state.json.tmp`, `state.lock` — the sync watermark
- `agent.log` and up to five rotated copies
- `logs\install_*.txt` — install transcripts
- `__pycache__\` — Python's own bytecode, inside our folder

Nothing writes to `D:\HDSOFT` or any other POS path. The test that proves it has
its own falsifier — a deliberate stray write that it must catch — so it cannot
pass for the wrong reason.

---

## What the installer touches on this machine

The complete list. If it is not here, we do not touch it.

**Our own folder** (wherever the operator copied it, e.g. `C:\thirdeyev`):

| Path | Written by | Removed by `uninstall_agent.ps1 -Purge` |
|---|---|---|
| `config.json` | placed by hand | yes |
| `state.json`, `state.json.tmp` | the agent | yes |
| `state.lock` | the agent | yes |
| `agent.log`, `agent.log.1` … `.5` | the agent | yes |
| `logs\install_*.txt` | the installer | yes |
| `diagnostics_*.zip` | `collect_diagnostics.bat` | yes |
| `__pycache__\` | Python | yes |

**One Scheduled Task**, named `thirdeyev`, registered for the current user at
`LeastPrivilege`. Removed by `uninstall_agent.ps1`.

Registering a task causes **Windows itself** to write to
`C:\Windows\System32\Tasks\` and to the scheduler's registry keys under
`HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Schedule`. That is the
operating system's own bookkeeping, done on our behalf by the documented API, and
unregistering the task reverses it. We name it because it is a machine change
outside our folder and this list is supposed to be complete.

**Python packages.** `pip install -r requirements.txt` installs `pyodbc`,
`requests`, `pytest` and their dependencies into the machine's Python
installation. **This is the one thing the uninstall does not reverse**, and that
is a deliberate choice: uninstalling shared packages could break anything else on
that machine that uses Python. Named here rather than hidden.

**Not touched at all:** `PATH`, any environment variable outside our own process,
file associations, the registry beyond the scheduler's own entries, Windows
services, the startup folder, firewall rules, and any file belonging to HD Soft.

---

## What would have to go wrong

For a write to reach the customer's POS database, **all** of these would have to
be true at once:

1. `monitor_ro` has been granted a write permission, or added to `sysadmin` or
   `db_owner` — **and**
2. the on-site probe did not catch it, which means the install was never re-run
   after the change — **and**
3. `sqlguard` was bypassed or the wiring was removed, which fails the test suite
   — **and**
4. someone wrote a write statement into the adapter, which fails the source scan.

The realistic risk is **(1) plus (2)**: someone changes the login's permissions
*after* we install, and nobody re-runs preflight. Our current answer is that
preflight runs on every install and the check is cheap to re-run at any time:

```
python preflight.py --skip-install
```

---

## What is **not** guaranteed

Stated plainly, because a boundary we name is worth more than one we imply.

1. **We do not control the `monitor_ro` login.** It was created on the customer's
   SQL Server by hand, and anyone with administrator rights on that machine can
   change its permissions at any time. What we guarantee is that we *check* it on
   every install and refuse to run if it is wrong — not that it cannot change
   between installs.

2. **`monitor_ro`'s permissions had no committed definition until now.** They
   existed only as something typed once into a management tool. `monitor_ro.sql`
   in this repository now defines them, is idempotent, and can be re-applied to
   re-assert them. Applying it is the customer's DBA's decision, not ours.

3. **We read the customer's data, and it leaves the machine.** Invoices, line
   items, cash counts, item names and staff user IDs are uploaded to our Supabase
   project over HTTPS. Read-only means we do not *change* their database; it does
   not mean nothing leaves it. What we upload is listed in `rows.py`.

4. **Anyone with administrator rights on the till can do anything**, including
   replacing our files. `MANIFEST.txt` detects that on the next preflight — it
   does not prevent it.

5. **The Scheduled Task runs only while the till user is logged on.** If the
   machine is logged out, cycles stop and resume at the next logon. Nothing is
   lost, because the watermark only ever moves forward.

6. **We have not tested against the customer's actual SQL Server.** There is no
   SQL Server on our build machine. Every layer above is tested here in the ways
   that can be tested here; layer 6 is the one that closes the gap, and it runs
   for the first time on site, before anything else does.

---

## How to check it yourself, on the machine, any time

```powershell
cd C:\thirdeyev
python preflight.py --skip-install
```

Look for the block headed **READ-ONLY PROOF**. Every probe must say `REFUSED`
and every permission must say `not held`, ending in:

```
  VERDICT: READ-ONLY CONFIRMED
```

Anything else means stop and call.

---

<!-- ─────────────────────────────────────────────────────────────── -->

## ⚠️ DRAFT — Arabic summary, NOT reviewed, NOT for use yet

> The owner reads Arabic, and this document is meant to be showable to him. The
> project rule is that Arabic customer-facing text is fixed, reviewed wording —
> so this is a **draft for review**, not approved text. Do not send it to anyone
> until it has been checked.

**ضمان القراءة فقط**

البرنامج بيقرأ من قاعدة بيانات الكاشير بس. مش بيكتب فيها ولا بيغيّر ولا بيمسح أي
حاجة — ولا أمر واحد.

اللي بيضمن ده:

1. حساب `monitor_ro` ممنوع عليه الكتابة على مستوى SQL Server نفسه، والمنع ده
   أقوى من أي صلاحية ممكن تتضاف بالغلط.
2. الكود نفسه بيرفض أي أمر مش قراءة قبل ما يخرج من الجهاز.
3. **وقبل كل تركيب، البرنامج بيحاول يكتب في قاعدة البيانات بنفسه ولازم كل محاولة
   تترفض.** لو أي محاولة نجحت، التركيب بيقف تماماً ومش بيكمل.

المحاولات دي مصمّمة إنها متغيّرش ولا صف واحد حتى لو اتسمح بيها.

---

*Last verified: 2026-08-09. Re-verified automatically on every install by
preflight step 3b.*
