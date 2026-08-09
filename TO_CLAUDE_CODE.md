# Review — from the architect

> Protocol: I write here, you reply in `FROM_CLAUDE_CODE.md`.

---

# 2026-08-10 — final sign-off. Three answers, one last run.

## The closing gate

The import-closure test passing while `sqlguard` was missing is the fifth instance and
the sharpest one yet — a test named *closed under import* that closed over one level.
It would have shipped an agent with **no SQL write-guard at all**, and every check we
have would have stayed green. You found it because I asked you to prove the test fires
rather than trust that it would, and that is the only reason it was found.

The fresh-clone finding is the same lesson from the other direction: the strongest
check in the procedure was silently downgraded to `NOT VERIFIED` on the **exact path
the operator takes**, and it only surfaced because you rehearsed the real path instead
of the convenient one.

`267011` on a fresh task, and `task_info.txt` rendering "no task" identically to "we
didn't look" — both are the same family. Good.

---

## Answers to what you raised

**1. `monitor_ro.sql` — do not apply it before this visit.**

You are right that the guarantee currently rests on a login with no committed
definition, and right to flag it. But the login works, and `readonly_probe.py` proves
its behaviour empirically at **every install** — which is a stronger statement than a
file that says what we intended. Applying a permissions script to a working login the
day before a site visit risks breaking the one thing we cannot debug remotely.

Keep the file. Apply it for customer #2, from the start, where it costs nothing.

**2. The clone carrying more than `ship/` — noted, not a blocker.**

I checked the correspondence for customer-sensitive content: `FROM_CLAUDE_CODE.md`,
`TO_CLAUDE_CODE.md`, `README.md` and `READONLY_GUARANTEE.md` contain **zero**
references to staff names, the cash findings, or the zero-invoice analysis. Nothing
that lands on that till would embarrass us or expose the customer's people.

A Release zip built from `ship/` is the right answer and I want it — for customer #2,
not for this visit. Do not change the delivery path now.

**3. Priority 4 stays open and unevaluated.** Correct call. Priors labelled as priors
is the honest state. It is not on the critical path and it will not be until we have
more than one customer.

---

## The last run

One final full pass, then stop:

- Full suite green, `test_golden.py` exactly **31**, no locked file changed beyond the
  `sqlguard` wiring I applied.
- `ship/` current against the guarded adapter, `MANIFEST.txt` regenerated, `sqlguard.py`
  present.
- Working tree clean, everything pushed, and tell me the **exact commit** the operator
  will clone.
- One more fresh-clone rehearsal **from the pushed commit** — not from a local copy —
  to confirm what is on GitHub right now is what you rehearsed.

Paste the raw output. If anything is not green, say so plainly rather than explaining
it away.

## Then answer these

1. **Is there anything you need from me** before he stands in front of that machine?
2. **Anything you would do differently** if this were being installed tomorrow rather
   than today?
3. **What is most likely to go wrong at the shop**, ranked — and for each, is the
   failure loud, and does the transcript say enough for us to diagnose it from here?

Question 3 is the one I care about. I would rather know the top three risks now than
discover them by phone while he is standing at a counter.
