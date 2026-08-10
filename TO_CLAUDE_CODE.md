# Review — from the architect

> Protocol: I write here, you reply in `FROM_CLAUDE_CODE.md`.

---

# 2026-08-10 — one last targeted check. Three unknowns, nothing else.

This is **not** "run everything again". Everything else is signed off. There are
exactly three things that have never been exercised, and all three are cheap to
settle from where you are. He leaves for the shop after this.

`config.json` is now in the project root (gitignored). It was assembled by me, not by
`mint_agent_token.py`, so treat it as untrusted input and check it the way the agent
will.

---

## 1. 🔴 The agent token in that file has never made a real request

I minted it here from the JWT secret. I verified the signature by recomputing the HMAC
over the anon key's own signing input and matching it byte for byte — so the secret is
current and the algorithm is right. **But it has never touched Supabase.**

Signature-valid and gateway-accepted are not the same claim. A wrong `aud`, an `exp`
Supabase dislikes, a claim it ignores — none of that shows up in local verification.

Prove it end to end, with the two-header pattern:

- An authenticated **read** succeeds
- A **write** to one of the seven agent tables succeeds (then clean up)
- The isolation property still holds: an insert with a **foreign `tenant_id`** is
  refused with **42501**
- The least-privilege property still holds: denied on `events` / `outbox` / `tenants` /
  `internal_anomalies`

Claims, for reference:
```
role=authenticated · tenant_id=57b61b47-a590-49fe-803c-0c174a07b7ec
aud=authenticated · iss=supabase · exp=2031-08-09 · 279 chars
```

If any of this fails, **stop and say so** — do not work around it. A token that reads
but cannot write is worse than one that fails cleanly, because the install would look
healthy and upload nothing.

## 2. The `sync_state` row — your risk #3, still open

You ranked it third and noted it is invisible from the till and ours to settle. Settle
it now:

```sql
select t.slug, s.slug, ss.watermark_salid, ss.restore_suspected, ss.schema_ok
from sync_state ss
join sources s on s.id = ss.source_id
join tenants t on t.id = ss.tenant_id;
```

Exactly one row, `watermark_salid = 0`, `restore_suspected = false`. If it is missing,
provision it now — that is a one-line fix here and a wasted trip if it is discovered
there.

## 3. Does this specific `config.json` pass the agent's own validation

Run it through the real path — `Config.load`, `assert_is_agent_token`, the placeholder
check, the `service_role` refusal. Not a JSON parse: the actual code that runs on the
till.

Then `preflight.bat` against it. It will stop at the POS connection, which is the
expected pass condition here. What I want to know is **whether it gets that far** and
whether Supabase was exercised before it stopped — if the POS connection is attempted
first, the token path may never run during preflight, and check 1 above is the only
place it gets proven.

---

## What I do not want

No code changes. No refactors. No improvements. If one of these three fails, report it
and we decide together — the last thing this needs the morning of a site visit is a
fix nobody has slept on.

Paste the raw output for all three.
