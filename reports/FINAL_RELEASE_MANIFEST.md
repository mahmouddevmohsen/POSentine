# POSentine — FINAL RELEASE MANIFEST

**Date:** 2026-08-16 (Release-preparation pass, `Docs/CONTEXT.md`)
**Prepared by:** repository-side automated release preparation, from Phase 3 GREEN/CLOSED baseline.

---

## Repository

| Field | Value |
|---|---|
| Repository | `mahmouddevmohsen/POSentine` |
| Visibility | **PUBLIC** (confirmed via `gh repo view`) |
| Branch | `main` |
| HEAD commit | `0c59084746d53412a7235d4d5b928557a2ebd0e7` — "chore(release): pin EXPECTED_SHA to the b759b93 artifact" |
| Working tree status | Dirty — 2 modified tracked files (`.gitignore`, `config.example.json`), ~28 new untracked files (release evidence reports, dashboard integration scripts/deliverables, this release-prep pass's new docs). Nothing staged. See §Git Cleanup below. |

## Phase status

| Phase | Status |
|---|---|
| Phase 1 — Dashboard semantic mapping | 🟢 **GREEN** |
| Phase 2 — Dashboard stabilization | 🟢 **GREEN** |
| Phase 3 — Secure Supabase integration | 🟢 **GREEN** |

Full evidence: `reports/FINAL_POSENTINE_DASHBOARD_CLOSURE_AUDIT.md`.

## Backend integrity

All 11 locked files re-confirmed byte-identical to HEAD `0c59084` this pass (`git diff --stat HEAD` — empty output on every one):

`metrics.py` · `report.py` · `events.py` · `adapter_hdsoft.py` · `schema.sql` · `schema_v7_withdrawals.sql` · `orchestrator.py` · `supa.py` · `delivery.py` · `mint_agent_token.py` · `.github/`

No backend business logic was touched at any point in Phase 1–3 or this release-preparation pass.

## Dashboard

| Field | Value |
|---|---|
| Source path | `dashboard/POSentine Arabic Dashboard/` |
| Entry point | `POSentine Dashboard.dc.html` |
| Deployment method | Static — no build, no framework (see `release-docs/DEPLOYMENT_VERCEL.md`) |
| Build status | N/A — static site |
| Test status | Harness 266/266, browser 22/22, live end-to-end 0 console errors (3 independent reproductions across the Phase 3 closure and this pass) |

## Security

| Area | Result |
|---|---|
| Secrets scan (tracked `git grep`) | Clean — only 2 known synthetic JWT-shape fixtures in `test_installer.py`/`test_logsetup.py` (decode to `{"role":"anon"}`, no real project ref) |
| Cross-tenant RLS | **PROVEN LIVE** — a validly-signed `dashboard_ro` token for a fabricated, non-existent tenant returned HTTP 200 with 0 rows on all 4 core tables; the real tenant's token was unaffected |
| Read-only boundary (`dashboard_ro`) | **PROVEN LIVE** — SELECT works, INSERT/UPDATE/DELETE all 403/42501 |
| Browser credential exposure | None — no `service_role`, no JWT secret, in any browser-reachable file; harness-checked (13 backend-isolation checks + 6 secret-hygiene checks, all PASS) |
| Customer-data hygiene | 6 specific paths under `reports/` gitignored this session (real customer hostnames, Telegram screenshots, SQL dumps); `dashboard/` and `Docs/` confirmed fully ignored |
| **New this pass:** local secret leak found and fixed | The harness's own permission-approval log (`.claude/settings.local.json`, gitignored, untracked) had recorded the raw Supabase JWT secret 3 times when Bash commands using it were approved. Redacted this pass. Never reached the public repo. **Recommend rotating the secret regardless** — cheap insurance, not a blocker. |
| **New this pass:** template file sanitized | `config.example.json` (tracked, public since an earlier commit) hardcoded the real production `tenant_id`, `source_id`, and Supabase project URL in what is meant to be a fill-in-the-blanks template. Not a secret (RLS is the real boundary, already proven), but unnecessary identifier exposure. Replaced with placeholder values this pass; pytest re-run confirms no regression (611/613, same 2 pre-existing failures). |

## Agent (till installer)

| Check | Result |
|---|---|
| Installer/updater/uninstaller scripts | Present and documented (`install_agent.ps1`, `update_agent.ps1`, `uninstall_agent.ps1`, `run_agent.ps1`) — idempotent, rollback-on-failure, user-level/LeastPrivilege by design per `README.md`. Not re-executed this pass (requires a live till or Windows sandbox — out of scope for repo-side release prep; nothing about this pass touches installer code, so no re-test is required for correctness). |
| Read-only guarantee | 7-layer, documented in `READONLY_GUARANTEE.md`, re-verified on every install by the on-site probe (layer 6) — architecture unchanged this pass |

## Tests — exact fresh results, this pass

| Suite | Result |
|---|---|
| `pytest -q` (full suite) | **611 passed / 2 failed** |
| `dashboard/verify_dashboard.mjs` | **266/266** |
| `dashboard/browser_check.py` (demo mode) | **22/22** |
| `reports/_verify_v8_schema.py` | PASS — 10/10 tables valid |
| `reports/_phase3_reconcile.py` | **33/33** fields, 0 discrepancies vs. 3 real Telegram reports |
| Live dashboard, real token, 7 screens | 0 console errors (3rd independent reproduction) |

**The 2 pytest failures, classified per the brief's own taxonomy:**
- `test_update_agent.py::test_bat_stops_cleanly_when_the_updater_is_not_next_to_it`
- `test_update_agent.py::test_bat_stops_cleanly_in_the_extracted_delivery_folder`
- **Classification: PRE-EXISTING / ENVIRONMENTAL.** Root cause: this development machine has `NoDefaultCurrentDirectoryInExePath=1` set, which stops `cmd.exe` resolving a bare batch-filename (`cmd /c UPDATE_POSENTINE.bat`) from the current working directory — the two tests invoke the bat by bare name and get `'UPDATE_POSENTINE.bat' is not recognized...` instead of the expected stdout. Reproduced identically, byte-for-byte, across every run this session and the prior Phase 3 session. Not caused by, or related to, any change in this release-preparation pass. Will not occur on a machine without that registry setting (most machines, including the actual customer till, which was verified clean on this exact point in the 2026-08-13/14 deployment sessions).

## Known non-blocking issues

1. Telegram bot token still unrotated (pre-existing, unrelated to dashboard/deployment work).
2. `schema_v5`/`v6` application status still unknown (pre-existing, carried from earlier sessions).
3. Codebuff till-audit finding F-1 (plaintext `config.json` in till backup folders) still has no vault narrative — an operational/documentation gap, not a code defect.
4. Cosmetic: the live "آخر نبضة" (last-beat) timestamp renders as a raw concatenated ISO string rather than a formatted time.
5. Cosmetic: the "آخر 5 أيام" (last 5 days) trend header is a static label; live mode can show up to 14 days without the header text updating.
6. `dashboard/POSentine Arabic Dashboard/uploads/pasted-1786821589649-0.png` — an unreferenced design-tool artifact (a screenshot of an unrelated portfolio page, not customer data, not linked from the dashboard UI). Harmless but dead weight; safe to delete, not done here since it's cosmetic housekeeping outside this pass's mandate.
7. Recommend rotating the Supabase JWT secret (see Security §, above) — not a blocker, cheap insurance.

## Remaining owner actions

Only things that genuinely cannot be performed here:

1. **Decide the dashboard deployment architecture**: CLI-only deploy (`dashboard/` stays gitignored) vs. git-tracked + Vercel auto-deploy (`dashboard/` gets committed, public repo visibility changes). See `release-docs/DEPLOYMENT_VERCEL.md` §"Decision needed."
2. **Install and authenticate the Vercel CLI**, then run the actual deployment. Interactive browser-based auth — cannot be done on the owner's behalf.
3. **Run the live production smoke test** once a Vercel URL exists — prepared and ready to execute the moment there's a URL to test.
4. **Rotate the Supabase JWT secret** (recommended, not blocking).
5. **Fill in real support contact details** in `release-docs/CLIENT_HANDOFF.md` before sending it externally.
6. Carried from earlier sessions, unrelated to this pass: rotate the Telegram bot token; confirm `schema_v5`/`v6` application status; write the Codebuff till-audit F-1 vault narrative.

---

## FINAL RELEASE GATE

| Gate | Result |
|---|---|
| Phase 1 | GREEN |
| Phase 2 | GREEN |
| Phase 3 | GREEN |
| Backend integrity | PASS |
| Secret hygiene | PASS (1 local-only leak found + fixed this pass; 1 template-file exposure found + fixed this pass) |
| Customer-data hygiene | PASS |
| Dashboard build | PASS (static, no build required) |
| Dashboard tests | PASS |
| Browser tests | PASS |
| Agent installer | PASS (documented, unchanged, not re-executed — out of scope) |
| Production deployment | **WAITING** |
| Live smoke test | **WAITING** |
| Client handoff docs | PASS |
| Git status | DIRTY (nothing unsafe pending — see Git Cleanup) |
| Release readiness | 🟡 **YELLOW** |

### Git Cleanup — what's pending and why it's safe

`git status` shows a dirty tree. None of it is unsafe to eventually commit:

- **Modified:** `.gitignore` (customer-data hygiene fixes, already reviewed), `config.example.json` (placeholder sanitization, already reviewed, pytest-confirmed no regression).
- **New:** `release-docs/` (this pass's deliverables), `reports/FINAL_*` and `reports/_phase3_*`/`_verify_v8_*` (Phase 3 evidence + tooling), `mint_dashboard_token.py` + `test_mint_dashboard_token.py` + `schema_v8_dashboard_ro.sql` (Phase 3 deliverables, no secrets — grepped clean).
- **Nothing** in this list contains a secret, a customer hostname, a hardcoded credential, or PII — every file was scanned this pass (`git grep` for JWT/bot-token shapes, `grep` for the specific raw secret string, manual review of the two hygiene findings above).
- **A commit was not created in this pass.** Per this repository's own commit discipline (only commit when explicitly asked) and the release brief's own hedge ("do not push automatically unless... clearly configured"), staging and committing this batch is offered as the next step, not assumed.

---

## FINAL VERDICT

### 🟡 READY EXCEPT FOR OWNER ACTIONS

The code and repository are ready. Every gate that can be closed without the owner's direct involvement is closed: Phase 1–3 are all GREEN with live, reproduced evidence; the backend is untouched; secret and customer-data hygiene are clean (including two real findings caught and fixed in this very pass); the dashboard is fully tested; deployment is fully prepared (settings, `vercel.json`, environment-variable analysis, a documented architecture decision awaiting the owner's choice); and client-facing documentation exists.

What remains is exactly the set of things only the owner can do: choose the deployment architecture, authenticate and run the Vercel deploy, and then let the live smoke test close the loop.

Not starting Phase 4. Not expanding scope. Stopping here.

---

## SESSION UPDATE — Forensic audit + status-bug fix + final handoff closure (same day)

### A. Financial reconciliation (Aug 11–13, real Telegram baseline)

| Date | Shift | Telegram | Supabase | Dashboard | Result |
|---|---|---:|---:|---:|---|
| 08-11 | Morning | 18,785 | 18,785 | 18,785 | ✅ match |
| 08-11 | Evening | 18,630 | 18,630 | 18,630 | ✅ match |
| 08-12 | Morning | 19,285 | 19,285 | 19,285 | ✅ match |
| 08-12 | Evening | 19,185 | 19,185 | 19,185 | ✅ match |
| 08-13 | Morning | 20,705 | 20,705 | 20,705 | ✅ match |
| 08-13 | Evening | *no report (`(9).txt` empty)* | 7,015 (sales 19,745, withdrawals 14,510) | 7,015 | ⚪ unverifiable — real Supabase/dashboard data, no Telegram report exists to cross-check |

40/40 checked financial fields exact matches across the 5 verifiable shifts. Zero fabricated values found anywhere reachable from live mode.

### B. Status reconciliation

| Date | Shift | Real status | Dashboard status (before fix) | Dashboard status (after fix) |
|---|---|---|---|---|
| 08-11 | Morning | 🟡 تحتاج مراجعة | 🟠 غير مكتملة ❌ | 🟡 تحتاج مراجعة ✅ |
| 08-11 | Evening | 🟠 غير مكتملة | 🟠 غير مكتملة ✅ | 🟠 غير مكتملة ✅ |
| 08-12 | Morning | 🟠 غير مكتملة | 🟠 غير مكتملة ✅ | 🟠 غير مكتملة ✅ |
| 08-12 | Evening | 🟠 غير مكتملة | 🟠 غير مكتملة ✅ | 🟠 غير مكتملة ✅ |
| 08-13 | Morning | 🟠 غير مكتملة | 🟠 غير مكتملة ✅ | 🟠 غير مكتملة ✅ |
| 08-13 | Evening | *no baseline* | 🟠 غير مكتملة (unverified guess) | 🟢 مكتملة (still unverified — no Telegram baseline exists) |

**Root cause:** `decorateFromBeats()` (dc.html) had no upper bound on its per-shift heartbeat window and compared a raw beat count against 240 instead of measuring silence duration — for any shift older than the ~10-hour lookback the fetch actually covered, this silently manufactured a false coverage gap on nearly every historical shift. Verified in a standalone Python simulation against live Supabase heartbeats/events before touching any code: the corrected algorithm (longest inter-heartbeat silence ≥ `COVERAGE_GAP_MINUTES=20`, inside a ±10min-padded window, in the same Cairo-local→UTC conversion `orchestrator.py` uses) reproduced all 5 known real statuses exactly. Implemented, then re-verified identically live against production.

`gap_explained` (device-Sleep/POS-activity classification) is intentionally not reproduced — that data is backend-only and never persisted anywhere the frontend can read. A real-but-backend-explained gap will still show INCOMPLETE instead of the backend's STABLE. Documented as a known, honest residual limitation in the source comment.

### C. Fake/demo audit

| Value | Location | Demo/Live | Client-visible? | Action |
|---|---|---|---|---|
| Mock shifts s5–s10 (SHIFT_DOMAIN, dc.html) | Overview/Shifts/Cashbook | Demo only, explicitly commented `MOCK` in source | Only in demo mode, always badged | None needed — safely isolated |
| `8,540 MB/10,240 MB/83%` db-size | Alerts/Health | Was demo only | No — removed prior session, reconfirmed absent this pass | Already fixed |
| Coverage-gap false positive | Shifts/Overview/Cashbook (status field) | **Live** | **Yes — real client-visible defect** | **Fixed this pass** |
| Alert cards showing generic text instead of real facts | Alerts (live) | Live | Yes, but not misleading (just uninformative) | Fixed this pass — real receipt #/user/amount now surfaced from the event's own payload |
| "آخر 5 أيام" stale label | Overview/Shifts/Products | Both (accurate in demo, stale in live) | Yes, cosmetic only | Fixed this pass — now dynamic |

### D. Missing data

- Aug 13 evening Telegram report — genuinely absent (`(9).txt` is 0 bytes). Owner action: **"Send me the Telegram report for Thursday, 13 August 2026, evening shift (محمود)."** The dashboard's 14,510 ج withdrawal figure for that shift is real Supabase data, unverified against any independent source.
- Products screen top-items — intentional limitation, documented in source (`mapShiftRow`'s `items: []` — avoids the dashboard recomputing raw invoices client-side and duplicating the backend's authoritative calculation).
- Users-screen aggregation (محمود/حمص invoice/amount totals) — not independently re-derived digit-by-digit this pass; flagged as not exhaustively checked, not as wrong.

### E. Production verification

- URL: `https://posentine-dashboard.vercel.app`, deployment `dpl_C2a7EUXNPzYV7XUCEanau8m7cTiW`, `target: production`, `READY`.
- Screens tested: all 7, both demo and live mode, on the actual production URL (not just local).
- Console errors: 0, in every pass.
- Demo mode: correct badge, nav badge `5` (not `6`), no misleading numbers.
- Live mode: real tenant data, Aug 11 morning correctly shows REVIEW, all other real shifts unchanged and correct, real alert facts visible, dynamic "آخر 14 يوم" labels correct.

### F. Tests — exact counts, this pass

| Suite | Result |
|---|---|
| `dashboard/verify_dashboard.mjs` | **224/225 passed** (1 known, root-caused, non-code failure — see below) |
| `dashboard/browser_check.py` | **22/22** |
| `pytest` | **610 passed / 3 failed** (2 pre-existing environmental `NoDefaultCurrentDirectoryInExePath`; 1 artifact-selection timing edge case — see below) |
| `reports/_phase3_reconcile.py` | PASS, re-run, unaffected by this session's changes |

**Harness failure (1):** `three real reports present matching SHIFT_DOMAIN REAL entries` now correctly reports `0` instead of crashing. Root cause: the original 3 evidence files (Aug 14–15, matching `SHIFT_DOMAIN`'s REAL fixture entries) were replaced during an earlier audit turn with 5 new Aug 11–13 reports + 1 empty file — none of which map to the old fixture dates. The harness previously crashed outright on the empty file (`who.split('—')` on `null`) and hardcoded an assumption of exactly 3 files; both are now fixed to be robust, and the check now honestly reports what it finds (0 files match) rather than crashing or silently passing. Not a code regression — the underlying claim (Aug 14–15 SHIFT_DOMAIN entries are real) was independently reconfirmed via live Supabase query this session.

**Pytest failures (3):**
1–2. Pre-existing, environmental (`cmd`'s `NoDefaultCurrentDirectoryInExePath` registry setting on this dev machine), unrelated to any code, reproduced identically across every run this project.
3. `test_artifact_carrying_protected_files_is_refused` — investigated in depth. Root cause: `update_agent.ps1` selects the "newest" release artifact via `Sort-Object LastWriteTime`; in the test, a legitimate and a malicious zip are written back-to-back and can land within the same filesystem timestamp resolution, so the malicious one sometimes doesn't win the tie and its `"contains a protected name"` gate is never reached. In every observed run, the update still correctly failed and rolled back — just via a different (also correct) gate, later in the pipeline. **Classified as a narrow test-timing/robustness gap, not a proven safety bypass** — no run has ever resulted in the malicious artifact actually being applied. Given the real-world safety property held in every execution and `update_agent.ps1` is a mature, field-proven, production-critical script outside this session's tested scope, it was deliberately **not modified** — changing artifact-selection logic without dedicated Windows-updater test coverage carries more real risk than the narrow issue it would close. Documented here in full rather than silently touched.

### G. Remaining issues

- 🔴 **BLOCKER: none.**
- 🟡 **NON-BLOCKING:** the pytest artifact-selection timing edge case (F.3) and the harness's now-honest 0/3 real-report match (F, harness) — both fully documented, neither affects the shipped dashboard.
- 🟢 **OPTIONAL ENHANCEMENT:** improve `update_agent.ps1`'s artifact selection to be timestamp-tie-resistant (e.g., prefer an explicit `-ZipName` pin or embed a monotonic build sequence rather than relying on filesystem mtime) — a separate, dedicated piece of work.
- 🔵 **INTENTIONAL LIMITATION:** Products screen top-items in live mode; `gap_explained` (Sleep-classification) not reproduced client-side; Aug 13 evening unverifiable against Telegram.

### Files changed this session

- `dashboard/POSentine Arabic Dashboard/POSentine Dashboard.dc.html` — status-bug fix, alert facts, dynamic labels (gitignored, not in git status)
- `dashboard/verify_dashboard.mjs` — robustness fix for the REAL REPORTS section (gitignored, not in git status)
- `release-docs/CLIENT_HANDOFF.md` — support contact filled in (tracked, shown in `git status`)
- This file

No locked backend file touched. No secret exposed, committed, or printed.
