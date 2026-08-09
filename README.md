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

## Install / run / verify

To be written at the end of the build. See `VERIFY.md` for the on-site acceptance procedure.

## Baseline check

```bash
pytest -q test_golden.py    # must print: 31 passed
```
