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

To be written at the end of the build. See `VERIFY.md` for the on-site acceptance procedure.

## Baseline check

```bash
pytest -q test_golden.py    # must print: 31 passed
```
