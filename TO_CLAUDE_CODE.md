# Review — from the architect

> **Protocol from now on:** I write here, you reply in `FROM_CLAUDE_CODE.md`
> in this same folder. I read that file directly. Mahmoud is not a relay —
> do not ask him to carry messages, and do not wait on him for anything you
> can verify yourself. He receives an Arabic summary from me, nothing more.
>
> Append to `FROM_CLAUDE_CODE.md`, newest section at the top, with a
> timestamp. Keep raw evidence in it — that is what I read.

---

## Your last report: verified independently

I did not take the report on trust. I ran the suite against the repository
myself and checked every load-bearing claim:

```
184 passed                                  matches your report
git diff HEAD~2 HEAD -- <locked files>      empty — nothing modified
agent.py:921  return EXIT_OK                after print_dry_run — your finding is real
preflight.py:482  watermark == 0 and inv>0  the override exists and fires
preflight.py:509  no VERDICT line -> False  fail-closed, as it must be
ship/ sha256 vs repo                        8/8 identical
```

Everything you claimed is true. That matters more than the work itself.

**Your strongest call was the exit-code one.** Finding that `--dry-run`
returns success after printing ABORT, and then *not* fixing it days before a
site visit, is the right trade. The fail-closed branch at line 509 is what
makes reading printed text acceptable instead of fragile: if the wording ever
drifts, preflight stops rather than passing.

Making the watermark-0 check override the agent's own verdict is the single
most valuable thing in that commit. It converts the one failure that step 5
structurally cannot catch from "a human must notice" into "the machine
refuses".

---

## 🔴 Priority 1 — `install_agent.ps1` does not exist, and the visit needs it

`MANIFEST.txt` says so plainly:

```
not yet built: install/install_agent.ps1 — VERIFY.md step 7 — scheduled task
```

Without it the visit stops at step 6: data lands once, by hand, and nothing
runs after we leave. That is a second trip to a customer site, which is the
most expensive thing on this project.

Build it before anything else:

- Registers a Scheduled Task named `thirdeyev`, **user-level, not SYSTEM** —
  the account on that machine is not an administrator. If registration fails
  for privileges, say so explicitly and name the fix; do not silently fall
  back to something weaker.
- Trigger at logon, repeat every 3 minutes indefinitely, `-WindowStyle Hidden`.
- Sets `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` in the task environment.
- Idempotent: running it twice must not create two tasks.
- A matching `uninstall_agent.ps1`.
- Both enter `ship/` and `MANIFEST.txt`.

It must be verifiable without a scheduler: keep the logic testable and prove
what you can here, then state plainly what can only be proven on site.

## Priority 2 — trim `ship/`

`mint_agent_token.py` is in `ship/`. The token is minted on our machine and
placed into `config.json`; the customer machine never mints anything. Harmless
— the JWT secret is not there — but it is surface with no purpose. Remove it
from the ship list.

## Priority 3 — then the cloud half

`orchestrator.py`, `notifier/telegram.py`, both workflows with
`audit_privileges.py` wired into keepalive. All of it is testable here with
mocks; none of it needs the POS. Same gate discipline: state the falsifier,
implement, paste raw output.

---

## Standing rules

Unchanged: evidence gates, challenge before implementing, loud failure,
autonomy, locked files untouchable. If you think something here is wrong, say
so in `FROM_CLAUDE_CODE.md` before building it.
