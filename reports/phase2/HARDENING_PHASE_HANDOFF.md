# POSentine — Hardening Phase: Implementation Plan & Handoff

**Status: PLANNING ONLY. No hardening code has been written or pushed. This document is the
plan; a future session implements it.** Written 2026-08-11, a session after the false-green
fix (`28cdc72`) and its audit report (`d58c8b7`, `AUG11_FALSE_GREEN_AUDIT.md`). Independently
re-verified before writing anything below — see §0.

---

## 0. Independent verification of the starting state

Re-checked directly this session, not taken from the prior report:

- `git rev-parse HEAD` = `e17692ff6929c4b7744f13e615a9cf457995e44e`, matches `origin/main`
  (fetched and compared). Working tree clean.
- `python -m pytest -q` → **433 passed**. `python -m pytest -q test_golden.py` → **31 passed**.
- `gh secret list` → all 5 secrets present (`SOURCE_ID`, `SUPABASE_SERVICE_ROLE_KEY`,
  `SUPABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TENANT_ID`), created 2026-08-10.
- `gh run list --workflow="POSentine delivery"` → 5 most recent runs all `completed success`,
  most recent at `2026-08-11T06:13:17Z` (the audit-summary send, `message_id=10`, confirmed in
  the prior session's job log).
- Read `notifier/telegram.py`, `delivery.py`, `.github/workflows/delivery.yml`, `schema.sql`
  in full (not summarized) to design against the actual current code, not a description of it.

**Conclusion: the "current verified state" block in this session's brief is accurate.**
Everything below is designed against the real files as they exist at `e17692f`.

---

## 1. Executive summary of the plan

Four limitations were flagged. Independent investigation confirms all four are real and
confirms no new bugs beyond them (see §2). Proposed hardening, in priority order:

| # | Item | Addresses | Migration? | Risk | Fixable now? |
|---|---|---|---|---|---|
| H1 | GitHub Actions `timeout-minutes` | stale/hung runs occupying the concurrency group | No | Very low | **Yes** |
| H2 | 14+ day gap anomaly | Limitation 4 (silent aging) | No | Low | **Yes** |
| H3 | Mid-shift coverage-gap detection (`STATUS_INCOMPLETE`) | Limitation 1 (partial coverage) | No | Low–medium | **Yes**, threshold needs a sanity check |
| H4 | `outbox.claimed_at` + stuck-`sending` anomaly | Limitation 3 (crash window) | **Yes, additive column** | Medium (ordering-sensitive) | Code: yes. Migration: **owner decision** |
| H5 | `shift_reports` `grand_total` CHECK constraint | Limitation 2 (no DB-level formula guard) | **Yes, additive constraint** | Low | Code: yes. Migration: **owner decision** |

None of these touch the POS, rotate credentials, or weaken an existing gate. All are additive.
Two need a live Supabase migration the current session has no credentials to apply (by design
— service_role lives only in GitHub Secrets); those are the same "owner applies via SQL
editor" pattern already used for `notify_before_golive` (§4 of the prior Phase-2 handoff) and
documented the same way (`schema_v5_...sql`, `schema_v6_...sql`).

---

## 2. Independent re-investigation of the four limitations

### 2.1 Limitation 1 — partial/mid-shift coverage

**Confirmed still open**, re-read from `orchestrator.py` directly (not from the prior report).
`_build_shift_report`'s `is_partial` (broadened in `28cdc72`) only compares the shift window
against `first_sync_at` — a single point in time. Nothing compares the shift window against
heartbeat *continuity* across that window. A shift with 40 real invoices but a 3-hour gap in
the middle (agent crashed, GitHub Actions outage, POS unreachable) computes
`total_invoices=40 > 0`, so `has_data=True` in `report.py`, and — absent a cash diff or notes —
still renders `STATUS_STABLE`.

**The good news, confirmed by reading `schema.sql` and `orchestrator.py`'s `_load_state`:**
the raw material to detect this already exists and needs **no new data source**:
- `heartbeats` table already has one row roughly every 3 minutes (Phase 1, unchanged),
  `(tenant_id, source_id, at, ok, rows_pulled)`, indexed `ix_heartbeats_recent`.
- The shift's own `window_start`/`window_end` (naive POS-local) are already computed by
  `metrics.shift_window`.
- Localizing to a UTC range for the heartbeats query is the same `zoneinfo` pattern already
  used everywhere else in `orchestrator.py`.

**Why this signal doesn't create false positives on a genuinely quiet shift:** heartbeats fire
on a fixed schedule *regardless of sales activity* — a cycle with zero new invoices still
heartbeats (`rows_pulled=0, ok=true`). A gap in heartbeats means the **agent or the network**
was down, not that the shop was quiet. This is the orthogonal signal the brief asks for in
§A ("without falsely flagging legitimate quiet shifts") — sales volume and agent liveness are
independent axes, and only the second is what "coverage" actually means.

### 2.2 Limitation 2 — no DB-level CHECK constraint

**Confirmed, re-read `schema.sql` in full this session.** All 9 `check (` occurrences are
enum/range checks (`channel`, `kind`, `level`, `status`, `shift_name`); none reference
`grand_total`. The formula lives only in `metrics.py:185`.

**New fact this session didn't have before:** the existing three false-green rows already in
production (`grand_total=0, sales=0, returns=0, delivery=0, collections=0`) trivially satisfy
`0 = 0 + 0 - 0 - 0` — a `CHECK` constraint added now would **not** fail on the existing bad
rows, so the migration is safe to apply without any data cleanup first.

### 2.3 Limitation 3 — Telegram crash-after-send-before-mark-sent

**Confirmed, re-read `notifier/telegram.py:485-499` this session.** The `client.update(...,
"sent", ...)` call is the very next statement after a successful `send_message()` return —
already structurally as tight as it can be. Telegram's Bot API has **no idempotency key** for
`sendMessage` and **no way to query "was message X delivered"** after the fact for a bot in a
private chat — so there is no way to safely auto-resend or auto-confirm. **The realistic
hardening is not "close the window" (already minimal) but "make a stranded row visible"**:
today, a row stuck at `status='sending'` is invisible forever — nothing records *when* it
became `sending`, so nothing can distinguish "claimed 200ms ago, still sending" from "claimed
3 days ago, orphaned by a crash." `outbox` has `created_at` and `sent_at` but no `claimed_at`.

### 2.4 Limitation 4 — 14+ day silent aging

**Confirmed, re-read `orchestrator.py:56,243` this session.** `MAX_SHIFT_LOOKBACK_DAYS = 14`
bounds `_closed_shifts()`'s candidate generation; `select_shift()` only ever looks inside that
window. A shift that closed more than 14 days ago and was never reported (a real outage
scenario, or the aftermath of a bug like the one just fixed) simply stops being a candidate —
no error, no anomaly, nothing.

**New fact this session didn't have before:** `_load_state`'s query for `shift_reports`
(`orchestrator.py`'s `_load_state`, the `existing` set) has **no date filter at all** — it
already fetches every `shift_reports` row ever created for this tenant/source, not just recent
ones. This means a gap-detection pass needs **zero new Supabase queries**: it can compute
"closed shifts between (install day) and (14 days ago) that are missing from `existing`"
entirely from state already being loaded every single run.

**No fifth limitation found.** The rest of the codebase (concurrency, retry classification,
DST/timezone, invoice/cash integrity, idempotency) was re-confirmed correct on direct read and
is unchanged from the prior audit — see §3 for the full A–K walk.

---

## 3. Full A–K audit walk (this session)

### A. Data completeness
Four coverage states now distinguishable in the design (was two): **complete** (data present,
no gap) → stable; **zero** (0 invoices) → `STATUS_NO_DATA` (already shipped, `28cdc72`);
**pre-installation/no-coverage** (whole window before `first_sync_at`) → suppressed via
`is_partial` (already shipped); **partial** (nonzero invoices, but a heartbeat gap inside the
window) → new `STATUS_INCOMPLETE` (H3, this plan). All four use only already-collected
signals — no POS contact of any kind.

### B. Financial integrity
Formula re-verified correct (`metrics.py:185`, unchanged). H5 (§4.5) adds a DB-level guard.
No other reconciliation gap found: cash-diff detection, invoice classification order, and
zero-invoice detection were all re-read and are unchanged/correct from the prior audit.

### C. Telegram delivery
Claim-before-send, 403 classification, 429/5xx bounded retry, 4096 truncation, and secret
redaction all re-read directly this session in `notifier/telegram.py` — all unchanged, all
correct, none of this plan's changes touch any of them. H4 (§4.4) is additive only.

### D. Outbox / state machine
Transitions unchanged: `pending → sending → sent` / `pending → sending → failed → dead`.
`claimed_at` (H4) does not add a new *state* — it timestamps an existing one, purely for
observability. Concurrent-GitHub-Actions-execution risk unchanged from the prior audit
(workflow `concurrency:` group + DB `insert_ignore`/atomic-claim, both re-confirmed correct).

### E. GitHub Actions
Re-read `.github/workflows/delivery.yml` in full. Confirmed: `concurrency: {group:
posentine-delivery, cancel-in-progress: false}` present and correct. **New finding this
session: no `timeout-minutes` on the `deliver` job** — falls back to GitHub's default 360
minutes. Combined with `cancel-in-progress: false`, a genuinely hung run (network partial-hang
not caught by `requests`'/`supa.py`'s own `timeout=30`) could occupy the concurrency group for
up to 6 hours, silently delaying every subsequent scheduled tick with no anomaly raised
anywhere. This is H1 (§4.1) — the single lowest-risk item in this whole plan.

### F. Supabase
Schema re-read in full. RLS/service-role assumptions unchanged and correct. Indexes
appropriate for the query patterns used. **Confirmed still true from the prior audit, not
re-litigated as a new finding:** `internal_anomalies` has no unique constraint — the one table
with no dedup protection. Not one of the four flagged limitations, kept out of scope for this
phase per "do not blindly expand scope" — noted as an optional follow-up in §6.

### G. Time / DST
No new issue found. Re-confirmed timezone handling is `zoneinfo`-based throughout with no
hardcoded offsets, and the existing DST test suite (`test_orchestrator.py`) covers both
boundaries in both seasons. **No change needed in this phase.**

### H. Report semantics
Clarification worth recording for whoever implements next: **the brief's status list
(stable/warning/incomplete/blocked/failed/dead/deferred) conflates two different axes.**
`report.py`'s statuses describe the **shift's own state** (is there enough evidence to call it
stable) — today: `STATUS_STABLE`, `STATUS_REVIEW`, `STATUS_CASH`, `STATUS_NO_DATA`, plus the
new `STATUS_INCOMPLETE` (H3). `outbox.status` describes the **message's delivery state**
(pending/sending/sent/failed/dead) — an entirely separate table, separate concern, already
correct. Conflating them (e.g. trying to make a shift report say "failed" when it means the
*Telegram send* failed) would be a design mistake — keep them separate.

**Invariant to preserve:** a shift can say `STATUS_STABLE` only if (a) at least one invoice was
observed (already enforced, `28cdc72`) **and**, after H3, (b) no heartbeat gap was detected
inside the window. Cash-diff still overrides both, since it's an independent signal
(`cash_counts`, unchanged priority).

### I. Observability
Two new anomaly kinds proposed (H3's coverage gap surfaces via the report status directly, not
via `internal_anomalies` — it's owner-facing by design, not internal): H4's `stuck_sending` and
H2's `shift_gap_aged_out`, both modeled on the existing `dead_man` pattern (open/resolved via
`internal_anomalies.resolved_at`, re-armable). H1 makes a hung workflow visible as a
GitHub-Actions-level failure (red job) instead of an invisible multi-hour stall.

### J. Regression protection
See §5 — a full test list per change, none touching `test_golden.py`'s count.

### K. Production safety
See the per-change tables in §4. Every change classified read-only vs DB-write vs
delivery-behavior-changing, with an explicit rollback for each.

---

## 4. Proposed changes, in detail

### 4.1 H1 — GitHub Actions `timeout-minutes`

- **Why:** an unbounded job can occupy the `posentine-delivery` concurrency group for up to 6
  hours on a genuine hang, silently delaying every scheduled tick behind it, with no anomaly
  raised anywhere.
- **Prevents:** stale/stuck workflow runs starving the schedule.
- **Files:** `.github/workflows/delivery.yml` — add `timeout-minutes: 10` to the `deliver` job
  (observed real runs: 14–26 seconds; 10 minutes is generous margin, not a tight bound that
  could false-positive on a slow-but-healthy run).
- **DB migration:** none. **Production behavior change:** none for a healthy run; a genuinely
  hung run now fails (red) instead of hanging silently — this IS the intended behavior change.
- **Tested by:** a YAML-parsing test asserting the key is present and within `[1, 60]` minutes
  — same pattern as `test_delivery.py`'s existing 6 workflow-spec checks.
- **Live verification:** none needed beyond the next scheduled/dispatched run completing
  normally (it will, since real runs take under 30 seconds).
- **Rollback:** delete the one line.
- **Risk: very low.**

### 4.2 H2 — 14+ day gap anomaly

- **Why:** a shift that ages out of `MAX_SHIFT_LOOKBACK_DAYS` is silently never reported — no
  signal anywhere that this happened.
- **Prevents:** a multi-week outage (or a future bug like the one just fixed) leaving permanent,
  invisible reporting gaps.
- **Files:** `orchestrator.py` only — a new pure function alongside `_closed_shifts` (e.g.
  `_aged_out_gaps(local_now, ctx, existing, max_lookback_days, max_detect_days)`) that walks
  candidate shift dates from `max_lookback_days` back to `max_detect_days` (proposed default:
  60 — bounded, not unbounded, to avoid a pathological scan as the tenant's history grows over
  years) and returns any `(shift_date, shift_name)` missing from `existing_reports`. Wired into
  `_build_anomalies` the same way `dead_man` is: one anomaly per newly-discovered gap,
  `resolved_at` used to avoid re-raising the same gap every 15 minutes forever.
- **DB migration:** none — reuses `internal_anomalies` (existing table) and `existing_reports`
  (already loaded every run, no date filter, confirmed in §2.4).
- **Production behavior change:** new `internal_anomalies` rows only; no shift report content
  changes, no new Telegram sends to the owner (internal-only channel, per `events.py`'s
  `INTERNAL_TYPES` convention).
- **Tested by:** unit tests with a `DBState` missing an old shift beyond `MAX_SHIFT_LOOKBACK_
  DAYS` but within the detection window → one anomaly; a second `plan()` call with the same
  state → no duplicate anomaly (already resolved-tracking pattern); a gap beyond the detection
  window → no anomaly (bounded, not a silent infinite scan).
- **Live verification:** dispatch `delivery.yml` with `--dry-run` against real production
  state and confirm the anomaly count in the printed plan summary; a live (non-dry) run and a
  read via the existing `diagnostics_forensic.py`/`diagnostics-oneoff.yml` pattern to confirm
  the row landed in `internal_anomalies`.
- **Rollback:** revert the commit — no schema to unwind, no data to clean up (the anomaly rows
  are informational, harmless to leave in place even if reverted).
- **Risk: low.**

### 4.3 H3 — mid-shift coverage-gap detection (`STATUS_INCOMPLETE`)

- **Why:** a shift with some real invoices but a real coverage hole (agent down for part of the
  window) currently still renders `STATUS_STABLE` — §2.1.
- **Prevents:** the general partial-coverage false-green (Limitation 1 exactly).
- **Files:**
  - `orchestrator.py`: a new query in `_load_state` (or a lazily-fetched helper scoped to the
    target shift only, to avoid fetching full heartbeat history on every run) for heartbeats
    within `[window_start_utc, window_end_utc]` (plus a small pad, e.g. ±10 min, to catch a gap
    that starts just before/after the boundary), then compute the maximum gap between
    consecutive heartbeat timestamps. Threshold proposed: **20 minutes** (≈6.7× the normal
    3-minute cycle — wide enough to absorb GitHub's own scheduling drift and a single missed
    cycle, tight enough to catch a real outage; same reasoning shape as the existing
    `HEARTBEAT_FRESH_MINUTES=15`/`DELETION_RESCAN_MAX_AGE_MINUTES=60` constants).
  - `report.py`: new `STATUS_INCOMPLETE = "🟠 بيانات هذه الوردية غير مكتملة"` (locked file,
    same review-and-document discipline as `STATUS_NO_DATA` in `28cdc72`), threaded through
    `pick_status`/`pick_summary`/`build_shift_report` via a new parameter (e.g.
    `has_coverage_gap: bool`), priority: `cash_diff > no_data > has_coverage_gap > notes >
    stable` (cash-diff and no-data both stay ahead of it — a totally-uncovered shift is a
    stronger claim than a partially-covered one, and a physical cash count is independent of
    either).
- **DB migration:** none.
- **Production behavior change:** a real, currently-mislabeled `STATUS_STABLE` shift becomes
  `STATUS_INCOMPLETE` **only if** a genuine ≥20-minute heartbeat gap is found inside its
  window — no change to any shift without one.
- **Tested by:** unit tests — normal cadence + few invoices (quiet shift) → still
  `STATUS_STABLE` (the explicit false-positive check the brief asks for); a synthetic 25-minute
  gap mid-shift with real invoices on both sides → `STATUS_INCOMPLETE`; a gap entirely outside
  the shift window → no effect; boundary at exactly the threshold.
- **Live verification:** `--dry-run` against real production heartbeat history for a past shift
  known to have had a clean run (expect `STATUS_STABLE` unchanged) — the existing Aug 9 evening
  install-boundary shift is a good candidate to also confirm no double-classification with
  `is_partial`.
- **Rollback:** revert the commit; no data migration to unwind (nothing persisted beyond the
  report text itself, which is never stored — only sent).
- **Risk: low–medium** — the only real risk is threshold mistuning (too tight → false positives
  on a slow cron tick; too loose → misses a real gap). Flagged for a quick owner sanity check
  on the 20-minute default before shipping, not because the mechanism itself is risky.

### 4.4 H4 — `outbox.claimed_at` + stuck-`sending` anomaly

- **Why:** a row stuck at `status='sending'` after a crash is invisible forever — nothing
  records when it entered that state, so nothing can tell "just claimed" from "orphaned three
  days ago" — §2.3.
- **Prevents:** silent, permanent loss of delivery *tracking* (not the message itself — Telegram
  already has it in the crash scenario; this is about the system knowing it does).
- **Files:**
  - `schema.sql` (+ new `schema_v5_outbox_claimed_at.sql`, additive): `alter table outbox add
    column if not exists claimed_at timestamptz;` (nullable — existing rows unaffected).
  - `notifier/telegram.py`: `_claim()`'s PATCH sets `claimed_at = now_utc` alongside
    `status='sending'`. A new pass (either at the top of `run()` or a small dedicated function
    called from `delivery.py`) scans `status='sending' AND claimed_at < now - STUCK_THRESHOLD`
    (proposed: 15 minutes — generous relative to `send_message`'s own worst case of 3 attempts
    × 30s timeout × backoff, comfortably under a minute in practice) and raises one
    `internal_anomalies` row per stuck id (dedup via a kind + the row id in `detail`, same
    "already open" pattern as `dead_man`/H2). **Explicitly does NOT auto-resend or
    auto-mark-sent** — Telegram has no way to confirm delivery after the fact, and guessing
    wrong risks exactly the duplicate-owner-message outcome this project has already, correctly,
    decided against. A stuck row surfaces as a loud internal signal for a human to check
    (Telegram chat history) and decide.
- **DB migration:** **yes, required before the code deploys** — `_claim()`'s PATCH would be
  rejected by PostgREST if `claimed_at` doesn't exist yet. **Strict ordering: apply the
  migration, verify it live (a `select claimed_at from outbox limit 1` succeeds), only then
  deploy the code.** This is the one item in this plan with a hard sequencing dependency —
  call it out explicitly to whoever implements next.
- **Production behavior change:** every future `sending` row gains a timestamp (harmless);
  stuck rows older than 15 minutes start generating internal anomalies (not owner-facing).
- **Tested by:** unit test asserting `_claim()`'s PATCH payload includes `claimed_at`; unit test
  with a `FakeClient` row `claimed_at` 20 minutes old → one anomaly, not re-raised on a second
  pass while still open; a row `claimed_at` 2 minutes old → no anomaly.
- **Live verification:** after the migration lands, a live `--dry-run` then live run, confirm
  via `diagnostics_forensic.py`-style read that `outbox.claimed_at` is populated on the next
  real send.
- **Rollback:** code — revert the commit (harmless even with the column present, since it just
  stops writing to it). Migration — `alter table outbox drop column claimed_at;` if ever needed,
  though there's no reason to: a nullable, unused column costs nothing to leave in place.
- **Risk: medium** — not because any single piece is risky, but because of the migration-then-
  code ordering requirement; get that wrong and the notifier's claim PATCH fails outright
  (loud, not silent — but still an avoidable self-inflicted outage). Owner decision required
  before applying the migration.

### 4.5 H5 — `shift_reports.grand_total` CHECK constraint

- **Why:** nothing at the database layer enforces the formula — a future code path, a manual
  `UPDATE`, or a bug could insert an internally-inconsistent row and Postgres would accept it.
- **Prevents:** a repeat of the *shape* of the original 1,140 EGP confusion, except enforced
  structurally instead of relying on every future code path getting the Python formula right.
- **Files:** new `schema_v5_grand_total_check.sql` (numbered `v5` if H4's migration isn't taken,
  `v6` if both are — final numbering is whoever implements next's call, not fixed here):
  ```sql
  do $$
  begin
    if not exists (
      select 1 from pg_constraint where conname = 'shift_reports_grand_total_formula'
    ) then
      alter table shift_reports
        add constraint shift_reports_grand_total_formula
        check (grand_total = sales + collections - returns - delivery);
    end if;
  end $$;
  ```
  Guarded for idempotent re-run, matching the existing `schema_v2`/`v3`/`v4` convention.
- **DB migration:** yes, additive, owner-applied via the Supabase SQL editor (same as
  `notify_before_golive` was). **Verified safe against existing data this session:** the three
  already-sent false-green rows are all-zero, and `0 = 0 + 0 - 0 - 0` is trivially true — the
  migration will not fail on data already in production.
- **Production behavior change:** none for any row the existing Python code produces (it always
  satisfies the formula by construction). Only a hand-written/buggy future `INSERT`/`UPDATE`
  that violates the formula would now be rejected by Postgres instead of silently landing.
- **Tested by:** this is explicitly **not pytest-testable** — no test in this codebase exercises
  a real Postgres constraint (all tests use in-memory fakes). Propose a short manual SQL
  verification script (run once via the Supabase SQL editor, not committed as a permanent
  fixture): attempt an `INSERT`/`UPDATE` that violates the formula, confirm Postgres raises
  `23514` (check_violation), then roll back the test transaction. Document the exact commands
  and the exact expected error in the migration file's own comment, same spirit as
  `READONLY_GUARANTEE.md`'s evidence trail for Phase 1.
- **Live verification:** the manual SQL check above, plus confirming a subsequent live delivery
  run still inserts real shift reports successfully (it will — the formula is satisfied by
  construction).
- **Rollback:** `alter table shift_reports drop constraint shift_reports_grand_total_formula;`
  — trivial, safe, no data changes needed either direction.
- **Risk: low.**

---

## 5. Test plan (summary — full detail is in §4 per change)

New tests, by file (exact counts to be finalized during implementation, none touching
`test_golden.py`):

- `test_orchestrator.py`: H2 (aged-out gap detection, ×3+), H3 (coverage-gap detection, ×4+,
  including the explicit no-false-positive-on-quiet-shift case).
- `test_notifier.py`: H4 (`claimed_at` written on claim, stuck-row anomaly raised/not
  re-raised/not raised-too-early, ×3+).
- `test_report.py`: H3's new `STATUS_INCOMPLETE` priority tests, mirroring the existing
  `STATUS_NO_DATA` pattern added in `28cdc72` (×4+).
- `test_delivery.py`: H1 (workflow YAML `timeout-minutes` present and sane, ×1).
- Manual, not pytest: H5 (live Postgres constraint check, documented not automated).

Acceptance for this section: full suite green at whatever the new total becomes, golden
**unchanged at 31**, no pyodbc in the cloud closure, no secret-shaped strings in any changed
file, no locked file touched except `report.py` (H3) with the same explicit review-and-document
discipline as `28cdc72`.

---

## 6. Explicitly out of scope for this phase

Per "do not blindly expand scope":

- `internal_anomalies` has no unique/dedup constraint (re-confirmed §3.F) — real, but not one
  of the four flagged limitations. Optional follow-up, not bundled here.
- Late-arriving invoices never retroactively correct an already-sent shift report (flagged in
  the prior audit, §7 of `AUG11_FALSE_GREEN_AUDIT.md`) — unrelated to the four limitations,
  left as-is.
- The report's visual layout for the subtractive formula (§3 of the prior audit, an owner
  decision already recorded, still open, still not this phase's job).
- Any dashboard, WhatsApp channel, or owner-activation-sequence work — unrelated to hardening,
  explicitly deferred per the project's own standing "deliberately deferred" list.

---

## 7. Owner decisions required before/during implementation

1. **H3's 20-minute coverage-gap threshold** — sanity-check against how this specific
   restaurant's network/agent actually behaves day to day (a look at real `heartbeats.ok=false`
   frequency before picking the final number would strengthen this beyond "reasoned default").
2. **H4's migration** (`outbox.claimed_at`) — needs to be applied via the Supabase SQL editor
   before the corresponding code deploys (strict ordering, §4.4).
3. **H5's migration** (`grand_total` CHECK) — same SQL-editor application pattern, no ordering
   dependency with any other change.
4. Whether to also close the two carried-forward, out-of-scope items from the prior audit
   (report layout, `internal_anomalies` dedup) in the same pass or defer them further — not
   required for this phase's acceptance criteria either way.

---

## NEXT CHAT START HERE

**Current commit:** `e17692f` (`origin/main`, verified aligned this session).

**Current test counts:** 433 passed (full suite), 31 passed (`test_golden.py`) — both
independently re-verified this session, not inherited from a prior claim.

**Current production state:** Phase 2 delivery live (`*/15` cron), owner NOT yet activated
(`go_live_at` still `NULL`), only the developer chat (`notify_before_golive=true`) receives
messages. False-green pre-install bug fixed and live-verified (`28cdc72`). Four hardening items
(H1–H5, this document) are **designed but not implemented**.

**Known risks (unchanged from `AUG11_FALSE_GREEN_AUDIT.md` §24, this session added no new
ones beyond confirming the same four in more depth):** mid-shift partial coverage undetected
(H3 fixes), no DB-level formula guard (H5 fixes), Telegram crash-window tracking gap (H4
fixes), 14+ day silent aging (H2 fixes), plus this session's own new finding: no
`timeout-minutes` on the delivery workflow job (H1 fixes).

**Exact task sequence for the next chat:**
1. Read this document in full, then re-verify §0's starting state is still current (git HEAD,
   test counts, `gh run list`) before touching anything — the same discipline this session
   applied to the previous session's claims.
2. Implement H1 first (lowest risk, no dependencies, immediate value — bounds every subsequent
   change's blast radius if something goes wrong mid-implementation).
3. Implement H2 (no migration, no ordering dependency, reuses already-loaded state).
4. Implement H3 (no migration; get the owner's read on the 20-minute default per §7.1 before
   or shortly after landing it).
5. For H4: write and land the migration SQL file first, get the owner to apply it via the
   Supabase SQL editor, **verify live that `outbox.claimed_at` is selectable**, only then
   implement and deploy the `_claim()`/anomaly code.
6. For H5: write the migration SQL file, get the owner to apply it, run the manual verification
   SQL from §4.5, confirm a live delivery run still succeeds afterward.
7. Full suite + golden re-run after every step, not just at the end.
8. Update `AUG11_FALSE_GREEN_AUDIT.md`-style evidence (or a new dated report) once H1–H5 are
   live-verified, and update G-Brain again per the same propagation rules this document's own
   G-Brain updates followed.

**First command/file the next agent should inspect:** `git log --oneline -5` and
`python -m pytest -q` to re-confirm §0 hasn't drifted, then this document's §4 for the exact
per-change design, starting with H1's one-line YAML change.

**Constraints (unchanged, non-negotiable):** never touch the POS; never rotate/modify the
Telegram token or any GitHub Secret; never weaken an existing safety gate; the six
Phase-1-locked files (`adapter_hdsoft.py`, `metrics.py`, `events.py`, `report.py`,
`test_golden.py`, `schema.sql`) require review before any edit — `report.py` and `schema.sql`
are the only two this plan touches, both with the same explicit-review-and-document pattern
already used in `28cdc72`; never declare something fixed without a test or a documented live
verification; keep `test_golden.py`'s count at exactly 31.

**Definition of done for this hardening phase:**
- H1–H3 implemented, tested, pushed, and live-verified (dry-run + at least one real delivery
  run showing the new behavior, or its correct absence on a known-clean shift).
- H4 and H5's migrations applied by the owner and live-verified independently of the code that
  depends on them; their code implemented, tested, pushed, and live-verified after.
- Full suite green at its new total, golden unchanged at 31, no pyodbc, no secrets exposed, no
  unreviewed locked-file edit.
- A dated audit/verification report (new file, following the `AUG11_FALSE_GREEN_AUDIT.md`
  naming pattern) documenting exactly what was verified and how, same tagging discipline
  (VERIFIED/FIXED/SAFE BY DESIGN/LIMITATION/NOT VERIFIED/OWNER DECISION REQUIRED).
- G-Brain updated: session log appended, `Phase-2-Handoff.md` (or a new
  `Phase-2-Hardening-Handoff.md` if the next session judges the existing note has grown too
  large) refreshed, `index.md` and `_CLAUDE.md` Active Context refreshed — read back after
  writing to confirm a cold session could continue from them alone, exactly as this document's
  own G-Brain update was verified (see the session log for the read-back confirmation).
