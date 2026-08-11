# POSentine — Deep Audit H6–H8 & H9–H50+ Failure-Mode Classification

**Date:** 2026-08-11 (Session 6, same day as the H1–H5 hardening)
**Companion report:** `reports/phase2/HARDENING_FINAL_REPORT.md` (H1–H5 detail)
**Scope:** independent verification of the H1–H5 claims, three new hardening items (H6–H8), and the mandated second-pass production-readiness review with full failure-mode classification.
**Every claim is tagged:** VERIFIED / FIXED / SAFE BY DESIGN / LIMITATION / NOT VERIFIED / OWNER DECISION REQUIRED / EXTERNAL ACCESS REQUIRED / OUT OF SCOPE / NOT SAFE TO AUTOMATE.

---

## 1. Executive summary

The POSentine delivery system was re-audited from the actual code, not from prior summaries. The H1–H5 implementation was independently confirmed in the tree (suite 467 → then 484 after this session's additions; golden 31 unchanged). The second-pass review found **no additional FIX NOW items**; three genuinely useful hardening items were implemented, tested, and independently reviewed with no blocking findings:

- **H6 — monthly-report silent miss fixed.** The monthly report was day-1-only: one missed day-1 run (GitHub schedule delay, Supabase outage, failed workflow) silently dropped the month's report forever. Now built on days 1–5 unless the outbox already holds its dedup key (bounded catch-up + already-enqueued gate; also kills the old 96×/day month-fetch storm on day 1).
- **H7 — repository security guards pinned.** Docs/ (twice carried real credentials) and `data UN/` (real customer POS exports) were guarded by .gitignore habit alone. A new `test_security_guards.py` pins the exclusions, asserts zero tracked files under them, and scans every tracked file for secret *values* — with falsifiers.
- **H8 — heartbeat-gap statistics for the owner's threshold check.** The 20-minute coverage threshold (H3) was a reasoned default pending real data. The dry-run now prints the inter-heartbeat gap distribution and the selected shift's own max gap, so the owner can judge the threshold against reality (hardening plan §7.1).

Also independently verified (and thereby confirmed) a claim left in the G-Brain handoff by a read-only Session 5: `pos_users.updated_at` / `pos_products.updated_at` freeze after first insert. **Confirmed TRUE by code** (payloads omit `updated_at`, schema has no trigger, merge-duplicates only writes provided columns) — and confirmed **functionally inert** (no consumer of those columns anywhere; `is_modifier` is a GENERATED column recomputed from updated fields). Classified LIMITATION, not a fix.

**Status: PASS WITH LIMITATIONS.** No known high-severity correctness, false-green, duplicate-send, lost-delivery, or silent-failure path remains in the audited scope. The remaining gates are owner actions (commit/push, apply two migrations, threshold sanity-check against live heartbeat data, number-match against يومية الخزينة).

## 2. Starting state (VERIFIED)

- Git: HEAD `08ed0b6` (doc-only), `origin/main` = `08ed0b6`, working tree carrying the H1–H5 changes uncommitted.
- Suite **467 passed**, golden **31 passed** — re-run and confirmed before any change.
- GitHub Actions `delivery` runs all green (16–26 s), latest `31465699434` completed success 2026-08-11 06:36Z; all 5 secrets present (presence only, never values).
- H1–H5 implementation present in the tree (timeout-minutes: 10; `_aged_out_gaps`; `_max_heartbeat_gap_minutes`/`STATUS_INCOMPLETE`; `_claimed_at_supported`/`scan_stuck_sending`; schema_v5/schema_v6 files).

## 3. Final state (VERIFIED)

- Suite **484 passed** (+17: H6 9, H7 7, H8 4 — including the strengthened falsifier), golden **31 unchanged**.
- New files: `reports/phase2/DEEP_AUDIT_H6_PLUS.md`, `test_security_guards.py`; modified: `orchestrator.py`, `delivery.py`, `test_orchestrator.py`, `test_delivery.py`.
- Locked files (`adapter_hdsoft.py`, `metrics.py`, `events.py`, `schema.sql`, `test_golden.py`) — zero diff. `report.py` still carries only the Session-4 H3 change. (VERIFIED via `git diff --name-only`.)
- `git diff --check` clean; workflow YAML parses; secret scan clean except intentional test samples in `test_security_guards.py`.

## 4. H1–H5 verification (what the tests prove — and do NOT prove)

| H | Implementation verified in tree | Tests prove | Tests do NOT prove |
|---|---|---|---|
| H1 | `timeout-minutes: 10` on `deliver` | the bound exists and is sane (real runs 14–26 s; bound < 15-min cron) | that GitHub's runner actually enforces it (GitHub-documented, OUT OF SCOPE to test locally) |
| H2 | `_aged_out_gaps` + `shift_gap_aged_out`, pre-install/no-heartbeat guards, re-arm dedup | gap detection bounds, pre-install exclusion, dedup/re-arm | production data content of a real gap (live behavior unobserved until push) |
| H3 | heartbeat continuity → `STATUS_INCOMPLETE`; priority cash > no-data > gap > notes > stable | 20-min inclusive boundary, sub-threshold no-false-positive, quiet-shift stable, outside-window ignored | that 20 min matches real `heartbeats` cadence — **this is exactly what H8 now measures** |
| H4 | `claimed_at` write + `stuck_sending` scan + feature probe | pre-migration code is byte-identical behavior (probe off → claim omits column, scan off); dedup via open anomaly set | live PostgREST behavior after the migration is applied (owner must apply + verify) |
| H5 | schema_v6 CHECK (idempotent, exact-equality) + structural guard test | migration files are additive, idempotent, and pinned against drift; exact equality provably safe for sums of `numeric(12,2)` (≤2 decimals always) | the 23514 manual check against live rows (OWNER ACTION REQUIRED) |

## 5. H6 — monthly-report catch-up (FIXED, VERIFIED locally)

- **Problem:** monthly report built only on local day 1; `_load_state` fetched the previous month's data only on day 1. One missed day-1 run = the month's report silently lost forever.
- **Root cause:** two day-1-only gates (`_build_monthly` `day != 1`; `_load_state` `day == 1`).
- **Fix:** `MONTHLY_CATCHUP_DAYS = 5`; `_build_monthly` builds on days 1..5 unless `MONTHLY_DEDUP` key is in `DBState.sent_monthly_keys`; `_load_state` probes the outbox (`tenant_id` only — **outbox has no `source_id` column**, VERIFIED) and skips the whole-month fetch once enqueued.
- **Bonus:** eliminates the day-1 month-fetch storm (was re-fetching a whole month of rows every 15 min on day 1; now exactly once until the row lands).
- **Idempotency:** build gate + outbox UNIQUE `(tenant_id, channel, recipient, dedup_key)` double protection; `apply()` unchanged (insert_ignore).
- **Tests (+9):** plan-level days 1/2/6, already-sent skip, `_load_state` gating ×3, delivery e2e catch-up (day 3 → built+sent), delivery e2e grace-closed (day 8 → none), dry-run heartbeat print.
- **LIMITATIONS (reviewer-confirmed, documented in code):** (a) a monthly row that went `dead` still occupies its UNIQUE key → catch-up never rebuilds it; that recipient permanently misses the month (surfaced by telegram_403 / last_error; the dead-letter design never auto-resents). (b) a recipient activated after the first build never receives that month (single-shot semantics, unchanged from the day-1 design).

## 6. H7 — repository security guards (FIXED, VERIFIED locally)

- **Problem:** secret-bearing paths guarded by .gitignore habit only; a deleted line or a single `git add -A` publishes real credentials (Docs/ carried the monitor_ro SQL password and the JWT/service_role key; `data UN/` holds real customer POS exports).
- **Investigation (VERIFIED):** the odd `.gitignore` line `data UN/` is **not a typo** — a real directory named `data UN` exists on disk and `git check-ignore -v` confirms the pattern is honored. No leak. No tracked file under Docs/ or data.
- **Fix:** `test_security_guards.py` (7 tests): pins `.gitignore` lines (Docs/, data UN/, ship/, diagnostics_*, config.json, .env, *.token/pem/key), asserts zero tracked files under them, scans every tracked file for secret *value* patterns (bot token, JWT, private key, GitHub PAT, Slack, Stripe), and includes falsifiers (patterns match real-shaped credentials; the same predicate fires on a mutated .gitignore).
- **Test doubles note:** the tracked-file scan uses `git ls-files` (local-only, acceptable).

## 7. H8 — heartbeat-gap statistics (FIXED, VERIFIED locally)

- **Problem:** H3's 20-minute threshold is a reasoned default (≈6.7× the 3-min cadence); no tool produced the real numbers for the owner's §7.1 sanity-check.
- **Fix:** pure `heartbeat_gap_stats()` (count, window span, max adjacent gap, counts of gaps ≥ 15/20/30) printed in **both** dry-run paths — `orchestrator.main`'s `_print_plan` and `delivery.py`'s dry-run (the workflow's actual path) — plus the selected shift's own max in-window gap and a `FLAGGED/ok` verdict against `COVERAGE_GAP_MINUTES`. Read-only, secret-free.
- **Tests (+4):** regular cadence (max ≤ 3 min), outage counting at each threshold, too-few heartbeats judges nothing, empty/unsorted inputs safe; delivery-level test asserts the block prints and that dry-run still writes nothing.

## 8. Additional hardening items discovered (H9–H50+ classification)

Legend: **FIXED** this phase · **SAFE BY DESIGN** (guarded or bounded intentionally) · **LIMITATION** (accepted, documented) · **NOT VERIFIED** (needs live evidence) · **OWNER DECISION REQUIRED** · **EXTERNAL ACCESS REQUIRED** · **OUT OF SCOPE** · **NOT SAFE TO AUTOMATE**.

| # | Failure mode | Classification | Evidence / guard |
|---|---|---|---|
| H9 | Telegram 429 | SAFE BY DESIGN | `retry_after` honoured, bounded attempts, backoff cap 30 s |
| H10 | 403 revoked bot / bad chat | FIXED | distinct `forbidden_403` + `telegram_403` internal anomaly |
| H11 | Telegram timeout/network | SAFE BY DESIGN | bounded retries; lost-response retry may duplicate (rare, documented); timeout 30 s |
| H12 | Duplicate sends | SAFE BY DESIGN | outbox UNIQUE + claim-before-send (`sending` never a claim surface) + concurrency `cancel-in-progress: false`; residual: transport-retry duplicate accepted & documented |
| H13 | Lost sends | SAFE BY DESIGN | crash-before-mark = lost by design (never duplicate); H4 makes orphans visible |
| H14 | Crash after send before ack | FIXED (H4) | `claimed_at` + `stuck_sending` anomaly (migration v5 pending owner apply) |
| H15 | Stuck outbox rows | FIXED (H4) | scan surfaces rows orphaned >15 min; never auto-resends |
| H16 | Corrupted outbox state | NOT VERIFIED | no corruption detector; rows are simple; LOW risk — no known write path corrupts them |
| H17 | Heartbeat gaps | FIXED (H3) | `STATUS_INCOMPLETE` for ≥20-min in-window silence |
| H18 | False heartbeat health | SAFE BY DESIGN | beats fire on fixed cadence regardless of sales; `ok=false` notes mirrored as anomalies (id-keyed) |
| H19 | Partial shift coverage | FIXED (H3 + is_partial) | straddle/no-coverage recorded-not-reported; mid-shift gap → incomplete |
| H20 | Complete shift, zero invoices | SAFE BY DESIGN | `STATUS_NO_DATA` is a distinct status; the false-green regression test pins it |
| H21 | Pre-install shifts | FIXED | `no_coverage` partial (28cdc72) + H2 pre-install exclusion + no-backfill design |
| H22 | Post-install shifts | SAFE BY DESIGN | deterministic closed-window selection with 14-day lookback |
| H23 | DST/fold/gap | SAFE BY DESIGN | zoneinfo; both Cairo seasons pinned by tests; fold=0 documented |
| H24 | Midnight crossing | SAFE BY DESIGN | selection is closed-window based, never clock-now |
| H25 | 7 AM / 7 PM boundaries | SAFE BY DESIGN | boundary tests at both DST seasons |
| H26 | Missing shift boundaries | SAFE BY DESIGN | deterministic `_closes_at` from tenant config |
| H27 | Duplicate shift reports | SAFE BY DESIGN | PK + dedup key + insert_ignore |
| H28 | Duplicate invoices | SAFE BY DESIGN | agent upserts on `salid` (merge-duplicates) |
| H29 | Missing invoices | SAFE BY DESIGN | monotonic watermark; ceiling+backlog anomaly; deletion detected only while agent fresh |
| H30 | Delayed invoices | SAFE BY DESIGN | late data still lands in its shift when the report is finally built; straddle → partial |
| H31 | Malformed invoice data | SAFE BY DESIGN | NULLs handled explicitly; `unknown_item`/`unknown_sal_t` anomalies; never coerced to 0 |
| H32 | Financial inconsistencies | FIXED (H5 pending apply) | formula `grand_total = sales + collections − returns − delivery` golden-pinned; CHECK migration created |
| H33 | grand_total drift | FIXED (H5 pending apply) | schema_v6 idempotent CHECK; exact equality provably safe for numeric(12,2) sums |
| H34 | Returns | SAFE BY DESIGN | `return` kind classified; golden-pinned |
| H35 | Delivery | SAFE BY DESIGN | `delivery` kind & cost subtracted; golden-pinned |
| H36 | Collections | SAFE BY DESIGN | included per golden formula |
| H37 | Mixed payment methods | SAFE BY DESIGN | kind classification (cash/external/return/other) |
| H38 | Cash drawer mismatch | FIXED | `cash_diff`/`cash_no_count` events + report notes; `assert_no_accusation` guard |
| H39 | Stale data | SAFE BY DESIGN | heartbeat-freshness gates deletion; dead_man at 60 min |
| H40 | Late-arriving data | SAFE BY DESIGN | wall-clock windows; shifts reported when closed, never before |
| H41 | DB transaction failures | SAFE BY DESIGN | conflict-ignoring writes; monotonic watermarks; next cycle re-reads |
| H42 | Concurrent workers | SAFE BY DESIGN | single-instance lock + stale takeover; GitHub concurrency group; overlap costs work, not wrong data |
| H43 | Race conditions | SAFE BY DESIGN | constraints are the arbiter; no read-then-write |
| H44 | Retry storms | SAFE BY DESIGN | max 3 attempts, backoff ≤30 s, dead-letter |
| H45 | Unbounded retries | SAFE BY DESIGN | dead after 3 |
| H46 | Silent failures | FIXED | loud config checks, fail-loud workflow, anomaly surfaces |
| H47 | Misleading "stable" | FIXED (H3) | stable only when data + coverage + no cash anomaly |
| H48 | False-green reports | FIXED | 28cdc72 (pre-install) + H2 (aged gaps) + H3 (mid-shift gaps); no known residual |
| H49 | False-red reports | SAFE BY DESIGN | partial/pre-install never reported; thresholds have margin; no-data is distinct |
| H50 | Insufficient observability | IMPROVED (H8) | dry-run gap stats; residual: dead-row anomaly (see H56) |
| H51 | Monthly report silent miss | FIXED (H6) | 5-day catch-up + already-enqueued gate |
| H52 | .gitignore regression | FIXED (H7) | pinned by tests with falsifiers |
| H53 | GitHub schedule-event delays | LIMITATION | observed 63–87-min gaps (02:51→04:18→05:33→06:36); GitHub-documented high-load delay; each run still delivers; NOT a false-green |
| H54 | Outbox unbounded growth | LIMITATION | no retention sweep; ~1 KB/row, low volume; **H6's `sent_monthly_keys` depends on rows persisting** (coupling documented) — any future retention must preserve monthly rows |
| H55 | pos_users/pos_products.updated_at frozen | LIMITATION | payloads omit `updated_at`; no trigger; merge never touches it — **no consumer anywhere** (grep-verified), `is_modifier` is GENERATED and still updates; telemetry-only |
| H56 | Non-403 dead outbox rows have no anomaly | LIMITATION | only 403 raises an anomaly; a 5xx-exhausted dead row is visible only in the table — FUTURE RECOMMENDATION: a dead-row scan anomaly |
| H57 | H6 dead/recipient edges | LIMITATION | documented in code + report §5 |

## 9. Root causes (of everything fixed in H6–H8)

- H6: day-1-only gates in two places (`_build_monthly`, `_load_state`) — a single point of schedule failure with no recovery path.
- H7: security relied on human discipline around .gitignore, not on a mechanism.
- H8: the threshold decision had no instrumentation feeding it real data.

## 10. Fixes — see §5–§7.

## 11. Tests added

- `test_orchestrator.py` +9: H6 plan-level (day 1/2/6, sent-key skip), `_load_state` gating ×3, H8 stats ×4 (incl. empty/unsorted).
- `test_delivery.py` +3: day-3 catch-up e2e, day-8 grace-closed e2e, dry-run heartbeat coverage print (still zero writes).
- `test_security_guards.py` +7: exclusions, tracked-set assertions, secret-value scan, falsifiers ×2.

## 12. Test results (VERIFIED)

- Full suite: **484 passed** (was 467; +17). Golden: **31 passed** (unchanged). `git diff --check` clean.

## 13. Production verification

- VERIFIED: workflow runs all success (16–26 s), latest 06:36Z; secrets present by name.
- NOT VERIFIED: live behavior of H6–H8 (uncommitted/unpushed; dry-run will validate against live data the moment the owner runs it). **No live Telegram send was performed this session** — the only approved path for that is the existing workflow.

## 14. Supabase verification

- VERIFIED (read-only, via code + fakes): outbox has no `source_id` column (H6 probe uses `tenant_id` only); outbox UNIQUE key; no outbox retention.
- OWNER ACTION REQUIRED: apply `schema_v5_outbox_claimed_at.sql`, verify `select id, status, claimed_at from outbox limit 1`, then rely on H4's scan; apply `schema_v6_grand_total_check.sql` + the in-file manual 23514 check. No service_role access exists in this environment — **no migration was applied**.

## 15. Telegram verification

- Code path re-verified: 4096 truncation with marker; 429 retry-after; 403 classified; bounded retries; masking of chat ids and redaction of the token in every error surface.
- No send performed (not authorized/safe from here); H4's stuck-sending scan remains the observability backstop for crash-orphaned sends.

## 16. Git / GitHub state (VERIFIED)

- HEAD `08ed0b6`; working tree carries all H1–H8 changes (uncommitted); no new commits made (no write permission; committing locally was deferred — the owner's single `git add -A && git commit && git push` covers everything).
- GitHub write: OUT OF SCOPE / EXTERNAL ACCESS REQUIRED.

## 17. Migrations

| File | Purpose | Status | Risk |
|---|---|---|---|
| schema_v5_outbox_claimed_at.sql | `outbox.claimed_at` + H4 stuck-scan | CREATED, NOT APPLIED | additive, idempotent, rollback documented; code ships safely either way (feature probe) |
| schema_v6_grand_total_check.sql | `grand_total` CHECK | CREATED, NOT APPLIED | idempotent; exact-equality provably safe for all code-written rows; manual 23514 check required |

## 18. Security findings

- VERIFIED: `data UN/` honored by git; Docs/, ship/, diagnostics_* excluded; no tracked secret values (H7 scan); workflow secrets by name only; no credential literals; masking/redaction present in agent, orchestrator, notifier, and delivery logs.
- LIMITATION: none new. No secrets printed, exposed, or rotated.

## 19. Reliability findings

- Strongest guarantees: constraint-arbitrated idempotency; bounded retries with dead-letter; crash-safe claims (loss never duplication); monotonic watermarks; fail-loud config and workflow; 10-min job timeout.
- LIMITATIONS: schedule-event latency (H53); unbounded outbox (H54); dead-row observability (H56).

## 20. Data-integrity findings

- Formula `grand_total = sales + collections − returns − delivery` — code source `metrics.py`, pinned by `test_golden.py` (31/31), verified against the shop's own يومية الخزينة screen in Phase 1 (HARDENING_FINAL_REPORT.md). H5 CHECK pending apply.
- No false-total path found in the audited scope.

## 21. False-green analysis

Every path by which a report could look trustworthy while data is untrustworthy, and its guard:
1. Pre-install zero-data reported "stable" → **FIXED** (28cdc72 `no_coverage` partial) + H2 anomaly.
2. Mid-shift heartbeat outage → **FIXED** (H3 `STATUS_INCOMPLETE`).
3. Aged-out missing shifts → **FIXED** (H2 `shift_gap_aged_out`).
4. Monthly report missing → **FIXED** (H6 catch-up).
5. Stuck/crash-orphaned delivery → **FIXED visibility** (H4).
6. No residual path identified. NOT VERIFIED: production behavior post-push.

## 22. False-red analysis

- Partial and pre-install shifts are recorded but never reported. Coverage gap requires ≥20-min in-window silence (≥6 missed 3-min cycles) with real invoices; sub-threshold single-cycle misses explicitly tested as non-flagging. Dead_man needs 60 min of silence.
- OWNER DECISION REQUIRED: confirm 20-min/15-min thresholds against real heartbeat cadence using H8's dry-run output (the threshold itself remains NOT VERIFIED against live data until then).

## 23. Delivery / idempotency analysis

- Verified: UNIQUE-constraint arbitration; claim-before-send; FIFO; daily cap counts sent-at; 4096 single-message truncation; gate re-check as defense-in-depth; H6 build-gate + UNIQUE double protection.
- Accepted residual: transport-retry rare duplicate (documented in `send_message`); crash-loss (never duplication) made visible by H4.

## 24. Concurrency analysis

- Agent: single-instance lock, 15-min stale takeover, monotonic sync_state writes (`lt` filters), idempotent upserts — overlap costs work, never wrong data.
- Cloud: one workflow run at a time (`cancel-in-progress: false`); H1 timeout bounds hangs; double-run tests prove row-level idempotency end-to-end.

## 25. Failure-mode analysis — see §8 table (H9–H57).

## 26. Remaining risks

1. H4/H5 migrations unapplied (owner) — code safe either way, features dormant until then.
2. Thresholds (20-min coverage, 15-min stuck) NOT VERIFIED against live heartbeat data — H8 now measures it.
3. GitHub schedule latency (H53) — operational latency, not correctness.
4. H6's two documented edges (dead monthly row; mid-window recipient addition).
5. Outbox growth / dead-row observability (H54/H56) — future phase.

## 27. Owner decisions required

1. Commit + push the working tree (all H1–H8).
2. Apply schema_v5 then schema_v6; verify each; run the 23514 check.
3. Run `python delivery.py --dry-run`; read the heartbeat-coverage block; confirm or adjust COVERAGE_GAP_MINUTES/STUCK threshold.
4. Decide on future: dead-row anomaly scan (H56), outbox retention preserving monthly rows (H54), `updated_at` fix (H55, telemetry-only).
5. Next gate: number-match a delivered report against يومية الخزينة, then set `go_live_at`.

## 28. External permissions required

- GitHub write (push) — EXTERNAL ACCESS REQUIRED.
- Supabase service_role (apply/verify migrations) — EXTERNAL ACCESS REQUIRED.
- Live Telegram send — only through the existing workflow; not performed this session.

## 29. Things intentionally NOT changed

- Locked files (except the Session-4 `report.py` H3 status, already documented).
- Dead-letter semantics (no auto-resend — the accepted anti-duplicate trade-off).
- Threshold values (owner check first, per §7.1 of the plan).
- No new dependencies; no POS write path; no pyodbc in the cloud closure (re-tested).
- The `updated_at` freeze (H55): zero consumers, telemetry-only — documented, not churned.

## 30. G-Brain records created/updated (VERIFIED read-back)

- `Logs/2026-08-11.md`: Session 6 checkpoint (renumbered to avoid colliding with the handoff's Session-5 label), per-H detail, verification state, owner actions.
- `Projects/POSentine/Phase-2-Handoff.md`: status line rewritten for H1–H8 + Session-5 claim confirmation.
- `index.md` line 92 and `_CLAUDE.md` line 79: suite 484/31, H1–H8 implemented, DEEP_AUDIT report pointer.

## 31. Exact commits

- None new. HEAD remains `08ed0b6`; all H1–H8 changes are uncommitted in the working tree. The single owner commit will contain: workflow timeout, orchestrator/notifier/report hardening, both migrations, and all tests (H1–H8).

## 32. Exact evidence

- `git status --short`, `git diff --stat` (809 insertions H1–H5; +17 tests this session), `git diff --check` clean.
- `python -m pytest -q` → 484 passed; `test_golden.py` → 31 passed.
- `gh run list` (green runs, 16–26 s), `gh secret list` (5 names).
- `git check-ignore -v "data UN/"` → `.gitignore:20`.
- grep evidence for H55 (payloads/triggers/consumers) and outbox-retention absence.

## 33. Recommended next steps

1. Owner: commit + push; apply v5 + v6; verify.
2. Owner: dry-run; read H8 heartbeat block; sanity-check thresholds.
3. Then: number-match a delivered shift report against يومية الخزينة.
4. Future phase candidates (all safe, in-scope): dead-row anomaly scan (H56), outbox retention preserving monthly rows (H54), optional `updated_at` maintenance (H55).

## 34. Additional improvements independently recommended

- A `dead`-row anomaly scan (analogous to H4's stuck-sending) so non-403 delivery deaths are surfaced, not just stored.
- An outbox retention policy that preserves `monthly_products` rows (H6 coupling) and optionally compresses `sent` rows after N days.
- Consider surfacing GitHub schedule latency in the dry-run (expected vs actual run spacing) for operator awareness.

## 35. FINAL STATUS

**PASS WITH LIMITATIONS.**

- No known high-severity correctness issue, false-green path, duplicate-send path, lost-delivery path, or silent-failure path remains in the audited scope (VERIFIED against code + tests; live behavior post-push NOT VERIFIED by design).
- Every discovered fixable bug has regression coverage (H1–H8, 484/31).
- Production-safe migrations are created, documented, and owner-ready; not applied (EXTERNAL ACCESS REQUIRED).
- G-Brain carries durable checkpoints; documentation reflects reality; the git state is understood.
- The honest, bounded remainder is owner execution, not code.
