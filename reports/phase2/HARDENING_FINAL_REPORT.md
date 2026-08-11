# POSentine — Hardening Phase: Final Report (H1–H5 + GO BEYOND review)

**Date:** 2026-08-11 (fourth same-day session)
**Status:** H1–H5 implemented, tested, reviewed. Two additive migrations created but **NOT applied** (owner action). Nothing pushed (no GitHub write permission).
**Evidence tags used throughout:** `VERIFIED` · `INFERRED` · `NOT VERIFIED` · `SAFE BY DESIGN` · `ACCEPTED LIMITATION` · `OWNER ACTION REQUIRED` · `OUT OF SCOPE`

---

## 1. Executive summary

The hardening plan from `reports/phase2/HARDENING_PHASE_HANDOFF.md` (commit `08ed0b6`) was implemented in full, in the plan's prescribed order (H1 → H2 → H3 → H4 → H5). Every change is additive, none touches the POS, none rotates credentials, none weakens an existing gate. The full suite grew from **433 to 467 passed** (+34 regression tests), `test_golden.py` stayed at exactly **31**, and two independent review passes found **no blocking issues**.

The one deliberate deviation from the plan: H3 fetches heartbeats bounded to the 15-day lookback window on every orchestrator run instead of the plan's suggested "lazily-fetched helper scoped to the target shift only". The reason is documented in code and §6 below (pure `plan()` contract + post-outage correctness). The one deliberate *addition* beyond the plan: H4's runtime feature probe (`_claimed_at_supported`) makes the H4 code safe to ship before its migration — the plan assumed strict apply→deploy ordering; the probe removes the blast radius of getting that ordering wrong.

Two database migrations (`schema_v5_outbox_claimed_at.sql`, `schema_v6_grand_total_check.sql`) are written, idempotent, verified safe against existing production data, but **require the owner to apply them in the Supabase SQL editor**. The H4 code self-adapts (feature probe); the H5 constraint only changes behavior once applied. Live application is `OWNER ACTION REQUIRED`.

---

## 2. Starting state (independently re-verified this session, not inherited)

| Fact | Value | Tag |
|---|---|---|
| `git rev-parse HEAD` | `08ed0b6` (plan doc; matches `origin/main` after fetch) | VERIFIED |
| Working tree | clean at start, 11 files modified/created by this session | VERIFIED |
| Full suite | 433 passed → **467 passed** | VERIFIED |
| `test_golden.py` | **31 passed** (unchanged) | VERIFIED |
| GitHub Secrets | all 5 present (`gh secret list`) | VERIFIED |
| Workflow runs | 5 most recent all `completed success`, 16–26s | VERIFIED |
| `timeout-minutes` in delivery.yml | absent (confirmed the H1 gap) | VERIFIED |
| Locked Phase-1 files at start | byte-identical since `467fa89` | VERIFIED |

---

## 3. H1 — GitHub Actions workflow timeout

- **Problem:** the `deliver` job had no `timeout-minutes`; with `cancel-in-progress: false`, a genuinely hung run could occupy the `posentine-delivery` concurrency group for up to GitHub's 360-minute default, silently delaying every scheduled tick with no anomaly anywhere.
- **Root cause:** default GitHub behavior, nothing in the workflow bounding runtime.
- **Fix:** `timeout-minutes: 10` on the `deliver` job, with a code comment documenting the reasoning.
- **Why 10 minutes (VERIFIED reasoning):** observed real runs 14–26 s (5 runs, this session's `gh run list`); worst legitimate case ≈ 2–3 min (cold pip cache + slow-but-healthy Supabase reads + Telegram retries ≈ 3×30 s timeouts + backoff). 10 min is ~30× the observed runtime — far above any legitimate slow-but-healthy case, and **under** the 15-min cron interval, so a hung run dies red before the next tick would queue behind it for more than one full cycle.
- **Files:** `.github/workflows/delivery.yml` (+9 lines incl. comment).
- **Tests (+1 + falsifier extensions):** `test_workflow_has_bounded_job_timeout` (`test_delivery.py`) asserts the key exists in the jobs block and is within `[1, 60]`; the falsifier `test_workflow_spec_checks_can_fire` now also trips on removal and on `120`. **VERIFIED:** 18 passed in `test_delivery.py`; YAML parses (`cron */15`, timeout 10, concurrency intact).
- **Rollback:** delete the one line. **Risk: very low.**
- **Production/live verification:** NONE performed (not pushed) — the next scheduled/dispatched run after push verifies it. **NOT VERIFIED.**

## 4. H2 — 14+ day silent-aging gap anomaly

- **Problem:** a shift that closed beyond `MAX_SHIFT_LOOKBACK_DAYS = 14` and was never reported stops being a candidate forever — no error, no anomaly, nothing. A multi-week outage (or another future bug like the false-green one) leaves a permanent invisible gap.
- **Root cause:** `select_shift()` only walks the 14-day lookback; nothing looks past it.
- **Fix (`orchestrator.py`):**
  - New pure `_aged_out_gaps(local_now, ctx, existing, max_lookback_days, max_detect_days, first_sync_local)` — walks the bounded window `(14, 60]` days back (`MAX_AGED_GAP_DETECT_DAYS = 60`, bounded so years of history never become a pathological scan), returns every closed shift missing from `existing_reports`, oldest-first.
  - **Distinguishes the four cases the brief demands:** (a) *intentionally unsupported historical data* → shifts whose window closed at or before `first_sync_at` are skipped (pre-install, no backfill by design); (b) *legitimate absence* → when `first_sync_at is None` (no agent coverage at all) the whole pass is skipped — `dead_man` already surfaces that case loudly; (c) *missing data* → a post-install shift absent from `existing` is exactly what gets flagged; (d) *operational backfill gap* → a shift still inside the 14-day lookback belongs to `select_shift` (it will be walked and recorded, `is_partial` or not) and is never claimed by this pass.
  - One `internal_anomalies` row per newly-discovered gap, `kind="shift_gap_aged_out"`, detail = `{shift_date, shift_name, window_end, age_days}`. Dedup via new `DBState.open_aged_gap_keys` (loaded from open rows in `_load_state`), re-armed by `resolved_at` — the exact `dead_man` pattern.
- **DB migration:** none (reuses `internal_anomalies` and the already-unfiltered `existing_reports` fetch — confirmed no date filter in `_load_state`).
- **Tests (+7 in `test_orchestrator.py`):** nearest-gap bound (back=15, never inside the lookback); plan-level raise-once-until-resolved; detection-window bound (back=20 in, back=21 out); pre-install never flagged; within-lookback never claimed; **re-arm after resolution**; no-heartbeats → zero noise. All **VERIFIED** (48 passed).
- **Production behavior:** internal anomalies only; no owner-facing text changes. For the current tenant (installed 2026-08-09) there are zero post-install gaps, so **zero anomalies expected** (INFERRED from first_sync).
- **Rollback:** revert the commit — no schema, no data cleanup needed.

## 5. H3 — mid-shift coverage-gap detection (`STATUS_INCOMPLETE`)

- **Problem (Limitation 1, the general partial-coverage false-green):** a shift with 40 real invoices but a 3-hour heartbeat gap mid-window computed `total_invoices > 0`, hence `has_data=True`, and rendered `STATUS_STABLE` — the report looked trustworthy while coverage was interrupted.
- **Root cause:** `is_partial` only compares the window against `first_sync_at` (a single point in time); nothing compared the window against heartbeat *continuity*.
- **Fix:**
  - `orchestrator.py`: `_load_state` now fetches heartbeat timestamps bounded to the shift-lookback window (15 days) — capped by `first_sync_at − 1 day` for fresh tenants (this tenant: ~2 days of beats). Pure `_max_heartbeat_gap_minutes(heartbeats, window_start, window_end, tz, pad=10min)` computes the longest silence between consecutive beats inside the window (padded). `has_coverage_gap = not is_partial and gap ≥ COVERAGE_GAP_MINUTES`.
  - `report.py` (**locked file — the only locked file touched, same review-and-document discipline as `28cdc72`'s `STATUS_NO_DATA`**, documented in-file): new `STATUS_INCOMPLETE = "🟠 بيانات هذه الوردية غير مكتملة"`, threaded through `pick_status` / `pick_summary` / `build_shift_report` via `has_coverage_gap`, priority **cash_diff > no_data > coverage_gap > notes > stable**.
- **Why the 20-minute threshold (reasoned, VERIFIED by inspection of constants; NOT data-validated):** heartbeats fire on a fixed ~3-min scheduled task cadence regardless of sales (a zero-invoice cycle still heartbeats `ok=true`), so a gap means agent/network down, never quietness. 20 min ≈ 6.7× the cadence — wide enough for GitHub scheduling drift and a single missed cycle, tight enough to catch a real outage; same reasoning shape as `HEARTBEAT_FRESH_MINUTES=15` (5 cycles) and `DELETION_RESCAN_MAX_AGE_MINUTES=60`. The plan's §7.1 owner sanity-check against real `heartbeats.ok=false` frequency remains **OWNER ACTION REQUIRED** (the one-line constant change is trivial if data says otherwise).
- **Deviation from plan, documented (VERIFIED):** the plan suggested a lazily-fetched helper scoped to the target shift. Rejected because (a) the target is only known inside the pure `plan()`, which takes no client, and (b) a post-outage target can legitimately sit up to 14 days back — scoping the fetch to the recent window would silently disable the check exactly when it matters most. The bounded 15-day fetch (~7.2k rows for a long-running tenant, ~8 paginated requests) is comparable to the already-accepted `invoice_lines` fetch (an order of magnitude larger in practice). Volume math is documented in the code.
- **Tests (+6 orchestrator + 8 report):** the explicit no-false-positive-on-quiet-shift case (few invoices + normal cadence → STABLE); 30-min mid-window gap → INCOMPLETE; gap entirely outside window → ignored; cadence-aligned 18-min gap → ignored; exact 20.0-min boundary → flagged (inclusive); zero heartbeats → no signal; plus report-level priority pinning (cash > no_data > gap > notes > stable) and body assertions. All **VERIFIED**.
- **Production behavior:** only a shift with a genuine ≥20-min heartbeat gap inside its window changes from STABLE to INCOMPLETE. The Aug 9 install-boundary shift (straddle → `is_partial`, never reported) cannot double-classify.
- **Rollback:** revert the commit; nothing persisted beyond report text (never stored). **Risk: low–medium (threshold only).**

## 6. H4 — `outbox.claimed_at` + stuck-`sending` anomaly

- **Problem (Limitation 3):** a row stuck at `status='sending'` after a process crash is invisible forever — nothing records *when* it entered that state, so "claimed 200ms ago" is indistinguishable from "orphaned 3 days ago". Telegram's Bot API has no idempotency key and no post-hoc delivery query, so safe auto-resend is impossible.
- **Fix:**
  - **Migration `schema_v5_outbox_claimed_at.sql` (new, additive, idempotent):** `alter table outbox add column if not exists claimed_at timestamptz;` — nullable, existing rows unaffected. Rollback + verification SQL documented in-file.
  - `notifier/telegram.py`: `_claim()` PATCH writes `claimed_at` alongside `status='sending'`; new `scan_stuck_sending()` raises **one internal anomaly per row** stuck > `STUCK_SENDING_THRESHOLD_MINUTES = 15` (`kind="stuck_sending"`, `outbox_id`, masked recipient, `source_id` when available, an explicit "NOT auto-resent" note); dedup via `_open_stuck_outbox_ids` (open anomalies), re-arm via `resolved_at`. Runs at the top of `run()` (live: inserts; dry-run: read-only count). **Explicitly does NOT auto-resend or auto-mark-sent.**
  - **Safety addition beyond the plan (VERIFIED by test):** `_claimed_at_supported()` probes the column each run (PostgREST `PGRST204` → `SupaError` → feature off). The code is therefore safe to ship **before** the migration: pre-migration behavior is byte-identical, and both the timestamp write and the scan turn on automatically once the owner applies `schema_v5`. This removes the blast radius of the plan's strict apply→verify→deploy ordering while keeping that ordering as the documented intent.
- **Tests (+7 in `test_notifier.py`):** claim writes `claimed_at`; pre-migration claim omits it and still delivers; 20-min-old stuck row → exactly one anomaly with masked recipient, row untouched, Telegram never contacted; already-open anomaly → not re-raised; 2-min-old row → no anomaly; column missing → scan off; dry-run counts without writing. All **VERIFIED** (37 passed).
- **Why 15 minutes:** `send_message`'s worst case is 3 attempts × 30 s timeout + backoff (< ~2 min) — 15 min is an order of magnitude of margin, so a slow-but-healthy send can never false-positive while a crash-orphaned row surfaces within a quarter hour.
- **Migration application + live verification: `OWNER ACTION REQUIRED`** (apply in SQL editor → verify `select id, status, claimed_at from outbox limit 1` succeeds → rely on the scan).
- **Rollback:** code — revert commit (harmless with column present); migration — `drop column claimed_at` (nullable, no reason to need it).

## 7. H5 — `shift_reports.grand_total` CHECK constraint

- **Problem (Limitation 2):** the daybook formula lived only in Python (`metrics.py`); nothing at the DB layer rejected an internally-inconsistent row.
- **Migration `schema_v6_grand_total_check.sql` (new, additive, idempotent):** pg_constraint-guarded `check (grand_total = sales + collections - returns - delivery)`. Safe against existing data — **VERIFIED** (the three already-sent false-green rows are all-zero and `0 = 0 + 0 - 0 - 0` trivially holds; the constraint cannot fail on them). Manual verification SQL (a `23514` check-violation demo inside a rolled-back transaction) documented in-file; rollback = `drop constraint`.
- **Safety analysis (VERIFIED, independently confirmed by the failure-mode reviewer):** the exact-equality form cannot false-reject any row the existing Python writes. All five accumulators are sums of `numeric(12,2)` values, so their exact decimal sums always have ≤ 2 decimal places; Python's `round(x, 2)` is therefore exact (the classic `.005` tie is mathematically impossible for sums of 2-decimal values). The tolerance variant `abs(grand_total − (...)) < 0.01` is documented as the fallback **only if** a future code path ever writes non-2-decimal accumulators.
- **Tests:** explicitly NOT pytest-testable (no real Postgres in this suite — documented plan decision). New `test_schema_migrations.py` (+5 structural guards) pins both migration files' properties: additive, idempotent-guarded, the exact verified formula (and that it never drifts into a plus-sign variant), rollback + manual verification documented, no `create table`, no edits to locked `schema.sql`.
- **Migration application + live verification: `OWNER ACTION REQUIRED`** (apply → run the in-file 23514 check → confirm a live delivery run still inserts shift reports).

---

## 8. Additional issues discovered (GO BEYOND — second independent production-readiness pass)

Two independent reviews ran: (a) a code reviewer over the H1–H5 diff, and (b) a failure-mode reviewer over the whole delivery system hunting trustworthy-looking-but-untrustworthy reporting. No **FIX NOW** items were found. Findings and the 50-mode classification:

| # | Failure mode | Verdict (this session) |
|---|---|---|
| 1 | false-green reports | **FIXED** — H2 + H3 + existing `is_partial`/`STATUS_NO_DATA` gates; priority pinned by tests |
| 2 | missing heartbeat intervals | **FIXED** — H3 (≥20-min gap inside window → INCOMPLETE); 15-min stuck-sending analog for outbox |
| 3 | partial invoice coverage | **FIXED** — H3 surfaces it via report status |
| 4 | stale collector state | SAFE BY DESIGN — deletion detection gated on fresh heartbeat + rescan; `dead_man` at 60 min |
| 5 | delayed Supabase writes | SAFE BY DESIGN — `supa.py` bounded retries; partial batch fails loudly with rows-landed count |
| 6 | duplicate shift processing | SAFE BY DESIGN — `shift_reports` PK + `insert_ignore`; workflow concurrency serializes |
| 7 | duplicate Telegram messages | SAFE BY DESIGN — claim-before-send + outbox UNIQUE; transport-retry duplicate = accepted (see §24) |
| 8 | Telegram success then crash | **FIXED** (visibility) — H4 `claimed_at` + `stuck_sending`; auto-resend deliberately impossible |
| 9 | outbox rows stuck in sending | **FIXED** — H4 scan |
| 10 | retry storms | SAFE BY DESIGN — 5 attempts (supa) / 3 (telegram), capped exponential backoff |
| 11 | 429 behavior | SAFE BY DESIGN — honours `retry_after` exactly (tested) |
| 12 | 5xx behavior | SAFE BY DESIGN — backoff then classify (tested) |
| 13 | 403 behavior | SAFE BY DESIGN — distinct kind, never retried, `telegram_403` anomaly, chat id masked (tested) |
| 14 | malformed Telegram responses | SAFE BY DESIGN — 200-with-non-JSON / `ok=false` → `permanent_400` (tested) |
| 15 | 4096 boundary | SAFE BY DESIGN — UTF-16-unit truncation with explicit marker (tested) |
| 16 | timezone/DST/fold/gap | SAFE BY DESIGN — zoneinfo only, both seasons pinned; fold=0 documented accepted edge |
| 17 | midnight boundary | SAFE BY DESIGN — tenant-local midnight for daily cap (both orchestrator & notifier) |
| 18 | shift crossing calendar dates | SAFE BY DESIGN — evening window spans dates by design; `resolve_shift` pinned by golden |
| 19 | late-running shifts | SAFE BY DESIGN — "most recent closed shift" rule, not "exactly 07:00" (tested) |
| 20 | agent installation boundary | SAFE BY DESIGN — `is_partial` (straddle + no_coverage), recorded never reported (tested) |
| 21 | 14-day backfill boundary | **FIXED** — H2 aged-out gap anomaly, bounded 60 days, pre-install excluded |
| 22 | stale monthly report | SAFE BY DESIGN — day-1-only + `monthly:{YYYY-MM}` dedup |
| 23 | daily alert cap semantics | SAFE BY DESIGN — sent-count based, both sides count consistently; backlog deferred FIFO |
| 24 | concurrent workflow executions | SAFE BY DESIGN — `concurrency` group, `cancel-in-progress: false` (commented: a cancelled send could strand a row) |
| 25 | cancellation/concurrency | SAFE BY DESIGN — never cancel in flight; queued runs pick up after |
| 26 | workflow hangs | **FIXED** — H1 timeout-minutes: 10 |
| 27 | secret exposure | ACCEPTED LIMITATION — `Docs/PHASE_2_DELIVERY_PLAN.md` token exposure pre-existing, owner-informed, **OWNER ACTION REQUIRED** (rotate); `Docs/` gitignored (VERIFIED) |
| 28 | logs containing credentials | SAFE BY DESIGN — token redacted from every error surface; chat ids masked; env-var names only in this session's files (secret scan clean) |
| 29 | service-role misuse | SAFE BY DESIGN — service_role lives only in GitHub Secrets; agent token denied on delivery tables (42501, VERIFIED in Phase 1) |
| 30 | RLS bypass assumptions | SAFE BY DESIGN — service_role bypass is the intended delivery role; RLS isolates tenants on ingestion tables |
| 31 | schema drift | DOCUMENTED — `notify_before_golive` drift captured in `schema_v4`; H4/H5 now have committed migration files |
| 32 | migration drift | SAFE BY DESIGN — v2–v6 convention; `schema.sql` locked, never edited |
| 33 | production vs repo schema | NOT VERIFIED until migrations applied — **OWNER ACTION REQUIRED** |
| 34 | unexpected NULLs | SAFE BY DESIGN — `or 0` defaults throughout; CHECK constraint NULL-vacuous (matches code behavior) |
| 35 | malformed timestamps | SAFE BY DESIGN — `fromisoformat` raises loudly (never silent) |
| 36 | missing tenants/recipients/settings | SAFE BY DESIGN — loud `SupaError` (tested) |
| 37 | configuration drift | SAFE BY DESIGN — all five env vars required loudly, even in dry-run (tested) |
| 38 | failed delivery then recovery | SAFE BY DESIGN — `failed` rows re-claimed FIFO, attempts bounded, `dead` terminal |
| 39 | crash recovery | **FIXED** (visibility) — H4; message-loss-not-duplication is the accepted design |
| 40 | idempotency under retries | SAFE BY DESIGN — DB constraints are the sole arbiter; structurally tested (`insert_ignore` only on constrained tables) |
| 41 | partial failures between Supabase writes | SAFE BY DESIGN — event 'detected'→'queued' only after outbox attempt; outbox UNIQUE prevents dup sends |
| 42 | reports from incomplete data | **FIXED** — H3 (mid-shift) + H2 (aged-out) + existing no-data gate |
| 43 | old data silently disappearing | **FIXED** — H2 |
| 44 | anomalies recorded but never surfaced | ACCEPTED LIMITATION — `internal_anomalies` is intentionally an internal channel (events.py `INTERNAL_TYPES`); surfaced via diagnostics workflow |
| 45 | anomalies that spam the owner | SAFE BY DESIGN — all new anomalies are internal-only; daily cap 3 for owner alerts |
| 46 | dead-man re-arm | SAFE BY DESIGN — `resolved_at` closes, later outage re-raises (tested); H2/H4 dedup follow the same pattern |
| 47 | noisy/duplicate anomaly generation | SAFE BY DESIGN — open-set dedup on all three new anomaly kinds (tested) |
| 48 | test doubles vs PostgREST | ACCEPTED LIMITATION — fakes honor the real UNIQUE constraints; `lt.` string-compare documented as safe only for `timespec='seconds'` output; falsifiers paired with every structural check |
| 49 | local vs production behavior | NOT VERIFIED — live dry-run + delivery after push (OWNER ACTION), per §22 |
| 50 | trustworthy-looking reports from untrustworthy data | **FIXED** — the four limitation gates (H2/H3/H4/H5) close the paths the audit found; golden 31 pins the arithmetic |

---

## 9. Issues fixed

1. H1 — hung workflow could starve the schedule for up to 6 h (timeout-minutes: 10).
2. H2 — shifts aging out of the 14-day lookback silently disappeared (internal anomaly).
3. H3 — mid-shift coverage interruptions rendered `STATUS_STABLE` (`STATUS_INCOMPLETE`).
4. H4 — crash-orphaned `sending` rows were invisible forever (`claimed_at` + `stuck_sending` anomaly).
5. H5 — no DB-level guard on the daybook formula (`grand_total` CHECK migration).

## 10. Issues intentionally not fixed (ACCEPTED LIMITATIONS / documented)

- Transport-retry duplicate message (lost HTTP response re-sends a delivered message) — accepted in `send_message`; reports beat the rare duplicate.
- Crash-after-send message loss — the accepted cost of never-duplicate; H4 now makes it visible.
- `internal_anomalies` has no unique constraint (dedup is the caller's job) — out of scope per plan §6.
- Late-arriving invoices never retroactively correct a sent report — out of scope per plan §6.
- Report visual layout for the subtractive formula — owner decision pending, out of scope.
- Exposed Telegram token in `Docs/PHASE_2_DELIVERY_PLAN.md` — owner's informed deferral; **OWNER ACTION REQUIRED** (rotate when ready).

## 11. Owner decisions required

1. **Apply `schema_v5_outbox_claimed_at.sql`** (Supabase SQL editor) and verify the column live — before relying on the stuck-sending scan (code is safe either way via the probe).
2. **Apply `schema_v6_grand_total_check.sql`**, run the in-file `23514` manual check, confirm a live delivery run still inserts.
3. **Sanity-check H3's 20-min coverage-gap threshold** (and H4's 15-min stuck threshold) against real `heartbeats.ok=false` frequency (plan §7.1) — one-line constant change if data says otherwise.
4. Push the implementation commit (exact commands in §12 / §26).
5. Live verification pass after push (§22).
6. (Standing, unchanged) number-matching against يومية الخزينة before owner activation.

## 12. Permission-blocked work (OUT OF SCOPE — this session had no GitHub write / no Supabase service_role)

- **No push:** `git push origin main` not attempted (no write permission). Exact steps: `git add -A && git commit -m "feat: hardening phase H1-H5 — workflow timeout, aged-gap anomaly, coverage-gap status, stuck-sending visibility, grand_total CHECK" && git push origin main`. **OWNER ACTION REQUIRED.**
- **No GitHub dispatch:** `gh workflow run "POSentine delivery" --repo mahmouddevmohsen/POSentine -f dry_run=true` (live verification) — needs the push first. **OWNER ACTION REQUIRED.**
- **No migration application:** both SQL files need the Supabase SQL editor (service_role lives only in GitHub Secrets by design). **OWNER ACTION REQUIRED.**
- No secrets were created, rotated, deleted, or read (only presence checked via `gh secret list`).

## 13. Files changed

| File | Change |
|---|---|
| `.github/workflows/delivery.yml` | H1: `timeout-minutes: 10` + rationale comment |
| `orchestrator.py` | H2: `_aged_out_gaps`, `_first_sync_local`, `MAX_AGED_GAP_DETECT_DAYS`, DBState fields, `_load_state` wiring, `_build_anomalies`; H3: `COVERAGE_GAP_MINUTES/PAD`, `_max_heartbeat_gap_minutes`, heartbeat fetch, `has_coverage_gap` |
| `report.py` (LOCKED — permitted, documented) | H3: `STATUS_INCOMPLETE`, `has_coverage_gap` threading, priority + summaries |
| `notifier/telegram.py` | H4: `claimed_at` on claim, `_claimed_at_supported` probe, `scan_stuck_sending`, `_open_stuck_outbox_ids`, `STUCK_SENDING_THRESHOLD_MINUTES`, Summary counter |
| `schema_v5_outbox_claimed_at.sql` (new) | H4 migration (additive, idempotent, rollback + verification) |
| `schema_v6_grand_total_check.sql` (new) | H5 migration (idempotent, manual 23514 check documented) |
| `test_delivery.py` | +1 H1 test, falsifier extensions |
| `test_orchestrator.py` | +13 H2/H3 tests |
| `test_report.py` | +8 H3 priority tests |
| `test_notifier.py` | +7 H4 tests (+ fake `lt.`/claimed_at support) |
| `test_schema_migrations.py` (new) | +5 structural migration guards |
| `reports/phase2/HARDENING_FINAL_REPORT.md` (new) | this report |

Locked Phase-1 files **not** modified: `adapter_hdsoft.py`, `metrics.py`, `events.py`, `schema.sql`, `test_golden.py` (`git diff --name-only` verified). **VERIFIED.**

## 14. Tests added (+34)

- `test_delivery.py`: 17 → **18** (H1 timeout spec + falsifier).
- `test_orchestrator.py`: **48** (H2 bounds/dedup/re-arm/no-heartbeats, H3 cadence/boundary/no-false-positive).
- `test_report.py`: **20** (H3 status priority + body text).
- `test_notifier.py`: 30 → **37** (H4 claim/probe/scan/dedup/dry-run).
- `test_schema_migrations.py`: **5** (new file, structural guards).

## 15. Full test result

`python -m pytest -q` → **467 passed** (was 433; +34). **VERIFIED** (final run after all review fixes).

## 16. Golden test result

`python -m pytest -q test_golden.py` → **31 passed, unchanged** (file untouched; the `report.py` edit cannot affect it — golden covers metrics/events/adapter only). **VERIFIED.**

## 17. Security checks

- Secret-shaped-value scan over every changed/new file (bot-token pattern, service-role pattern, private keys, GitHub tokens): **clean** — env-var names only, never values. **VERIFIED.**
- No pyodbc in the delivery closure: existing mechanical tests + workflow structural guard all pass. **VERIFIED.**
- `git diff --check`: clean (no whitespace errors). **VERIFIED.**
- No credential in any test: fake tokens used everywhere. **VERIFIED.**
- Locked-file policy: only `report.py` touched, with the plan's explicit permission and in-file documentation. **VERIFIED.**

## 18. Git state

- Local HEAD: `08ed0b6` (`origin/main` aligned at session start). **VERIFIED.**
- Working tree: 8 modified + 3 new files, **not committed, not pushed** (no write permission). Exact state above. **VERIFIED.**

## 19. GitHub state

- 5 secrets present; workflow live (`*/15` cron); 5 recent runs all success (16–26 s). **VERIFIED (read-only).**
- No dispatch performed this session (nothing pushed to run against). **NOT VERIFIED** for the new code.

## 20. Supabase state

- `go_live_at` still NULL; only the dev chat receives (notify_before_golive). **VERIFIED (prior session's live read + unchanged since).**
- `outbox.claimed_at` column: **NOT PRESENT** until the owner applies `schema_v5`. **NOT VERIFIED** (expected absent).
- `shift_reports` CHECK constraint: **NOT PRESENT** until `schema_v6`. **NOT VERIFIED** (expected absent).
- All live reads/writes of the new code: **NOT VERIFIED** (local fakes only — feature probe means no breakage either way).

## 21. Telegram delivery state

- Last real delivery: 2026-08-11 morning run `31437546150`, `message_id=3`, `telegram_403=0` (prior session). **VERIFIED (API-level).**
- No new sends this session; none attempted (no push). **NOT VERIFIED.**

## 22. Production verification

**NOT PERFORMED — OWNER ACTION REQUIRED.** Exact sequence after push:
1. `gh workflow run "POSentine delivery" -f dry_run=true` → confirm the plan prints: no `shift_gap_aged_out` (tenant installed 2026-08-09 → no aged gaps, INFERRED), `stuck_sending=0` (no column yet), and the shift report statuses unchanged on known-clean shifts (`STATUS_STABLE`).
2. Apply `schema_v5`; verify `select id, status, claimed_at from outbox limit 1`; run a live dispatch; read back `outbox.claimed_at` populated.
3. Apply `schema_v6`; run the manual 23514 check; run a live dispatch and confirm shift reports still insert.
4. A dry-run + live run after that to observe the new anomaly counters and a clean-shift report.

## 23. Rollback strategy

- H1: delete the one YAML line.
- H2/H3: revert the commits — no schema, no data to clean (anomaly rows informational; report text never stored).
- H4: code revert (harmless with the column present); `alter table outbox drop column claimed_at` if ever needed (unnecessary — nullable, unused, free).
- H5: `alter table shift_reports drop constraint shift_reports_grand_total_formula` (trivial, no data changes).

## 24. Remaining limitations

- H3/H4 thresholds are reasoned defaults pending owner data-check (§11.3).
- Transport-retry duplicate + crash-after-send loss (accepted, §10).
- Migrations not applied → H4 scan and H5 guard inactive until owner action.
- Token exposure in `Docs/` (pre-existing, owner's deferral).
- DST fold=0 edge (accepted, documented in Phase-2 handoff §5).
- Live behavior of the new code unobserved (nothing pushed).

## 25. Future recommendations

1. Add `internal_anomalies` dedup protection (unique on (kind, detail->>'outbox_id')-style keys or an application key column) — the one table without it (plan §6, still open).
2. Data-validate H3's 20-min threshold against real heartbeat gaps before owner go-live.
3. Rotate the Telegram token (already known-exposed).
4. Revisit late-arriving-invoice correction semantics after number-matching.
5. Consider a monthly anomaly digest surfaced to the developer chat so `internal_anomalies` rows don't sit unread (currently only discoverable via the diagnostics workflow).

## 26. Exact next steps (OWNER ACTION REQUIRED)

1. `git add -A && git commit -m "feat: hardening phase H1-H5 — workflow timeout, aged-gap anomaly, coverage-gap status, stuck-sending visibility, grand_total CHECK" && git push origin main`
2. Dispatch a dry-run; verify the plan summary + no behavior change on clean shifts.
3. Apply `schema_v5_outbox_claimed_at.sql`; verify the column; run a live dispatch; read back `claimed_at`.
4. Apply `schema_v6_grand_total_check.sql`; run the manual 23514 check; run a live dispatch.
5. Owner sanity-check the two thresholds (§11.3).
6. Then the standing next step: number-match a delivered report against يومية الخزينة before any owner activation.

## 27. G-Brain synchronization status

**VERIFIED — written and read back after each step:**
- `Logs/2026-08-11.md`: Session 4 appended with per-H checkpoints (problem → root cause → fix → files → tests → results → security → owner actions → next H → repo state → new risks).
- `Projects/POSentine/Phase-2-Handoff.md`: status frontmatter + §9 risk entry updated to IMPLEMENTED.
- `index.md`: POSentine Phase-2 + log entries updated.
- `_CLAUDE.md`: "Last updated" line + Active Context paragraph updated (suite 467, migrations pending, next steps).
All four written with exact current state (not inferred claims) and verified by re-read.

## 28. Independent review findings

- **Code review (H1–H5 diff):** no blocking issues. Three findings fixed during the session: (a) `first_sync_at is None` would raise ~90 noise anomalies → pass skipped; (b) `_build_anomalies`'s `local_now` param misnamed (received naive `pos_now`) → renamed, preventing a naive/aware regression; (c) `stuck_sending` anomaly omitted `source_id` → set when available. A test-fake `lt.` assumption documented. **All fixed; suite re-run green.**
- **Failure-mode review (50-mode GO BEYOND):** no FIX NOW items. Independently confirmed the H5 exact-equality constraint cannot false-reject Python-written rows (sums of numeric(12,2) values have ≤2 decimals — the `.005` tie is impossible). Noted the residual: keep the constraint's safety premise documented (tolerance variant is the fallback if a future path writes non-2-decimal totals).

## 29. Suggestions for a future hardening phase

- `internal_anomalies` unique/dedup key (surfaced twice now).
- Live anomaly surfacing to the dev chat (monthly digest) — internal_anomalies currently needs the diagnostics workflow to be seen.
- A heartbeat-continuity *falsifier* for H3 (a corrupted heartbeat list with fake-regular beats must NOT trip the check) — the check's falsifier currently lives only in the threshold-boundary test.
- Backfill-gap self-healing after prolonged outages (currently the 14-day walk re-reports everything reachable; shifts lost to a >60-day outage stay silent by design — bounded, documented).

## 30. FINAL RISK ASSESSMENT

| Risk | Level | Mitigation / owner action |
|---|---|---|
| Migrations not applied → H4/H5 inactive | Medium | Owner applies both; H4 code safe either way (probe); H5 only changes behavior once applied |
| Code not pushed → live cron runs old code | Low | All new code additive or feature-gated; no behavior change expected on the old path until push |
| 20-min / 15-min thresholds unvalidated | Low–Medium | One-line constant change; owner data check (§11.3) |
| Exact-equality CHECK false-rejects | Negligible | Mathematically impossible for code-produced rows (documented); tolerance fallback documented |
| Transport-retry duplicate message | Low (accepted) | Inherent to Telegram API; reports beat the rare duplicate |
| Crash-after-send message loss | Low (accepted) | Never-duplicate design; now visible via `stuck_sending` |
| Token exposure (pre-existing) | Medium (open) | Owner rotation decision |
| No live verification of new code | Medium until done | §22 sequence after push |

**Overall: the hardening phase is complete to the limit of available permissions. The code is tested (467/31), reviewed (two independent passes, clean), and production-safe either side of the two pending migrations. The remaining gates are owner actions, not code.**
