# POSentine Phase 2 — Task 02 Report

## 1. Task
- **Exact task name:** Task 2 — Implement `orchestrator.py` (the delivery decision engine), with the five approved decisions applied.
- **Exact scope authorized by me (Mahmoud):** Cloud-side only; no POS connection; no pyodbc; use the existing `metrics.py` / `events.py` / `report.py` interfaces; zoneinfo/IANA timezone handling; deterministic shift selection; `shift_date` and morning/evening support; database-backed UNIQUE-constraint idempotency (never read-then-write); respect `go_live_at` and `notify_before_golive`; preserve Phase 1 behavior; do not modify locked files; `test_golden.py` must remain exactly 31. Re-check repo state, confirm the migration in the live schema, inspect the locked interfaces. After implementation: run orchestrator tests, full suite, golden suite, verify no pyodbc and no secrets, check git diff. **Do NOT commit, do NOT push, do NOT start Task 3.**

## 2. Objective
Build the component that decides what to build and when — the most recent closed shift with no report yet, the day-1 monthly top products, and the sendable alerts — and enqueue the resulting messages to `outbox` (and record `shift_reports`, `events`, `internal_anomalies`), with the database's UNIQUE constraints as the sole idempotency authority. Nothing is sent from here; the notifier (Task 3) is the only sender.

## 3. Files Inspected
- `schema.sql` (locked, read only) — delivery table shapes and constraints.
- `metrics.py`, `events.py`, `report.py` (locked, read only) — the exact interfaces the orchestrator must call: `compute_shift`, `compare_to_last_week`, `shift_window`, `resolve_shift`, `previous_week_shift`, `top_items`, `MIN_INVOICES_FOR_STATS`, `build_shift_report`, `build_monthly_products`, `build_alert`, `note_line`, `detect_*`, `filter_sendable`, `apply_daily_cap`, `assert_no_accusation`.
- `supa.py` (read only) — `select`/`count`/`insert_ignore`/`insert`/`update` semantics.
- `rows.py` (read only) — the naive-vs-aware clock contract and `pos_ts`/`utc_ts`.
- `agent.py` (read only) — heartbeat/anomaly-note format (the orchestrator's mirroring contract), rescan/`last_seen_at` semantics, `restore_suspected`/`schema_ok` halt flags.
- `schema_v2_grants.sql`, `schema_v3_revoke_inherited.sql` — service_role/authenticated split.
- `Docs/config.json` (credential file, used only for the live-schema probe; no value was printed or stored).
- `test_supa.py`, `test_preflight.py` — existing test conventions and the ast import-closure pattern.

## 4. Files Created
- `orchestrator.py` (new, ~640 lines) — the delivery orchestrator.
- `test_orchestrator.py` (new, ~600 lines) — 32 tests.
- `reports/phase2/TASK_02_ORCHESTRATOR_REPORT.md` — this file.

## 5. Files Modified
- None. No tracked file was modified. Verified by `git diff --stat` (empty) and `git status` (only the three new untracked paths).

## 6. Files Deleted
- None.

## 7. Implementation Details

### 7.1 Shape
- `plan(now_utc, ctx, state, force_shift=None, shift_date=None) -> Plan` — **pure**: no clock, network, or randomness. Same inputs → same Plan.
- `run(client, *, tenant_id, source_id, now_utc, force_shift, shift_date) -> Plan` — thin runner: `_load_context` (tenants/recipients/alert_settings), `_load_state` (sync_state, heartbeats, shift_reports, invoices/lines/cash, users, products, outbox-sent count, internal_anomalies), `plan()`, `apply()`.
- `apply(client, ctx, plan, now_utc)` — writes **only** via conflict-ignoring inserts:
  - `events` → `insert_ignore` on `(tenant_id, source_id, type, dedup_key)`
  - `shift_reports` → `insert_ignore` on `(tenant_id, source_id, shift_date, shift_name)`
  - `outbox` → `insert_ignore` on `(tenant_id, channel, recipient, dedup_key)`
  - `internal_anomalies` → plain `insert` (no unique constraint exists; duplicates are prevented by read-side dedup on heartbeat id / open-kind).
  - Conditional `update` (status `detected`→`queued`, filter includes `status=eq.detected`) after the enqueue is attempted — the database arbitrates, never app logic. **No read-then-write idempotency anywhere.**

### 7.2 The five approved decisions, as implemented
1. **`notify_before_golive`** — read from `recipients` in `_load_context` (migration confirmed live, see §8). Gate order in `eligible_recipients`: `active` first (never bypassed), then the go-live gate, which `notify_before_golive` bypasses **and only that gate**; `alert_settings.notify` is applied to events separately by the locked `filter_sendable`.
2. **Shift dedup** — outbox `dedup_key` is exactly `shift_report:{shift_date}:{shift_name}` (e.g. `shift_report:2026-06-30:evening`). `shift_reports` PK is the arbiter; `on_conflict` decides.
3. **Daily cap** — `already_sent_today` = count of `outbox` rows with `kind=alert`, `status=sent`, `created_at >= tenant-local midnight` (tenant timezone via `zoneinfo`). Only sent rows count; pending/failed/dead never do. Cap constant `DAILY_ALERT_CAP = 3` (matches `events.py`).
4. **Monthly dedup** — `monthly:{YYYY-MM}` of the reported (previous) month; outbox UNIQUE is the arbiter. Built only on tenant-local day 1.
5. **`go_live_at`** — events' naive POS-local `occurred_at` is localized with `ZoneInfo(tenants.timezone)` **before** the comparison; `filter_sendable` (locked) is fed localized copies and the original naive events are stored. No UTC+3 anywhere; the DST tests pin both summer and winter boundaries.

### 7.3 Shift selection
Rule: **the most recent closed shift with no report row yet** (never "is it exactly 07:00 now"). A run 20 minutes or a day late still picks the right shift; a 14-day backward scan recovers after downtime, one shift per run. Window boundaries come from `tenants.shift_morning_start`/`shift_evening_start` (07:00/19:00), resolved against tenant-local wall clock. `force_shift`/`shift_date` override for manual runs (exact pair = explicit test action even if the window hasn't closed; `force_shift` alone = most recent closed occurrence; `shift_date` alone = most recently closed shift of that date — note: at 07:10 "today" has nothing closed yet, so `shift_date=today` returns None by design).

### 7.4 Shift report
Built via `metrics.compute_shift` (deleted invoices excluded), last-week comparison via `metrics.compare_to_last_week` (honours `MIN_INVOICES_FOR_STATS`; unavailable → the report's fixed wording, never a misleading number), notes = level 2/3 events in the shift + daily-cap overflow in the shift, cash line from `detect_cash_diffs` with `had_no_count` precedence (a `no_count` is never shown as a shortage). `is_partial` (the shift in progress at the first heartbeat) is **recorded but never reported**, per the schema comment. `assert_no_accusation` runs on every outgoing body.

### 7.5 Events
Detected over the last 24h from cloud data: zero_invoices, refunds, cash_diffs, **deleted_invoice (cloud-side via `last_seen_at` absence)**, no_sales. Deletion detection is gated on: fresh heartbeat (≤15 min), a rescan within 60 min, `restore_suspected = false` and `schema_ok = true` — absence is only evidence while the agent is provably alive (review-driven hardening). Event statuses: under-cap sendable → `detected` (→ `queued` after enqueue); everything else (notify off, pre-go-live, capped, level 2/3 report notes) → `suppressed` — so a notify flip or go-live never replays history. The Phase 1 handoff's mandated **heartbeat-silence (`dead_man`) alert** is implemented into `internal_anomalies`, and it now **re-arms**: a fresh heartbeat resolves the open `dead_man` (`resolved_at`), so a second outage raises a fresh one. Agent anomaly notes (ok=false heartbeats) are **mirrored** into `internal_anomalies` keyed on heartbeat id.

### 7.6 CLI
`orchestrator.py --tenant-id --source-id [--force-shift] [--shift-date] [--dry-run]`. Reads `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` from the environment only (GitHub Secrets). `--dry-run` builds and prints the full Arabic report bodies and enqueues/sends nothing. `apikey` is set to the service-role JWT (the gateway admits any valid project JWT; the role travels in `Authorization`) — the workflow's five secrets are sufficient.

## 8. Tests and Validation (all actually executed)
- **Live-schema migration confirmation:** `GET /rest/v1/recipients?select=tenant_id,notify_before_golive&limit=1` with the agent token returned **HTTP 403, code 42501 "permission denied for table recipients"** — PostgREST resolved the select list against the schema cache before the permission check, so a `PGRST204` (column missing) would have appeared instead. **The column exists in the live schema.**
- `python -m pytest test_orchestrator.py -q` → **32 passed in 0.25s**.
- `python -m pytest -q` → **371 passed in 6.22s** (was 339 at Task 1; the 32 new tests are the only addition).
- `python -m pytest -q test_golden.py` → **31 passed in 0.06s** (untouched).
- **Falsifier proven:** a corrupted copy of `orchestrator.py` with `import pyodbc` appended → the closure walker reports `{'pyodbc'}`; the real closure is `{__future__, argparse, collections, dataclasses, datetime, events, json, metrics, os, report, requests, rows, supa, time, typing, zoneinfo}` — clean.
- `python -c "import orchestrator"` → ok; `python orchestrator.py --help` → usage prints.
- Covered behaviors: DST winter and summer boundaries (both sides of 07:00), late runs, reported-shift exclusion, exact dedup keys (`shift_report:…`, `monthly:2026-07`, `alert:refund:0`), daily cap at sent=0/1/3, go_live suppression, bypass-gate-only-go-live, active never bypassed, `notify` gate, monthly day-1-only, partial shift recorded-not-sent, deletion gated on staleness **and** on `restore_suspected`, no-accusation guard (monkeypatched banned word raises), dead_man once + re-arm, heartbeat-note mirroring exactly once, double-run idempotency (exactly one `shift_reports` row and one `outbox` shift_report row; all constrained-table writes are `insert_ignore`).

## 9. Security / Guardrail Verification
- **No POS write capability introduced:** True — the module never opens a connection to the customer's SQL Server; its import closure is cloud-only (verified by the closure test + falsifier).
- **No pyodbc in Phase 2 delivery components:** True — `grep -c 'import pyodbc' orchestrator.py` = 0; the closure test fails the suite if it ever appears; `requirements-cloud.txt` (requests, pytest, tzdata) contains no pyodbc.
- **No secrets logged:** True — no logging of credentials anywhere; the CLI reads env vars; dry-run prints report bodies and dedup keys only.
- **No secrets hardcoded:** True — no tokens/keys/chat ids in source (the seeded chat id is not referenced).
- **No JWT secret changed:** True — nothing was rotated or re-minted; the live probe used the existing agent token read-only.
- **No client-side POS configuration modified:** True — no `config.json`, till, or task touched.
- **No protected/locked file modified:** True — `git diff --stat` empty; locked files read-only. `test_golden.py` is exactly **31** tests.

## 10. Git State
- Current branch: `main`
- Current commit: `7eb1796` (unchanged — nothing committed)
- Working tree: clean of tracked changes; new **untracked** files: `orchestrator.py`, `test_orchestrator.py`, `reports/` (Task 1 report + this file)
- Committed: nothing. Pushed: nothing. `main...origin/main` shows no divergence.

## 11. Problems / Risks
- **Alert backlog across the cap (cross-task, per the approved decision).** The cap counts *sent* rows only, so a notifier outage accumulates pending alert rows past 3. **Task 3's sender must apply its own cap/send-order awareness** — noted so it is not lost.
- **DST fold/gap ambiguity (documented decision).** `occurred_at.replace(tzinfo=tz)` uses Python's fold=0 default for the fall-back repeated hour and the spring-forward nonexistent hour. Standard, accepted; affects only events inside a 1-hour window twice a year.
- **`select_shift(shift_date=today)` semantics.** At 07:10, "today" has no closed shift → None (nothing of that date has closed) rather than yesterday's evening. Correct per the rule; documented for manual runs.
- **Deletion window now salid-merged**, but the merge only runs after a completed rescan (`last_rescan_at` set) — a pre-rescan run (fresh install) cannot infer deletions, by design.
- **`dead_man` and mirrored anomalies are ours, not the owner's** — they land in `internal_anomalies`; nothing surfaces them to the owner yet (Task 3's domain).
- **Not yet run against live Supabase as service_role.** The unit/fake-client suite passes, but `run()` against the real project (service-role reads/writes, PostgREST filter syntax like `and=(...)`/`or=(...)`) is unproven until the workflow (Task 4) executes a `dry_run`. The Phase-1 discipline applies: "not verified yet" is the honest status.

## 12. Decisions Required From Me
1. Approve the orchestrator as built (including the two additions beyond the letter of the prompt: `dead_man` re-arm-on-recovery and heartbeat-note mirroring — both were Phase-1 designed contracts with no writer; both are isolated and testable).
2. Confirm the level-2/3 and capped events being recorded as `suppressed` is the intended "never replay history" semantics (it matches the go-live one-transaction rule; a different status would need a schema change).
3. Nothing else — the five approved decisions were implemented exactly as given.

## 13. Final Status
**COMPLETE** — 32 new tests, 371 total, golden 31, closure proven fail-able and clean, migration verified live, no locked file touched, nothing committed or pushed. The only unverified surface is the orchestrator's real network run against Supabase as `service_role`, which belongs to the workflow task.

## 14. Next Step
**Task 3 — `notifier/telegram.py`** (claim pending outbox rows, send via the Telegram Bot API, retry/backoff with 429 `retry_after`, 403 classified distinctly, no secret logging, 4096-char handling) **including the Task-2 review note that the sender must respect the daily cap when draining a backlog**. **Not executed — awaiting your explicit approval.**
