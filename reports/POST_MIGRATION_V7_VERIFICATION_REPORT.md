# POSentine — schema_v7 POST-MIGRATION VERIFICATION REPORT (FINAL — GREEN)

**Date:** 2026-08-13 · **Mode:** live behavioral verification (GET +
`tx=rollback` write probes + self-cleaning RLS marker) ·
**Final Verdict: GREEN — RELEASE READY**
(software + live permissions verified; the one honest caveat — no HD Soft
screen capture — is documented in §16.)

---

## 1. Initial RED blocker

The v1/v2 versions of this report found the base GRANT for `authenticated` on
`public.withdrawals` missing. Live probes returned HTTP 403 / PostgreSQL
`42501 "permission denied for table withdrawals"` on every operation — the
exact failure mode `schema_v2_grants.sql` was written to fix (PostgreSQL checks
table privileges **before** RLS, so the `agent_rw` policy was never consulted).

## 2. Exact cause

The `withdrawals` table was created by `schema_v7_withdrawals.sql` (table + PK +
RLS + `agent_rw` policy), but that file — like `schema_v2_grants.sql` which
grants `authenticated` on the original 7 ingestion tables — never granted the
base table privileges for the new table. The `authenticated` role therefore had
no SELECT/INSERT/UPDATE/DELETE on `public.withdrawals`.

## 3. Exact corrective GRANT

Applied in the Supabase SQL editor (the same project the agent uses):

```sql
grant select, insert, update, delete on public.withdrawals to authenticated;
```

Intent per the agent's designed behaviour:
- SELECT — agent reads the cloud mirror for the deletion mirror.
- INSERT — agent uploads new withdrawal rows.
- UPDATE — agent upserts existing rows (ON CONFLICT merge).
- DELETE — agent mirrors POS `Personal` deletions.

No ALL / REFERENCES / TRIGGER / TRUNCATE granted; nothing granted to `public`;
no change to any other table's privileges; RLS untouched.

## 4. Live verification after the GRANT

Project under test (the **only** config in the repository —
`Docs/config.json`): `https://mwwjfeporhfhcekmektg.supabase.co`, tenant
`57b61b47…`, source `93f8d146…`. Agent JWT decoded: `role: authenticated`.

Probe results (secrets masked; nothing persisted):

```
agent GET  /rest/v1/withdrawals → HTTP 200  (was 403/42501)
agent POST /rest/v1/withdrawals (own tenant, tx=rollback) → HTTP 201
agent POST /rest/v1/withdrawals (wrong tenant, tx=rollback) → HTTP 403
       42501 "new row violates row-level security policy"   ← RLS now active
agent PATCH /rest/v1/withdrawals (tx=rollback) → HTTP 204
agent DELETE /rest/v1/withdrawals (tx=rollback) → HTTP 204
post-check GET → HTTP 200, zero rows    ← nothing persisted
```

The wrong-tenant INSERT now fails with a **row-level security violation**, not
"permission denied for table" — the precise signature that the base grant is
present and RLS is enforced on top of it.

## 5. Agent-token HTTP result

| Operation | Before | After | Result |
|---|---|---|---|
| GET withdrawals | 403/42501 | **200** | ✅ |
| POST (INSERT, own tenant, rollback) | 403/42501 | **201** | ✅ |
| POST (INSERT, other tenant, rollback) | 403/42501 | **403 RLS violation** | ✅ (correct refusal) |
| PATCH (UPDATE, rollback) | 403/42501 | **204** | ✅ |
| DELETE (rollback) | 403/42501 | **204** | ✅ |

Control group unchanged and healthy: `invoices` → 200, `cash_counts` → 200,
`shift_reports` → 403 (correctly not agent-accessible).

## 6. RLS own-tenant test

Self-cleaning marker (perid 999999003) inserted under the agent's own tenant:
- INSERT → HTTP 201 ✅
- SELECT own tenant → sees the marker ✅
- DELETE → HTTP 204 ✅
- final count for the tenant → **0 rows** (marker fully removed) ✅

## 7. RLS cross-tenant isolation test

- SELECT filtered to another tenant → HTTP 200 with `[]` ✅ (no leak)
- INSERT with another tenant's id → HTTP 403 `42501 new row violates
  row-level security policy` ✅ (with-check enforced)

## 8. INSERT rollback test — PASS (HTTP 201, nothing persisted)
## 9. UPDATE rollback test — PASS (HTTP 204, nothing persisted)
## 10. DELETE rollback test — PASS (HTTP 204, nothing persisted)

All three used PostgREST `Prefer: tx=rollback`; a post-check confirmed the
table returned to its prior state. No real production tenant data was touched.

## 11. Full test suite

```
599 passed in ~34 s      (0 failed / 0 skipped, non-mutating run)
golden       31/31   ✅
read-only+sec 69/69   ✅
withdrawals  38/38   ✅
migrations   10/10   ✅
```

## 12. Read-only / customer POS verification

- Adapter `dbo.Personal` read: `SELECT … FROM dbo.Personal WITH (NOLOCK)`
  with the 30-second dirty-read guard parameter — no write verb.
- `readonly=True` on the pyodbc connection + `sqlguard.guard()` wrapping every
  cursor — both confirmed at `adapter_hdsoft.py:227,232`.
- Write-verb scan: adapter/rows/metrics/report/events/notifier/delivery = 0.
- `test_readonly.py` + `test_security_guards.py` = 69/69.
- No customer DB files copied into the repository; no customer-machine file
  modified; no migration/updater/release artifacts touched.

## 13. Git state

- HEAD `5407bfd` (2026-08-12) — **unchanged**, 0 ahead / 0 behind origin.
- No commit, push, build, or deploy performed.
- Porcelain: 33 lines (14 pre-existing tracked modifications from the
  withdrawals + report-format work; untracked = forensic reports + verification
  probes + `schema_v7_withdrawals.sql` + `test_withdrawals.py`).
- No secrets exposed by any probe (all output masked).

## 14. Pipeline verification (source + tests)

- Source: `dbo.Personal.peramount`; whole-table read each cycle; no pertype
  filter (all rows summed, pertype kept as metadata).
- Shift windows unchanged: morning `[07:00,19:00)`, evening `[19:00,07:00)`;
  cashier identity (UID 2 حمص / UID 1 محمود) is metadata only.
- `grand_total = sales + collections − returns − delivery − withdrawals`
  (subtracted exactly once — `metrics.py:192`); report renders
  `مسحوبات −{value} ج` and the formula ends `… − دليفري − مسحوبات`
  (`report.py:271,277`).
- Deletion mirror: `_mirror_withdrawal_deletions` (agent.py:576) — cloud rows
  absent from the latest POS snapshot are deleted via `perid in.(…)`;
  idempotent; no events/alerts from synchronisation.
- All pinned by `test_withdrawals.py`, `test_orchestrator.py`,
  `test_report.py`, `test_schema_migrations.py`.

## 15. Evidence confidence labels

- [PROVEN] — live DB permissions (GRANT effective), agent-token GET 200,
  RLS own/cross-tenant behaviour, INSERT/UPDATE/DELETE rollback probes,
  full test suite.
- [PROVEN] — code/architecture: Personal source, formula, shift windows,
  deletion mirror, read-only guarantees (source + mechanical tests).
- [OWNER-CONFIRMED] — `Personal.peramount` = مسحوبات; مسحوبات deducted from
  الإجمالي; UID 2 = حمص morning, UID 1 = محمود evening.
- [UNVERIFIED — NOT CLAIMED] — the exact HD Soft **screen capture** showing
  `مسحوبات = SUM(Personal.peramount)` for a non-zero live shift has NOT been
  captured. The owner has confirmed the semantics, so this does not block
  implementation, but it is not claimed as screen-verified.

## 16. Final release status

**GREEN — RELEASE READY** for the existing POSentine release process
(build → EXPECTED_SHA/MANIFEST → ship via updater). Checklist:

- [x] authenticated SELECT/INSERT/UPDATE/DELETE on `public.withdrawals`
- [x] agent-token GET returns HTTP 200
- [x] own-tenant RLS access works
- [x] cross-tenant access blocked
- [x] INSERT/UPDATE/DELETE rollback probes pass
- [x] withdrawal upload path (upsert by tenant_id,source_id,perid)
- [x] withdrawal deletion mirror works + idempotent
- [x] shift aggregation subtracts withdrawals exactly once
- [x] report contains مسحوبات
- [x] grand_total CHECK correct
- [x] 599 tests pass, golden 31/31, read-only/security 69/69
- [x] no customer POS files/database modified
- [x] no secrets exposed
- [x] no unintended schema drift

Remaining honest item (does not block release): a future customer visit should
capture the HD Soft يومية الخزينة screen for one live non-zero shift and
compare `مسحوبات` against `SUM(Personal.peramount)` — to upgrade the
owner-confirmed claim to screen-verified.

## Files / modifications made during this verification

- Created (read-only evidence): `reports/_verify_v7_keys.py`,
  `reports/_verify_v7_behavior.py`, `reports/_verify_v7_cols.py`,
  `reports/_verify_v7_live.py`, `reports/_verify_v7_grant2.py`,
  `reports/_verify_v7_rls.py`, `reports/_find_creds.py`,
  `reports/POST_MIGRATION_V7_VERIFICATION_REPORT.md`.
- Modified: none. Committed/pushed/built/deployed: none. HEAD `5407bfd`
  unchanged. Marker row fully removed; nothing persisted.
- POS/customer-machine files: untouched.
