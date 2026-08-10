# POSentine Phase 2 — Task 04 Report

## 1. Objective
Build and verify the GitHub Actions cloud-delivery layer: `.github/workflows/delivery.yml` (the schedule + on-demand entry point), `delivery.py` (the composition root that runs orchestrator → notifier in one process), and `test_delivery.py` (the contract for both). Per the Task 4 spec: schedule every 15 minutes; `workflow_dispatch` with `dry_run` (boolean, **default true**), `force_shift` (choice none|morning|evening), `shift_date` (optional string); dry-run must build and print the full Arabic report to the job log and enqueue/send NOTHING; the five GitHub Secrets referenced by NAME ONLY; the job must **fail loudly** on any unhandled exception. Everything below is **VERIFIED IN CODE/TESTS** unless explicitly marked **NOT YET VERIFIED IN REAL CLOUD** — the workflow has NOT been pushed and has NOT executed.

## 2. Files Created
- `delivery.py` (new) — the cloud composition root (orchestrator → notifier in one process).
- `.github/workflows/delivery.yml` (new) — the GitHub Actions workflow.
- `test_delivery.py` (new) — **17 tests** (11 Task-4 core + 6 workflow-spec validation tests: 5 spec checks + 1 falsifier, added this session).
- `reports/phase2/TASK_04_GITHUB_ACTIONS_REPORT.md` — this file.

## 3. Delivery Architecture
```
GitHub Actions (delivery.yml, cron */15 or workflow_dispatch)
    → delivery.py (ONE process — a failure anywhere paints the run red)
        → orchestrator.py  (decide: most recent closed shift w/o report, day-1 monthly, sendable alerts;
                            write shift_reports/events/outbox/internal_anomalies via conflict-ignoring inserts)
        → notifier/telegram.py  (claim pending→sending, send via Telegram Bot API, mark sent/failed/dead)
    → Supabase (service_role, HTTPS only)  ·  → Telegram Bot API
```
The delivery system **never connects to the customer's POS**: no pyodbc, no SQL Server, no agent, nothing on the till — mechanically enforced (see §16).

## 4. delivery.py Behavior (VERIFIED IN CODE/TESTS)
- **Composition root**: one process, orchestrator then notifier; any exception exits non-zero (the workflow reports red).
- **`--dry-run`**: builds and prints the orchestrator's decided plan AND the notifier's would-send view, with the full Arabic report bodies; enqueues NOTHING, claims NOTHING, sends NOTHING (tests assert zero write calls and zero Telegram contacts).
- **`--force-shift morning|evening` / `--shift-date YYYY-MM-DD`**: passed through to the orchestrator's `force_shift`/`shift_date` (test pins the exact pair reaching the plan).
- **Loud configuration failure**: all five env vars are checked up front via `_require_env` (also in dry-run) — a missing secret raises `SystemExit` naming the GitHub Secret; a delivery system that silently succeeds without its configuration must not exist.
- **Environment-only secrets**: values come exclusively from `os.environ` (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `TELEGRAM_BOT_TOKEN`, `TENANT_ID`, `SOURCE_ID`); the CLI defaults `--tenant-id`/`--source-id` from env.
- **No hardcoded secrets**: no token, key, or chat-id value anywhere in the module or its tests (tests use explicit `FAKE_*` values).
- **Injectability for tests**: `client` / `session` / `sleep` / `now_utc` keyword arguments (same convention as `orchestrator.run` and `notifier.run`); production (the workflow) passes nothing.
- **Idempotency**: repeated live passes do not duplicate reports or re-send (see §17).
- `python delivery.py --help` prints the full CLI (verified).

## 5. Workflow Design (VERIFIED IN CODE/TESTS; EXECUTION NOT YET VERIFIED IN REAL CLOUD)
`name: POSentine delivery`; `on:` has `schedule` + `workflow_dispatch`; `permissions: contents: read` (least privilege); `concurrency: group posentine-delivery, cancel-in-progress: false` (never cancel an in-flight run — a cancelled send could leave an outbox row in `sending`); job `deliver` on `ubuntu-latest` with steps: Checkout (actions/checkout@v4) → Set up Python 3.12 (actions/setup-python@v5, pip cache on requirements-cloud.txt) → Install cloud dependencies → Verify configuration (all 5 secrets present, `::error::` + exit 1 otherwise) → Structural guard (no pyodbc in the delivery closure, `grep` + `::error::` + exit 1) → Resolve run mode (dispatch+dry_run=true ⇒ dry-run mode with a `::notice::`) → Run cloud delivery (builds ARGS, validates shift_date format, runs `python delivery.py`).

## 6. Cron (VERIFIED IN CODE)
`schedule: - cron: '*/15 * * * *'` — every 15 minutes. The GitHub scheduler may drift 5–20 minutes under load; the orchestrator's "most recent closed shift with no report yet" rule absorbs the drift by design (no attempt to fix scheduler drift — per spec).

## 7. workflow_dispatch (VERIFIED IN CODE)
`workflow_dispatch:` present with `inputs:` containing exactly the three spec inputs: `dry_run` (boolean, default true), `force_shift` (choice none|morning|evening, default none), `shift_date` (string, optional). Dispatch with `dry_run=false` runs LIVE delivery.

## 8. Dry-run (VERIFIED IN CODE/TESTS; NOT YET VERIFIED IN REAL CLOUD)
- Input `dry_run` defaults to **true** — the plan's safe default ("ابدأ دائمًا بـ dry_run: true").
- When dispatch + `dry_run=true`, the job sets `mode=dry-run`, prints a `::notice::` ("reports will be printed to this log; nothing will be enqueued and nothing will be sent"), and passes `--dry-run` to delivery.py.
- `test_dry_run_builds_and_prints_but_writes_nothing` and `test_dry_run_never_contacts_telegram` prove the behavior at the delivery.py level (zero writes, zero Telegram calls). The real job-log Arabic output against live data remains a **NOT YET VERIFIED IN REAL CLOUD** owner action.

## 9. Force-shift (VERIFIED IN CODE/TESTS)
Input `force_shift` = choice `none | morning | evening`, default `none`. The run step passes `--force-shift <value>` only when not `none`. `test_force_shift_and_shift_date_reach_the_orchestrator` pins the passthrough (`--force-shift morning --shift-date 2026-07-02` resolves `2026-07-02 morning`).

## 10. Shift-date (VERIFIED IN CODE/TESTS)
Input `shift_date` = optional string. The run step validates the format with a regex `^[0-9]{4}-[0-9]{2}-[0-9]{2}$` and exits with `::error::` on a mismatch before delivery.py runs. `test_invalid_shift_date_is_loud` pins delivery.py's own guard (`2026/07/02` → SystemExit).

## 11. Secrets (VERIFIED IN CODE; VALUES NOT YET VERIFIED IN REAL CLOUD)
Referenced **by NAME ONLY** — the names `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `TELEGRAM_BOT_TOKEN`, `TENANT_ID`, `SOURCE_ID` appear in the YAML as `${{ secrets.<NAME> }}` and in delivery.py/`_require_env`; no value appears in source, YAML, logs, or reports. **NOT YET VERIFIED IN REAL CLOUD:** whether the five secrets actually exist in `Settings → Secrets and variables → Actions` — only the owner can create them, and only a real workflow run proves them.

## 12. Security Guards (VERIFIED IN CODE/TESTS)
1. **No POS write capability**: delivery.py import closure is cloud-only — verified by the ast transitive closure test AND a falsifier (a corrupted copy with `import pyodbc` is caught — `test_closure_walker_can_detect_pyodbc`).
2. **No pyodbc in the delivery closure**: enforced by `test_no_pyodbc_in_delivery_import_closure` (forbidden: pyodbc, adapter_hdsoft, agent, fake_adapter, sqlguard) and by the workflow's own `grep` structural guard step.
3. **No secrets in source/tests/reports**: token/key/chat-id patterns are absent from delivery.py, test_delivery.py, the YAML, and the phase-2 reports (verified by pattern scan this session; tests use FAKE values only).
4. **Telegram token / service-role key never logged**: notifier applies `redact` on every error surface and `mask_chat_id` on every log line (Task 3, tested there); delivery.py prints only masked recipient ids (`recipient=****`) and fixed Arabic templates.
5. **JWT not rotated**: nothing rotated or re-minted; nothing touched on the till or in `config.json`.
6. **No client config changed**: no tracked file modified (see §21).

## 13. Tests (VERIFIED — ALL EXECUTED THIS SESSION)
`test_delivery.py` — **17 passed**: loud missing-config failures (URL, token even in dry-run, tenant id); dry-run builds+prints+writes-nothing; dry-run never contacts Telegram; live end-to-end (pending → sending → sent with sent_at, shift + monthly both delivered to the dev chat); **double-run idempotency** (seed-count-aware — see §17); force-shift/shift-date passthrough; invalid shift-date loud; closure falsifier; no-pyodbc closure; plus **6 new workflow-spec tests**: cron `*/15`; dispatch inputs with `dry_run` default true (block-scoped); force_shift/shift_date inputs + validation (block-scoped); secrets referenced by name only (exactly the five, both env blocks, and **no env line anywhere carries a literal value**); fail-loudly (`set -euo pipefail`, least-privilege permissions, concurrency no-cancel, standard job skeleton); and a **falsifier** (`test_workflow_spec_checks_can_fire`) that mutates in-memory copies — wrong cron, flipped dry-run default, corrupted force_shift options, and an injected `MY_TOKEN: abc-123` env line each trip their predicate.

## 14. Full-Suite Result (VERIFIED — EXECUTED THIS SESSION)
`python -m pytest -q` → **418 passed** (was 411/412 with 1 failure at the audited checkpoint; the failing `test_live_double_run_is_idempotent` was fixed, see §17; the 6 workflow-spec tests were added).

## 15. Golden Result (VERIFIED — EXECUTED THIS SESSION)
`python -m pytest -q test_golden.py` → **31 passed** — file untouched, exactly 31 as required.

## 16. No-pyodbc Closure (VERIFIED)
Real transitive closure of `delivery.py` = stdlib (`argparse, datetime, os, typing`) + `metrics, orchestrator, supa, notifier.telegram` (which pull `events, report, rows, requests, zoneinfo`) — **no pyodbc, no adapter_hdsoft, no agent, no sqlguard**. The falsifier proves the walker can fail. `requirements-cloud.txt` = `requests>=2.31, pytest>=8.0, tzdata>=2024.1` — no pyodbc, and **no dependency added this session** (the workflow-spec tests are dependency-free).

## 17. Idempotency (VERIFIED)
`test_live_double_run_is_idempotent` proves it through the real cloud entry point: the fake store is seeded with every already-reported shift in the 14-day lookback; run 1 creates **exactly one new** shift_report row (baseline + 1); run 2 adds nothing — the row count does not increase, the single shift_report outbox row is run 1's (`shift_report:2026-06-30:evening`, `sent`), the single monthly outbox row is run 1's (outbox UNIQUE blocked the re-enqueue), and run 2 claimed and sent nothing (`claimed=0 sent=0`). **The database constraints are the arbiter**, never read-then-write. (The audited failure was a test/fake-store contract mismatch — the assertion counted the fake's own seed rows — not a delivery defect; fixed by measuring the delta against the captured seed baseline instead of a hardcoded 1.)

## 18. Failure Behavior (VERIFIED IN CODE; NOT YET VERIFIED IN REAL CLOUD)
- The job **fails loudly**: every run step uses `set -euo pipefail`; an unhandled exception in orchestrator/notifier/HTTP layers exits non-zero and paints the job red.
- Missing configuration: the "Verify configuration" step exits 1 with a `::error::` naming the missing secret; delivery.py's `_require_env` is the second, in-process guard.
- A delivery system that fails silently is worse than one that does not exist — this is the design contract. Whether GitHub renders the red run correctly against the real repo is **NOT YET VERIFIED IN REAL CLOUD**.

## 19. Current Limitations
- **No real execution yet**: the workflow is untracked/unpushed, so GitHub Actions has never run it; no live dry-run output exists; the real service-role PostgREST reads/writes (claim `PATCH … or=(…)` syntax, `and=(…)`/`or=(…)` filters) are unproven against live Supabase.
- **Secret existence unknown**: the five GitHub Secrets are assumed by name; whether they are configured is an owner-side fact.
- **Dev recipient row state unknown**: `recipients.notify_before_golive = true` on the dev row (the delivery plan's step-2 INSERT) has unknown execution state in the live DB.
- **YAML syntax vs content**: GitHub's own parser is the ultimate workflow-syntax check and cannot run locally; the local tests pin the spec's content invariants, not arbitrary YAML well-formedness. Malformed YAML would surface as an invalid workflow the moment the file is pushed.
- **No secret scanning tool** (gitleaks/trufflehog) is wired in — out of Task 4's spec; flagged as a possible hardening item, not a deliverable.

## 20. Real-World Verification Status (NOT YET VERIFIED IN REAL CLOUD — ALL)
- GitHub Actions actually executed: **NO** (workflow never pushed; `gh run list` = zero runs).
- `dry_run` actually executed against live data: **NO** (only in unit tests with fakes).
- Supabase service-role execution succeeded: **NO** (the only live Supabase interaction remains the Task-2 read-only migration probe, HTTP 403/42501, agent token).
- Real Telegram message actually received: **NO** (no real sendMessage has ever been made).
- Production delivery succeeded / owner delivery: **NO.**
These become verifiable only through an owner-controlled run: push → `workflow_dispatch` with `dry_run=true` → inspect Arabic job-log output → `dry_run=false` to the dev chat → number-matching → owner activation (Task 5 / plan steps 5–6).

## 21. Git State (VERIFIED)
- Branch `main`, HEAD `7eb1796` ("chore: session log through the release gate") — unchanged, **nothing committed, nothing pushed** (`main` = `origin/main`).
- Working tree: clean of tracked changes; untracked: `.github/`, `delivery.py`, `notifier/`, `orchestrator.py`, `reports/`, `test_delivery.py`, `test_notifier.py`, `test_orchestrator.py`.
- `git diff --stat` empty; `git diff --check` clean.

## 22. Supabase State (VERIFIED ONLY FOR THE COLUMN PROBE)
`recipients.notify_before_golive` exists in the live schema (verified 2026-08-10, Task 2, HTTP 403/42501 probe). Everything else about delivery config is per the delivery plan, **NOT re-verified live this session**: `alert_settings.notify` = false for all types; `tenants.go_live_at` = not configured; dev-recipient `notify_before_golive=true` row state unknown; the five GitHub Secrets' existence unknown.

## 23. Telegram State
Bot token rotation is still **owner-pending** (the current token was exposed in `Docs/PHASE_2_DELIVERY_PLAN.md` and in chat — G-Brain handoff §9, 🔴; value deliberately not reproduced here). No real message sent; the notifier's real-API behavior (claim syntax, 403 classification, 4096 policy) is unproven until the first live send.

## 24. Next Required Owner Actions
1. Create the five GitHub Secrets (Settings → Secrets and variables → Actions): `TELEGRAM_BOT_TOKEN` (the NEW token after `/revoke` via BotFather), `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `TENANT_ID`, `SOURCE_ID`.
2. Verify/execute the dev-recipient INSERT from the delivery plan (`notify_before_golive = true` on the dev chat row).
3. Authorize committing and pushing `delivery.py`, `.github/workflows/delivery.yml`, `test_delivery.py`, `notifier/`, `orchestrator.py`, `reports/` — then run `workflow_dispatch` with `dry_run=true` and read the Arabic job log.
4. Rotate the Telegram bot token (BotFather `/revoke` then `/token`) and put the new one in Secrets only.
5. Then, only after approving the dry-run output: `dry_run=false` for the first real message to the dev chat, followed by the number-matching step (يومية الخزينة) before any owner activation.

## 25. Risks
- **Bot token exposure** (pre-existing, owner-action required; the only credential currently known to be exposed in a repo file).
- **Unverified network surface**: the service-role run and the real Telegram send are the two biggest unverified claims; both are gated behind the owner actions above. "Not verified yet" is the honest status.
- **Transport-retry duplicate risk** (Task 3 decision): a lost-response retry can deliver twice; accepted and documented.
- **Dead rows are final** (Task 3): a row reaching `dead` is never re-sent; manual clearing is documented.
- **Scheduler drift** (5–20 min): absorbed by design, not fixed.
- **No secret-scanning CI** wired in (see §19).

## 26. Task 5 Boundary
Task 5 (final validation: DST both sides, double-run idempotency, cap of 3, go_live suppression, notify_before_golive bypass, no-pyodbc-in-delivery closure, 4096 truncation, 403-vs-network classification, **full suite green, golden stays 31**) is at the **code/test level already satisfied** by the cumulative suites from Tasks 2–4 — **418 passed / golden 31**. What Task 5 additionally requires and CANNOT be done locally is the **real-world validation sequence** (G-Brain handoff §8): live `dry_run=true` job output → number matching against يومية الخزينة → first real dev-chat delivery → `SrUserval` semantics check → owner activation. **NOT STARTED** — it needs the pushed workflow, the secrets, and the owner's approval.

## 27. Exact Handoff Point
- **Last completed task:** Task 4 — delivery.py + workflow + tests, **17/17**, full suite **418 passed**, golden **31 passed**; nothing committed/pushed; no real cloud execution.
- **Last implementation action:** fixed the audited failing test (`test_live_double_run_is_idempotent`) to assert the seed-aware delta, and added 5 dependency-free workflow-spec validation tests.
- **Exact next action:** get the owner's authorization to commit/push the seven untracked paths, create the five GitHub Secrets, then run `workflow_dispatch` with `dry_run=true` and inspect the Arabic job log (this is also the start of Task 5's real-world half).
- **Continue reading first:** this report → `delivery.py` + `test_delivery.py` → `.github/workflows/delivery.yml` → the Task 3 report (notifier contract) → G-Brain `Phase-2-Handoff.md` (updated alongside this report).

## 28. Files Inspected (this session)
`Docs/CLAUDE_CODE_PHASE2_PROMPT.md` (Task 4 spec + Task 5 list), `Docs/PHASE_2_DELIVERY_PLAN.md` (gates, secrets table, dry-run procedure), `delivery.py`, `test_delivery.py`, `.github/workflows/delivery.yml`, `requirements-cloud.txt`, the Task 1–3 reports, `notifier/telegram.py` (read-only, Task 3), the G-Brain handoff + 2026-08-10 log.

## 29. Files Not Modified
No tracked file was modified (`git diff --stat` empty). The locked files (`adapter_hdsoft.py`, `metrics.py`, `events.py`, `report.py`, `schema.sql`, `test_golden.py`) were not touched; `orchestrator.py`, `notifier/telegram.py`, and their tests were read only. Nothing on the till, no `config.json`, no JWT, no Supabase writes.

## 30. Final Status
**COMPLETE AT CODE/TEST LEVEL** — Task 4's local requirements are implemented and mechanically enforced: 17/17 Task-4 tests (incl. a workflow falsifier), **418/418 full suite**, **31/31 golden**, no pyodbc (closure + falsifier + workflow grep guard), secrets by name only, fail-loudly everywhere, nothing committed or pushed. The remaining surface is entirely **NOT YET VERIFIED IN REAL CLOUD**: no workflow run, no live dry-run, no service-role execution, no Telegram delivery — all gated on the owner actions in §24. Next: Task 5's real-world validation (started by the owner-authorized push + `dry_run=true` dispatch).
