# POSentine — Rollback Procedures

Two independent systems, two independent rollback paths. Rolling back one never requires touching the other.

---

## 1. Dashboard (Vercel)

Vercel keeps every previous deployment addressable and promotable — this is the fast path and should be preferred over any code-level revert.

**Instant rollback (seconds, no rebuild):**
1. Vercel dashboard → the project → **Deployments**.
2. Find the last known-good deployment (production deployments are marked).
3. Click **⋯ → Promote to Production**.

That's it — the production URL now serves the previous build immediately. No redeploy, no CLI needed for this path.

**CLI equivalent:**
```bash
vercel ls                      # list deployments, find the good one's URL
vercel promote <deployment-url> --prod
```

**If the bad deployment shipped a broken live-config activation** (e.g. the localStorage schema changed): rolling back the deployment does not undo `localStorage` already set in a browser. The owner would need to re-run the activation snippet from `release-docs/DEPLOYMENT_VERCEL.md` §4 once against the restored version. This is a one-time, one-command fix per affected browser, not a data-loss event — nothing on the backend is touched.

**No customer data is ever at risk from a dashboard rollback.** The dashboard is read-only against Supabase; rolling it back or forward changes nothing server-side.

---

## 2. POSentine Agent (the till)

This already has a proven, tested rollback path — `UPDATE_POSENTINE.bat` / `update_agent.ps1` — used for every prior release (v1.0.1 → v1.0.3). Nothing new is introduced here.

**To roll back to a previous release:**
1. Download the previous release's `posentine-<commit>.zip` from `https://github.com/mahmouddevmohsen/POSentine/releases`.
2. Place it in the same Downloads folder the updater expects.
3. Run `UPDATE_POSENTINE.bat` exactly as for a forward update — it is commit-addressed, not "latest"-addressed, so pointing it at an older release's zip **is** the rollback.
4. The updater's own read-back verification (MANIFEST check, scheduled-task re-registration, a proven new heartbeat) applies identically on a rollback as on a forward update — see `README.md` and `UPDATE_README.txt`.

**If the update itself is mid-failure:** `update_agent.ps1` already exports the previous scheduled task before touching it and restores it byte-for-byte on any read-back failure (documented in `README.md`, "It also rolls back"). A failed update leaves the machine exactly as it was — this is existing, tested behavior, not new.

**Nothing on the till writes to the POS database at any point** (`READONLY_GUARANTEE.md`), so no rollback scenario here risks POS data — the only state that can regress is which version of the read-only agent is running.

---

## 3. Supabase schema

**Not covered by an automated rollback** — schema migrations (`schema_v2` … `schema_v8_dashboard_ro.sql`) are additive and were designed and applied one at a time, by hand, in the Supabase SQL Editor, with static verification (`_verify_v8_schema.py`-style checks) before and after each. If a future migration needs reverting, the correct move is a new, deliberately-written down-migration reviewed with the same rigor as the original — not an automatic revert. None of the applied migrations to date have needed this.

**RLS/grants rollback, if ever needed:** `schema_v8_dashboard_ro.sql`'s effect can be undone with:
```sql
drop policy if exists dashboard_ro_select on public.tenants;
-- (repeat for the 9 child tables)
revoke select on public.tenants, public.shift_reports, public.events,
  public.internal_anomalies, public.withdrawals, public.heartbeats,
  public.cash_counts, public.pos_users, public.pos_products, public.sync_state
  from dashboard_ro;
revoke dashboard_ro from authenticator;
drop role if exists dashboard_ro;
```
This would immediately break the live dashboard (back to demo-only) without touching any other role or table. **Not something to run without a specific reason** — included here only because a rollback document that omits the one irreversible-feeling layer would be incomplete.

---

## 4. What rollback never has to consider

- **The locked backend files** (`metrics.py`, `report.py`, `events.py`, `adapter_hdsoft.py`, `schema.sql`, etc.) have not changed across any of this release-preparation work — confirmed byte-identical to HEAD `0c59084` throughout. A rollback of the dashboard or the deployment never touches them.
- **Customer POS data** — read-only, agent-side, every layer proven (`READONLY_GUARANTEE.md`). No rollback scenario in this document writes to it.
