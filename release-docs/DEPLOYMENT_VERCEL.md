# POSentine Dashboard — Vercel Deployment

**Status:** Prepared, not deployed. Vercel CLI is not installed/authenticated in this environment — deployment itself is an owner action (see §8).

---

## ⚠️ Decision needed before deploying — read this first

`dashboard/` is currently **entirely gitignored** (deliberately, since Phase 1/2 — it carries the owner's real business data in its live-config path). That means:

- **Vercel's git-based auto-deploy** (connect the GitHub repo → deploy on push) will **not** see the dashboard folder at all, because Vercel builds from what git tracks.
- **Vercel CLI deploy** (`vercel deploy` run from inside the dashboard folder) uploads the local filesystem directly and is **not** blocked by `.gitignore` the same way — it can deploy the folder without it ever being committed to the repo.

**This is an architecture decision, not a technical blocker, and it hasn't been made yet:**

| Option | What it means | Trade-off |
|---|---|---|
| **A — CLI deploy, keep `dashboard/` gitignored** | `cd "dashboard/POSentine Arabic Dashboard" && vercel --prod`, run manually whenever the dashboard changes | Simplest, no public-repo exposure change. No auto-deploy on push — a human runs the deploy command each time. |
| **B — Un-ignore and commit `dashboard/`, connect Vercel to the repo** | Auto-deploys on every push to `main` | The dashboard's HTML/JS becomes visible in the **public** repo's history forever. The file itself carries no secrets (verified — see the security section below) and only a generic `المالك` placeholder in demo mode, but this is still a visibility change worth a deliberate yes, not a default. |

**Recommendation: Option A.** It requires zero repo changes, deploys today, and matches the existing "everything sensitive stays local unless deliberately shipped" pattern already established for this project (`.gitignore`'s own comment on `dashboard/` says it will be un-ignored "as part of the Supabase integration phase" — that phase is now closed, but the un-ignore step itself was never explicitly re-confirmed and shouldn't happen by default).

This document prepares both paths. **Nothing below commits or un-ignores `dashboard/` — that step is the owner's call.**

---

## 1. What's being deployed

A **static site** — no build step, no framework, no server code.

- **Root directory:** `dashboard/POSentine Arabic Dashboard/`
- **Entry point:** `POSentine Dashboard.dc.html` (not `index.html` — see the rewrite below)
- **Supporting files:** `support.js` (vendored `.dc.html` runtime), `github.md` (internal notes, not user-facing), `uploads/` (one unreferenced design-tool artifact — see Known Issues)
- **Third-party at runtime:** loads React/ReactDOM from `unpkg.com` (with SRI) and Google Fonts. Both are contacted on every page load — see the CSP note in §6.

## 2. Vercel project settings

| Setting | Value |
|---|---|
| Framework Preset | **Other** (static HTML — no framework detected) |
| Root Directory | `dashboard/POSentine Arabic Dashboard` |
| Build Command | *(none — leave empty)* |
| Output Directory | `.` (the root directory itself) |
| Install Command | *(none — leave empty)* |
| Node.js Version | Irrelevant — no build runs |

A `vercel.json` is already in place at that root directory:

```json
{
  "rewrites": [
    { "source": "/", "destination": "/POSentine Dashboard.dc.html" }
  ]
}
```

This makes `/` serve the actual entry file without renaming it (renaming would break every existing test and tool in this repo that references the exact filename `POSentine Dashboard.dc.html` — `verify_dashboard.mjs`, `browser_check.py`, this repo's own local server invocations). Every other path (`/support.js`, `/uploads/...`) is served as a normal static file, unaffected by the rewrite.

## 3. Environment variables

**None are required.** This is the clean part of the story: the dashboard has no build-time or server-side configuration at all. Live-data credentials are supplied entirely **client-side, at runtime, in the browser**, via `localStorage` or `window.POSENTINE_LIVE` — never through Vercel project settings, never baked into the deployed files, never in the repo.

| Variable | Public/safe for browser? | Needed in Vercel? |
|---|---|---|
| Supabase anon key | Yes — it's the public API-gateway key, safe by Supabase's own design | No — set client-side after deploy, not in Vercel |
| Dashboard `dashboard_ro` JWT | Read-only, tenant-scoped, revocable by rotating the JWT secret | No — set client-side after deploy, not in Vercel |
| Supabase JWT secret | **Never.** Not a browser value under any circumstance. | **Never — do not add this to Vercel, ever, in any environment.** |
| `service_role` key | **Never.** Server-only, GitHub Actions only. | **Never.** |

## 4. Post-deployment activation (owner, one-time per browser/device)

The deployed page loads in **demo mode** by default (fabricated fixture data, clearly labeled "بيانات تجريبية"). To show real data, the owner opens the deployed URL, opens the browser console, and runs the same activation snippet already used to verify this locally:

```js
localStorage.setItem('posentine-live-config', JSON.stringify({
  supabaseUrl: 'https://<project-ref>.supabase.co',
  anonKey: '<the public anon key>',
  dashboardToken: '<a token minted by mint_dashboard_token.py>'
}));
location.reload();
```

This persists in that browser's `localStorage` until cleared — it is a one-time step per browser/device, not per page load. **This document does not change or improve that mechanism** — it is exactly what Phase 3 already proved works; documenting it here is not scope creep, just where it's now used from.

## 5. Deploy steps (owner action — requires Vercel authentication)

```bash
npm install -g vercel      # if not already installed
cd "dashboard/POSentine Arabic Dashboard"
vercel login               # opens a browser, requires the owner's Vercel account
vercel                     # first run: links/creates the project, deploys a preview
vercel --prod              # promotes to the production URL
```

**BLOCKED ACTION:** deploying to Vercel.
**WHY:** requires Vercel CLI installation + `vercel login` (an interactive browser-based auth flow) — genuinely owner-only, cannot be scripted or approved on the owner's behalf.
**EXACT OWNER ACTION:** run the four commands above from `dashboard/POSentine Arabic Dashboard/`.
**EXACT COMMAND/CLICK PATH:** `npm install -g vercel` → `vercel login` (approve in browser) → `vercel` → `vercel --prod`.
**WHAT I WILL DO IMMEDIATELY AFTER:** run the full Browser Production Smoke Test (§ below) against the resulting production URL — all 7 screens, console errors, real-data reconciliation — the same rigor already applied to the local instance in the Phase 3 closure audit.

## 6. Security notes specific to deployment

- **CSP:** if a Content-Security-Policy header is ever added at the Vercel level, it must allow `'unsafe-eval'` (the `.dc.html` runtime uses `new Function()`) and the two third-party origins (`unpkg.com`, Google Fonts). Verified this session — no CSP is currently configured, so this is forward-looking, not a current gap.
- **No credential in the deployed files:** re-confirmed this session via the dashboard test harness (`no hardcoded JWT literal`, `no key/password literal values`, `storage keys are theme + live-config only`) — 266/266, unchanged by this deployment prep.
- **Preview deployments are public by default on Vercel's free tier** unless "Vercel Authentication" or a password is enabled for previews. Since demo mode is the safe default and no secret ever lives in the deployed files, an unauthenticated preview leaking is low-risk — but if the owner wants preview URLs private, that's a Vercel project setting to enable explicitly, not something this repo controls.

## 7. Rollback

See `release-docs/ROLLBACK.md`.

## 8. What still requires the owner

- Deciding Option A vs. B above (git-tracked or CLI-only).
- Installing and authenticating the Vercel CLI (`vercel login`).
- Running the actual deploy.
- The one-time browser-console activation step per device that will view live data.

Everything else in this document — project settings, `vercel.json`, the environment-variable determination, the security review — is already done.
