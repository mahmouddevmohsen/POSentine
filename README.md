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

`ship/` is not complete yet: `install/install_agent.ps1` and `uninstall_agent.ps1`
do not exist, so it covers VERIFY.md steps 1–6 but not step 7. `make_ship.py` prints
that in red every time it runs rather than letting the gap go quiet.

## Baseline check

```bash
pytest -q test_golden.py    # must print: 31 passed
```
