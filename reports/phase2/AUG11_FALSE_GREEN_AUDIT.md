# POSentine — Forensic Correctness Audit & Hardening (2026-08-11)

Scope: the Aug 9/10 Telegram-vs-POS discrepancy, the 1,140 EGP internal contradiction, and
a full false-green/data-completeness audit of the Phase 2 delivery pipeline, per
`Docs/CONTEXT.md`. Every claim below is tagged **VERIFIED**, **FIXED**, **SAFE BY DESIGN**,
**LIMITATION**, **NOT VERIFIED**, or **OWNER DECISION REQUIRED** — never asserted bare.

---

## 1. Executive summary

Two separate things were tangled together in the original report and had to be pulled apart:

1. **The 1,140 EGP "contradiction" is not a bug.** `grand_total = sales + collections −
   returns − delivery` (not a naive sum of the four listed lines) is the correct, verified,
   already-implemented formula. It reproduces both Telegram totals in the brief to the exact
   EGP. **VERIFIED**, no code change.

2. **A real, live, actively-recurring false-green bug was found and fixed.** The orchestrator
   had no gate against reporting a shift that ended before the agent ever started collecting
   data. Because there is no historical backfill by design, such a shift always computes to
   zero invoices — and the report builder had no branch for "zero data" other than falling
   through to "🟢 الوردية مستقرة". This was not a hypothetical: it was reproduced against
   live production Supabase data (§19) — three all-zero "stable" shift reports had already
   been generated and sent (to the developer's own test chat; the real owner is not yet
   live). **FIXED**, live-verified as stopped (§20).

The August 9/10 number differences described in the brief are explained by two unrelated,
correctly-functioning mechanisms, not by any defect: (a) the formula in point 1, and (b) the
POS screenshot being a different reporting window than the Telegram shift (§6) — this part
remains **NOT VERIFIED** because it requires a photo comparison only the shop can provide,
exactly as `PHASE_2_DELIVERY_PLAN.md` already specifies as the pending gate.

---

## 2. Root cause of the August 9/10 discrepancies (as reported to Telegram)

Both "differences" cited in the brief between the Telegram report and the POS screenshot are
consistent with the shift window / accounting-formula explanations below, not with a bug in
what was computed. No code path was found that miscalculates a shift's own numbers. **VERIFIED**
for the formula; **NOT VERIFIED / OWNER DECISION REQUIRED** for the POS-screen comparison,
because it needs the shop's own screen, which no code here can obtain.

---

## 3. The 1,140 EGP internal Telegram contradiction — solved

Reported: `sales 15,695 + delivery 570 + receipts 2,915 = 19,180`, but the report says
`total 18,040`. Difference: 1,140.

**Root cause: the four line items are not meant to be summed together.** The real formula,
implemented at `metrics.py:185` and documented in that file's own header (verified against HD
Soft's "يومية الخزينة" screen line-for-line before any code was written, per
`Docs/HANDOFF_TO_NEXT_AI.md`):

```
grand_total = sales + collections − returns − delivery
```

Applying it to the exact numbers in the report: `15,695 + 2,915 − 0 − 570 = 18,040` — exact
match, to the EGP. The same formula also reproduces the "previous" report in the brief exactly:
`15,950 + 3,940 − 0 − 765 = 19,125`.

**Why delivery and returns subtract instead of add:** delivery revenue is already inside
`sales` (it is collected through the till as a cash-kind invoice with a `delivery_cost`
sub-amount), so `delivery` is subtracted to isolate the till's own net cash position — it is
a deduction line, not a separate income line. Returns are subtracted for the ordinary reason
(money given back). `collections` (مقبوضات, `kind='external'` invoices) is added because it is
money that came in through a channel other than a cash-kind sale.

**What was actually wrong:** the *report's layout*, not its arithmetic. Listing the four
fields vertically with a separator line before "الإجمالي" visually implies a plain sum, and
nothing in the text says otherwise. This is exactly the failure mode section 2 of the brief
warned about: "the code must never label them in a way that implies they are additive."

**Fix:** none applied to the formula (it is correct and already reviewed/locked). The layout
question — whether to add an explicit note or visual cue distinguishing the subtractive lines
— is **OWNER DECISION REQUIRED**: it is a communication/UX choice about a Phase-1 locked
template (`report.py`), not a correctness defect, and changing fixed Arabic template wording
needs the same "no AI touches what the owner reads without review" discipline that produced
the golden tests. Recommendation if asked: prefix `دليفري` and `مرتجع مبيعات` with a small
`−` marker, or add a one-line formula footnote. **NOT IMPLEMENTED — awaiting your call.**

## 4. The 1,610 EGP previous discrepancy — explained

The "previous" Telegram report's own four fields are **internally consistent** under the same
formula (§3: 15,950 + 3,940 − 0 − 765 = 19,125, exact). There is no internal contradiction in
that report. The 1,610 EGP gap the brief describes is between that Telegram total and a
**POS daily-treasury screenshot** for a different figure — that is a cross-window comparison
question (§6), not a second instance of the same arithmetic bug. **VERIFIED** (formula side);
**NOT VERIFIED** (which POS aggregation window the screenshot actually represents — requires
the shop's own screen).

---

## 5. THE FALSE-GREEN BUG — full account

### 5.1 The defect

`orchestrator.py`'s shift-report builder computed an `is_partial` flag meant to suppress
delivery for a shift the agent could not have fully observed:

```python
is_partial = (state.first_sync_at is not None
              and start <= state.first_sync_at.astimezone(tz).replace(tzinfo=None) < end)
```

This only catches the **one** shift whose window *straddles* `first_sync_at` (the shift
during which the agent was installed). A shift whose window closed entirely **before**
`first_sync_at` computes `is_partial = False` and flows through the normal path.

Because `agent.py` adopts `MAX(salid)` on its first run and reads nothing behind it — "no
backfill, by design" (`installer.py`, `VERIFY.md` step 4a, `Docs/HANDOFF_TO_NEXT_AI.md`) —
every such pre-install shift is **guaranteed** to compute zero invoices, not just "probably
quiet." `report.py`'s `pick_status()` had no branch for zero data: with no cash-diff and no
notes, it falls straight through to `STATUS_STABLE = "🟢 الوردية مستقرة"`.

Combined with `MAX_SHIFT_LOOKBACK_DAYS = 14` and `select_shift()` returning one missing shift
per orchestrator pass (most-recently-closed-and-missing first), a fresh install with an empty
`shift_reports` table walks backward through history, one shift per cron tick, generating and
enqueuing a fully green "stable, zero-difference" report for every closed shift the agent
never actually watched. **VERIFIED, BUG, FIXED.**

### 5.2 Live proof (not hypothetical)

Dispatched the pre-existing read-only diagnostic (`diagnostics_forensic.py` via
`.github/workflows/diagnostics-oneoff.yml`, SELECT-only, structurally guarded) against
production Supabase twice during this session (runs `31459618884`, `31462948321`). Real
`shift_reports` rows, before the fix was pushed:

| shift | sales | n_cash | is_partial | outbox status |
|---|---|---|---|---|
| 2026-08-08 evening | 0.0 | 0 | **False** | **sent** (2026-08-11T00:35:18Z) |
| 2026-08-08 morning | 0.0 | 0 | **False** | **sent** (2026-08-11T02:52:01Z) |
| 2026-08-07 evening | 0.0 | 0 | **False** | **sent** (2026-08-11T05:34:05Z) |

All three were built with every financial field at exactly zero, `is_partial=False`, enqueued,
and actually delivered to Telegram (`message_id` confirmed by the notifier's own send path) —
three live, real "🟢 الوردية مستقرة" messages for shifts the agent never observed, roughly
2–3 hours apart, actively continuing at the moment this audit started. The real Aug 9/10
shifts in the same table (`2026-08-09 evening`, correctly `is_partial=True`; `2026-08-10
morning`/`evening`, real data) show the mechanism works correctly for the shifts it *was*
designed to catch — the gap was specifically the "entirely before, not straddling" case.

**Blast radius was contained by luck, not by design:** `tenants.go_live_at` is still `None`
(confirmed in the same diagnostic run) — the real owner recipient is gated off
(`eligible_recipients`, `orchestrator.py`), so only the developer's own test chat received
these. Had go-live already happened, the owner would have received the same three messages,
and — per `MAX_SHIFT_LOOKBACK_DAYS=14` — roughly 20+ more were queued to follow over the next
several days. **VERIFIED** from live data.

### 5.3 The fix

- `orchestrator.py`: `is_partial` now covers two cases — `straddle` (unchanged) and
  `no_coverage` (new): `end <= first_sync_at`, i.e. the whole window closed at or before the
  agent's first sync. Both are recorded (so the lookback scan stops retrying them) and never
  enqueued, exactly like the existing straddle case already did.
- `report.py`: `pick_status`/`pick_summary`/`build_shift_report` gained a `has_data` parameter
  (`m.total_invoices > 0`). Zero invoices now produces a new, distinct
  `STATUS_NO_DATA = "⚪ لا توجد بيانات كافية لهذه الوردية"` instead of falling through to
  `STATUS_STABLE`. Priority order is deliberate: a real cash-count discrepancy (`cash_counts`
  is an independent data source from invoices) still outranks "no data" — a cashier can log a
  count with zero sales, and that must never be masked.
- `schema.sql`: the `is_partial` column comment updated to match (documentation only — the
  column and its live semantics already exist; no migration required or applied).

This closes the *proven* case exactly (shift entirely before install) and the *general* case
(zero invoices for any reason — outage, agent down, POS unreachable) with the same one-line
invariant: **a shift can only be called "stable" if at least one real invoice was observed in
its window.** This is not a new arbitrary rule; it is the brief's own stated principle
("No comparison data ≠ stable") applied literally at the one place it was missing.

### 5.4 What the fix does NOT cover — honest limitation

A shift that starts and ends entirely **after** `first_sync_at`, but during which the agent
was down for **part** of the window (crashed at 22:00, resumed at 02:00, shift is 19:00–07:00)
will show a **nonzero but undercounted** `total_invoices`. It will still be reported
`STATUS_STABLE` if nothing else flags it. Nothing in this fix (or, before it, anywhere in the
codebase) cross-checks per-shift invoice count against heartbeat continuity across that
specific window — the existing freshness gates (`HEARTBEAT_FRESH_MINUTES`,
`DELETION_RESCAN_MAX_AGE_MINUTES`) protect the *deletion-detection* feature only, evaluated at
report-build time, not shift-covering-window time. **LIMITATION, NOT FIXED.** A proper fix
would need to record heartbeat gaps and cross-reference them against each shift's
`[window_start, window_end)` before deciding `has_data` — a bigger, riskier change than
today's proven bug warranted under "smallest change that solves the problem." Flagging for a
follow-up rather than rushing an unproven heartbeat-continuity gate into the same push.
**OWNER DECISION REQUIRED**: worth a dedicated follow-up task, not bundled here.

---

## 6. Shift-vs-daily window analysis

The Telegram report is an explicit 12-hour shift window (`metrics.py`: morning `[07:00,19:00)`,
evening `[19:00,07:00)` next day, tenant-local wall clock, no hardcoded UTC offset —
`zoneinfo.ZoneInfo(ctx.timezone)` used throughout `orchestrator.py`). The POS screenshot in
the brief is described as a "daily treasury" view. Whether that view aggregates the whole
calendar day, both shifts, or some other HD Soft-internal window is **not something any code
in this repo can determine** — it depends entirely on what that specific HD Soft screen
computes internally, which is out of this system's observation. `VERIFY.md` §10 already states
the correct acceptance procedure: compare `shift_reports.grand_total` against HD Soft's own
"يومية الخزينة" for the *same shift*, photographed at the shop. That comparison has not been
done for Aug 9/10 (per `reports/phase2/FINAL_AUDIT_AND_TELEGRAM_VERIFICATION.md` §28-29, still
open as of this session). **NOT VERIFIED — OWNER DECISION REQUIRED** (needs a shop visit/photo,
not more code).

---

## 7. Agent installation / backfill analysis

- Install: 2026-08-10, ~17:30 Cairo local (`Docs/HANDOFF_TO_NEXT_AI.md`, corroborated by the
  live heartbeat log: first productive heartbeat `id=4` at `2026-08-10T14:30:47Z` =
  `17:30:47` Cairo). **VERIFIED.**
- No backfill by design: `agent.py` adopts `MAX(salid)` on first run, reads nothing behind it
  (`installer.py`, `VERIFY.md` step 4a). **VERIFIED, SAFE BY DESIGN** — this is the correct,
  deliberate behavior; the bug was never in the collector, only in the reporter's assumption
  that "no invoices" always means "verified zero."
- How the system now distinguishes "shift existed before install" from "current collection":
  `orchestrator.py`'s broadened `is_partial` (§5.3), using `state.first_sync_at` (earliest
  heartbeat) against the shift's own `[start, end)`. **FIXED, VERIFIED** by the new
  regression tests (`test_shift_entirely_before_install_is_never_reported`,
  `test_multi_shift_backfill_never_leaks_a_stable_report`,
  `test_shift_boundary_ending_exactly_at_first_sync_has_no_coverage`).
- Late-arriving/backfilled data during normal operation (a rescan bringing in an invoice whose
  `sold_at` falls in an *already-reported* shift): the `shift_reports` PK
  `(tenant_id, source_id, shift_date, shift_name)` with `insert_ignore` means the report is
  never recomputed once sent — a late invoice does not retroactively change a delivered
  number. This is **SAFE BY DESIGN** against sending a second, different total for the same
  shift, but it is also a **LIMITATION**: if a legitimately late invoice arrives after the
  report was already sent, the owner never sees the correction. No mechanism recomputes or
  flags a stale already-sent shift report. Not addressed in this pass (out of the proven bug's
  scope) — **OWNER DECISION REQUIRED** on whether a correction message is wanted.

---

## 8. Data lineage (per financial field)

Traced end-to-end by direct code read (`rows.py`, `metrics.py`, `orchestrator.py`,
`report.py`, `schema.sql`) — **VERIFIED**:

| Field | POS source | Classification rule | Cloud column | Aggregation | Report line |
|---|---|---|---|---|---|
| sales | `Sales.total` where kind='cash' | `kind` set by the adapter: return first, then salT=1→cash | `invoices.total` | `sum(total)` where kind='cash' (`metrics.py:169`) | `مبيعات` |
| collections | `Sales.total` where kind='external' | salT=2→external | `invoices.total` | `sum(total)` where kind='external' (`metrics.py:172`) | `مقبوضات` |
| returns | `Sales.total` where kind='return' | saleRtype=1 or saltype=1, checked first | `invoices.total` | `sum(total)` where kind='return' (`metrics.py:175`) | `مرتجع مبيعات` |
| delivery | `Sales.delivery_cost`, every invoice | n/a (summed regardless of kind) | `invoices.delivery_cost` | `sum(delivery_cost)` (`metrics.py:166`) | `دليفري` |
| grand_total | derived | `sales+collections−returns−delivery` | `shift_reports.grand_total` | `metrics.py:185` | `الإجمالي` |
| cash reconciliation | `SR.SrUserval`/app value | `SrUserval=0` → `no_count`, never a shortage | `cash_counts.user_value/app_value` | `detect_cash_diffs` (`events.py:166`) | `💵 الخزينة` line |
| n_cash/n_return/n_external | invoice count | same classification as sales/returns/collections | n/a (counted) | `metrics.py:170,176,173` | invoice-count lines |
| top products | `SaleDe` lines, joined `Items.Itid` (never `itcode`) | modifiers (`list_price=0`) excluded | `invoice_lines` | `top_items()` (`metrics.py:232`) | `🔥 أكثر 5 أصناف` |

Every field carries `shift_date`, `shift_name`, `window_start`/`window_end` (naive POS-local,
`rows.py`'s `pos_ts`), tenant timezone (`Africa/Cairo`, from `tenants.timezone`), and is
computed exclusively inside the shift's own `[start,end)` window (`orchestrator.py:378-379`).
No step in this chain mixes daily and shift aggregation, and no step silently defaults to
"today" — every query is parameterized by an explicit `(shift_date, shift_name)` pair chosen
by `select_shift()` before any data is read. **VERIFIED.**

---

## 9. Timestamp analysis

Two clocks, never mixed, enforced at the type level: `rows.py`'s `pos_ts()` raises on any
timezone-aware datetime; `utc_ts()` raises on any naive one. `orchestrator.py` uses
`zoneinfo.ZoneInfo(ctx.timezone)` throughout — no hardcoded UTC+3 anywhere in the delivery
closure (confirmed by direct read and by the existing DST test suite:
`test_dst_winter_boundary_selects_yesterday_morning`,
`test_dst_summer_boundary_selects_yesterday_morning`, and three more in `test_orchestrator.py`,
covering both the 07:00 and 19:00 boundaries in both DST regimes). Shift boundary tests
(06:59/07:00/18:59/19:00, midnight-crossing) exist at the `metrics.resolve_shift` level in
`test_golden.py`. **VERIFIED, SAFE BY DESIGN**, well covered before this audit — no changes
needed here.

---

## 10. Financial semantics — canonical formula

`grand_total = sales + collections − returns − delivery` (`metrics.py:185`). **VERIFIED**
against both live Telegram totals in the brief and the golden-test baseline (`19,205 = 19,205`
against HD Soft's own screen, pre-dating any code). This is the single authoritative formula;
no other formula exists anywhere in the codebase.

---

## 11. Reconciliation invariants

**No database-level CHECK constraint enforces the formula** — confirmed by an exhaustive grep
of every `check (` in `schema.sql` (9 total, all enum/range checks on `channel`/`kind`/
`level`/`status`/`shift_name`; none reference `grand_total`). The formula is Python-only
(`metrics.py:185`). A future write path (manual `UPDATE`, a different code path, a bug) could
in principle insert a `shift_reports` row whose `grand_total` doesn't match its own four
fields, and nothing in Postgres would refuse it. **LIMITATION — OWNER DECISION REQUIRED**: a
`CHECK (grand_total = sales + collections - returns - delivery)` constraint would close this
at the database layer, independent of application code. Not added in this pass — it requires
a live migration against production (`ALTER TABLE ... ADD CONSTRAINT`), which this audit
cannot apply itself (no local service-role credentials by design; see `Docs/HANDOFF_TO_NEXT_AI.md`
open risk #1). The migration SQL is a one-line addition if you want it; recommend adding it
via the Supabase SQL editor the same way `notify_before_golive` was added (§13 of the prior
audit).

---

## 12. Data completeness gates

- **Zero-invoice gate**: **FIXED** (§5).
- **Pre-install/no-coverage gate**: **FIXED** (§5).
- **Mid-shift outage with partial (nonzero) coverage**: **LIMITATION**, not fixed (§5.4).
- **"Shift closed" vs "shift data completely collected"**: before this fix, these were
  conflated for the pre-install case (closed ⇒ reported, regardless of whether it was ever
  watched). Now distinguished for the *zero-coverage* case; still conflated for the
  *partial-coverage* case (§5.4). **PARTIALLY FIXED.**

---

## 13. False-green prevention — direct answer to the brief's central question

Before this fix: **no.** A shift could be marked "🟢 الوردية مستقرة" purely because it had no
cash-diff and no notes, with zero regard for whether any invoice was ever observed. Proven
live (§5.2).

After this fix: a shift with zero observed invoices can no longer be reported `STATUS_STABLE`
— it is either suppressed entirely (pre-coverage case, recorded but never enqueued) or, for a
zero-invoice shift that *is* enqueued for some other reason not covered here, rendered with
the new `STATUS_NO_DATA` state instead. A real cash-count discrepancy still overrides both,
since it comes from an independent source. **FIXED for the proven case and the general
zero-invoice case. LIMITATION remains for partial (nonzero but undercounted) coverage
(§5.4).**

---

## 14. Zero-fill audit

Checked every place a missing value could silently become `0`:

- `rows.py`'s `line_payload`: an unknown item's `list_price` yields `NULL`, never `0` — the
  docstring explicitly calls this out ("coercing an unknown item to 0 would delete it from
  the detection quietly"). **SAFE BY DESIGN, VERIFIED.**
- `metrics.py`'s `_f`-equivalent inline `float(x or 0)` pattern (e.g. `total`, `delivery_cost`):
  these operate on values that are genuinely `0` when summed over zero invoices — not a
  "missing value coerced to 0" case, since the underlying invoice rows either exist (with a
  real total) or don't exist at all (nothing to sum). **SAFE BY DESIGN.**
- `events.py`'s cash-count classification: `SrUserval=0` is explicitly classified as
  `no_count` (the cashier didn't count), never treated as a real zero difference, both in
  detection (`events.py:186`) and in the report line (`report.py`'s `cash_line`, "⏸ لم يتم جرد
  الخزينة"). **SAFE BY DESIGN, VERIFIED**, and it is exactly this distinction that made the
  new `STATUS_NO_DATA` design correct: the codebase already had a working precedent for
  "no data ≠ zero" at the cash layer; this audit extended the same discipline to the invoice
  layer, where it had been missing.
- The one place zero-fill *was* silently happening is the one this audit fixed: a shift with
  zero real invoices produced a report whose zero *counts* were accurate (0 invoices really
  were seen) but whose *status text* implied a verified, checked "stable" — the text, not the
  numbers, was the zero-fill. **FIXED.**

---

## 15. Invoice count integrity

- Duplicate invoices: `invoices` PK is `(tenant_id, source_id, salid)` — rescans upsert on
  this key, cannot duplicate. **SAFE BY DESIGN, VERIFIED** (`schema.sql:153`).
- Deleted invoices: absence-based detection with a mandatory 30-minute stability guard
  (`events.py`'s `detect_deleted`, `DELETION_CONFIRM_MINUTES=30`) specifically to avoid
  `NOLOCK` reading a row mid-write and firing a false deletion alert. **SAFE BY DESIGN,
  VERIFIED.**
- Restored invoices / `salid` going backwards: `sync_state.restore_suspected` flag exists and
  is checked before deletion detection is even enabled (`orchestrator.py`'s
  `deletion_enabled` condition). **SAFE BY DESIGN, VERIFIED** from code; not independently
  re-tested against a real restore in this session — **NOT VERIFIED** end-to-end (would need
  a real or simulated backup restore, out of scope for a read-only audit).
- Salid gaps: documented as a known, harmless artifact of the bigint identity cache (jumps of
  ~9,999 after a service restart), explicitly NOT treated as evidence of deletion. **SAFE BY
  DESIGN, VERIFIED** (`events.py:236` comment, matches `Docs/HANDOFF_TO_NEXT_AI.md`).

---

## 16. Concurrency audit

From direct read of `.github/workflows/delivery.yml`, `supa.py`, `notifier/telegram.py`,
`orchestrator.py` (cross-checked by an independent research pass — see the parallel-agent
findings folded into this report):

- **Workflow-level**: `concurrency: { group: posentine-delivery, cancel-in-progress: false }`
  serializes every invocation (scheduled or manual) of the delivery workflow — no two runs
  execute in parallel. **VERIFIED, SAFE BY DESIGN.**
- **DB-level, independent of the above**: `shift_reports` PK, `outbox` UNIQUE
  `(tenant_id, channel, recipient, dedup_key)`, and `events` UNIQUE
  `(tenant_id, source_id, type, dedup_key)`, all written via `insert_ignore` (`ON CONFLICT DO
  NOTHING`), make duplicate shift reports and duplicate outbox enqueues impossible even if the
  workflow-level serialization were ever removed. **VERIFIED, SAFE BY DESIGN.**
- **Outbox claim**: `_claim()` is a single atomic `UPDATE ... WHERE status IN (pending,failed)
  SET status='sending' RETURNING *` — the status transition *is* the lock; a second concurrent
  claim simply matches zero rows already flipped. **VERIFIED, SAFE BY DESIGN**, backed by an
  existing test that starts a row at `status='sending'` and asserts it's never reclaimed.
- **The one table with no dedup protection**: `internal_anomalies` has no unique constraint at
  all. Two overlapping 403-handling passes (which the workflow concurrency group prevents
  today, but nothing in the DB itself prevents) could insert duplicate anomaly rows.
  **LIMITATION**, low severity (internal-only channel, not owner-facing), **NOT FIXED** in
  this pass — out of scope of the proven bug.
- **Daily alert cap** (`DAILY_ALERT_CAP=3`): correct only because of workflow-level
  serialization — the cap is computed once per notifier run against an in-process counter, not
  a DB-level `CHECK`/advisory lock. **SAFE IN PRODUCTION TODAY, LIMITATION IN THE ABSTRACT**:
  if the `concurrency:` block were ever removed, this cap could be exceeded. **NOT FIXED**
  (no evidence it needs to be — the workflow-level guard is real and intentional, per the
  workflow's own comment).

---

## 17. Failure/recovery matrix

| Scenario | Classification | Evidence |
|---|---|---|
| Supabase outage during read | SAFE — `supa.py` retries transport/5xx/429 up to 5 attempts, raises loudly on exhaustion; no partial write possible before any write is attempted | **VERIFIED** |
| Supabase outage during write (`insert_ignore`) | SAFE — same retry, and `on_conflict do nothing` makes a retried write idempotent | **VERIFIED** |
| Telegram 400/403 | SAFE — never retried, `403` recorded as a distinct `telegram_403` internal anomaly, row moved to `failed`/`dead` | **VERIFIED** |
| Telegram 429/5xx | SAFE — bounded retry with backoff (honors `retry_after` for 429) | **VERIFIED** |
| Transport error mid-send (response lost after Telegram accepted it) | **DUPLICATION** — explicitly documented and accepted in `notifier/telegram.py`'s own comment: a lost-response retry can re-POST the same message. Bounded to 3 attempts. | **VERIFIED, ACCEPTED TRADEOFF, not a new finding** |
| Process crash between successful Telegram send and the `status='sent'` DB write | **LOSS** (of tracking, not of the message) — the outbox row is permanently stuck at `status='sending'`, never reclaimed (by design — reclaiming it would risk a duplicate send), and nothing sweeps it back. The message *did* reach the owner; the system just forgets it sent it. No reconciliation job exists anywhere in the codebase for this. | **LIMITATION, NOT FIXED** — **OWNER DECISION REQUIRED** on whether a manual/periodic sweep is wanted |
| GitHub Actions retry / re-run | SAFE — idempotent by the same DB constraints | **VERIFIED** |
| Agent crash / POS unavailable | SAFE — agent-side, Phase 1, unaffected by this pass; watermark only moves forward, nothing lost | **SAFE BY DESIGN** (carried forward from Phase 1, not re-verified this session) |
| Partial rescan | SAFE — upsert on PK, no duplication | **VERIFIED** |
| Stale heartbeat | SAFE for deletion-detection (gated off); **LIMITATION** for shift-report completeness (§5.4) | **PARTIALLY FIXED** |
| Incomplete historical data (pre-install shift) | Was **FALSE GREEN** | **FIXED** (§5) |

---

## 18. Future failure scenarios (beyond today)

- **30 days of operation / month boundary**: the monthly top-products report
  (`_build_monthly`) only fires on local day 1, dedup-keyed `monthly:{year}-{month}` — safe
  against duplicate sends across a month boundary. **SAFE BY DESIGN**, not independently
  re-run this session (would need to fast-forward the clock; existing tests cover the
  day-1 gate structurally). **NOT VERIFIED against a real month rollover.**
- **14+ day outage**: `MAX_SHIFT_LOOKBACK_DAYS=14` means a shift closed more than 14 days ago
  is never picked up by `select_shift()` at all — it silently ages out of the lookback window
  with no report ever generated and no alert that it was skipped. **LIMITATION, NOT FIXED**:
  there is no "gap detected, N shifts silently skipped" signal anywhere. If the agent (or the
  cloud workflow) is down for more than 14 days, some shifts get no report and nothing tells
  anyone. **OWNER DECISION REQUIRED** on whether this deserves an explicit anomaly.
- **Large outbox backlog / repeated retries**: bounded by `MAX_ATTEMPTS=3` per row before
  `dead`; no unbounded retry loop exists. **SAFE BY DESIGN.**
- **Schema migration drift**: already observed once (`recipients.notify_before_golive` applied
  live but not committed to `schema.sql` until a later session) — a real, already-happened
  instance of "the committed schema disagrees with the live database." **VERIFIED as a
  process risk**, not a code defect. No migration-tracking mechanism exists (no Alembic/Flyway
  equivalent) — every migration is a hand-run SQL file. **LIMITATION.**
- **Timezone/POS clock change**: `orchestrator.py` reads `tenants.timezone` and uses
  `zoneinfo` — a timezone change is a data change, not a code change, and would be picked up
  automatically. POS clock drift is already monitored (`clock_drift` internal event,
  `CLOCK_DRIFT_WARN_SECONDS=300`). **SAFE BY DESIGN.**

---

## 19. Bugs found

1. **False-green shift reports for pre-install/uncovered shifts** (§5). Severity: high —
   proven live, actively recurring, would have reached the real owner once live. **FIXED.**

No other correctness bugs were found in the delivery/notifier/orchestrator closure during this
audit (concurrency, idempotency, retry classification, and timezone handling were all found
correct on direct read and cross-checked by an independent research pass — §16-17).

## 20. Bugs fixed

1. §5.3 — `orchestrator.py`'s `is_partial` broadened to cover shifts with zero coverage
   (`no_coverage = end <= first_sync_at`), not just the straddling shift.
2. §5.3 — `report.py` gained a distinct `STATUS_NO_DATA` state, threaded through
   `pick_status`/`pick_summary`/`build_shift_report` via a new `has_data` parameter
   (`m.total_invoices > 0`), with cash-diff still taking priority since it comes from an
   independent data source.

## 21. Tests added

15 new tests, none added to `test_golden.py` (its exact count, 31, is a documented
field-acceptance contract checked literally by installers — `VERIFY.md` step 3,
`preflight.bat`):

- `test_orchestrator.py` (+4): `test_shift_entirely_before_install_is_never_reported` (direct
  regression for the live bug), `test_shift_boundary_ending_exactly_at_first_sync_has_no_coverage`
  (half-open interval edge), `test_multi_shift_backfill_never_leaks_a_stable_report`
  (reproduces the exact live scenario — walks `plan()` repeatedly the way the cron does,
  asserts no pre-coverage shift is ever announced `STATUS_STABLE`).
- `test_report.py` (+11, new file — `report.py` had zero direct unit tests before this audit,
  only indirect coverage through orchestrator integration tests): pins existing
  `STATUS_STABLE`/`STATUS_CASH`/`STATUS_REVIEW` priority, covers the new `STATUS_NO_DATA`
  branch and its priority against cash-diff and notes, and one full `build_shift_report`
  integration test per state (zero-invoice, real-data, cash-diff, and the zero-invoice +
  real-cash-diff combination — proving cash reconciliation is never masked by "no data").

## 22. Full test results

```
python -m pytest -q test_report.py test_orchestrator.py   → 47 passed
python -m pytest -q test_golden.py                         → 31 passed  (unchanged)
python -m pytest -q                                        → 433 passed (418 → 433)
```

All re-run in this session, this session's working tree, not taken from any prior report.
**VERIFIED.**

## 23. Structural/security checks

- No `pyodbc` import in the cloud closure (`delivery.py orchestrator.py notifier/ report.py
  metrics.py events.py rows.py` plus both new/changed test files) — grep returned zero
  matches. **VERIFIED.**
- No secret-shaped strings (`\d{8,10}:[A-Za-z0-9_-]{30,}`, the Telegram-bot-token pattern) in
  any file touched this session. **VERIFIED.**
- No locked Phase-1 file touched except `report.py` (deliberate, documented in §3/§5.3/§21 —
  this audit *is* the review). `metrics.py`, `events.py`, `adapter_hdsoft.py`, `test_golden.py`
  confirmed byte-identical (`git diff --stat` empty for all four). **VERIFIED.**
- No token/secret rotated, deleted, or modified. No production data deleted. The only
  production-affecting action this session took was read-only (`diagnostics_forensic.py` via
  the pre-existing one-off workflow, SELECT/count only) plus the code push itself (§5.3),
  which changes future behavior, not existing rows. **VERIFIED.**

## 24. Remaining limitations (honest list)

1. Mid-shift partial outage with nonzero-but-undercounted invoices is not detected (§5.4).
2. No DB-level `CHECK` constraint enforces the `grand_total` formula (§11).
3. A crash between a successful Telegram send and the `status='sent'` DB write permanently
   loses tracking of that row — no reconciliation sweep exists (§17).
4. `internal_anomalies` has no dedup constraint (§16).
5. Shifts more than 14 days old silently age out of the lookback with no "gap detected" signal
   (§18).
6. Late-arriving invoices never retroactively correct an already-sent shift report (§7).
7. The Telegram report's visual layout still implies the four financial lines are additive,
   even though the underlying math is correct (§3) — a text/format question, not a bug.

## 25. Owner decisions required

1. §3 — whether to change the report's visual layout to mark delivery/returns as subtractive.
2. §6 — provide a يومية الخزينة photo for the shop's Aug 9/10 shifts to close the
   window-comparison question (this is the one item no code can resolve).
3. §11 — whether to add the `grand_total` `CHECK` constraint (one-line migration, needs to be
   applied via the Supabase SQL editor, same as the `notify_before_golive` column was).
4. §17 — whether a periodic sweep for stuck `status='sending'` outbox rows is wanted.
5. §18 — whether silently-skipped 14+ day-old shifts deserve an explicit anomaly.
6. Cleanup: the three already-sent zero-data reports in production
   (`shift_report:2026-08-07:evening`, `2026-08-08:morning`, `2026-08-08:evening`) are
   harmless (dev chat only, `go_live_at` still null) and were **not deleted** — evidence
   preserved per the brief's explicit instruction. Your call on whether to leave them as a
   permanent audit trail or manually clear them once reviewed.

---

## Final production readiness status

**The proven, live, actively-recurring false-green bug is fixed and verified stopped at the
code level** (pushed to `main`, commit `28cdc72`; the next cron cycle runs the corrected
logic). The 1,140/1,610 EGP "contradictions" are explained and are not bugs. Six real,
lower-severity limitations remain, none of them false-green risks, all documented above with
an explicit owner decision where one is needed. **The system can now distinguish a shift it
never observed from a shift it verified as stable — the specific class of contradiction this
audit was commissioned to close does not recur.** Whether the shop's own يومية الخزينة screen
agrees with `shift_reports.grand_total` for the same window (§6) is the one open question this
audit could not settle from code alone, and it was already the correctly-identified pending
gate before owner go-live.
