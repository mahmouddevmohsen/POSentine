# POSentine Phase 2 — Task 01 Report

## 1. Task
- **Exact task name:** Task 1 — Schema inspection and Phase 2 contract cross-check.
- **Exact scope authorized by me (Mahmoud):** Read `schema.sql`; inspect the actual schema (tables, columns, types, relationships, indexes, constraints, fields required by Phase 2); cross-check against Phase 2 requirements; identify mismatches, ambiguities, and missing database constraints. **No source file modified. No implementation code created. No commit. No push. No continuation to Task 2.** A mandatory report must be written to `reports/phase2/TASK_01_SCHEMA_REPORT.md`.

## 2. Objective
Establish, from the actual committed schema (not memory), the exact contract the Phase 2 delivery components (`orchestrator.py`, `notifier/telegram.py`, GitHub Actions workflow) must code against: real table/column names and types for the delivery tables, the shift-report row identity, the outbox status lifecycle, the presence/absence of `recipients.notify_before_golive`, and every gap or ambiguity that could silently break the build or the delivery. Nothing is to be assumed; everything below was read from the repository.

## 3. Files Inspected
- `schema.sql` (398 lines) — the authoritative committed schema (LOCKED file, read only).
- `schema_v2_grants.sql` (67 lines) — table privilege model for `service_role` / `authenticated`.
- `schema_v3_revoke_inherited.sql` (99 lines) — inherited-privilege cleanup (v3).
- `supa.py` (354 lines) — PostgREST client used for all reads/writes (read only; not locked but untouched).
- `metrics.py` (338 lines) — LOCKED; delivery-side interface the orchestrator must call.
- `report.py` (290 lines) — LOCKED; report-text builders.
- `events.py` (384 lines) — LOCKED; event detection, `filter_sendable`, `apply_daily_cap`, `assert_no_accusation`.
- `Docs/CLAUDE_CODE_PHASE2_PROMPT.md` — the Phase 2 requirements being cross-checked against.
- `Docs/PHASE_2_DELIVERY_PLAN.md` — the agreed delivery plan (gates, recipient seeding, go-live transaction).
- Repository state verified via `git status -sb`, `git log --oneline -1`, `git diff --stat`, `grep -rn notify_before_golive`, `ls reports`.

## 4. Files Created
- `reports/phase2/TASK_01_SCHEMA_REPORT.md` — this file (the only file created).

## 5. Files Modified
- None. No source, schema, doc, or config file was modified.

## 6. Files Deleted
- None.

## 7. Implementation Details

This task produced no code. It produced the following verified contract, read directly from `schema.sql`.

### 7.1 Schema inventory (15 tables, all RLS-enabled)
`tenants`, `sources`, `recipients`, `alert_settings`, `sync_state`, `heartbeats`, `pos_users`, `pos_products`, `invoices`, `invoice_lines`, `cash_counts`, `events`, `outbox`, `shift_reports`, `internal_anomalies`.

### 7.2 Actual columns of the seven Phase 2 tables (verbatim from `schema.sql`)

**`tenants`**
| Column | Type | Notes |
|---|---|---|
| id | uuid PK default gen_random_uuid() | |
| slug | text UNIQUE NOT NULL | `sobh_onthefast` seeded |
| name | text NOT NULL | |
| timezone | text NOT NULL default 'Africa/Cairo' | orchestrator must resolve via `zoneinfo` — NOT a hardcoded UTC+3 |
| shift_morning_start | time NOT NULL default '07:00' | local wall-clock, DST-agnostic by design |
| shift_evening_start | time NOT NULL default '19:00' | |
| currency | text NOT NULL default 'ج' | |
| go_live_at | timestamptz NULL | events older than it are suppressed |
| created_at | timestamptz NOT NULL default now() | |

**`recipients`**
| Column | Type | Notes |
|---|---|---|
| id | bigserial PK | |
| tenant_id | uuid NOT NULL FK→tenants ON DELETE CASCADE | |
| channel | text NOT NULL CHECK in ('telegram','whatsapp') | |
| address | text NOT NULL | chat_id or phone |
| label | text NULL | |
| active | boolean NOT NULL default true | |
| — | UNIQUE (tenant_id, channel, address) | |

⚠️ **`notify_before_golive` is ABSENT.** Confirmed by grep: it appears only in `Docs/PHASE_2_DELIVERY_PLAN.md` and `Docs/CLAUDE_CODE_PHASE2_PROMPT.md`, never in any `.sql`/`.py`.

**`alert_settings`**
| Column | Type | Notes |
|---|---|---|
| tenant_id | uuid NOT NULL FK→tenants CASCADE | PK part |
| alert_type | text NOT NULL | PK part |
| detect | boolean NOT NULL default true | detection always on |
| notify | boolean NOT NULL default false | send gate |
| threshold | numeric(12,2) NULL | |
| — | PRIMARY KEY (tenant_id, alert_type) | |

Seeded alert types: `zero_invoice`, `refund`, `cash_diff`, `deleted_invoice`, `no_sales`, `db_size` (all `detect=true`, `notify=false` until go-live).

**`events`**
| Column | Type | Notes |
|---|---|---|
| id | bigserial PK | |
| tenant_id | uuid NOT NULL FK→tenants CASCADE | |
| source_id | uuid NOT NULL FK→sources CASCADE | |
| type | text NOT NULL | |
| dedup_key | text NOT NULL | |
| level | smallint NOT NULL CHECK (1..3) | 1=immediate, 2=in report, 3=info |
| occurred_at | timestamp NOT NULL | ⚠️ naive — POS local time |
| payload | jsonb NOT NULL default '{}' | report.py reads keys: expected/actual/diff, salid, receipt_num, amount, items, list_value, gap_minutes … |
| status | text NOT NULL default 'detected' CHECK in ('detected','suppressed','queued','reported') | |
| created_at | timestamptz NOT NULL default now() | |
| — | UNIQUE (tenant_id, source_id, type, dedup_key) | dedup via `insert_ignore` |
| — | INDEX ix_events_open (tenant_id, source_id, occurred_at desc) WHERE status IN ('detected','queued') | |

**`outbox`**
| Column | Type | Notes |
|---|---|---|
| id | bigserial PK | |
| tenant_id | uuid NOT NULL FK→tenants CASCADE | |
| channel | text NOT NULL CHECK in ('telegram','whatsapp') | |
| recipient | text NOT NULL | no FK to recipients by design |
| kind | text NOT NULL | documented values: shift_report, monthly_products, alert (no CHECK) |
| body | text NOT NULL | full message; 4096-cap handling belongs to the notifier |
| event_id | bigint NULL FK→events ON DELETE SET NULL | links alert rows to events |
| dedup_key | text NOT NULL | |
| status | text NOT NULL default 'pending' CHECK in ('pending','sending','sent','failed','dead') | |
| attempts | smallint NOT NULL default 0 | |
| last_error | text NULL | |
| created_at | timestamptz NOT NULL default now() | |
| sent_at | timestamptz NULL | set after successful send |
| — | UNIQUE (tenant_id, channel, recipient, dedup_key) | second idempotency arbiter |
| — | INDEX ix_outbox_queue (status, created_at) WHERE status IN ('pending','failed') | claim queue |

**`shift_reports`**
| Column | Type | Notes |
|---|---|---|
| tenant_id | uuid NOT NULL | PK part |
| source_id | uuid NOT NULL | PK part |
| shift_date | date NOT NULL | PK part |
| shift_name | text NOT NULL CHECK in ('morning','evening') | PK part; matches metrics constants |
| window_start | timestamp NOT NULL | naive, POS local |
| window_end | timestamp NOT NULL | naive, POS local |
| primary_user | text NULL | |
| sales / returns / delivery / collections / grand_total | numeric(12,2) NULL | grand_total comment pins: sales + collections − returns − delivery (verified 19,205) |
| n_cash / n_return / n_external | integer NULL | |
| is_partial | boolean NOT NULL default false | current shift at install: recorded, never reported |
| generated_at | timestamptz NOT NULL default now() | |
| — | PRIMARY KEY (tenant_id, source_id, shift_date, shift_name) | **the natural key — DB-level idempotency arbiter** |

**`internal_anomalies`**
| Column | Type | Notes |
|---|---|---|
| id | bigserial PK | |
| tenant_id | uuid NULL | |
| source_id | uuid NULL | |
| kind | text NOT NULL | documented: unknown_salT, schema_drift, restore_suspected, clock_drift, date_change_log, dead_man, telegram_403 |
| detail | jsonb NOT NULL default '{}' | |
| created_at | timestamptz NOT NULL default now() | |
| resolved_at | timestamptz NULL | |
| — | no unique constraint | append-only log; duplicates acceptable |

### 7.3 The three Phase 2 contract questions, answered from the schema
1. **What uniquely identifies a shift report row?** The composite primary key `(tenant_id, source_id, shift_date, shift_name)`. This is the DB-level arbiter the prompt demands: the orchestrator writes via `INSERT ... ON CONFLICT DO NOTHING` (PostgREST `Prefer: resolution=ignore-duplicates` with `on_conflict=tenant_id,source_id,shift_date,shift_name`) and lets the conflict decide. For the *outbox* side, idempotency rests on `UNIQUE (tenant_id, channel, recipient, dedup_key)` — the orchestrator must derive a deterministic `dedup_key` per shift report (proposal: `shift_report:{shift_date}:{shift_name}`), constant across runs.
2. **What is the outbox status lifecycle?** `pending → sending → sent` on success; `pending → sending → failed → dead` on failure, with `attempts` (default 0) as the retry counter and a documented cap of 3 retries; `sent_at` records success time, `last_error` records the final failure reason. The CHECK constraint enforces exactly this set. `ix_outbox_queue` (partial on pending/failed) is the claim surface.
3. **Does `recipients` have `notify_before_golive`?** **No.** The migration below is required before the delivery plan's recipient-seeding INSERT can run. Per the prompt it is provided here and **NOT executed**.

```sql
-- Migration: recipients.notify_before_golive (idempotent; Supabase SQL Editor)
alter table recipients
  add column if not exists notify_before_golive boolean not null default false;

comment on column recipients.notify_before_golive is
  'مستقبِل تجريبي: يستقبل حتى قبل ضبط tenants.go_live_at. للمطوّر فقط.';
```

### 7.4 Grants / RLS model for delivery (from v2/v3, verified live in Phase 1)
- Delivery components run as **`service_role`**: `GRANT ALL PRIVILEGES ON ALL TABLES/SEQUENCES IN SCHEMA public` — bypasses RLS, full access to `events`, `outbox`, `shift_reports`, `internal_anomalies`, `alert_settings`, `recipients`, `tenants`, `sources`. This is the intended and only channel for delivery.
- The installed agent token (`authenticated` role) is **structurally locked out** of all eight delivery tables — verified live with `42501` refusals during Phase 1 pre-visit checks. `schema_v2_grants.sql` / `schema_v3_revoke_inherited.sql` are the committed definitions of this split.
- Consequence: the sender can read settings and write deliveries, and cannot touch the customer's POS by construction (no SQL Server path exists anywhere in this phase).

### 7.5 Cross-check findings — mismatches, ambiguities, missing constraints
1. **MISSING — `recipients.notify_before_golive`.** Absent from the schema; required by the delivery plan and by Task 3's gate order (`active` → `go_live_at` → `alert_settings.notify`, with `notify_before_golive` bypassing **only** the go-live gate). Migration provided in §7.3, not run. **This blocks the plan's recipient-seeding INSERT and Task 3's bypass test until applied.**
2. **AMBIGUITY — naive vs aware timestamp comparison at the go-live gate.** `events.occurred_at`, `invoices.sold_at`, `shift_reports.window_start/end` are naive `timestamp` (POS local, by the schema's governing rule); `tenants.go_live_at`, `outbox.created_at/sent_at`, `shift_reports.generated_at` are `timestamptz`. `events.filter_sendable` compares `e.occurred_at < go_live_at`. In Python, comparing a naive datetime against an aware one raises `TypeError`. The orchestrator must attach the tenant zone to `occurred_at` (treating it as tenant-local wall clock) before comparing. This is implementation-level but must be decided deliberately — a wrong choice either crashes or silently mis-suppresses.
3. **NO DEDICATED TABLE FOR MONTHLY REPORTS.** `shift_reports` covers shifts only; the monthly top-products row (`outbox.kind = 'monthly_products'`) has no table of its own. Its only idempotency arbiter is `outbox`'s `UNIQUE (tenant_id, channel, recipient, dedup_key)` — the orchestrator must therefore use a stable monthly `dedup_key` (proposal: `monthly:{YYYY-MM}`) and write it with conflict-ignore. Verified the prompt's "produced exactly once" requirement is satisfiable, but **only through outbox dedup**, not a dedicated PK.
4. **AMBIGUITY — daily-cap day boundary.** `events.apply_daily_cap(cap=3)` needs `already_sent_today`, which the orchestrator must compute from `outbox` rows. Nothing in the schema records a "commercial day" on outbox (`created_at` is timestamptz). The definition of "today" (tenant-local date via `zoneinfo`?) and which statuses count toward the cap (`sent` only, or `sent`+`failed`?) is unspecified and must be pinned before Task 2/3.
5. **NOTE — `outbox.kind` has no CHECK constraint.** Free text; the comment documents `shift_report | monthly_products | alert`. Not blocking; optionally enforceable.
6. **NOTE — `outbox.recipient` has no FK to `recipients`.** Deliberate (recipients may be deleted without losing send history). The notifier must read `recipients` first and copy `address` into `outbox.recipient`.
7. **NOTE — `internal_anomalies` is written by nobody yet.** The Phase 1 handoff mandates alerting on **heartbeat silence** (`dead_man`) and the notifier spec requires recording `telegram_403` distinctly. The table's `kind` vocabulary already includes both — the schema is ready; the writers are Task 2/3 work.
8. **NOTE — DST is structurally safe at the schema level.** Shift boundaries are stored as `time` (07:00/19:00) and invoices as naive POS-local `timestamp`, so boundary arithmetic is DST-agnostic by design. The **only** DST-sensitive step is resolving "what time is it for this tenant now", which must go through `zoneinfo.ZoneInfo(tenants.timezone)` (`Africa/Cairo`, summer UTC+3 / winter UTC+2). The prompt's prohibition on hardcoding UTC+3 is consistent with the schema.
9. **NOTE — indexes are sufficient for delivery.** `ix_outbox_queue` covers claiming; `shift_reports` PK b-tree covers "latest closed shift" ordering; `ix_events_open` covers open-event detection. No new index is required for the documented access patterns.
10. **NOTE — `events` payload keys must match `report.py`.** `report.cash_line` and `build_alert` read `payload['expected']`, `payload['actual']`, `payload['diff']`, `payload['salid']`, `payload['receipt_num']`, `payload['amount']`, `payload['list_value']`, `payload['items']`, `payload['gap_minutes']`. The orchestrator's event construction must produce exactly these keys (they already match `events.py` detector outputs).

## 8. Tests and Validation
No code was written, so no new tests were authored or executed. To report a fresh, actually-executed baseline, the untouched repository suite was run:
- `python -m pytest -q` → **339 passed in 6.68s** (0 failures).
- `python -m pytest -q test_golden.py` → **31 passed in 0.06s** (golden suite unchanged).
- `git diff --stat` → empty (no working-tree modifications).
- `grep -rn 'notify_before_golive'` → only in `Docs/` markdown, never in schema/code (the basis for §7.2/§7.5 finding 1).

## 9. Security / Guardrail Verification
- **No POS write capability introduced:** Not applicable — no code written; delivery remains cloud-only by design. No connection to the customer's SQL Server exists in any Phase 2 component (none exist yet).
- **No pyodbc introduced into Phase 2 delivery components:** Not applicable — no Phase 2 components were created. The no-pyodbc closure test remains a Task 5 deliverable.
- **No secrets logged:** This report contains no token, key, password, JWT, or credential values. Secret names are referenced by name only (`TELEGRAM_BOT_TOKEN`, `SUPABASE_SERVICE_ROLE_KEY`, etc. are not mentioned here beyond policy wording; the seeded recipient chat id is not reproduced).
- **No secrets hardcoded:** No code written; nothing to hardcode.
- **No JWT secret changed:** Nothing was changed anywhere.
- **No client-side POS configuration modified:** `config.json` / till files untouched.
- **No protected/locked file modified:** `schema.sql`, `metrics.py`, `report.py`, `events.py`, `adapter_hdsoft.py`, `test_golden.py` were read only. Verified by `git diff --stat` (empty).
- **Migration NOT executed:** The `notify_before_golive` SQL in §7.3 was deliberately **not** run and **not** saved to a file; it is presented for your approval and execution.

## 10. Git State
- Current branch: `main`
- Current commit: `7eb1796` ("chore: session log through the release gate")
- Working tree status: clean (no tracked modifications) — the only new path is the untracked `reports/phase2/TASK_01_SCHEMA_REPORT.md`
- Committed: nothing by this task
- Pushed: nothing by this task (branch shows no ahead/behind vs `origin/main`)

## 11. Problems / Risks
- `notify_before_golive` missing from schema — blocks Task 3's bypass logic and the plan's recipient INSERT until the migration is applied (see §12.1).
- Naive-vs-aware datetime comparison at the go-live gate is the likeliest silent-crash trap in Task 2/3; it must be resolved deliberately (see §7.5.2).
- Daily-cap counting semantics are unspecified (which statuses, whose "today") — pinning it wrong changes which alerts the owner receives (see §7.5.4).
- Monthly report idempotency depends entirely on a stable outbox `dedup_key`; no table backs it (see §7.5.3).
- `internal_anomalies` has no writers yet; `dead_man` (heartbeat-silence) and `telegram_403` recording are pending orchestrator/notifier duties carried over from Phase 1.
- No new risk introduced by this task: no code, config, or data was touched.

## 12. Decisions Required From Me
1. **Approve and apply the `recipients.notify_before_golive` migration** (§7.3). State whether you will run it yourself in the Supabase SQL Editor, or whether you want me to save it as a file (e.g. `schema_v4_recipients_notify_before_golive.sql`, following the existing v2/v3 naming) for you to review first — I have not created it to respect "no implementation code".
2. **Confirm the shift-report outbox `dedup_key` scheme** (`shift_report:{shift_date}:{shift_name}`) or specify another — it is the load-bearing idempotency key.
3. **Confirm the daily-cap counting rule** for Task 2/3: tenant-local "today", counting `sent` alert rows (proposal), or another definition.
4. **Confirm the monthly `dedup_key` scheme** (`monthly:{YYYY-MM}`) or another.

## 13. Final Status
**COMPLETE WITH WARNINGS** — the inspection and cross-check are complete and evidence-backed; the warnings are the pre-existing schema gaps in §7.5 (§12 items) that must be resolved before Task 3/4 work, none of which this task was authorized to fix.

## 14. Next Step
Task 2 — `orchestrator.py` (pure function: DST-aware shift resolution, shift-report idempotency via `shift_reports` PK, event filtering + daily cap, outbox enqueue). **Not executed — awaiting your explicit approval, and (if you so choose) the application of the §7.3 migration first.**
