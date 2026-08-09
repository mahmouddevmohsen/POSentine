# Review — from the architect

> Protocol: I write here, you reply in `FROM_CLAUDE_CODE.md`.

---

# 2026-08-09 — CLOSE THE PROJECT. Final gate, then hand over.

## The trigger finding

The LogonTrigger catch is the most valuable thing found on this project, and it is
worth naming why rather than just fixing it.

We had a green verification last session that proved the wrong thing. `Start-ScheduledTask`
proved the task *can* run. It said nothing about whether it *would*. On a till logged in
for weeks, the agent would have run **zero times** after the operator left — and every
check we had would still have been green.

That is the third instance of the same shape on this project: a check that shares the
fault it is meant to detect. Watermark-0 comparing the table against itself. My manifest
hashes reading the same working tree twice. And now a trigger verified by firing it
ourselves. You found the third one by asking what Phase E would be waiting for *before*
writing the wait. Keep that habit.

Both defects from the failure-mode review — `--confirm` never judging drift while
VERIFY.md claimed it did, and a corrupt `state.json` wedging the agent forever — are the
same class. Good finds.

Your two pushbacks are accepted: no `TRUNCATE` probe (there is no zero-row form and a
permitted probe empties their sales table), and `HAS_PERMS_BY_NAME` in its place.

---

## Done on my side

**`sqlguard_wiring.patch` is applied.** `adapter_hdsoft.py` now imports `sqlguard` and
`connect()` returns `sqlguard.guard(cn)`. Verified here:

```
31 passed          test_golden.py, unchanged
302 total          280 passed + 21 skipped + 1 (git_revision, my copy has no .git)

sqlguard behaviour, live:
  SELECT TOP 1 ... WITH (NOLOCK)          passed
  UPDATE / DELETE / DROP / EXEC           WriteAttempt
  SELECT 1; DROP TABLE                    WriteAttempt   (multi-statement)
  /* comment */ DELETE FROM               WriteAttempt   (comment-hidden verb)
  SELECT * INTO x FROM                    WriteAttempt
```

Do **not** re-apply. Verify it is present and that the transcript now prints WIRED
rather than NOT WIRED.

---

## 🔴 The one thing that will bite at handover

`adapter_hdsoft.py` changed. **`ship/` still holds the pre-guard copy.**

If `ship/` is not rebuilt, the integrity check at step 0 compares `ship/` against a
`MANIFEST.txt` built from the same stale `ship/` — both agree, both are wrong, and the
agent that runs on the till has **no SQL guard at all**. Silent, and it would pass every
check we have.

Rebuild `ship/`, regenerate `MANIFEST.txt`, and confirm `sqlguard.py` is in the ship
list — the import-closure test should force this, so verify it actually fired rather
than assuming it would.

---

## Closing gate — the rehearsal that matters

The operator will **clone from GitHub at the shop**, not copy this folder. So the final
test must be exactly that:

1. `git clone` the pushed repo into a fresh directory, as he will
2. Place a real `config.json`
3. Double-click the one-click entry point
4. It must reach the POS-connection failure and **stop cleanly** — there is no SQL Server
   on your machine, so a clean, well-explained stop *is* the pass condition
5. Confirm the install transcript exists, is readable, and contains no secret
6. Run `collect_diagnostics.bat` and confirm the zip is produced and secret-free

Anything that only works in the development folder and not in a fresh clone is a defect
that would surface at the counter.

Then: **commit and push to GitHub.** He downloads from there.

## Also required

- Full suite green, `test_golden.py` exactly **31**, no locked file modified beyond the
  patch I applied.
- `VERIFY.md` consistent with what the code now does. It has been wrong twice — once
  about drift, once about the trigger. Read it against the code, not against memory.
- G-Brain: the trigger trap as a reusable technique, plus the project note and index.
- Priority 4 stays unevaluated and **labelled as priors, not findings**. Do not spend
  budget on it now.

## Final report — write it in `FROM_CLAUDE_CODE.md`

For someone who has to trust this without reading the code:

- What is proven, and by what evidence
- What is **not** proven and cannot be from here — the POS connection succeeding, a
  `LastTaskResult: 0`, the first real shift report
- Known limitations, including the revoked-token blind spot you already named
- Exactly what the operator does at the shop, and what he sends back
- What is deliberately deferred: orchestrator, telegram, workflows, go-live

Be honest about the boundary. A clear list of what is untested is worth more than a
claim of completeness.

## Budget

Be economical. Do not re-verify what is already verified above — the patch, the golden
tests, and sqlguard's behaviour are settled. Spend what you have on the fresh-clone
rehearsal and the push; that is the only thing standing between us and the visit.
