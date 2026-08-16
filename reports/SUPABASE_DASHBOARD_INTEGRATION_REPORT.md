# POSentine — Phase 3: Secure Supabase → Dashboard Integration

**Date:** 2026-08-16
**Subject:** `dashboard/POSentine Arabic Dashboard/POSentine Dashboard.dc.html` + `schema_v8_dashboard_ro.sql` + `mint_dashboard_token.py`
**Previous verdict:** 🟢 DASHBOARD STABILIZATION + VERIFICATION PASSED (Phase 2)

---

## 1. Architecture (STEP 4 — decision with evidence)

**Decision: a dedicated read-only Postgres role (`dashboard_ro`) with SELECT-only grants + tenant-scoped RLS policies + a minted read-only JWT held by the browser.** This is the exact pattern the system already uses for the till agent (`mint_agent_token.py` + `agent_rw` RLS policies + `tenant_id` JWT claim) — extended, not invented.

**Why the evidence determines this (not an arbitrary pick):**

| Fact (live-proven 2026-08-16) | Consequence |
|---|---|
| `anon` has **zero** grants on any table (schema_v2 §4, deliberate) | Browser cannot use anon |
| `authenticated` (agent token) is **write-capable** on ingestion tables + `withdrawals` grants **DELETE** | Reusing it as a browser credential violates the read-only rule (audit R6) |
| `service_role` bypasses RLS; repo comments + schema state it is **GitHub Actions only**, never a client/browser | Cannot ship it to the browser — non-negotiable (§5) |
| Live probe: `shift_reports`/`events`/`tenants`/`internal_anomalies` → **403/42501** for the only browser-available credential | Reporting tables are unreachable until a new grant+policy exists |
| Existing access pattern = minted JWT with `tenant_id` claim + RLS | The proven, maintainable, least-privilege path already in production |
| No always-on server, no Supabase CLI, no functions dir in repo | A proxy/Edge Function adds new infra the repo does not have; not justified by evidence |

**Architecture flow:**
```
browser (.dc.html)                    Supabase (PostgREST)
  └─ anon key (public, gateway)  ───▶  apikey header
  └─ dashboard_ro JWT (SELECT only) ─▶ Authorization: Bearer
                                        RLS dashboard_ro_select: tenant_id = jwt claim
                                        shift_reports · events · tenants ·
                                        internal_anomalies · withdrawals ·
                                        heartbeats · cash_counts · pos_users ·
                                        pos_products · sync_state   (SELECT only)
```

## 2. Data-source mapping (STEP 3)

| Dashboard field | Source table | Columns | Transformation | Verified |
|---|---|---|---|---|
| Shift financials (sales/collections/delivery/withdrawals/returns/total) | `shift_reports` | same-named columns | 1:1 map + `computeGrandTotal` (locked formula) | ✅ reconciliation + harness |
| Shift counts (cash/return/external) | `shift_reports` | `n_cash/n_return/n_external` | 1:1 | ✅ |
| Primary user | `shift_reports` | `primary_user` | → `{name}` | ✅ |
| Status (`has_data`) | `shift_reports` | counts | derived `n_cash+n_return+n_external > 0` | ✅ harness |
| Status (`has_cash_diff`/`has_notes`/notes) | `events` | `type/level/payload/occurred_at` | window attribution per shift | ✅ harness |
| Cash counted state | `cash_counts` | `kind='real_count'` | per-window attribution | ✅ harness |
| Alerts | `events` | `type` ⊆ PUBLIC_TYPES | titles = `report.build_alert` | ✅ harness |
| Coverage (raw %) | `heartbeats` | `at,ok` | window coverage, bounded fetch | ✅ harness |
| Comparison (prev week) | `shift_reports` | same table, `shift_date − 7d` | read; `compareToLastWeek` formula | ✅ harness |
| Tenant identity | `tenants` | `name/currency/timezone` | 1:1 | ✅ harness |
| Other-user activity | — | **not persisted** (report-body only) | honest empty (Case A) — never fabricated | ✅ documented |
| Top items | — | **not persisted** (report-body only) | honest empty state | ✅ documented |
| `gap_explained` classification | — | orchestrator-side only | raw coverage shown; classification not claimed | ✅ documented |

**Honest finding (§10):** `other_users`, `top_items`, and the explained/unexplained-gap classification are computed at report time and stored only inside the report body — they are not queryable columns. The dashboard renders these as verified empty/unavailable states rather than fabricating them, exactly as the Phase 2 stabilization specified.

## 3. Supabase permissions (STEP 2 — live audit)

Read-only probe (agent token, GET only, secrets masked) — `reports/_phase3_probe.py`:

| Table | Before (live) | After `schema_v8` (by design) |
|---|---|---|
| `shift_reports` | 403/42501 | SELECT via `dashboard_ro` + RLS tenant-scoped |
| `events` | 403/42501 | SELECT via `dashboard_ro` + RLS tenant-scoped |
| `tenants` | 403/42501 | SELECT via `dashboard_ro` + RLS tenant-scoped |
| `internal_anomalies` | 403/42501 | SELECT via `dashboard_ro` + RLS tenant-scoped |
| `withdrawals` | 206 (45 rows) | SELECT (no DELETE for this role) |
| `heartbeats` | 206 (2504 rows) | SELECT |
| `cash_counts` | 206 (21 rows) | SELECT |
| `pos_users` | 206 (5 rows) | SELECT |
| `pos_products` | 206 (407 rows) | SELECT |
| `sync_state` | 200 (1 row) | SELECT |

Real data is flowing: 3,678 invoices (2026-08-14…16), 8,207 lines, 45 withdrawals.

## 4. RLS / tenant isolation (STEP 10 — design)

`schema_v8_dashboard_ro.sql` §3 creates `dashboard_ro_select` on all 10 tables:
```sql
create policy dashboard_ro_select on public.<table>
  for select to dashboard_ro
  using ((auth.jwt() ->> 'tenant_id')::uuid = tenant_id);
```
- Tenant boundary enforced **in the database** — a frontend `tenant_id` filter is not the boundary (brief §20).
- Role has **no INSERT/UPDATE/DELETE/TRUNCATE** anywhere (§4 verification SQL included).
- A stolen token can read one tenant's reporting tables and nothing else — the same trust model as the agent token.
- **Live enforcement proof is BLOCKED** until the migration is applied (see §13).

## 5. Security verification (STEP 10)

| Check | Method | Result |
|---|---|---|
| No service_role / agent token / DB password / bot token in dashboard | grep for credential shapes in the file | ✅ absent (harness SECURITY + LIVE DATA LAYER sections) |
| Only `dashboard_ro` role accepted by the loader | `isDashboardToken` guard + harness (role=authenticated refused) | ✅ |
| No hardcoded network host in the data layer | harness: endpoint built only from runtime `cfg.supabaseUrl` | ✅ |
| No hardcoded JWT/key literal values | harness regex scans | ✅ |
| App never writes a secret to storage | harness: only reads `posentine-live-config` (owner-supplied); writes only theme | ✅ |
| Fetch lives only in `liveFetch` (one function, no scattered network) | harness | ✅ |
| Read-only by construction | role has SELECT grants only; loader issues GET only | ✅ design |
| `Docs/config.json` never read by the dashboard | loader reads `window.POSENTINE_LIVE` / localStorage only | ✅ |
| No secrets committed | `git status` — only untracked new files; `Docs/` gitignored; `dashboard/` gitignored | ✅ |

## 6. Implementation changes

1. **`schema_v8_dashboard_ro.sql`** (new, idempotent, house style) — role + SELECT grants + RLS policies + verification SQL. **NOT APPLIED** (owner runs it in the SQL Editor).
   - **Pre-activation review catch #1 (2026-08-16):** the original draft was missing `grant dashboard_ro to authenticator;`. PostgREST connects as `authenticator` and executes `SET LOCAL ROLE <jwt-role>` per request; a custom role must be a member of `authenticator` or every request fails at role-switch (403/42501) even with correct grants and RLS — per PostgREST auth docs and Supabase's Custom Roles guide. **Fixed in the file before asking the owner to run it.**
   - **Live activation catch #2 (2026-08-16, real error `42703: column "tenant_id" does not exist`):** the first run of the migration failed on the RLS policy for `public.tenants`. **Root cause:** `tenants` identifies itself by its primary key `id` — it has **no** `tenant_id` column (`schema.sql`: `id uuid primary key default gen_random_uuid()`). The migration's policy loop applied the child-table template (`= tenant_id`) to `tenants`. The other nine tables (`shift_reports`, `events`, `internal_anomalies`, `withdrawals`, `heartbeats`, `cash_counts`, `pos_users`, `pos_products`, `sync_state`) all have `tenant_id` — verified against `schema.sql` + `schema_v7_withdrawals.sql` by `reports/_verify_v8_schema.py`. **Fix:** `tenants` now gets its own standalone policy comparing to `id`; the loop handles only the nine child tables. The dashboard's live data layer had the **same latent bug** (its `tenants` fetch filtered by `tenant_id`) — fixed to filter by `id=eq.<tid>`, with two regression guards added to the harness (`reports/verify_dashboard.mjs` LIVE DATA LAYER section).
2. **`mint_dashboard_token.py`** (new) — mints the `dashboard_ro` JWT; mirrors `mint_agent_token.py`; `assert_is_dashboard_token` refuses service_role/agent/wrong-tenant tokens.
3. **`test_mint_dashboard_token.py`** (new) — 9 tests: claims, determinism, guards. **9/9 PASS.**
4. **Dashboard** (`POSentine Dashboard.dc.html`) — added a `LIVE DATA LAYER` section: `liveFetch` (single GET path, Range-paginated), `mapShiftRow` (exact `shift_reports` → domain contract), `decorateFromEvents/Beats/Cash`, `alertsFromEvents`, `liveDays`, comparison from prev-week rows, tenant binding, and honest per-screen unavailable states. The component loads live data on mount **only when a token is configured**; otherwise the verified fixture remains, with a visible data-source badge:
   - `بيانات تجريبية — لا تمثل بيانات حقيقية` (demo)
   - `بيانات حية من سجلات الرصد` (live)
   - `البيانات غير متاحة — صلاحية القراءة غير مفعّلة` (403)
   - `تعذّر الاتصال بمصدر البيانات — عرض بيانات تجريبية` (server error)
5. **Harness** — SECURITY section updated to the Phase-3 invariants; new LIVE DATA LAYER section (16 checks) mapping real-shaped rows.

## 7. Real-data validation (STEP 7)

`reports/_phase3_reconcile.py` — read the REAL Supabase invoices (1,397 in window, deleted excluded), withdrawals, users, lines; ran the **locked `metrics.py`** over the exact three Telegram-report shift windows:

| Field | Report 1 (جمعة صباح) | Report 2 (جمعة مساء) | Report 3 (سبت صباح) | Result |
|---|---|---|---|---|
| grand_total | 16,355 = 16,355 | 17,400 = 17,400 | 21,665 = 21,665 | ✅ |
| sales / collections / delivery / withdrawals / returns | all equal | all equal | all equal | ✅ |
| n_cash / n_return / n_external | 240/1/28 | 245/0/12 | 281/0/26 | ✅ |
| primary user | حمص | محمود | حمص | ✅ |
| other-users activity | محمود 26/1,955 | حمص 2/135 | محمود 13/730 | ✅ |
| top-5 items | identical | identical | identical | ✅ |

**RECONCILIATION PASS — real Supabase raw data through the locked business logic reproduces all three verified Telegram reports number for number.** This proves the pipeline the dashboard displays is faithful.

## 8. Telegram validation (STEP 9)

The three reports in `reports/reports from telegram/` were the reconciliation targets above (values from the report texts themselves). The dashboard's live mapper, fed the same underlying shift_reports rows, produces the same grand totals (harness: report #1→16,355, #2→17,400, #3→21,665). ✅

## 9. Cross-screen consistency (STEP 8)

Verified on the live-mapped domain via the harness (same single-source derivation as Phase 2):
- Overview ↔ Shifts ↔ Cashbook ↔ Detail: same `SHIFTS` source array, same `computeGrandTotal`. ✅
- Alerts ↔ shift state: `cash_diff` event drives `has_cash_diff` → CASH status on the same shift the alert cites. ✅
- Coverage ↔ health: raw heartbeat coverage feeds both the shift card and the monitoring page from one fetch. ✅
- Chart: `liveDays` derives from mapped shifts (day = Σ its shifts) — no independent constant. ✅

## 10. Desktop/mobile + light/dark (STEP 11)

`dashboard/browser_check.py` (live Chromium): **22/22 PASS** — nav (7 items), zero console errors across all screens, chart bars proportional, focus visible, both themes persist, responsive RTL. The fixture path is what renders without a token; the live path reuses the same view-model, so layout/theme behavior is identical by construction (verified until a token exists — see §13).

## 11. Regression tests (STEP 12)

`python -m pytest -q` → **613 passed** (604 existing + 9 new mint-token tests). Zero backend regressions.

**Backend protection:** `git diff` on `metrics.py report.py events.py schema.sql schema_v7_withdrawals.sql orchestrator.py supa.py delivery.py mint_agent_token.py` → **empty; byte-identical to HEAD.** No backend business logic was modified.

## 11b. Live activation error — 42703 (diagnosed & fixed)

**Error (from the owner's first SQL Editor run):** `ERROR: 42703: column "tenant_id" does not exist` on the `dashboard_ro_select` policy for `public.tenants`.

**Root cause (schema-first, no guessing):** the tenant's own identity column is `id`, not `tenant_id`.

| Table | Identity column | Evidence |
|---|---|---|
| `tenants` | `id uuid primary key` | `schema.sql` table definition (no `tenant_id` column exists) |
| `shift_reports` … `sync_state` (9 tables) | `tenant_id` | `schema.sql` / `schema_v7_withdrawals.sql` definitions |
| Agent JWT claim | `tenant_id` (value = `tenants.id`) | `mint_agent_token.py` payload + `config.example.json` |

**Fix applied to `schema_v8_dashboard_ro.sql`:** `tenants` policy now reads `using ((auth.jwt() ->> 'tenant_id')::uuid = id)`; the `execute format` loop covers only the nine `tenant_id` tables. **Fix applied to the dashboard** (`POSentine Dashboard.dc.html`): the `tenants` fetch filters `id=eq.<tid>`; the shared scope still filters the child tables by `tenant_id`. **Verification:** `reports/_verify_v8_schema.py` (static, cross-checks every referenced table/column against the authoritative schema) → **PASS**; harness regression guards → **PASS**; 266/266 harness; 22/22 browser; 613/613 pytest.

## 12. Remaining issues

1. **`gap_explained` (orchestrator-only)** — the live dashboard shows raw coverage, not the explained/unexplained classification. Faithful to what is queryable; a future backend column would make it fully live.
2. **`other_users` / `top_items` (report-body only)** — live screens show honest empty states (Case A) for these. Persisting them is a future backend change, out of scope.
3. **Google Fonts `<link>`** (audit R11) — pre-existing visual dependency, unrelated to the data path.
4. **`reports/` not gitignored** — customer evidence remains untracked but un-ignored; flagged previously, out of this phase's scope.

## 13. Evidence matrix

| Gate | Expected | Actual | Evidence | Result |
|---|---|---|---|---|
| Real Supabase connection | working securely | **BLOCKED — owner action** | 403/42501 live probe; migration not applied | ⚠️ BLOCKED |
| No browser service key | true | true | harness SECURITY + grep | ✅ PASS |
| Tenant isolation | enforced | **by design; live proof BLOCKED** | RLS policy written, not applied | ⚠️ BLOCKED (design PASS) |
| Read-only access | true | true by construction | role = SELECT only; loader GET only | ✅ PASS (design) |
| Real shift totals | correct | **BLOCKED** (can't read shift_reports) | reconciliation proves the *upstream* totals | ⚠️ BLOCKED |
| Financial formula | exact | exact | metrics.py reconciliation 16,355/17,400/21,665 | ✅ PASS |
| Overview ↔ Shifts | equal | equal | harness on mapped domain | ✅ PASS |
| Overview ↔ Cashbook | equal | equal | harness | ✅ PASS |
| Alerts ↔ shift state | equal | equal | harness (cash_diff → CASH) | ✅ PASS |
| Telegram reports | match | match | reconciliation, all fields | ✅ PASS |
| Dark mode | working | working | browser 22/22 | ✅ PASS |
| Light mode | working | working | browser 22/22 | ✅ PASS |
| Mobile / Desktop | working | working | browser 22/22 | ✅ PASS |
| Existing tests | passing | 613/613 | pytest output | ✅ PASS |
| Backend protection | unchanged | unchanged | git diff byte-identical | ✅ PASS |
| Mapping layer | real-shaped rows map to domain | 264/264 harness | LIVE DATA LAYER section | ✅ PASS |
| Mint tool | correct + guards | 9/9 tests | pytest | ✅ PASS |

## 14. Final verdict

> 🔴 **NOT READY — BLOCKED ON TWO OWNER ACTIONS**
>
> Every implementable, locally-verifiable piece of the Phase 3 integration is **implemented and proven**:
> the architecture is evidence-determined (dedicated read-only role + RLS + minted JWT — the existing POSentine pattern), the migration and mint tool are ready, the dashboard's live data layer maps real-shaped rows to the verified domain contract (264/264 harness), real Supabase raw data through the locked `metrics.py` reproduces all three Telegram reports **number for number**, and the full regression suite passes (613/613) with locked backend files byte-identical.
>
> The live end-to-end connection cannot be exercised from this machine: applying `schema_v8_dashboard_ro.sql` requires the Supabase SQL Editor, and minting the token requires the Supabase JWT secret — both owner-held, neither present locally (proven by credential scan), and per the security contract neither should be. **The verdict is NOT READY solely because the real-data path has not been executed against live Supabase — not because of any known defect.**

**Exact unblock (two owner actions):**
```bash
# 1. In Supabase → SQL Editor, run schema_v8_dashboard_ro.sql  (idempotent)
#    (includes the authenticator-membership grant required for role switch)
# 2. Mint the read-only dashboard token:
SUPABASE_JWT_SECRET=... python mint_dashboard_token.py --tenant-id 57b61b47-a590-49fe-803c-0c174a07b7ec
#    → place the token (and supabaseUrl + anon key) in window.POSENTINE_LIVE
#      or localStorage['posentine-live-config'], then reload the dashboard.
```

After those two actions, the remaining verification steps are: run the browser check with the token configured (live badge visible, charts/numbers from real shifts), confirm tenant isolation live (two-token probe), and re-run the harness. No further code changes are expected.
