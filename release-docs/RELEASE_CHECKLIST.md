# POSentine — Release Checklist

Last updated: 2026-08-16 (release-preparation pass, `Docs/CONTEXT.md`)

## Phase status

| Phase | Status | Evidence |
|---|---|---|
| Phase 1 — Dashboard semantic mapping | 🟢 GREEN | `reports/FINAL_POSENTINE_DASHBOARD_CLOSURE_AUDIT.md` |
| Phase 2 — Dashboard stabilization | 🟢 GREEN | 266/266 harness, 22/22 browser |
| Phase 3 — Secure Supabase integration | 🟢 GREEN | Cross-tenant RLS proven live, write-protection proven live, reconciliation 33/33 |

## Security checks

| Check | Result |
|---|---|
| Cross-tenant RLS isolation | PASS — validly-signed fabricated-tenant token → HTTP 200, 0 rows on all 4 core tables; real tenant token unaffected |
| Write protection (`dashboard_ro`) | PASS — INSERT/UPDATE/DELETE all 403/42501, live-tested |
| JWT tamper resistance | PASS — unsigned payload edit → HTTP 401 |
| `service_role` never reaches browser | PASS — grepped, harness-checked |
| JWT secret never committed | PASS — tracked `HEAD` clean (2 synthetic test fixtures only) |
| JWT secret never written to disk this session | PASS-with-note — one local-only leak (harness permission log) found and redacted this same session; recommend rotation as cheap insurance |
| `reports/` customer-data hygiene | PASS — 6 specific paths gitignored (Telegram screenshots, forensic reports, customer hostnames) |
| `config.example.json` real-identifier exposure | FIXED this pass — real `tenant_id`/`source_id`/Supabase URL replaced with placeholders (pre-existing since an earlier commit; not a secret, but unnecessary identifier exposure in a template file) |
| Repository visibility | Confirmed PUBLIC — governs every check above |

## Dashboard checks

| Check | Result |
|---|---|
| Harness (`verify_dashboard.mjs`) | 266/266 |
| Browser check (demo mode) | 22/22 |
| Live render (real token, 7 screens) | 0 console errors, 3 independent reproductions this project |
| Financial reconciliation vs. real Telegram reports | 33/33 fields, 0 discrepancies |
| Backend files touched by dashboard work | None — all 11 locked files byte-identical to HEAD `0c59084` |

## Installer / agent checks

| Check | Result |
|---|---|
| Read-only guarantee (7-layer, `READONLY_GUARANTEE.md`) | Documented and enforced; layer 6 (on-site probe) re-verifies on every install |
| Install / update / uninstall scripts present and idempotent | Present: `install_agent.ps1`, `update_agent.ps1`, `uninstall_agent.ps1`, `run_agent.ps1` — documented behavior in `README.md`; not re-run this pass (requires a live till or a Windows sandbox, out of scope for this repo-side release prep) |
| Full pytest suite | 611 passed / 2 failed — both pre-existing, environmental (`NoDefaultCurrentDirectoryInExePath=1` on this machine breaks bare-filename `cmd /c` resolution for 2 updater tests; unrelated to product code, reproduced identically across every run this session) |

## Deployment checks

| Check | Result |
|---|---|
| Vercel project settings determined | PASS — see `release-docs/DEPLOYMENT_VERCEL.md` |
| `vercel.json` created | PASS — `dashboard/POSentine Arabic Dashboard/vercel.json` |
| Environment variables required | None — confirmed static, credential-free build |
| Actual deployment | **WAITING FOR OWNER** — Vercel CLI not installed/authenticated in this environment; requires interactive `vercel login` |
| Live production smoke test | **WAITING FOR OWNER DEPLOYMENT** — cannot test a URL that doesn't exist yet; will be run immediately once a production URL exists |

## Final release decision

See `reports/FINAL_RELEASE_MANIFEST.md` for the full gate table and verdict.
