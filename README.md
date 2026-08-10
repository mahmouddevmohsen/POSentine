# POSentine

A **read-only** monitoring layer over a restaurant POS (HD Soft, SQL Server 2014 Express,
Windows). It reads invoices from the POS database, uploads them to Supabase, and sends a
shift-closing report to Telegram every 12 hours (07:00 and 19:00, Africa/Cairo).

> This product handles real people's money. A wrong number is worse than no number, because
> a wrong number gets believed. Every failure mode that matters here is **silent**.

## Governing rules

1. **Read-only on the POS.** Not one write statement. `monitor_ro` holds no write permission.
2. **No AI in the product.** Every sentence is a fixed string in `report.py`, chosen by `if/else`.
3. **No `datetime.now()` outside the scheduler.** Windows are passed in explicitly.
4. **POS timestamps are stored as-is.** No UTC conversion.
5. **`service_role` lives only in GitHub Secrets** — never on the customer machine.
6. **Unknown values are never coerced into benign defaults.** Loud beats silent.
7. **Our failures are never his alerts.** Agent and infrastructure problems are internal.

## Layout

| File | Role | Status |
|---|---|---|
| `adapter_hdsoft.py` | SQL Server reads + invoice classification | 🔒 locked |
| `metrics.py` | Shift computation + cash-daybook formula | 🔒 locked |
| `events.py` | Event detection, dedup, severity | 🔒 locked |
| `report.py` | Arabic report and alert text builders | 🔒 locked |
| `test_golden.py` | 31 golden tests from verified customer data | 🔒 locked |
| `schema.sql` | Supabase schema (already applied) | 🔒 locked |

The locked files encode decisions derived from analysis of the customer's real database.
They are not edited. If one of them is wrong, the build stops and we discuss it.

## Database privileges

`schema.sql` enables RLS on all 15 tables and writes policies for 7, but issues no `GRANT`.
PostgreSQL checks table privileges *before* RLS, so those policies were never consulted and
every role got `42501` on every table. Two additive migrations fix it without touching
`schema.sql`:

| File | What it does |
|---|---|
| `schema_v2_grants.sql` | Grants `service_role` everything; grants `authenticated` `select/insert/update` on the seven agent tables plus `heartbeats_id_seq`. |
| `schema_v3_revoke_inherited.sql` | Revokes the `TRUNCATE/REFERENCES/TRIGGER/MAINTAIN` that `anon` and `authenticated` inherited from the `postgres` default ACL, and closes the inheritance so new tables cannot re-acquire it. One transaction. |

Verified state: `anon` holds nothing, `authenticated` holds exactly seven tables with
`INSERT,SELECT,UPDATE` and no `DELETE`, `service_role` holds all fifteen.

### ⚠️ Known open condition — `supabase_admin`

`supabase_admin`'s default ACL grants `anon` and `authenticated` full `arwdDxtm` on any table
**it** creates. Our tables are created by `postgres`, where the inheritance is closed, so the
path is dormant. It cannot be fixed from here — altering that role's defaults is not possible
through the Management API's role.

It is therefore guarded rather than closed. `audit_privileges.py` runs from the monthly
keepalive workflow and **fails the build** if `anon` appears on any table in `public`, if the
agent token gains `DELETE`/`TRUNCATE`, if it can reach an orchestrator table, or if it loses a
grant it needs. An empty query result is treated as a failure, not a pass.

```bash
SUPABASE_ACCESS_TOKEN=<pat> python audit_privileges.py    # exit 0 clean, 1 drift
```

Requires a `SUPABASE_ACCESS_TOKEN` repository secret — `information_schema` is not reachable
through PostgREST, and exposing it via an RPC function would create the very surface that
currently makes `TRUNCATE` unreachable.

## Install / run / verify

`VERIFY.md` is the on-site acceptance procedure, steps 1–10. Steps 1–4 are the
read-only ones and are bundled into `preflight.bat`, which the operator
double-clicks: console to UTF-8, Python, dependencies, config and the **decoded**
token, the golden baseline, then `agent.py --dry-run`. It stops at the first
failure, names the VERIFY.md step, and says what to do. Steps 5 onward stay manual
because they involve decisions.

The logic lives in `preflight.py`, not in the `.bat` — cmd is a poor language to be
careful in, and everything in `preflight.py` is covered by `test_preflight.py`.

Two checks there are not in VERIFY.md as anything a script could previously catch:

| Check | Why it exists |
|---|---|
| The block is judged, not the exit code | `agent.py --dry-run` exits `0` after printing an `ABORT` block. Exit status is not the verdict. |
| `watermark_salid = 0` with invoices behind it fails | At watermark 0 the agent's own cross-check compares the whole table against the whole table, agrees with itself, and prints `PASS`. Two identical wrong answers. |

### The folder that goes on the customer machine

```bash
python make_ship.py     # builds ship/ and ship/MANIFEST.txt
```

`ship/` is generated, gitignored, and never hand-maintained. A committed second copy
of `agent.py` is exactly how a customer machine ends up running code that drifted
from the tests months ago. `MANIFEST.txt` carries a sha256 of every file, and
`preflight.bat` checks it before it checks anything else.

`config.json` is never bundled. It is placed on the machine separately, by hand.

### The scheduled task (VERIFY.md step 7)

```powershell
powershell -ExecutionPolicy Bypass -File .\install\install_agent.ps1 -ShowXml   # inspect
powershell -ExecutionPolicy Bypass -File .\install\install_agent.ps1            # register
powershell -ExecutionPolicy Bypass -File .\install\uninstall_agent.ps1          # remove
```

The task's 3-minute repetition **is** the agent's loop — `agent.py` runs one cycle per
invocation and exits, so a dead process is replaced on the next tick and a reboot
recovers by itself.

| Choice | Why |
|---|---|
| Explicit task XML, not `New-ScheduledTrigger` | An indefinite repetition through the cmdlets relies on `[TimeSpan]::MaxValue` surviving a round trip into task XML, which is version-dependent and has a known failure. XML with an `<Interval>` and no `<Duration>` means forever, everywhere — and `-ShowXml` lets it be read before it is registered. |
| Action is `powershell.exe -WindowStyle Hidden`, not `python.exe` | A console app launched by the scheduler shows its window. A black window on the till every three minutes during service is not acceptable. |
| Not `pythonw.exe` | Under `pythonw`, `sys.stderr` is `None`, so `agent.py`'s logging `StreamHandler` fails on every record and logging swallows the failure. Silent is the one thing this product may not be. |
| `install/run_agent.ps1` wrapper | A Scheduled Task action has a command, arguments and a working directory — no environment block. The wrapper is that block: `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`. |
| The python path is passed in, not written into the wrapper | `run_agent.ps1` is hashed in `MANIFEST.txt`. A script that rewrites itself fails the integrity check `preflight.bat` runs first. |
| `ExecutionTimeLimit` `PT15M` | `MultipleInstancesPolicy` is `IgnoreNew`, so one hung cycle blocks every later one. 15 minutes matches `agent.py`'s own `LOCK_STALE_SECONDS`, so the task and the agent cannot disagree about when a cycle is dead. |
| Batteries settings forced to `false` | Both default to `true`. On a till behind a UPS the defaults stop the agent on the first power blip, silently. |
| `LogonType InteractiveToken`, `RunLevel LeastPrivilege` | The till account is not an administrator. Running while logged off needs a stored password or an admin-granted logon right. The trade-off — cycles stop while logged out — is stated in VERIFY.md step 7 rather than worked around. |

The `.ps1` files are **ASCII-only and saved with a UTF-8 BOM**: Windows PowerShell 5.1
reads a BOM-less `.ps1` as the system ANSI code page, and one non-ASCII character
becomes a parse error at a line that looks fine.

## Baseline check

```bash
pytest -q test_golden.py    # must print: 31 passed
```

## Deferred, on purpose

| Item | Status | Why |
|---|---|---|
| PyInstaller single executable | **DEFERRED** (2026-08-09, reaffirmed 2026-08-10) | Not to be attempted before the site visit. **Unevaluated** — two investigations were dispatched and both died before returning. What exists is *priors, not findings*: `test_golden.py` runs **on the customer machine** as acceptance evidence and `MANIFEST.txt` checks 24 files individually, and one opaque binary weakens both before the antivirus question is even reached. Reopen after the visit, with measurements. |
| `config.json` from an interactive prompt | **DEFERRED**, same decision | Moves the risk rather than removing it: pasting a 279-character JWT into a Windows console is its own failure mode, and `Config.load` already catches every typo class it would prevent. |
| `monitor_ro.sql` | **Written, deliberately not applied** | The login works and `readonly_probe.py` proves its behaviour empirically at every install — stronger than a file recording intent. Applying a permissions script to a working login the day before a visit risks the one thing that cannot be debugged remotely. Apply it for customer #2, from the start. |

Neither of the first two is closed. They are open and parked, with the reason recorded.
