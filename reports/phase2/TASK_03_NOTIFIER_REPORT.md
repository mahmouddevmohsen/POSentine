# POSentine Phase 2 — Task 03 Report

## 1. Task
- **Exact task name:** Task 3 — Implement `notifier/telegram.py`, the only component that talks to the Telegram Bot API.
- **Exact scope authorized by me (Mahmoud):** Cloud-side only; claim pending outbox rows before sending (a crash mid-send cannot re-send); respect the recipient/alert gates (`active` → `go_live_at` → `alert_settings.notify`, with `notify_before_golive` bypassing only the go-live gate); retry with backoff on 429 (honouring `retry_after`) and 5xx; never retry 400/403; classify 403 distinctly (the user never pressed Start) and record it as `telegram_403`; never log the bot token, service role key, or a full chat id; decide and implement the 4096-character handling; respect the daily cap of 3 when draining an alert backlog (Task 2 review note). No POS connection, no pyodbc, no modification of locked files, `test_golden.py` must remain exactly 31. Do NOT commit, do NOT push, do NOT start Task 4.

## 2. Objective
Build the sender half of Phase 2 delivery: take the `outbox` rows the orchestrator enqueues and deliver them to Telegram with a crash-safe claim step, correct retry/classification, gate re-checking (defense-in-depth), cap-aware backlog draining, and secret-safe logging — all unit-tested with no network, matching the project's "evidence, not memory" discipline.

## 3. Files Inspected
- `Docs/CLAUDE_CODE_PHASE2_PROMPT.md` — the Task 3 spec (a–e) and Task 5 test list.
- `Docs/PHASE_2_DELIVERY_PLAN.md` — gates, recipient seeding, go-live transaction.
- `schema.sql` — outbox status lifecycle, `ix_outbox_queue` claim surface, `internal_anomalies` `telegram_403` kind, `recipients`/`alert_settings`/`tenants` shapes.
- `orchestrator.py` (Task 2) — `TenantContext`/`Recipient`/`eligible_recipients` (the gate logic reused as the single source of truth), `Envelope`/dedup keys, `apply` semantics.
- `supa.py` — `Supa` client API (`select`, `count`, `update` with PostgREST filters, `insert`), redaction conventions, retry conventions.
- `events.py` — `DAILY_ALERT_CAP`, `assert_no_accusation`, internal-anomaly kind vocabulary.
- `report.py` — fixed-template structure (grand-total line position informed the 4096 decision).
- `test_orchestrator.py` — test conventions, the closure-walker pattern with its falsifier.
- `requirements-cloud.txt` — requests/pytest/tzdata (no pyodbc).

## 4. Files Created
- `notifier/__init__.py` — package marker.
- `notifier/telegram.py` (~340 lines) — the notifier.
- `test_notifier.py` (~680 lines) — **30 tests**.
- `reports/phase2/TASK_03_NOTIFIER_REPORT.md` — this file.

## 5. Files Modified
- None. No tracked file was modified (verified by `git diff --stat` empty and `git status` showing only untracked new paths).

## 6. Files Deleted
- None.

## 7. Implementation Details

### 7.1 Shape
- **`run(client, *, tenant_id, token, source_id=None, now_utc=None, session=None, sleep=None, max_attempts=3, dry_run=False) -> Summary`** — one delivery pass: load context → count sent alerts today → **claim** → **gate** → **cap** → **send** → **mark**. Fails loudly (raises) on infrastructure problems: a missing `tenants` row, a count that refuses a total. The token never leaves the environment; `session`/`sleep` are injectable for tests (defaults: `requests.Session()`, `time.sleep`).
- **`send_message(token, chat_id, text, *, session, sleep, max_attempts, timeout)`** — one Telegram `sendMessage` with bounded retry and explicit classification (`TelegramError.kind`: `forbidden_403 | permanent_400 | rate_limited | server_error | transport | exhausted`).
- **`apply_4096_policy(body, limit=4096)`** — the 4096 decision (spec e).
- **`gate_check(ctx, row)`** — defense-in-depth re-check in the approved order, reusing `orchestrator.eligible_recipients`.
- **`main()` CLI** — `--tenant-id`, `--source-id`, `--dry-run`, `--max-attempts`; env-only secrets (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `TELEGRAM_BOT_TOKEN`, `TENANT_ID`, `SOURCE_ID`).

### 7.2 Claim before send (spec a)
`_claim` = one `PATCH outbox SET status='sending' WHERE tenant_id=… AND channel='telegram' AND (status IN ('pending','failed')) ORDER BY created_at ASC` (PostgREST `or=(…)` + `order`, `return=representation`). The returned rows are additionally **sorted by `created_at` in Python** so the FIFO drain order never depends on the gateway honouring `order` on PATCH. Claiming first means a crash after claim but before mark leaves the row in `sending`, which is never a claim surface — no re-send (the accepted cost: a row left in `sending` by a crash is lost, never duplicated). Failed rows are reclaimed (retry); sent/dead/sending are not. FIFO order makes the backlog drain oldest-first, which is what the cap logic below needs.

### 7.3 Gates (spec b)
The gates were applied at enqueue by the orchestrator; the notifier re-checks them per claimed row because config can change between enqueue and claim (a deactivated recipient, a flipped notify flag). The eligible-recipient logic is **`orchestrator.eligible_recipients`** — imported, never re-implemented, so the two components cannot drift. Order: `active` (never bypassed) → `go_live_at` (bypassed only by `notify_before_golive`) → `alert_settings.notify` (alerts only; the type is parsed from the `alert:{type}:…` dedup key). A gate failure marks the row `failed` with the reason in `last_error` and counts toward the 3-attempt → dead bound, so a permanently-blocked row cannot spin forever.

### 7.4 Retry / classification (spec c)
- **429** → sleep exactly Telegram's `parameters.retry_after` (JSON body), retry. Tested with `retry_after=7` → `sleep(7.0)`.
- **5xx** → exponential backoff (0.5 s base, 30 s cap), retry.
- **Transport errors** (timeout/connection) → backoff, retry. Documented trade-off: a lost response can mean the message was delivered, so a retry can duplicate; reports beat the rare duplicate.
- **400/403 → never retried** within the run. **403 is raised as its own kind** and additionally writes an `internal_anomalies` row with `kind='telegram_403'` and a **masked** recipient in the detail — the single most likely go-live failure must be obvious, never look like a network problem.
- Per-run retries are bounded by `max_attempts` (default 3); the whole run counts as **one** outbox attempt. `attempts` increments per failed run; at 3 the row goes `dead` (matches the schema lifecycle `failed(≤3) → dead`). Tested over four consecutive failing runs: `failed, failed, dead, dead` and the dead row is never claimed again.

### 7.5 Daily cap backlog (Task 2 review note)
The cap is the sender's final authority on what the owner actually receives: it counts `kind=alert AND status=sent AND sent_at >= tenant-local midnight` (sent-based, not the orchestrator's enqueue-time count). `cap_room = max(0, 3 − sent_today)`. Claimed alert rows beyond the room are **reverted to `pending`** (a day boundary, not a failure — attempts untouched) and picked up tomorrow in FIFO order. Shift reports and monthly products are never subject to the alert cap. Tested: cap exhausted → all alerts deferred, shift report still sent; room for 2 → the two oldest alerts sent, newest deferred; over-cap → everything deferred.

### 7.6 The 4096 decision (spec e) — **decided: truncate with an explicit marker, never split**
`apply_4096_policy` measures UTF-16 code units (Telegram's real limit — an emoji is 2 units, so a code-point count would silently under-count). Over the limit, the head is kept and the marker `⚠️ الرسالة أطول من الحد المسموح — تم قصّها` is appended, always visibly.

**Why truncation and not splitting into several messages:**
1. The outbox row is the unit of delivery — one row, one message, one status. Splitting would either mark the row sent before all parts landed, or re-send the whole row on a mid-split failure (duplicate fragments). Truncation keeps the lifecycle atomic and the retry story simple.
2. Realistic reports (fixed Arabic templates, at most 5 notes) are ~1,500–2,000 units — far under 4096. Only a pathological report can overflow.
3. In the shift-report template the decision-relevant numbers (grand total, invoice counts) sit in the first two thirds, so a truncated report still carries the essential facts — and the marker never hides that something was cut.

### 7.7 Secrets (spec d)
- Token and service-role key exist only as environment variables (GitHub Secrets). Never in source, files, logs, or the DB.
- `redact(text, token)` is applied to every error surface before it is stored in `last_error` or printed.
- `mask_chat_id` (`'9876543210' → '98…10'`) is applied to every log line and every `telegram_403` anomaly detail. Tested by planting the token inside a transport-error message and asserting it never reaches `last_error` or the summary log.
- `assert_no_accusation` runs on the outgoing body inside `send_message` — the single choke point before the text leaves the process — as defense-in-depth (the orchestrator already asserts at build time).

### 7.8 Dry run
`--dry-run` prints what a pass would do (rows, gate-blocked flags, full Arabic bodies) and claims/sends/writes nothing — matching the workflow's `dry_run` semantics.

## 8. Tests and Validation (all actually executed)
- `python -m pytest test_notifier.py -q` → **30 passed in 0.35s**.
- `python -m pytest -q` → **401 passed in 6.30s** (was 371 at Task 2; the 30 new tests are the only addition).
- `python -m pytest -q test_golden.py` → **31 passed in 0.06s** (untouched — exactly 31).
- `python -m notifier.telegram --help` → usage prints; `import notifier.telegram` → ok.
- **Falsifier proven:** `test_closure_walker_can_detect_pyodbc` (a temporary module importing `pyodbc` is caught) runs alongside `test_no_pyodbc_in_notifier_import_closure`. The notifier's actual transitive import closure is `{__future__, argparse, collections, dataclasses, datetime, events, json, metrics, orchestrator, os, report, requests, rows, supa, time, typing, zoneinfo}` — no pyodbc, no adapter_hdsoft, no agent.
- Covered behaviors: claim touches only pending/failed (a `sending` row is never re-claimed — crash-safe); FIFO claim order; success marks `sent` + `sent_at`; inactive recipient blocked; owner blocked pre-go-live while the dev bypass sends; missing recipient row blocked; `alert_settings.notify` off blocks alert rows; 429 honours `retry_after`; 5xx backs off; 400/403 never retried; 403 recorded distinctly (`telegram_403`, masked recipient); **403 vs transport proven distinct end to end through `run()`** (1 call + anomaly vs 3 retried calls + no anomaly); transport retries then fails with the token redacted; dead after 3 attempts then never claimed; cap exhausted defers alerts (shift report still sent); cap room sends oldest alerts first; over-cap defers everything; dry run claims/sends nothing; missing `tenants` row raises loudly; full pass (shift + alert + monthly in FIFO); and the **Task 2 → Task 3 seam**: the orchestrator's fake run enqueues the pending shift report and the notifier delivers it with the verified Arabic body.

## 9. Security / Guardrail Verification
- **No POS write capability introduced:** True — the notifier's import closure is cloud-only (closure test above); no SQL Server connection exists anywhere in it.
- **No pyodbc in Phase 2 delivery components:** True — closure verified; `requirements-cloud.txt` (requests, pytest, tzdata) has no pyodbc.
- **No secrets logged:** True — token/service-role key never logged; `redact` on every error surface; chat ids masked in logs and anomalies.
- **No secrets hardcoded:** True — source contains no token, key, or real chat id value (`grep` for the seeded chat id, the exposed bot token prefix, and key names returned nothing in the new files).
- **No JWT secret changed:** True — nothing rotated or re-minted.
- **No client-side POS configuration modified:** True — nothing touched outside the repository.
- **No protected/locked file modified:** True — `git diff --stat` empty; `schema.sql`, `metrics.py`, `events.py`, `report.py`, `adapter_hdsoft.py`, `test_golden.py` untouched; `test_golden.py` exactly 31.

## 10. Git State
- Current branch: `main`; HEAD `7eb1796` (unchanged — nothing committed).
- Working tree: clean of tracked changes; **untracked**: `notifier/`, `orchestrator.py`, `test_notifier.py`, `test_orchestrator.py`, `reports/` (Tasks 1–3 reports).
- Committed: nothing. Pushed: nothing. `main...origin/main` shows no divergence.

## 11. Problems / Risks
- **Not yet run against live Supabase as `service_role`, and no real Telegram message has been sent.** The unit/fake suite passes, but the real PostgREST `PATCH … or=(…)` claim syntax and a real `sendMessage` are unproven until the workflow (Task 4) runs a `dry_run` and then the first real send to the developer chat. "Not verified yet" is the honest status.
- **Transport-retry duplicate risk (documented decision):** a timeout/lost-response retry can deliver a message twice. Accepted: reports beat the rare duplicate; Telegram's `message_id` in the response would allow a future dedup task.
- **Dead rows are final:** a row that reaches `dead` (3 failed attempts) will not be re-sent even if the underlying problem is fixed (e.g., the user presses Start after a 403 run exhausted the attempts). The orchestrator will not re-enqueue it (dedup). If this becomes a real-life pain, the owner can clear/backfill the row manually — noted, not changed.
- **Cap semantics differ slightly between components by design:** the orchestrator counts alerts by enqueue time (`created_at`), the notifier by send time (`sent_at`). Under normal operation they agree; after an outage the notifier's count is the final authority and holds the owner's inbox to 3/day. The seam test pins the enqueue→deliver flow.
- **`--max-attempts` doubles as the dead threshold** in a run (attempts reaching it ⇒ `dead`). Coherent, but a future operator must not raise it casually — raising it raises how many times a stuck row is tried before dying.

## 12. Decisions Required From Me
1. **Approve the 4096 handling decision: truncate with a visible Arabic marker, never split** (reasoning in §7.6). If you prefer splitting into multiple messages, say so and I will rework it with the duplicate-fragment trade-off spelled out.
2. **Approve the gate re-check (defense-in-depth)** — the notifier re-checks `active`/`go_live_at`/`alert_settings.notify` at claim time by reusing the orchestrator's `eligible_recipients`, and marks blocked rows `failed` (→ dead after 3). This is a hardening layer beyond the orchestrator's enqueue-time gates; it changes nothing for the normal flow.
3. **Approve the sender-side cap** (sent-based, FIFO, over-cap alerts deferred back to `pending`).
4. Note: real-network verification (claim syntax, first real send) can only happen once Task 4's workflow exists and the five GitHub Secrets are set — and the dev recipient row's `notify_before_golive` must be true in the live DB (per the delivery plan's step-2 INSERT, execution state unknown).

## 13. Final Status
**COMPLETE** — 30 new tests, **401 total passed**, golden **31 passed**, closure proven fail-able and clean, no secret values anywhere, no locked file touched, nothing committed or pushed. (Review-hardened after implementation: `assert_no_accusation` wired into the send choke point; FIFO claim order guaranteed in Python rather than left to PostgREST; the fake client now honours filters so tests cannot diverge from real PostgREST behaviour.) The only unverified surfaces are the live-Supabase claim syntax and a real Telegram delivery, both belonging to Task 4's dry-run/first-send.

## 14. Next Step
**Task 4 — `.github/workflows/delivery.yml`**: schedule every 15 minutes + `workflow_dispatch` with `dry_run` (default true), `force_shift`, `shift_date`; runs the orchestrator then the notifier; the five GitHub Secrets. **Not executed — stopping here for your approval, per the brief.**
