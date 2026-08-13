# POSentine — PHASE WITHDRAWALS IMPLEMENTATION REPORT

**Date:** 2026-08-13 · **Status:** GREEN
**Full test suite:** 599 passed · **Golden suite:** 31 passed · **Read-only/security suite:** 69 passed

---

## 1. Executive summary

مسحوبات (withdrawals) is now implemented end-to-end with the user-confirmed
source and semantics:

- **Source:** `dbo.Personal.peramount` — read whole-table every cycle through the
  existing read-only adapter path (SELECT + `WITH (NOLOCK)`, `readonly=True`,
  sqlguard-guarded).
- **Formula:** `الإجمالي = مبيعات + مقبوضات − مرتجع − دليفري − مسحوبات`.
- **Attribution:** by shift window `[07:00, 19:00)` / `[19:00, 07:00)` — the same
  boundaries as invoices. `peruser` (UID 2 = حمص morning · UID 1 = محمود evening)
  is carried as metadata only; it never splits or re-adds the total.
- **Deletion:** Personal rows deleted on the POS are mirrored to the cloud by the
  agent (absence is the signal, same philosophy as the invoice rescan), so a
  deleted withdrawal stops being subtracted from future daybooks.
- **pertype:** no evidence-backed exclusion exists (pertype=0 → 116 rows,
  pertype=1 → 1 row; owner confirmed `SUM(Personal.peramount)` with no filter),
  so all rows are summed. Pinned by tests.
- **Cloud schema:** additive `schema_v7_withdrawals.sql` — new `withdrawals`
  table (PK `tenant_id,source_id,perid` = the upsert conflict key), RLS with the
  same `agent_rw` tenant claim, `shift_reports.withdrawals` column, and the
  `grand_total` CHECK constraint updated to include the deduction.

The live-evidence values are pinned as DB-derived expectations (never claimed as
screen observations): 8/11 morning 9,910 → 18,785 − 9,910 = **8,875**.

**DO NOT DEPLOY YET.** This report's final section lists what remains before any
release.

---

## 2. Initial git state

| Item | Value |
|---|---|
| HEAD | `5407bfd` chore(release): pin EXPECTED_SHA to the 8a8b545 artifact |
| Branch | `main`, 0 ahead / 0 behind origin |
| Pre-existing modified (report-format work) | orchestrator.py, report.py, test_orchestrator.py, test_report.py (+ VERIFY.md / READONLY_GUARANTEE.md doc fixes from the prior forensic task) |
| Pre-existing untracked | reports/ forensic files |

No partial withdrawal implementation existed before this task — `report.py` had a
`withdrawals` parameter (display-only, from the presentation task) but no data
path fed it. This task wired the real source through.

---

## 3. Evidence used

1. `reports/FINAL_WITHDRAWAL_50_PERFORMANCE_REPORT.txt` — the client-machine
   source investigation (Personal identified as the مسحوبات source).
2. `reports/FINAL_MASTER_FORENSIC_REPORT.txt` — local forensic baseline.
3. User-confirmed business semantics (authoritative):
   - `Personal.peramount` = مسحوبات
   - مسحوبات is **subtracted** from الإجمالي
   - UID 2 = حمص = morning · UID 1 = محمود = evening
4. The existing source, tests, and VERIFY.md conventions.

---

## 4. Confirmed business rule

```
withdrawals  = SUM(Personal.peramount) over the shift window
grand_total  = sales + collections − returns − delivery − withdrawals
```

- Shift windows are the existing half-open ones: morning `[07:00, 19:00)`,
  evening `[19:00, 07:00)` (perdate 06:59:59 → previous evening; 07:00:00 →
  morning).
- Withdrawals never enter sales/collections; the deduction happens once.
- Mahmoud/حمص amounts remain a display breakdown of sales — never a separate
  addition (no double-counting, inherited and re-pinned).

---

## 5. Exact implementation changes

### 5.1 `adapter_hdsoft.py` — the read path (SELECT-only)
- `Withdrawal` dataclass (perid, amount, perdate, user_uid, branch_id,
  per_type, note).
- `PullResult.withdrawals` field.
- `_row_to_withdrawal()` row parser.
- Personal columns added to `_REQUIRED` (schema-drift guard on startup).
- New SELECT in `pull()`:
  ```sql
  SELECT Perid, peramount, perdate, peruser, PerBRID, pertype, pernote
  FROM dbo.Personal WITH (NOLOCK)
  WHERE perdate < DATEADD(second, -?, GETDATE())
  ORDER BY Perid
  ```
  - Whole-table read every cycle (no perid watermark — Personal rows can be
    deleted and Perid has gaps, so a monotonic watermark is unsafe; this is the
    same conceptual strategy as the invoice rescan).
  - Same 30-second dirty-read guard as invoices, passed as a parameter.

### 5.2 `rows.py` — payload mapping
- `withdrawal_payload(w, tenant_id, source_id)`: Perid→perid, peramount→peramount,
  perdate via `pos_ts` (POS-local wall time, refuses aware datetimes),
  peruser/perbr_id/pertype/pernote as evidence metadata.

### 5.3 `agent.py` — upload + deletion mirror
- `CycleResult.withdrawals`; `build_cycle()` maps pulled rows to payloads.
- `upload()` upserts `withdrawals` with `on_conflict="tenant_id,source_id,perid"`
  (after cash_counts; before/after failure logging covers it).
- `_mirror_withdrawal_deletions()`: compares cloud perids against the latest
  snapshot and deletes cloud rows absent from the POS (idempotent; documented
  self-healing edge for a row edited inside the 30-second guard window).
- Dry-run and confirm output include the withdrawals count.

### 5.4 `metrics.py` — the formula
- `ShiftMetrics.withdrawals`; `compute_shift(..., withdrawals=0.0)` sets it and:
  `grand_total = sales + collections − returns − delivery − withdrawals`
  (pure — no `datetime.now()`, window passed explicitly).

### 5.5 `orchestrator.py` — cloud-side wiring
- `Withdrawal` dataclass; `DBState.withdrawals`.
- `_sum_withdrawals(state.withdrawals, start, end)` — pure window filter
  (inclusive start / exclusive end), rounding to 2 dp.
- `_load_state()` fetches withdrawals for the same 8-day window as invoices;
  `_row_withdrawal()` parses them.
- `_build_shift_report()` sums withdrawals for the target shift **and** the
  previous-week shift (fair comparison on both sides), passes the value into
  `compute_shift`, `shift_row.withdrawals`, and the report body.
- Dataclass field ordering fixed (withdrawals inserted before the non-default
  `users` field).

### 5.6 `report.py` — the Telegram line
- `build_shift_report(..., withdrawals=...)`: when provided, renders
  `مسحوبات         −{money} ج` between دليفري and مرتجع مبيعات, and extends the
  formula line to `… − دليفري − مسحوبات`. The value is display-only — the total
  is already computed in metrics. (The orchestrator always passes the real value,
  including 0, so a zero withdrawal renders `مسحوبات −0 ج`.)

### 5.7 `schema_v7_withdrawals.sql` — additive cloud migration
- `create table if not exists withdrawals` with
  `primary key (tenant_id, source_id, perid)` (matches the agent's upsert
  conflict key), index on (tenant_id, source_id, perdate), RLS enabled, and the
  `agent_rw` policy carrying the same tenant JWT claim as the other ingestion
  tables.
- `shift_reports`: `add column if not exists withdrawals numeric(12,2) not null
  default 0`; the v6 `grand_total` CHECK is dropped (guarded by pg_constraint
  existence) and re-added as
  `check (grand_total = sales + collections - returns - delivery - withdrawals)`.
  Old rows have withdrawals=0, so `0 = 0 − 0` keeps them valid.
- Documented manual verification SQL (accept + reject cases) and a rollback.

---

## 6. Personal query

See §5.1. It is a parameterised SELECT + NOLOCK, protected by sqlguard and the
adapter's `readonly=True` connection. The write-verb scan shows **0** write verbs
in the adapter.

## 7. Shift-window behavior

Same `resolve_shift`/`shift_window` as invoices; `_sum_withdrawals` filters by
`[start, end)`. Boundary tests pin 06:59:59 → prev evening, 07:00:00 → morning,
18:59:59 → morning, 19:00:00 → evening.

## 8. pertype decision

All `pertype` values are summed. The forensic report found pertype=0 (116 rows)
and pertype=1 (1 row); no application logic provides evidence that pertype=1 is
not a withdrawal, and the owner confirmed `SUM(Personal.peramount)` without a
filter. Inventing an exclusion would be fabrication, so none was added. `pertype`
rides as metadata on each row. Pinned by tests at both the adapter and
orchestrator levels.

## 9. Cashier attribution

Attribution is the **shift window**, never the user. A morning withdrawal by
محمود (uid=1) still counts in the morning shift. `peruser` is preserved in the
payload and row for evidence; it does not partition the sum, and no names are
hard-coded into any calculation.

## 10. Deletion / rescan behavior

- The adapter reads all of dbo.Personal every cycle (no perid watermark), so a
  deletion or correction is observed within one cycle — same strategy as the
  invoice rescan.
- The agent mirrors deletions to the cloud (`_mirror_withdrawal_deletions`):
  cloud perid missing from the latest snapshot → deleted (via `perid in.(...)`).
  Idempotent: identical snapshots produce identical results, no events/alerts —
  it is synchronization, not detection.
- Documented edge: the 30-second dirty-read guard excludes rows newer than 30 s
  from the snapshot; a row created/edited within that window is momentarily
  removed from the cloud and re-uploaded next cycle (self-healing in one cycle).
  HD Soft's perdate is a creation timestamp, so the practical risk is nil.

## 11. Idempotency

- Upsert conflict key `(tenant_id, source_id, perid)` — the database's PK is the
  arbiter (same conflict-ignoring pattern as invoices/cash_counts).
- Mirror-delete is a pure function of the snapshot: same snapshot → same outcome.
- `_sum_withdrawals` is deterministic; no cloud events or alerts are created.

## 12. Telegram / report changes

- New line `مسحوبات −{value} ج` in the daybook block (between دليفري and
  مرتجع مبيعات, matching the approved order).
- Formula line extended with `− مسحوبات`.
- `shift_reports.withdrawals` column persisted.
- The report remains inside the existing Meta/Telegram architecture (plain text,
  Arabic preserved) and passes `assert_no_accusation` before any send.

## 13. Tests added

### New file `test_withdrawals.py` (37 tests)
Covers: formula with 0/1/multiple withdrawals; boundary cases (06:59:59, 07:00:00,
18:59:59, 19:00:00); outside-window exclusion; two-cashier attribution by window;
metadata preservation; adapter whole-table read + row parsing; SELECT-only +
NOLOCK + dirty-read guard parameter; empty table; large (1,000-row) deterministic
sum; schema-drift guard for Personal columns; payload mapping incl. pos_ts
refusing aware datetimes; PerBRID round-trip; agent upload conflict key; mirror
delete (stale only, none when matching, idempotent, multi-stale, self-healing
edge); pertype inclusion; live-evidence golden values (Perid 3, per-shift sums,
18,785 → 8,875); report rendering of the مسحوبات line and formula; no-leak-into-
sales checks; zero-withdrawal line.

### Updated test files
- `test_orchestrator.py` (70 tests): `state()`/`wdl()` builders; shift_row carries
  withdrawals and subtracts them; outside-window ignored; boundary match; peruser
  not splitting; empty withdrawals keep the verified formula; previous-week
  comparison on both sides (≥ MIN_INVOICES_FOR_STATS); pertype summed unfiltered.
- `test_report.py` (46 tests): zero/non-zero withdrawals line + formula.
- `test_schema_migrations.py` (10 tests): v7 table + PK + RLS + agent_rw +
  CHECK drop/re-add with the withdrawals term (and a guard that `+ withdrawals`
  never appears).
- `test_agent.py` / `test_readonly.py`: fakes updated for the new table
  (upsert/delete on the audit-probe cloud; confirm counts include withdrawals).

## 14. Full test results

| Suite | Result |
|---|---|
| Full suite (baseline 550 before this task) | **599 passed** in ~33 s |
| test_golden.py | **31 passed** (untouched, exact contract preserved) |
| test_readonly.py + test_security_guards.py | **69 passed** |
| test_withdrawals.py | **37 passed** |
| Failures / skipped | 0 / 0 |

All runs non-mutating: `PYTHONDONTWRITEBYTECODE=1 python -m pytest -p
no:cacheprovider`; git porcelain count unchanged by running tests.

## 15. Read-only safety verification

- Write-verb classification (production modules):
  adapter_hdsoft **0** · rows **0** · metrics **0** · report **0** · events **0** ·
  notifier/telegram **0** · delivery **0** · supa **1** (the REST client's own
  write methods) · agent **1** (a comment mention) · orchestrator **2**
  (comments describing the constraint design) · sqlguard **13** / readonly_probe
  **24** (deny-lists/probe content by design).
- The only runtime cloud writes remain the designed agent sync path
  (upserts/updates/inserts to the monitoring tables).
- `test_readonly.py`'s mechanical guards pass: no POS-facing module carries a
  write statement; the adapter sends only SELECT+NOLOCK; sqlguard blocks write
  verbs on a guarded connection.
- No INSERT/UPDATE/DELETE/ALTER/CREATE path was introduced against the customer
  POS. Supabase writes are limited to the intended monitoring tables.

## 16. Performance impact

- dbo.Personal is tiny (~117 rows observed) and is read whole with a range
  filter on perdate + NOLOCK. No new index is created on the customer machine
  (the DBA-side recommendations from the forensic report — Sales.saldate /
  SalesDe.saleid indexes — remain recommendations only).
- Cloud: the withdrawals fetch is bounded to the same 8-day window as invoices;
  the mirror-delete select reads only perid columns. No unbounded queries.
- No caching or additional complexity was added.

## 17. Remaining uncertainties

1. **Screen cross-check:** the per-shift sums (9,910 / 6,260 / 245 / 3,145 / 0)
   come from the forensic report and are pinned as DB-derived expectations. A
   direct HD Soft يومية الخزينة screen comparison for a live shift is still the
   strongest confirmation and should happen at the next customer visit.
2. **pertype=1 semantics:** no exclusion was implemented because none is
   evidence-backed. If HD Soft's daybook ever shows a sum that excludes pertype=1,
   that evidence should be brought back before changing the rule.
3. **Schema application:** `schema_v7_withdrawals.sql` must be run in Supabase
   (SQL Editor) — it is not applied by this task, and nothing is deployed.

## 18. Exact changed files

**Modified (tracked):**
- `adapter_hdsoft.py` · `agent.py` · `rows.py` · `metrics.py` · `orchestrator.py` ·
  `report.py` (this task's production changes)
- `test_withdrawals.py` (**new**), `test_orchestrator.py` · `test_report.py` ·
  `test_agent.py` · `test_readonly.py` · `test_schema_migrations.py` (tests/fakes)

**Created:**
- `schema_v7_withdrawals.sql` · `test_withdrawals.py` ·
  `reports/PHASE_WITHDRAWALS_IMPLEMENTATION_REPORT.md`

**Not touched:** schema.sql, schema_v2–v6, `.github/`, `install/`,
`UPDATE_POSENTINE.bat`, `make_ship.py`, `delivery.py`, `notifier/`,
`config.example.json`, `requirements*`, EXPECTED_SHA, MANIFEST, release artifacts,
any customer-machine file.

(`VERIFY.md` / `READONLY_GUARANTEE.md` carry the doc-path fix from the prior
forensic task — pre-existing modifications, not part of this task.)

## 19. Git final state

- HEAD `5407bfd` unchanged; nothing committed, nothing pushed.
- Porcelain: 24 lines (14 tracked modified + 10 untracked: 8 pre-existing
  reports, `schema_v7_withdrawals.sql`, `test_withdrawals.py`).
- No secrets introduced (tracked py/sql scan clean); no customer DB files copied
  into the repository; no generated junk included.

## 20. Explicit statement

**"Customer POS database was not modified."**

No SELECT path writes; no INSERT/UPDATE/DELETE/ALTER/CREATE was introduced
against the POS; nothing was deployed to the customer machine; Supabase
production schema is unchanged until `schema_v7_withdrawals.sql` is deliberately
applied.

---

## 21. What remains before deployment

1. Review this report and the diff.
2. Apply `schema_v7_withdrawals.sql` in Supabase when ready (idempotent; the
   rollback is documented in the file).
3. Build the next release ZIP (bump/verify EXPECTED_SHA + MANIFEST) and ship via
   the existing updater path — the agent on the till will then start uploading
   withdrawals.
4. At the next customer visit, cross-check one live shift against the HD Soft
   يومية الخزينة screen (مسحوبات line).
5. Decide whether the already-approved report-format changes ("الوردية مكتملة",
   employee line) should be committed alongside — they are currently uncommitted
   working-tree changes from the earlier presentation task.

**GREEN** — Personal is implemented as the confirmed withdrawal source;
withdrawal is subtracted from grand_total; reports show the مسحوبات line; shift
boundaries are correct; the read-only guarantee remains intact; the regression
suite (599) and golden suite (31) pass; no unexplained behavior was introduced.
