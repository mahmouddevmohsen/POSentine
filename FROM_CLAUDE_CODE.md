# Replies to the architect

Newest section at the top.

---

# 2026-08-10 16:40 UTC — probe fixed, ZIP integrity closed, one thing I did not do

**329 passed** (was 311), `test_golden.py` exactly **31**, no locked file touched.
Commit `d7ede19`. Session log at `logs/session_20260810_160838.log` — every command and
every failure, per your rule 2.

---

## STEP 1 — state, before I changed anything

- **Committed:** `HEAD = origin/main = 467fa89`, clean tree, nothing unpushed.
- **In progress:** nothing half-finished. Untracked were the customer's unpacked
  diagnostics. **I checked specifically: no diagnostics file was ever committed**, so no
  customer data reached the repo or GitHub. They are still untracked and I left them that
  way — they carry the till's username and folder layout.
- **Broken:** the probe defect, exactly as you diagnosed.

Two things the bundle showed that your note did not name, and both matter:

**The truncation had a specific cause.** `_wrap(message, width - 10)` in `format_report`.
SQL Server puts the error number near the **end** of the message, so trimming the tail
removed precisely the part that identifies it. That is why you got
`...[SQ...` instead of `(8102)`.

**`fn_my_permissions` returns one row per column**, so the report printed `SELECT` 46
times. Establishing that UPDATE was absent meant reading 46 identical tokens.

---

## STEP 2 — the fix, all four parts

### (a) + (b) The column is asked for, not assumed

```sql
SELECT TOP 1 c.name
  FROM sys.columns c
  JOIN sys.tables tb ON tb.object_id = c.object_id
  JOIN sys.schemas sc ON sc.schema_id = tb.schema_id
  JOIN sys.types ty ON ty.user_type_id = c.user_type_id
 WHERE sc.name = ? AND tb.name = ?
   AND c.is_identity = 0
   AND c.is_computed = 0
   AND c.is_rowguidcol = 0
   AND ty.name NOT IN ('timestamp', 'rowversion')
 ORDER BY c.column_id
```

`rowguidcol` and `rowversion` are in there because they fail the same way for the same
reason (Msg 272), and finding that out on customer #2 would be this bug again wearing a
different number.

`write_probes(columns)` takes the mapping as a **required** argument. A default would be
a hardcoded schema, and a hardcoded schema is the bug — there is a test that fails if
anyone gives it one back.

**A table with no writable column** reports `NO PROBEABLE COLUMN`, blocks the install,
and says in the report that this is a limitation of the probe and **not** evidence either
way. It never silently reduces what was tested.

### 🔴 One thing I fixed that you did not ask for, and I want you to overrule it if you disagree

**The INSERT probe had the same defect.** It was

```sql
INSERT INTO dbo.Sales (salid) SELECT salid FROM dbo.Sales WHERE 1 = 0
```

`salid` is the identity column. It returned `229` on this machine only because permission
is checked before the identity rule — **the moment a login actually held INSERT, that
probe would have returned Msg 544** ("cannot insert an explicit value into an identity
column") and been INCONCLUSIVE for exactly the reason the UPDATE probes were. It is the
same bug one statement over, waiting for a customer whose grants are slightly different.

It now uses the same discovered column. One line, same function, and leaving a known twin
of the bug in place while fixing its sibling did not seem like the smaller change.

### (c) The error number survives, and every non-refusal says why

```
    REFUSED              UPDATE dbo.Sales
      UPDATE dbo.Sales SET saltot = saltot WHERE 1 = 0
      SQLSTATE 42000   Msg 229
      [42000] [Microsoft][ODBC Driver 11 for SQL Server][SQL
      Server]The UPDATE permission was denied on object 'Sales',
      database 'HD_Rest_Cashier', schema 'dbo'. (229)
      (SQLExecDirectW)
```

The number is on its own line before the message, so it survives even if the message is
mangled. `_wrap` is replaced by `_fold` for server text — it wraps onto as many lines as
it takes and loses nothing.

### (d) The tests

- `Msg 8102/544/271/272` → `PROBE DEFECT`, asserted to be neither `PERMITTED` nor
  `REFUSED`.
- A probe defect **still blocks the install** — there is a test asserting `passed is
  False`, so nobody can "fix" this by relaxing the gate.
- A probe defect **must not print** `These credentials can change the customer's POS
  database`, with a paired falsifier asserting that wording **does** appear when a write
  is genuinely permitted.
- A test that fails if any probe targets `salid`/`saledeid`/`Itid`.
- A test that fails if `write_probes()` regrows a default schema.
- A test that `Msg 229` is still a clean `REFUSED` — without it, structural detection
  could swallow real permission denials and the probe would stop proving anything.

### The reproduction

Simulated the customer's schema — identity keys raising 8102, everything else raising 229:

```
A. THE CUSTOMER'S MACHINE, WITH THE FIX
    dbo.Sales        saltot        (not identity, not computed)
    dbo.SalesDe      saleprice     (not identity, not computed)
    dbo.Items        itsaleprice   (not identity, not computed)
    ... 9 probes, all REFUSED, all Msg 229 ...
  VERDICT: READ-ONLY CONFIRMED
  passed = True

B. IF DISCOVERY EVER RETURNED AN IDENTITY COLUMN ANYWAY (Msg 8102)
  VERDICT: CANNOT VERIFY - OUR PROBE IS AT FAULT, NOT THIS LOGIN
    ** UPDATE dbo.Sales / dbo.SalesDe / dbo.Items
    The install is blocked deliberately. Send this block and the
    diagnostics zip - this is fixable from our side, and it does
    NOT mean anything is wrong with this machine.
  passed = False
  says 'NOT READ-ONLY': False
```

**A is the install that should have happened yesterday.** B is the belt-and-braces: still
blocked, still failing closed, but no longer accusing a customer whose credentials were
fine.

### What I did not do

**I did not touch the aggregate verdict logic.** `passed` still requires every write
`REFUSED`; `PROBE_DEFECT` and `NO_PROBEABLE_COLUMN` were added to what fails it, not
removed. The only thing that changed is the *wording* of the failure. You asked me to stop
if I found myself relaxing the gate — I didn't, and the test named
`test_a_probe_defect_blocks_the_install` exists so that a future session cannot.

---

## STEP 3 — I recommend **Option B**, and I think the framing of A needs one correction

**Recommendation: B. Implemented.**

The correction: **A does not work for this customer.** They installed from
`C:\Users\Techno\Downloads\POSentine-main` — a repository ZIP — which means there is no
git on that till. "Require `git clone`" is not a wording change, it is a **new dependency
on a machine we do not control, discovered at the counter.** That is the same trade you
rejected for `monitor_ro.sql`, and I think it fails for the same reason.

There is also a third fact neither option mentioned: today **`NOT VERIFIED` does not stop
the install.** `verify_manifest` returns it as a status string and preflight prints it and
carries on. So the customer machine did not merely lack a check — it ran without one and
said so in passing.

**What I built:** `python make_ship.py --zip` produces the artifact the operator
downloads.

```
release artifact: posentine-467fa89f6bcf.zip  (125 KB)
  MANIFEST.txt present: True
  fake_adapter present: False
  tests present       : False
  correspondence      : False
  built from: 467fa89f6bcfb1e3d5cd3dc9b0f14442b08fb1c2
```

Unpacked where there is no `.git`, as the operator would:

```
  .git present: no
  code integrity   OK - 25 files match MANIFEST.txt

  and after editing one file:
  STOPPED: files differ from the versions we verified: ['agent.py']
```

It also fixes something I flagged at handover and never acted on: a repository ZIP puts
`fake_adapter.py`, the whole test suite and **our correspondence** on the customer's till.
This carries 24 files.

Two tests cover it, and the `NOT VERIFIED` wording now names the cause and the fix instead
of shrugging. `posentine-*.zip` is gitignored — committing it would be a second copy of
the code, which is what `ship/` exists to prevent.

**One decision I did not make unilaterally, and want from you:** should `NOT VERIFIED`
become a **hard stop**? It is the safer behaviour and the reason the artifact now exists.
I left it non-fatal because turning it into a stop would have blocked yesterday's install
outright, and that is your call to make, not mine to slip into a bug-fix commit.

---

## STEP 4 — the PyInstaller task

**Marked DEFERRED, not closed**, with the reason written into `README.md` so it survives
this conversation. It records that it is **unevaluated**, that what exists is **priors,
not findings**, and that both investigations died before returning anything. Your decision
that it is not to be attempted before the visit is recorded alongside it, as is the
`monitor_ro.sql` decision.

---

## Rule 5 — the recurring shape, sixth instance

The identity probe is the sixth, and it is the purest one yet: **a check that could not
fail correctly, because the statement it sent could not succeed for a reason unrelated to
what it was testing.** It would have returned INCONCLUSIVE on *any* schema with an
identity primary key, which is nearly all of them — the check was never really testing
permissions at all.

What the earlier five have in common is that the check shared a source with the thing it
checked. This one is a variant: **the check shared a failure mode with something that was
not the subject.** The question that finds it is a sibling of the usual one:

> *If this check failed, would I know why?*

The probe could not answer that, and `INCONCLUSIVE` with a truncated message was the
symptom. That is why (c) is not cosmetic.

---

## What is still open

- **Your call on whether `NOT VERIFIED` becomes a hard stop.**
- **Nothing is pushed yet** — the commit is local. Say the word and I push, or push it
  yourself; I did not want to publish a change to the install path without you seeing it.
- The release zip is **built but not attached to a GitHub Release.** That is an
  outward-facing publish and I have not done it.
- PyInstaller / generated config: deferred, unevaluated.
- `monitor_ro.sql`: written, deliberately not applied, per your decision.

## What I need from you

1. Hard stop on `NOT VERIFIED`, yes or no.
2. Whether the INSERT-probe change is in scope or should be reverted.
3. Whether to push `d7ede19` and cut the Release.

---

# 2026-08-10 02:00 UTC — three checks. All three settle green. Two things to read first.

No code changed. Raw output below.

**Read these two before the rest:**

1. **`config.json` is not in the project root.** It is at **`Docs/config.json`**. I ran
   all three checks against that file. If you meant to put a copy in the root, it is not
   there — and on the till it must sit **next to `agent.py`**, not in `Docs/`.
2. **The token test wrote a heartbeat that cannot be removed** — the token has no
   `DELETE`, by design. Detail in check 1c. It is harmless, and I have proven it does not
   disturb first-run detection, but you should know it is there.

---

## Check 1 — the token against live Supabase

`https://mwwjfeporhfhcekmektg.supabase.co`, two-header pattern, the agent's own client.

### 1a. Authenticated read

```
    [ OK ] sync_state      readable, 1 row(s) for this tenant/source
    [ OK ] invoices        readable, 0 row(s) for this tenant/source
    [ OK ] heartbeats      readable, 0 row(s) for this tenant/source
    [ OK ] pos_products    readable, 0 row(s) for this tenant/source
    [ OK ] invoice_lines   readable, 0 row(s) for this tenant/source
    [ OK ] cash_counts     readable, 0 row(s) for this tenant/source
    [ OK ] pos_users       readable, 0 row(s) for this tenant/source
```

All seven agent tables. Gateway accepted the anon key, RLS accepted the token.

### 1b. Authenticated write

```
    [ OK ] heartbeats     INSERT accepted, id=2, at=2026-08-10T01:54:23.784595+00:00
    [ OK ] sync_state     UPDATE accepted (matched 0 row(s); lt.0 guard means it changes nothing)
```

The `sync_state` UPDATE went through the agent's real monotonic guard
(`watermark_salid=lt.0`), so it matched nothing and changed nothing — which is the
correct behaviour and also why it is safe to have run.

### 1c. 🔴 Cleanup is impossible, and that is the design

```
    [ OK ] DELETE refused - the token holds INSERT/SELECT/UPDATE and no DELETE
           DELETE heartbeats failed: HTTP 403 {"code":"42501", ...}
```

You asked me to clean up after the write. **I cannot, and neither can the agent.**
`schema_v2_grants.sql` gives `authenticated` `select/insert/update` on the seven tables
and deliberately no `DELETE`. That refusal is a property we want, so I did not look for a
way around it.

What is left behind, permanently:

```
    heartbeats id=2   ok=true   drift_seconds=null   rows_pulled=0
    note kind: "pre_visit_token_test"
```

I chose `heartbeats` precisely because it is append-only telemetry — an extra row is
noise, not state. Three consequences, all checked:

- **It does not make the agent think it has already synced.** First-run is decided on
  `sync_state.watermark_salid`, never on heartbeats. Proven below.
- **It will not trip Phase C.** `--confirm` reads the *newest* heartbeat, and Phase B
  runs a real cycle before Phase C, so the real one will be newer.
- **It would trip `--confirm` if run standalone right now**, because its
  `drift_seconds` is null and `--confirm` now refuses a heartbeat with no clock reading.
  That is the check I added on your last note working as intended. It resolves the moment
  a real cycle runs.

### 1d. Tenant isolation

```
    [ OK ] refused, code 42501
           POST heartbeats failed: HTTP 403 {"code":"42501", ... "message":"new row
           violates row-level security policy ..."}
```

A foreign `tenant_id` is refused with **42501**. Same result as gate 3.

### 1e. Least privilege

```
    [ OK ] events               denied, code 42501
    [ OK ] outbox               denied, code 42501
    [ OK ] tenants              denied, code 42501
    [ OK ] internal_anomalies   denied, code 42501
    [ OK ] sources              denied, code 42501
    [ OK ] alert_settings       denied, code 42501
    [ OK ] shift_reports        denied, code 42501
```

I added the last three beyond your list; same answer.

### 1f. One result I will not claim as proof

```
    [ OK ] events INSERT denied, code PGRST204
```

`PGRST204` is *"column not found in schema cache"* — PostgREST rejected my payload shape
**before** it reached the permission check. So that particular line proves nothing about
privileges. The clean proof is 1e: `events` is denied `42501` at the table level, so no
insert can reach it. I am flagging it rather than letting a green tick stand for
something it did not test.

**Check 1 verdict: the token reads, writes, is confined to its tenant, and is confined to
its seven tables.** Signature-valid and gateway-accepted are now the same claim.

---

## Check 2 — the `sync_state` row

Your query joins `sources` and `tenants`. The agent token is denied on both (42501,
above), so I could not run it as written and did not try to route around it. This is what
the token can see:

```
  rows visible to this token: 1
    tenant_id              57b61b47-a590-49fe-803c-0c174a07b7ec
    source_id              93f8d146-ba68-4d58-8eda-f797f3e28bd4
    watermark_salid        0
    watermark_saledeid     0
    rescan_from_salid      0
    pos_max_salid          None
    last_sync_at           None
    last_rescan_at         None
    restore_suspected      False
    schema_ok              True

  exactly one row              YES
  watermark_salid = 0          YES
  restore_suspected = false    YES
  schema_ok = true             YES

  => MATCHES what you asked for
```

`last_sync_at` is null, which is consistent: nothing has ever synced. **Risk #3 is
closed.**

The one thing I have *not* verified is that this row's `tenant_id`/`source_id` map to
`sobh_onthefast` and the right source — that needs the join, and the join needs
`service_role`. The IDs match `config.json` exactly, which is what the agent checks. If
you want the slug confirmed, it is one query from your side.

And the reason it matters that this row exists:

```
  reconcile_with_cloud -> is_first_run = True
  (decided on sync_state.watermark_salid, not on heartbeats)
  heartbeats for this tenant/source now: 1
```

So the agent will correctly treat the shop as a first install, adopt `MAX(salid)`, and
backfill nothing — with my stray heartbeat sitting there and changing none of it.

---

## Check 3 — does *this* config pass the agent's own validation

### 3a. The real code path

```
  [ OK ] Config.load            accepted the file
         required keys          all present
         placeholder guard      passed (no <angle bracket> values)
         assert_is_agent_token  passed - decoded, role and tenant_id checked

         aud         authenticated
         exp         1944006409
         iat         1786326409
         iss         supabase
         role        authenticated
         tenant_id   57b61b47-a590-49fe-803c-0c174a07b7ec
         expires     2031-08-09T01:46:49+00:00

  [ OK ] preflight sql block   complete, no placeholders
```

And the refusals still refuse, exercised against this file's own tenant:

```
  [ OK ] service_role refused: agent token has role='service_role', must be 'authenticated'
  [ OK ] wrong tenant refused: agent token is for tenant 00000000-...-000000000000, but co...
```

`expires 2031-08-09` matches the `exp=2031-08-09` in your note, and `aud`, `iss`,
`role` and `tenant_id` match line for line. Five years, as intended.

### 3b. 🎯 Your suspicion was right: preflight never exercises the token here

**Run A — real Supabase URL:**

```
  [ OK ] golden baseline: 31 passed

  VERIFY.md step 3b — read-only proof (attempts to write, requires refusal)
         attempting UPDATE, DELETE and INSERT against the POS database.

  STOPPED at VERIFY.md step 3b — read-only proof
    could not connect to the POS database: ('08001', '[08001] [Microsoft][ODBC SQL
    Server Driver][DBNETLIB]SQL Server does not exist or access denied. (17) ...
```

**Run B — byte-identical config except `supabase_url` pointed at
`https://this-host-does-not-exist-posentine.invalid`:**

```
  STOPPED at VERIFY.md step 3b — read-only proof
    could not connect to the POS database: ('08001', '[08001] [Microsoft][ODBC SQL
    Server Driver][DBNETLIB]SQL Server does not exist or access denied. (17) ...
```

**Identical stop with Supabase unreachable.** That is the proof rather than my reading of
the source: nothing contacted Supabase before step 3b, so on this machine **check 1 above
is the only place the token is proven.** You were right to ask.

**What happens on site is different, and worth being precise about.** Once the POS
connects, step 3b passes and step 4 runs `agent.py --dry-run`, which calls
`reconcile_with_cloud` — a Supabase **read**. So the order at the shop is:

| Step | Touches Supabase | What it proves |
|---|---|---|
| 3b | no | the POS refuses our writes |
| 4 | **read** | the token authenticates and the gateway accepts it |
| Phase B | **write** | the token can actually upload |
| Phase C | read | the data landed |

A token that reads but cannot write would pass step 4 and fail at Phase B — a clean stop,
not a silent one. Check 1 has now removed that possibility in advance.

### One thing that happened on the way, worth a line

The first run of check 3b stopped before it started:

```
  STOPPED at VERIFY.md step 0 — code integrity
    files in this clone have been edited: ['TO_CLAUDE_CODE.md']
    What to do: git checkout -- .
```

That was your note, uncommitted. The git-based integrity check added yesterday bit on a
real edit the first time it met one. It is live and it works.

---

## Summary

| Check | Result |
|---|---|
| 1. Token reads / writes / isolated / least privilege | ✅ all four |
| 2. `sync_state` row provisioned as required | ✅ exactly one, watermark 0 |
| 3a. This config passes the agent's own validation | ✅ |
| 3b. Does preflight exercise the token before stopping | ❌ **no** — proven, and check 1 is why that is now covered |

**Nothing failed.** Two things for you to act on, neither of which I changed:

- **Put `config.json` where the agent will look for it** — next to `agent.py` on the
  till, not in `Docs/`. On this machine it is only in `Docs/`.
- **Heartbeat `id=2` is permanent** and will be the newest until the first real cycle.
  Expected, harmless, and named here so nobody wonders what it is when they read
  `--confirm` output on site.

Nothing else was touched. Working tree clean apart from this reply.

---

# 2026-08-10 01:00 UTC — SIGN-OFF. Last run green. Three answers.

Everything below is raw output. Nothing is green that I had to explain.

---

## The last run

### 1. Full suite

```
$ python -m pytest -q
........................................................................ [ 92%]
.......................                                                  [100%]
311 passed in 5.80s
```

### 2. Golden baseline

```
$ python -m pytest -q test_golden.py
...............................                                          [100%]
31 passed in 0.06s
```

### 3. Locked files — every change since before this work began

```
$ git diff --stat 5926951 HEAD -- adapter_hdsoft.py metrics.py events.py \
                                   report.py test_golden.py schema.sql
 adapter_hdsoft.py | 6 +++++-
 1 file changed, 5 insertions(+), 1 deletion(-)
```

The whole of it — your wiring patch, and nothing else:

```diff
+import sqlguard
@@
     cn.autocommit = True
-    return cn
+    # نقطة الاختناق: كل أمر بيعدي على sqlguard.assert_read_only قبل ما
+    # يوصل للسيرفر. مش قاعدة مراجعة - مسار كود بيرمي استثناء.
+    return sqlguard.guard(cn)
```

`metrics.py`, `events.py`, `report.py`, `test_golden.py`, `schema.sql`: **zero changes.**

### 4. `ship/` regenerated and verified

```
files in MANIFEST.txt: 25
mismatches: NONE
sqlguard.py present: True | 7c2c0962c7d6e11c1a8f4076161837eec84308e4fe62ed69e687a5805cff8078
adapter_hdsoft.py   : c67cd4913c843e3c41f8ee2d463c7e420e53ee6f02a505ce37cf406d73e08195
```

`c67cd491…` is the **guarded** adapter (the pre-patch copy was `f75ef36e…`).

### 5. 🎯 The commit the operator will clone

```
d38eb247110d037fe402099480856b6cf6a80af5
```

Short form **`d38eb24`**. Local `HEAD` and `origin/main` are the same object.

### 6. Fresh-clone rehearsal — from GitHub, from that commit

```
$ git clone https://github.com/mahmouddevmohsen/POSentine.git final
$ git rev-parse HEAD
d38eb247110d037fe402099480856b6cf6a80af5
--- does it match what we pushed? ---
YES - identical to origin/main d38eb24
```

Then a real `config.json` with a minted 279-character token, and the one-click entry
point:

```
  code integrity   OK — clean checkout of commit d38eb247110d

  VERIFY.md step 1 — console and Python
  [ OK ] Python 3.11.15
  [ OK ] console encoding utf-8

  VERIFY.md step 2 — dependencies
  [ OK ] requirements installed
         pyodbc 5.3.0
         drivers ['SQL Server']
  [ OK ] picked SQL Server

  VERIFY.md step 3 — config and token
  [ OK ] TOKEN OK — role is 'authenticated' and tenant_id matches config
  [ OK ] sql block complete — monitor_ro@localhost\HDSOFT/HD_Rest_Cashier
  [ OK ] golden baseline: 31 passed          <-- run on the cloned machine

  VERIFY.md step 3b — read-only proof
         attempting UPDATE, DELETE and INSERT against the POS database.
```

and the stop, which **is** the pass condition here:

```
  Failed in PHASE A — VERIFY.md step 3b — read-only proof

  WHAT FAILED
    could not connect to the POS database: ('08001', '[08001] [Microsoft][ODBC SQL
    Server Driver][DBNETLIB]SQL Server does not exist or access denied. (17)
    (SQLDriverConnect); ... ConnectionOpen (Connect()). (67)')

  WHAT TO DO
    Check the sql block in config.json against the
    machine's actual SQL Server instance name.
    Photograph this and call.

  THE STATE OF THIS MACHINE
    Nothing was written to the POS or to the cloud, and
    no scheduled task was registered. This machine is
    exactly as it was before you double-clicked.

=== installer exit = 1 ===
```

### 7. Transcript and diagnostics, from that same clone

```
TRANSCRIPT  install_20260810_005359.txt  123 lines   secrets: NONE
ZIP         diagnostics_20260810_005453.zip  11 files, 6 KB
            config.json present: False   secrets: NONE
```

All three secrets (agent token, anon key, SQL password) checked whole **and** by leading
24-character fragment. Absent from both.

### One thing to be precise about

This reply changes `FROM_CLAUDE_CODE.md`, so the final pushed commit is one *after* the
commit I rehearsed. **No code moved:**

```
$ git diff --stat d38eb24 6b999ca
 FROM_CLAUDE_CODE.md | 286 ++++++++++++++++++++++++++++++++++++++++++++++++++++
 1 file changed, 286 insertions(+)

$ git diff --name-only d38eb24 6b999ca | grep -v '\.md$'
NONE - every code, script and config file is byte-identical
```

So: **rehearsed on `d38eb24`, final commit `6b999ca`, identical code.** Have him clone
`6b999ca` (i.e. just clone `main`) so this document travels with him.

---

## 1. What I need from you before he stands at that machine

Four things, in the order they will stop him.

**a) The exact SQL connection details.** `config.example.json` guesses
`localhost\HDSOFT` and `HD_Rest_Cashier`. If either is wrong, he stops at step 3b with
the block above — which is a clean stop, but it is a stop, and it is *the single most
likely one*. If anyone can read the instance name off that machine, or off an HD Soft
config file, before the visit, it removes the top risk entirely.

**b) Confirmation that `monitor_ro` exists, its password, and that Mixed Mode
authentication is ON.** SQL Server ships Windows-auth-only. If Mixed Mode was never
enabled, `monitor_ro` cannot log in at all, and fixing it needs a SQL Server
administrator and a service restart — not something to discover at a counter.

**c) 🔴 Confirm the `sync_state` row exists in Supabase for this `tenant_id` +
`source_id`.** This one is ours, it is invisible from the till, and it looks like a POS
problem when it is not. `reconcile_with_cloud` **refuses to run** without that row:

> `no sync_state row for this tenant/source. That is a provisioning problem, not an
> empty history — refusing to treat it as a first install, which would skip everything
> already synced.`

That refusal is correct and I would not soften it. But it costs a trip if it is
discovered on site. One `select` from our side settles it.

**d) A prepared `config.json`, handed over as a file.** Do not have him type or paste a
279-character JWT into anything. Put the finished file on the USB stick next to the
folder. Every failure mode of hand-editing that we catch, we catch *after* he has
already made the mistake.

Optional but worth two minutes: confirm the till account can register a scheduled task.
I proved it works for a non-administrator on *this* machine; a managed machine with
group policy is a different question, and the installer stops cleanly if it cannot.

---

## 2. What I would do differently if this were tomorrow rather than today

**I would not change the code.** It is where I want it, and changing anything now trades
a known state for an unknown one the day before a visit — the same reasoning you applied
to `monitor_ro.sql`, and I think it applies here too.

What I would change is the **preparation**, all of it off the critical path:

- **Send the four items above ahead of the visit**, especially (c). Three of the top five
  risks below disappear if a) and b) are answered in advance.
- **Have a SQL Server administrator reachable by phone** for the 45 minutes he is there.
  Two of the top three failures need someone with rights we do not have.
- **Ask him to run `preflight.bat` first, not `INSTALL.bat`.** It is the same checks
  without Phase B onwards, it writes nothing to the cloud, and it takes two minutes. If
  it passes, `INSTALL.bat` will get to Phase D. If it stops, he has lost two minutes
  instead of ten and the queue has not moved. **This costs nothing and I would do it.**
- **A Release zip built from `ship/`** — you have already ruled this out for this visit
  and I agree. Customer #2.

---

## 3. What is most likely to go wrong, ranked

The question you care about. Loud = he cannot miss it. Diagnosable = the transcript alone
is enough for us to say what to do next, from here.

| # | What | Likely | Loud? | Diagnosable? | Who can fix it on site |
|---|---|---|---|---|---|
| 1 | SQL instance/database name wrong | **High** | ✅ | ✅ | Him, if he has the name |
| 2 | `monitor_ro` missing / wrong password / Mixed Mode off | **Medium-high** | ✅ | ✅ | **Nobody** — needs a SQL admin |
| 3 | No `sync_state` row provisioned | Medium | ✅ | ✅ | **Nobody on site** — us, in seconds |
| 4 | Read-only probe returns `INCONCLUSIVE` | Low-medium | ✅ | ✅ | **Nobody** — us, in minutes |
| 5 | No ODBC driver on the machine | Low-medium | ✅ | ✅ | Him, with the installer |
| 6 | Dry-run cross-check returns `ABORT` | Low | ✅ | ✅ | **Nobody — and it must not be** |
| 7 | HTTPS to `*.supabase.co` blocked | Low | ✅ | ✅ | Whoever runs the network |
| 8 | Phase E times out | Low | ✅ | ✅ | Us, remotely |

### The three I would actually worry about

**#1 — the SQL connection details.** This is the one the rehearsal hit, and it is the one
most likely to happen. The stop is clean, names the file and the field, and the
transcript carries the verbatim ODBC error including the SQLSTATE. **Nothing was written
anywhere**, so he can fix the field and double-click again. It is entirely preventable
by answering question 1(a).

**#2 — `monitor_ro` cannot log in.** `Login failed for user 'monitor_ro'` is already
mapped to its own instruction ("Wrong password, or SQL Server Mixed Mode auth is off.
STOP. Call."). Loud and diagnosable — but **he cannot fix it and neither can we
remotely.** It needs a SQL Server administrator on that machine. If this happens without
one available, the visit fails and we go back. That is the trip-costing risk, and
question 1(b) is what removes it.

**#3 — the read-only probe returns `INCONCLUSIVE`.** The risk I own. My classifier
recognises SQL Server permission errors by native code (`229`/`230`/`262`) with a message
fallback. Those codes are stable across versions, and checking the *code* rather than the
text means a localised SQL Server still classifies correctly — but I have never run this
against a real SQL Server, and **an inconclusive answer deliberately stops the install.**

It is worth being clear about what that means: **this can stop a perfectly good install.**
I still think the rule is right — "we could not tell" and "it is refused" must never
produce the same outcome — but it is a false-stop I can cause and you should expect it as
a possible outcome rather than a surprise.

The mitigation is that it is cheap to clear: the transcript prints the SQLSTATE, native
number and full message **verbatim for every probe**, which is exactly what I need to fix
the classifier. Turnaround is minutes, and he re-runs `preflight.bat --skip-install`
rather than starting over.

### One I want to name separately, because it is the dangerous kind

**#6, `VERDICT: ABORT` on the dry-run cross-check.** Low probability, and if it happens
**nobody should fix anything on site.** It means we are reading their data wrong, and
every number after that point would be confidently incorrect. The correct outcome is that
he photographs it, changes nothing, and we go home and think. The installer will not let
him past it, which is the whole reason it exists.

### What none of this covers

Everything downstream of the first successful POS connection — the dry-run numbers, a
real `LastTaskResult: 0`, the first shift report reconciling against
**يومية الخزينة**. Those cannot be rehearsed from here and they are not in the table
above because I have no basis for ranking them.

---

## Closing

Priority Zero is enforced and proved on every install. The install is one click with
every gate intact and a wait that proves the agent runs without a human. The logs are
bounded, masked, and there is one file to send when something goes wrong.

Five checks on this project turned out to share the fault they were meant to detect. The
question that found the last two was cheap and I would use it again on anything: *what
would this check still pass if it were broken?*

Thank you for insisting on the empirical version of everything. The trigger finding, the
closure test, and the fresh-clone rehearsal all came from you refusing to accept a
verification that had not actually been run.

Good luck at the shop.

---

# 2026-08-10 00:35 UTC — FINAL. Closing gate passed, pushed. Handover below.

**311 passed** (was 209 at the start of this work), `test_golden.py` exactly **31**.
Pushed to `origin/main` at **`3e804bc`**. `ship/` rebuilt against the guarded adapter.

Three defects found during the closing gate itself, all fixed. **Two of them would
have shipped**, and one of them is the exact thing you told me to verify rather than
assume.

---

## The closing gate

### 🔴 You were right to make me check that the closure test fired. It did not.

I removed `sqlguard.py` from `SHIPPED` and ran the import-closure test. **It passed.**

`_local_imports` walked only the direct imports of the three entry points.
`sqlguard` is imported by `adapter_hdsoft.py` — one level down — so it was invisible:

```
agent.py           -> ['adapter_hdsoft', 'logsetup', 'mint_agent_token', 'rows', 'supa']
preflight.py       -> ['adapter_hdsoft', 'agent', 'mint_agent_token', 'readonly_probe']
test_golden.py     -> ['adapter_hdsoft', 'events', 'metrics']

adapter_hdsoft.py  -> ['sqlguard']   <-- never walked
```

A ship folder with no `sqlguard.py` would have passed every check we have and given
`ModuleNotFoundError` on the till at install time — **with the SQL guard absent
entirely.** A test called "closed under import" that closes over one level is not a
closure. Now transitive, and it fails correctly:

```
E   AssertionError: agent.py reaches ['sqlguard.py'], which ship/ does not contain.
FAILED test_the_ship_list_is_closed_under_transitive_import[agent.py]
FAILED test_the_ship_list_is_closed_under_transitive_import[preflight.py]
FAILED test_the_ship_list_is_closed_under_transitive_import[test_golden.py]
```

It has a falsifier that fails if `sqlguard` ever becomes a *direct* import of
`agent.py`, because at that point the test stops proving the walk is deep.

`ship/` rebuilt: `adapter_hdsoft.py` is now `c67cd4913c84` (was `f75ef36e6d34`) — the
guarded copy. `sqlguard.py` `7c2c0962c7d6` is in it. 25 files.

### 🔴 A fresh clone had no integrity check at all

The rehearsal found this immediately. **`ship/` is gitignored**, so a `git clone` has
no `ship/` and no `MANIFEST.txt`. Step 0 printed `NOT VERIFIED` — the strongest check
in the whole procedure, silently downgraded on **the exact path the operator takes.**

`verify_manifest` now falls back to git. A clean checkout of a named commit is a
*stronger* statement than a manifest, not a weaker one — it ties the machine to the
commit our tests ran against:

```
code integrity   OK - clean checkout of commit f33fa8a961e8
```

Untracked files are ignored deliberately (`--untracked-files=no`): `config.json`,
`state.json`, `agent.log` and `logs/` all appear after cloning, and treating them as
drift would stop every real install. An edited **tracked** file stops with the file
named and `git checkout -- .` as the fix. A copied `ship/` folder keeps using its
manifest. Five tests, against a real git repo rather than a mock.

### 🔴 VERIFY.md step 7 would have stopped a healthy install

You said to read it against the code. It told the operator:

> Expect `LastTaskResult : 0`.

immediately after registering. **A freshly registered task reports `267011` until its
first run.** Following the document, the operator stops a perfectly good install — the
same fault as before, inverted. Now it explains `267011`, and points at the field that
actually matters:

> **The field that matters right now is `NextRunTime`.** If it is empty, the task is
> registered but *not scheduled*, and it will never run. **STOP IF `NextRunTime` is
> empty.**

It also now says **do not use `Start-ScheduledTask`** — starting it yourself proves the
task *can* run and says nothing about whether it *will*, which is the whole point of
the wait. The logon-only wording is corrected, and the rollback is documented. Step 0b
no longer claims preflight writes nothing anywhere: it names step 3b's refused probes,
`pip`, and the transcript.

### The rehearsal — cloned from GitHub, exactly as he will

```
git clone https://github.com/mahmouddevmohsen/POSentine.git rehearsal
```

Then a real `config.json` with a minted 279-character token, and the one-click entry
point. All six of your steps:

| # | Step | Result |
|---|---|---|
| 1 | Clone the pushed repo | ✅ `f33fa8a` |
| 2 | Place a real `config.json` | ✅ |
| 3 | Run the one-click entry point | ✅ |
| 4 | Reach the POS failure and stop cleanly | ✅ |
| 5 | Transcript exists, readable, no secret | ✅ 123 lines, 3/3 secrets absent |
| 6 | Diagnostics zip produced, secret-free | ✅ 11 files, 7 KB, 3/3 secrets absent |

It got through integrity, Python, dependencies, config, the decoded token, **31 golden
tests on the cloned machine**, and stopped at step 3b on the real driver error:

```
  Failed in PHASE A — VERIFY.md step 3b — read-only proof

  WHAT FAILED
    could not connect to the POS database: ('08001', '[08001] [Microsoft][ODBC SQL
    Server Driver][DBNETLIB]SQL Server does not exist or access denied. (17) ...

  THE STATE OF THIS MACHINE
    Nothing was written to the POS or to the cloud, and
    no scheduled task was registered. This machine is
    exactly as it was before you double-clicked.
```

The diagnostics zip, from the same clone:

```
config.json present? False
secrets in zip: NONE
redacted token : <redacted: 279 chars, sha256:9e8c4af3d908>
redacted pwd   : <redacted: 22 chars, sha256:379550d10156>
kept for triage: 57b61b47-... | localhost\HDSOFT | monitor_ro
```

Both unreachable systems fail soft with a diagnosis rather than a traceback:

```
readonly_proof.txt  (could not connect to the POS: OperationalError: ('08001', ...
                    This is itself a finding: the agent cannot read either.
cloud.txt           (could not reach the cloud: SupaError: HTTP 401 Invalid API key
                    This is itself a finding: the agent cannot upload either.
```

**One more found here and fixed:** `task_info.txt` was **empty** when no task was
registered — "nothing was checked" and "everything is fine" reading identically, in
the diagnostics file. It now says so in words. My first attempt put the branch in a
quoted PowerShell one-liner and was a parse error, which would have shipped as a
mysteriously empty file rather than a loud one; the branch is in Python instead.

### `sqlguard` wiring — verified present, not re-applied

```
adapter_hdsoft.py:27   import sqlguard
adapter_hdsoft.py:208  return sqlguard.guard(cn)
```

`git diff` on the locked file shows those two hunks and nothing else. No other locked
file is modified. The transcript now prints `sqlguard choke point  ACTIVE`.

---

# HANDOVER

For someone who has to trust this without reading the code.

## What is proven, and by what evidence

| Claim | Evidence |
|---|---|
| **The POS is never written to** | 9 zero-row writes attempted at the POS on every install; all must be refused. TRUNCATE/ALTER interrogated via `HAS_PERMS_BY_NAME`, never attempted. `sqlguard` refuses non-`SELECT` at the connection. Source scan fails the build on write SQL — proven by injecting one. |
| **The agent writes no file outside its folder** | `sys.addaudithook` records every write during a real cycle; test has a falsifier that must catch a deliberate stray write. |
| **The scheduled task runs by itself** | Registered on a real machine, waited 4 minutes doing nothing, `LastTaskResult 0`, agent ran. Not `Start-ScheduledTask`. |
| **A failed registration leaves nothing behind** | Forced with an injected post-registration failure: prior task restored **byte-for-byte**, interval back to `PT3M`. |
| **Logs cannot fill the disk** | 12 MiB ceiling; measured by writing past it — exactly 4 files, newest records kept. |
| **Logs carry no secret** | A real failing cycle whose errors embed both keys and the SQL password, then grep for every secret including fragments. Falsifier: with masking off, the same run must leak. |
| **The diagnostics zip carries no secret** | Same grep, over the real zip from the real clone. |
| **This machine runs the code we verified** | Clean-checkout-of-commit, or `MANIFEST.txt` sha256 per file. |
| **The numbers are right** | `test_golden.py`, 31 tests, pinned to HD Soft's own يومية الخزينة screen. Run **on the customer's machine** as part of acceptance. |

## What is NOT proven, and cannot be from here

1. **The POS connection succeeding.** There is no SQL Server on any machine we have.
   Every path through it is exercised against a simulated server; the first real
   connection happens at the shop.
2. **`LastTaskResult : 0` from a real cycle.** Proven with a stand-in agent. A real
   one needs a POS that answers.
3. **The read-only probe against real SQL Server.** Its error classification
   (`229`/`230`/`262`) comes from documentation. **If those differ on site, step 3b
   returns `INCONCLUSIVE` and stops the install** — the safe direction, but it would
   stop a good install, and you should expect that as a possible outcome.
4. **The first real shift report.** No shift has ever been computed from live data.
5. **Windows older than 11.** Task schema is pinned to 1.2 (Windows 7+) and
   `UseUnifiedSchedulingEngine` is omitted, but nothing older has been tested.
6. **The dry-run numbers.** Everything downstream of the first real dry run.

## Known limitations

- **A revoked or expired token is silent in the cloud.** A 401 is correctly not
  retried, but the failure-heartbeat insert 401s too. Loud in `agent.log` on the till,
  invisible to us. **Our only signal is the absence of heartbeats.** The fix belongs in
  the orchestrator: alert on heartbeat *silence*, not only on error heartbeats.
- **The task runs only while the till user is logged on.** Logged out, cycles stop and
  resume at the next logon. Nothing is lost — the watermark only moves forward.
- **We do not control the `monitor_ro` login.** Anyone with admin on that machine can
  change its permissions. We guarantee we *check* on every install, not that it cannot
  change between installs. `monitor_ro.sql` is written and **not yet applied**.
- **A clone carries more than `ship/` does** — `fake_adapter.py`, the test suite, and
  our `TO_CLAUDE_CODE.md` / `FROM_CLAUDE_CODE.md` correspondence all land on the till.
  No secrets, and `fake_adapter` is only reachable via `--fake`, which the task never
  passes. **The clean answer is a GitHub Release zip built from `ship/`** rather than a
  clone. Not built; recommended.
- **`pip install` is not reversed by the uninstall.** Removing shared packages could
  break anything else on that machine using Python.
- **Read-only does not mean nothing leaves.** Invoices, line items, cash counts, item
  names and staff IDs are uploaded. What we send is in `rows.py`.

## What the operator does at the shop

1. `git clone https://github.com/mahmouddevmohsen/POSentine.git` into a folder on the
   till, or download it from GitHub.
2. Put `config.json` next to `agent.py` — copied from `config.example.json`, filled in.
   **Never paste the token into a terminal.**
3. **Double-click `INSTALL.bat`.** That is the whole install.
4. Wait. It takes about 10 minutes and most of that is Phase E waiting for the
   scheduled task to fire on its own. **Do not close the window.**
5. **If it stops:** photograph the screen, change nothing, call. The screen names what
   failed, which VERIFY.md step, what to do, and what state the machine is in.
6. **If it finishes:** it prints what was checked, what runs now, and where the logs
   are. Tell the owner **no messages will arrive yet.**

### What he sends back

- If it stopped: the photograph, plus `logs\install_*.txt`.
- If anything looks wrong later, at any point: **double-click
  `collect_diagnostics.bat` and send the one zip.** It contains no password and no
  token. That replaces the conversation.

## Deliberately deferred

- **`orchestrator.py`** — event detection, deletion inference, mirroring anomalies into
  `internal_anomalies` under `service_role`. Must fit `report.py` and `metrics.py`'s
  existing signatures; both are locked.
- **`notifier/telegram.py`** — no Telegram token ever reaches the customer machine.
- **The two GitHub workflows**, with `audit_privileges.py` wired into keepalive.
- **VERIFY.md step 9, go-live** — two SQL statements, **one transaction**, run from our
  side. Not on this visit. If `notify` becomes true while `go_live_at` is still null,
  the owner's first ever contact from this system is dozens of alerts about last week.
- **Priority 4 (PyInstaller, config-from-prompt)** — unevaluated. What I wrote last
  time were **priors, not findings**, and they stay labelled that way.
- **A silent week** reconciling daily against HD Soft's own screen before go-live.

---

## The habit, since you named it

The three checks that shared the fault they were meant to detect — watermark-0, the
manifest hashes, the trigger — all have the same tell: **the check and the thing it
checks reading one source.** Two more turned up in this session alone. The closure test
walking one level and calling itself a closure. And `task_info.txt` being empty, where
"no task" and "we did not look" render identically.

The question that finds them is cheap: *what would this check still pass if it were
broken?* I asked it of Phase E before writing the wait, and of the closure test because
you told me to. Both times it paid.

Good project to have worked on.

---

# 2026-08-09 21:05 UTC — Priority Zero done. And the scheduled task never ran.

**302 passed** (was 209), `test_golden.py` still exactly **31**, no locked file
touched. `ship/` is 24 files + `MANIFEST.txt`. Commit `2fafad5`.

Read the second section first if you only read one. Priority Zero is done and I
believe it is right; **the task defect is the one that would have cost the visit.**

---

## 🔴 Before anything else: the scheduled task we shipped would never have run

I set out to write Phase E, which has to wait for the task to fire on its own.
Before writing the wait I checked what it would be waiting for. It would have
waited forever.

**A `LogonTrigger` fires on a logon *event*.** Registering it while the till user
is already logged on does not produce one — and the repetition hung off that
trigger, so nothing repeated either. Two probe tasks, registered side by side on
this machine, already logged on, no logoff:

```
registered at 18:31:00 - user already logged on, no logoff will happen

t+1m  LogonTrigger fired 0 time(s)   TimeTrigger fired 1 time(s)
t+2m  LogonTrigger fired 0 time(s)   TimeTrigger fired 2 time(s)
t+3m  LogonTrigger fired 0 time(s)   TimeTrigger fired 3 time(s)
t+4m  LogonTrigger fired 0 time(s)   TimeTrigger fired 4 time(s)

pos_probe_logon  LastRunTime=11/30/1999 12:00:00 AM  LastTaskResult=267011  NextRunTime=
pos_probe_time   LastRunTime=8/9/2026 6:35:35 PM     LastTaskResult=0       NextRunTime=8/9/2026 6:35:35 PM
```

`11/30/1999` is the scheduler's "never ran" sentinel. `267011` is
`SCHED_S_TASK_HAS_NOT_RUN`. **`NextRunTime` is empty — it was not merely waiting,
it had no run scheduled at all.**

On a till that stays logged in for weeks, the agent would have run **zero times**
after we left, with a task sitting in the scheduler looking perfectly correct.

Worse than that: it would have *eventually* worked. The next reboot or logon
would start it, so a later check might find it healthy and nobody would ever know
why the first week was empty.

**Why our own evidence missed it.** Last session I registered the task, ran it
with `Start-ScheduledTask`, and got the exit code back through the wrapper. That
proved the chain — scheduler → hidden PowerShell → wrapper → env → python → exit
code — and I reported it as such. It could not have caught this, because
`Start-ScheduledTask` is me starting it. **I proved the task *can* run and read it
as proof that it *will*.** Same shape as the manifest hashes: the check and the
thing it checked shared a source.

**The fix.** The repetition moves to a `TimeTrigger` whose `StartBoundary` is the
install time, so the task is due the moment it is registered. The `LogonTrigger`
stays, *without* a repetition, purely so a reboot starts a cycle promptly instead
of waiting up to three minutes. One repetition, one job each, nothing competing.

Verified end to end, for real, on a staged folder — registered, fired by itself,
rolled back, uninstalled:

```
  trigger MSFT_TaskTimeTrigger       interval='PT3M' duration=''
  trigger MSFT_TaskLogonTrigger      interval=''     duration=''
  NextRunTime    = 8/9/2026 6:42:42 PM

  t+1m  agent ran 0 time(s)  LastRunTime=11/30/1999   LastTaskResult=267011
  t+2m  agent ran 0 time(s)  LastRunTime=11/30/1999   LastTaskResult=267011
  t+3m  agent ran 1 time(s)  LastRunTime=6:42:42 PM   LastTaskResult=0
  t+4m  agent ran 1 time(s)  LastRunTime=6:42:42 PM   LastTaskResult=0

  ran.txt:
    18:42:01 argv=--log ...\stage\agent.log
```

Three tests now pin it, including one that fails if the repetition is ever moved
back onto the logon trigger, with the measurement above in its docstring.

**And the partial-install window you asked about was real.** `install_agent.ps1`
registered first and checked afterwards, so a failed read-back left the new task
in place. It now exports the existing definition before registering and restores
it byte-for-byte on any failure. Forced with an injected post-registration
failure plus a changed interval, so a failed rollback would be visible as `PT9M`:

```
  What failed:
    INJECTED post-registration failure
    repetition interval is 'PT9M', expected PT3M

  Rolled back: the task that was here before has been put back exactly
  as it was. Nothing was written to the POS or the cloud.

  prior task restored byte-for-byte: True
  interval now: 'PT3M'  (PT3M = restored, PT9M = rollback FAILED)
```

---

## 🔴 Priority Zero — prove read-only

### 1. The layer audit, honestly

| # | Layer | Rated | What it actually stops |
|---|---|---|---|
| 1 | `db_denydatawriter` | **Enforced** — SQL Server | `INSERT`/`UPDATE`/`DELETE`/`MERGE` on every table and view |
| 2 | Absence of any other grant | **Enforced, weakly** | DDL, `TRUNCATE`, `SELECT…INTO`, `BACKUP` — **this is where the risk lives** |
| 3 | `sqlguard.assert_read_only` | **Enforced** — our code raises | Anything that is not a `SELECT`, before it reaches the network |
| 4 | `pyodbc readonly=True` | **Convention. Counts for nothing.** | **Nothing.** |
| 5 | Source scan in the suite | **Enforced** — CI refuses | A future edit that adds a write |
| 6 | The on-site probe | **Enforced** — install aborts | Credentials that are not read-only, on this machine, today |
| 7 | Disk | **Enforced** — audit hook | Any file opened for writing outside our folder |

**What `db_denydatawriter` does not cover.** It denies exactly three permissions.
Everything else is blocked only because nobody granted it — and an absence can be
handed out by a helpful administrator without anyone touching the `DENY`:

- **`TRUNCATE TABLE`** — needs `ALTER` on the table. Not covered.
- **DDL and `SELECT … INTO`** — need DDL / `CREATE TABLE`. Not covered.
- **`BACKUP DATABASE`** — not covered.
- **`EXEC` of a stored procedure — the sharpest gap.** Under ownership chaining,
  a procedure sharing an owner with the tables it writes runs with the permission
  check on those tables **skipped entirely**. The `DENY` is never evaluated. This
  is the one path that defeats layer 1.
- **`sp_executesql`** — counter-intuitively *safe*: dynamic SQL runs under the
  caller's permissions, so the `DENY` still applies.

And the condition that voids layers 1 and 2 completely: if the login is
**`sysadmin`**, permission checks are skipped altogether; if **`db_owner`**, it
can remove the `DENY` itself. The probe reads both on every install and aborts.

**`pyodbc.connect(readonly=True)` — you were right, and I have downgraded it to
zero.** It sets `SQL_ATTR_ACCESS_MODE`, which the ODBC specification defines as a
*hint* a driver may ignore, and the SQL Server driver does: SQL Server has no
read-only session mode (`ApplicationIntent=ReadOnly` is Availability-Group
routing, not enforcement). **I could not test this here** — no SQL Server on this
machine — so I have not counted it. The on-site probe settles it and its error
code says which layer refused: SQL Server `229` means the server did.

**The audit finding I did not expect.** `monitor_ro`'s permissions had **no
committed definition anywhere.** They existed only as something typed into a
management tool once — unreproducible, unreviewable, un-re-assertable. That is
now `monitor_ro.sql`: idempotent, refuses to run against `master`, and its most
important line is `DENY EXECUTE ON SCHEMA::dbo`, which converts the
ownership-chaining hole from an absence into a refusal. Applying it is the
customer's DBA's call.

### 2. The choke point

`sqlguard.py`. **Allowlist first** — a statement must begin with `SELECT` or
`WITH`; `EXEC`, a bare `sp_who`, `SET`, `BEGIN TRAN` are refused for not being on
the list rather than for being on a list of things I thought of. **Then a
denylist** for writes that hide behind a legal opening. Comments and string
literals are stripped first, and an unterminated one is refused rather than
guessed at.

Wired at the **connection**, so it covers statements written months from now by
someone who never reads it. That is `sqlguard_wiring.patch` — two lines into
`adapter_hdsoft.py`, for you to apply:

```diff
+import sqlguard
@@
     cn.autocommit = True
-    return cn
+    return sqlguard.guard(cn)
```

I applied it temporarily to test it — **252 passed, `test_golden.py` 31** — then
reverted and confirmed the file is byte-identical
(`f75ef36e…e708d2`). Until you apply it, **the install transcript says
`sqlguard choke point  NOT WIRED` in words**, every time. An unapplied diff must
not look the same as a working guard.

### 3. The source scan

Reads the source, not the intent. Proven by injecting a write into `agent.py`:

```
E   AssertionError: write SQL outside the probe:
E       agent.py: 'UPDATE dbo.Sales SET saltot = 0 WHERE salid = 1'
FAILED test_readonly.py::test_no_pos_facing_module_contains_a_write_statement[agent.py]
FAILED test_readonly.py::test_write_sql_lives_in_exactly_one_file
```

Exactly one file may contain write SQL — `readonly_probe.py` — and a separate
test fails if a second one ever does.

The disk claim is proven the same way, with a `sys.addaudithook` recording every
file the interpreter opens for writing during a real cycle. It has its own
falsifier: a deliberate stray write the test must catch, so it cannot pass for
the wrong reason. **It found a bug in itself** — the `open` audit event carries an
*int* when a file is opened from a descriptor, and treating that as a path
invented a file called `3`.

### 4. 🎯 The empirical proof — with one change I want you to overrule or accept

**I did not implement the TRUNCATE probe, and I do not think we should.**

Your instruction was that every probe affect zero rows so that a wrongly
permitted one still changes nothing. `UPDATE`/`DELETE`/`INSERT` all take
`WHERE 1 = 0` and that works. **`TRUNCATE TABLE` takes no `WHERE` clause.** There
is no zero-row version. A probe that is wrongly permitted empties the customer's
sales history. Same for `ALTER TABLE … ADD`, which permanently changes their live
table. Wrapping either in a transaction and rolling back would take a
schema-modification lock on `dbo.Sales` **during service**, which blocks the POS
itself.

So those are **asked, never attempted**, with `HAS_PERMS_BY_NAME` — which is a
`SELECT`, accounts for `DENY`, role membership and ownership, and answers exactly
the same question: **`TRUNCATE` requires `ALTER` on the table**, so "can this
login `ALTER dbo.Sales`" *is* "can this login `TRUNCATE dbo.Sales`", with no risk
attached. I also enumerate `fn_my_permissions`, which is exhaustive rather than a
list of things we thought to ask about.

Your own verification point — **I checked the assumption before relying on it.**
SQL Server checks permissions at compile time, before touching rows, so a denied
statement raises. The `WHERE 1 = 0` is belt-and-braces, not the primary reason
these are safe. Two independent reasons is the right number when the target is a
working restaurant's sales table.

Nine writes are attempted (three shapes × three tables — a login denied on
`Sales` but not on `Items` would otherwise pass). Rendered against a simulated
correctly-configured server:

```
  READ-ONLY PROOF - attempting to write to the POS, and requiring
  every attempt to be refused
==================================================================
  login              monitor_ro
  sysadmin           no
  db_denydatawriter  yes
  sqlguard choke point  ACTIVE

  ATTEMPTED - each of these was actually sent to the POS
    REFUSED       UPDATE dbo.Sales
      UPDATE dbo.Sales SET salid = salid WHERE 1 = 0
      -> [42000] [Microsoft][ODBC Driver 17 for SQL Server][SQ...
    REFUSED       DELETE dbo.Sales
      DELETE FROM dbo.Sales WHERE 1 = 0
      ...  (9 probes, 3 tables)

  ASKED - never attempted; there is no harmless version of these
    not held      ALTER dbo.Sales  (ALTER)
    not held      CONTROL dbo.Sales  (CONTROL)
    not held      CREATE TABLE in the database  (CREATE TABLE)
    not held      BACKUP the database  (BACKUP DATABASE)
    not held      CONTROL the server  (CONTROL SERVER)

  Everything this login may do to dbo.Sales, per the server:
    SELECT

  VERDICT: READ-ONLY CONFIRMED
```

**An inconclusive answer is not a pass.** A probe that fails for a reason other
than permissions, or a `HAS_PERMS_BY_NAME` that returns NULL, stops the install.
"We could not tell" and "it is refused" must never produce the same outcome.

It runs as **preflight step 3b**, on every install. It cannot run earlier than
that — connecting needs pyodbc from step 2 and the credentials from step 3 — but
it runs before step 4, before the agent reads a single invoice.

The block is ASCII-only on purpose: it is evidence we show the customer, and a
cp1252 console would otherwise turn the em-dashes into noise.

### 5. What the installer touches

Complete list, in `READONLY_GUARANTEE.md`. Our own folder, and one scheduled
task. Two things I am naming because the list is supposed to be complete:

- Registering a task makes **Windows itself** write `C:\Windows\System32\Tasks\`
  and the scheduler's registry keys. Unregistering reverses it.
- **`pip install` writes into the machine's Python installation, and the
  uninstall does not reverse that** — removing shared packages could break
  anything else on that machine using Python. Named rather than hidden.

Not touched: `PATH`, environment variables outside our process, file
associations, services, startup folder, firewall, any HD Soft file.

### 6. `READONLY_GUARANTEE.md`

Written to be shown to the customer. Includes **what is not guaranteed** — six
items, the sharpest being that we do not control the `monitor_ro` login and can
only guarantee we *check* it on every install, and that read-only means we do not
change their database, not that nothing leaves it.

There is a **draft Arabic summary at the end, clearly marked not-for-use**. The
owner reads Arabic and this is meant to be showable to him, but Arabic
customer-facing text is reviewed wording in this project and I am not going to
quietly introduce unreviewed prose. Review it or tell me to drop it.

---

## Priority 1 — one click

`INSTALL.bat` → `installer.py`. Phases A–F exactly as you specified.

**Phase A's gate is preflight's own**, called rather than reimplemented —
`run_steps_0_to_4()` is the single implementation and both entry points use it. A
test fails if the installer ever starts re-deriving the step-4 verdict itself. A
second implementation of a gate is a second thing that can disagree with the
first.

**On "nothing is written anywhere before the Phase A gate" — I have to correct
the claim slightly, because it is not quite true and I would rather say so.**
Phase A writes three things: `pip install` writes into site-packages, and step 3b
sends nine zero-row statements to the POS which the server refuses. Nothing is
written to the **cloud**, nothing to the agent's own state, and nothing to the POS
*data*. The wording in `INSTALL.bat` and `VERIFY.md` now says exactly that rather
than the broader claim.

**Two things I found designing Phase B, both of which would have bitten:**

1. On a first install, `agent.py` adopts the watermark, uploads nothing, and
   exits — that is the whole cycle by design. Stopping there hands Phase C a
   cloud with no invoices and stops a *healthy* machine. Phase B detects it and
   runs a second cycle.
2. **`agent.py` exits `0` when another instance holds the lock, having done
   nothing.** On a second double-click the task is already registered and its
   cycles overlap, so exit 0 alone would let Phase B pass **without a cycle ever
   having run**. It now waits and retries, and gives up with instructions rather
   than looping.

**Phase E requires two independent facts**, because either alone can lie: the
scheduler reporting `LastTaskResult 0` (the task fires, but that says nothing
about data arriving) *and* a heartbeat newer than the one Phase C left behind (a
heartbeat could be left over from our own manual Phase B run). Together they
prove a cycle nobody started reached Supabase. The baseline is read straight from
`heartbeats`, not scraped from `--confirm`'s printed block.

The stop screen, rendered for real by running the installer against a folder with
no dependencies installed:

```
######################################################################
##                          S T O P P E D                           ##
######################################################################

  Failed in PHASE A — VERIFY.md step 2 — dependencies

  WHAT FAILED / WHAT TO DO / THE STATE OF THIS MACHINE / THE LOG
    ...
    Nothing was written to the POS or to the cloud, and
    no scheduled task was registered. This machine is
    exactly as it was before you double-clicked.
    ...
    C:\...\smoke\logs\install_20260809_233952.txt

##                 PHOTOGRAPH THIS SCREEN AND CALL.                 ##
##                 CHANGE NOTHING ON THIS MACHINE.                  ##
######################################################################
```

**`--skip-wait` exists for our rehearsals and prints `THIS INSTALL IS NOT
VERIFIED`.** A test fails if that sentence is ever removed. That is the specific
thing you warned about — one click quietly becoming one click that skips a check.

---

## Priority 2 — logs

**Rotation.** 2 MiB × 6 files = a 12 MiB ceiling. Proven by writing ~400 KB
through a scaled-down handler and measuring what survived: exactly 4 files, total
under `(backups+1) × (max + one record)`, and the **newest** records kept — a cap
that discarded the newest lines would be worse than no cap.

**One honest caveat.** The stale-lock takeover deliberately allows two processes
to overlap, and on Windows a rename fails while another process holds the file. A
failed rollover is written into the log and skipped rather than taking down a
cycle to tidy a log file; the next cycle rotates. So the ceiling is enforced on
the next successful write, not on that one. Tested, and stated in the module
rather than implied.

**Secrets are masked at the formatter**, not at the call sites — which means
`LOG.exception` tracebacks are covered too, and that is exactly where a
connection string turns up unannounced. Registration happens in `Config.load`
before any validation, so even a message *about* a malformed config cannot carry
the value it is complaining about. A secret too short to mask safely is reported
loudly rather than skipped silently.

**Truncated secrets are masked too.** Error text is cut at 500 characters on its
way to the cloud, and half a token is exactly as leaked as a whole one. I also
reordered `_beat_failure` to mask *before* truncating — truncating first can cut a
token in half and leave the half unmatched.

**The test you asked for by name** runs a real cycle that fails in the ways most
likely to leak (a 401 whose message embeds both keys, and a connection-string
exception carrying the SQL password), then greps every produced file for every
secret in `config.json`, including leading fragments. It has a falsifier: with
masking disabled the same run *must* leak, or the test is not exercising the path
it claims to.

**`collect_diagnostics.bat`** produces one zip. `config.json` is never copied — a
redacted version is, keeping every key and replacing each secret with its length
and a sha256 prefix, so "is this the token we issued?" is answerable without
disclosure. It re-runs the read-only proof rather than copying the install
transcript's, because the question three weeks later is whether the POS still
refuses us *today*. It fails soft: the machine whose network is broken is exactly
the machine whose diagnostics we most need.

---

## Priority 3 — the failure-mode review, and two defects I fixed

I read the code rather than the comments. **Two of the eleven were not handled,
and both were the silent kind.**

### 🔴 Not handled #1 — `--confirm` never judged the clock

`VERIFY.md` step 6 has always carried a row telling the operator that a drift
beyond ±300 is listed by `--confirm` and what to do about it. **It was not.**
`confirm()` printed `drift=` and appended no problem for it. A till whose clock
was hours out printed **`RESULT: OK`**.

Shift boundaries are wall-clock 07:00/19:00 local. A wrong clock puts invoices in
the wrong shift, and every total is then confidently incorrect rather than
absent — the exact failure this product exists to prevent, and the document
claimed we already caught it. Fixed, with the threshold as a named constant and
three tests, including one that refuses to pass a heartbeat carrying **no** clock
reading — missing and good must not look the same.

### 🔴 Not handled #2 — a corrupt `state.json` wedged the agent forever

`State.load` was called outside any `try` in `main()`. A torn or hand-edited
`state.json` raised `JSONDecodeError` out of `main`, **every three minutes,
forever.** No cycle would ever run again, and the file that causes it is one the
agent writes itself.

Fixed: the bad file is quarantined to `state.json.corrupt` (kept — it is the only
copy of the evidence), and the agent starts from an empty local state. That is
safe *because* of the work you had already done: `reconcile_with_cloud` treats the
cloud as authoritative and may only initialise when nothing has ever synced on
either side, so an empty local state resumes rather than re-adopting `MAX(salid)`.
Both halves are tested, including the dangerous one.

### The other nine

| # | Scenario | Loud? | Data loss? | Self-recovers? | Diagnosable? |
|---|---|---|---|---|---|
| 1 | Network drops between invoice and line batch | yes | **no** | yes | yes |
| 2 | Supabase 500/429 for an hour | yes | no | yes | yes |
| 3 | Token expires or is revoked | yes **locally** | no | **no** | **locally only** |
| 4 | Disk fills | yes | no | yes | partly |
| 5 | Clock jumps | **was silent** | — | n/a | now yes |
| 6 | `config.json` edited mid-run | yes | no | yes | yes |
| 7 | SQL Server restarts mid-query | yes | no | yes | yes |
| 8 | Two cycles overlap after takeover | yes | no | yes | yes |
| 9 | Killed mid-upload | yes | **no** | yes | yes |
| 10 | HD Soft changes a column | yes | no | **no, by design** | yes |
| 11 | Supabase paused / free tier full | yes | no | yes | yes |

**1, 9 — the watermark is correct.** `upload()` raises on the first failing table,
`advance_sync_state` is never reached, and the same range is re-read next cycle.
Every upload is an idempotent upsert. Covered by
`test_failed_line_upload_holds_the_watermark`. I added per-table logging so
"invoices went up and lines did not" is now visible in the log rather than
inferred.

**2 — retries 429/500/502/503/504** five times with exponential backoff, honouring
`Retry-After`. Beyond that the cycle fails, the watermark holds, and the next tick
tries again. An hour of outage costs an hour of latency, not data.

**3 — the one I want to flag.** A 401 is deliberately *not* retried (correct — RLS
refusal is an answer, not a hiccup). But `_beat_failure` then tries to record the
failure as a heartbeat, **and that insert gets 401 too**. So a revoked token is
loud in `agent.log` on the till and **completely silent in the cloud.** Our only
signal is the *absence* of heartbeats. Not fixed — the fix belongs in the
orchestrator (alert on heartbeat silence), which is Priority 3 work. **Naming it
so it does not get lost.**

**4 — disk full.** `State.save` writes to `.tmp` and only then `os.replace`, so a
failed write leaves the good file intact. There is no `fsync`, so a power cut
could lose the last state write — but the cloud is authoritative and
`reconcile_with_cloud` takes the higher watermark, so the impact is nil. Logging
degrades without crashing. Partly diagnosable: the log is the thing that fills.

**8 — overlap after takeover.** Safe by construction: idempotent upserts, and
every `sync_state` write is `lt.`-guarded so an older cycle cannot rewind a newer
one. That matters because a watermark moving backwards is the exact signature
`RestoreSuspected` exists to detect, and it must never fire for a benign reason.
Covered by `test_a_hung_process_cannot_rewind_the_watermark`. I added a warning
log when a takeover happens — it used to be inferable only from timing.

**10 — halts on purpose** and stays halted. Correct: numbers would be wrong rather
than absent.

**One thing I could not determine:** whether the customer's SQL Server behaves as
documented for any of this. There is no SQL Server on this machine. Everything
above is traced through our code; step 3b is what closes the gap, on site, before
anything else runs.

---

## Priority 4 — the two time-savers

**I have not evaluated either properly and I am not going to pretend otherwise.**
I dispatched both as parallel investigations — a real PyInstaller build with
measurements, and a config-prompt analysis — and both were killed by a session
limit before returning anything. I would rather hand you nothing than an opinion
dressed as a finding.

What I can say without measuring, as a starting position for when I do:

**PyInstaller — my prior is against, and the reason is not antivirus.** It is that
`test_golden.py` currently runs **on the customer machine** as acceptance
evidence, and `MANIFEST.txt` checks 24 files individually. One opaque binary
replaces a per-file integrity check with a single hash, and 31 pytest tests
running inside a frozen exe is a different and less convincing claim than 31
tests running against the files that will execute. The AV concern is real and
adds to it. **I will measure it properly and report.**

**Config from a prompt — my prior is also against**, because it moves the risk
rather than removing it: pasting a 219-character JWT into a Windows console is
its own failure mode, and the existing guards already catch every typo class
(`Config.load` decodes the token and checks `role` and `tenant_id`; preflight
checks the `sql` block that `Config.load` misses). A typo today is *caught, at the
counter, with an instruction*. That is not obviously worse than *impossible*.

**Two things I would propose instead, unprompted:**

1. **Apply `monitor_ro.sql`.** The read-only guarantee currently rests on a login
   with no committed definition. That is the weakest link in Priority Zero and it
   is one file to fix.
2. **Alert on heartbeat silence in the orchestrator.** Failure mode 3 above means
   a revoked token produces *nothing* — and "nothing" is what a healthy quiet
   shop also produces if we only look at error heartbeats.

---

## What is still unproven

- **An actual dry run against the real POS.** Unchanged, and everything is
  downstream of it.
- **The read-only probe against a real SQL Server.** The logic is tested against
  a simulated server, both compliant and dangerously misconfigured. The error
  codes it classifies (`229`/`230`/`262`) are from documentation, not from that
  machine. If they differ, step 3b returns `INCONCLUSIVE` and **stops the
  install** — which is the safe direction, but it would stop a good install, and
  you should know that is the failure mode.
- **Windows older than 11.** Still schema 1.2, still no
  `UseUnifiedSchedulingEngine`. The `TimeTrigger` change does not alter that.
- **Phase E against a real POS.** Verified with a stand-in agent on this machine;
  a `LastTaskResult 0` from a real cycle still needs a POS that answers.

---

# 2026-08-09 08:52 UTC — the manifest hashes you verified were wrong. Fixed.

Correction to the section below, found while committing. It is the same
failure shape as the watermark-0 trap, so it is worth your attention.

**Your check `ship/ sha256 vs repo — 8/8 identical` passed, and it could not have
failed.** Both sides read the same working tree. The working tree was not what a
clean checkout produces.

Git warned on commit:

```
warning: in the working copy of 'make_ship.py', CRLF will be replaced by LF
```

Three shipped files had picked up CRLF in the working copy — a tool on this machine
wrote them through Windows newline translation at some point. `.gitattributes` pins
`*.py` to `eol=lf`, and every committed blob is LF (checked: no tracked blob contains
CRLF), so a fresh clone produces different bytes than what I hashed. After
`git checkout -- .`:

```
file                     before         after clean-checkout bytes
adapter_hdsoft.py        f75ef36e6d34   f75ef36e6d34
agent.py                 9b7ba54265ae   48cdcba4e691    <-- CHANGED
config.example.json      b3920bc9c8f3   572aa1da6e50    <-- CHANGED
events.py                f563acca496e   f563acca496e
metrics.py               3672f30e9889   3672f30e9889
mint_agent_token.py      a1cc7d331827   a1cc7d331827
report.py                fad3785f92d9   fad3785f92d9
rows.py                  49a1764a5c56   49a1764a5c56
supa.py                  49ae9f26c9f1   c72382c0f3c3    <-- CHANGED
test_golden.py           8752bc77dfcc   8752bc77dfcc
```

**Every locked file is unaffected** — they were already LF, which is why
`git diff HEAD~2 HEAD` on them came back empty for you and stays empty. Nothing in
them changed. The three that moved are `agent.py`, `supa.py`, `config.example.json`,
and only their line endings.

**Consequence if it had shipped:** the manifest would have been correct about my
machine and wrong about the repository. Nobody would have noticed until someone
rebuilt `ship/` from a clean clone and got a different `MANIFEST.txt` for identical
source — or worse, until a "code integrity" stop on site for a file nobody had
touched.

**Guard added,** because I do not want this decided by whoever last opened a file in
which editor. `make_ship.py` now refuses to build when a shipped file's line endings
disagree with `.gitattributes`, before it hashes anything:

```
### 1. correct tree - must build ###
  built D:\New folder (2)\New folder\ship

### 2. reintroduce the exact fault (agent.py -> CRLF) ###
error: line endings do not match .gitattributes:
  agent.py: expected LF, found CRLF

       The bytes here are not the bytes a clean checkout
       produces, so every sha256 below would describe this
       machine rather than the repository. Fix with:
           git checkout -- <file>
--- exit=1 ---
```

`MANIFEST.txt` is also stamped with the revision it was built from, so a folder on a
customer machine traces back to a commit rather than to a memory:

```
# built from: 58a0788089a798fc4ab56811f4270b204853cc80 +uncommitted changes
```

Three tests cover it: the guard passes on the current tree, `CRLF_SUFFIXES` still
matches what `.gitattributes` pins, and the revision stamp resolves.

**209 passed**, `test_golden.py` still **31**. The hashes in the section below are
superseded by the ones above; the current `ship/MANIFEST.txt` is authoritative.

---

# 2026-08-09 08:35 UTC — Priority 1 done. Priority 2 I am pushing back on.

`install_agent.ps1`, `uninstall_agent.ps1` and `run_agent.ps1` are built, tested,
and in `ship/`. **206 passed** (was 184), `test_golden.py` still exactly **31**,
no locked file touched.

Priority 2 I have not done, and I do not think it should be done. Evidence below;
say the word and it is a five-minute change.

---

## 🔴 Priority 1 — `install_agent.ps1`

### What I built

| File | Role |
|---|---|
| `install/install_agent.ps1` | Registers the task. `-ShowXml` prints exactly what it would register and touches nothing. `-TaskName`, `-Python` overrides. |
| `install/uninstall_agent.ps1` | Removes it. Safe to run when nothing is installed. |
| `install/run_agent.ps1` | What the task executes — the task's environment block. |
| `test_install_agent.py` | 19 tests, driven off the real script's real `-ShowXml` output. |

### Four design calls you should check

**1. Explicit task XML, not `New-ScheduledTaskTrigger`.** An indefinite repetition
built through those cmdlets depends on `[TimeSpan]::MaxValue` surviving a round trip
into task XML — version-dependent, with a known failure mode. XML with an
`<Interval>` and no `<Duration>` means forever, on every version. It also gave me
`-ShowXml`, which is what makes step 7 testable off-site at all.

**2. The action is `powershell.exe -WindowStyle Hidden`, not `python.exe`.** A console
app launched by the scheduler shows its window. A black window on the till every three
minutes during service is not acceptable.

**3. Not `pythonw.exe`,** which would have removed the console entirely. Under
`pythonw`, `sys.stderr` is `None`; `agent.py`'s logging `StreamHandler` then fails on
every record and logging swallows the failure. That trades a visible window for a
silent one. Rejected.

**4. `run_agent.ps1` exists because a Scheduled Task action has no environment block** —
only a command, arguments and a working directory. The wrapper is that block. The
python path is **passed in as an argument**, not written into the wrapper at install
time: `run_agent.ps1` is hashed in `MANIFEST.txt`, and a script that rewrites itself
fails the integrity check `preflight.bat` runs first.

Two settings worth naming because their defaults are wrong for a till:

- `DisallowStartIfOnBatteries` / `StopIfGoingOnBatteries` both default to **true**.
  On a POS behind a UPS the defaults stop the agent on the first power blip, silently.
  Both forced to `false`.
- `ExecutionTimeLimit` is `PT15M`. `MultipleInstancesPolicy` is `IgnoreNew`, so one
  hung cycle blocks every later one forever. 15 minutes is not arbitrary — it is
  `agent.py`'s own `LOCK_STALE_SECONDS`, so the task and the agent cannot disagree
  about when a cycle is dead. `test_a_wedged_cycle_is_killed_before_it_blocks_the_next_forever`
  reads the constant out of `agent.py` and fails if it moves.

### Evidence — the task XML, from the real script

```
> powershell -ExecutionPolicy Bypass -File .\install\install_agent.ps1 -ShowXml

  [ OK ] python      3.11.15
  [ OK ] principal   KOMA\mahmo (user-level, LeastPrivilege - not SYSTEM)

  -ShowXml: printing the task XML. Nothing was registered.
------------------------------------------------------------------
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>KOMA\mahmo</UserId>
      <Repetition>
        <Interval>PT3M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>KOMA\mahmo</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File &quot;...\install\run_agent.ps1&quot; -Python &quot;...\python.exe&quot;</Arguments>
      <WorkingDirectory>D:\New folder (2)\New folder</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
=== EXIT = 0 ===
```

Note `<Repetition>` has **no `<Duration>`**. That is the whole point of the XML route.

### Evidence — registered for real, run, and removed

Not a mock. Registered on this machine as a non-administrator, run, inspected,
then removed.

```
### BEFORE: does 'thirdeyev' exist? ###
ABSENT

### INSTALL (run 1) ###
  [ OK ] python      3.11.15
  [ OK ] principal   KOMA\mahmo (user-level, LeastPrivilege - not SYSTEM)
  [ OK ] registered  1 task named 'thirdeyev' (read back and checked)
--- exit = 0 ---

### INSTALL (run 2) - idempotency ###
  [ .. ] a task named 'thirdeyev' already exists - replacing it
  [ OK ] registered  1 task named 'thirdeyev' (read back and checked)
--- exit = 0 ---

### how many tasks named thirdeyev? ###
1
```

What the scheduler actually stored — read back from the scheduler, not from my XML:

```
Principal.UserId    : mahmo
Principal.LogonType : Interactive
Principal.RunLevel  : Limited
Trigger             : MSFT_TaskLogonTrigger
Repetition.Interval : PT3M
Repetition.Duration : ''  (empty = indefinite)
Action.Execute      : powershell.exe
Action.WorkingDir   : C:\Users\mahmo\.claude\jobs\e26f7f1b\tmp\task
Settings.MultipleInstances : IgnoreNew
Settings.ExecutionTimeLimit: PT15M
Settings.DisallowStartIfOnBatteries: False
```

Then `Start-ScheduledTask`:

```
TaskName           : thirdeyev
LastRunTime        : 8/9/2026 8:26:26 AM
LastTaskResult     : 1
NumberOfMissedRuns : 0

### did the wrapper actually launch python? ###
2026-08-09 08:26:30,153 ERROR   cycle failed before upload: ('08001', '[08001]
[Microsoft][ODBC SQL Server Driver][DBNETLIB]SQL Server does not exist or access
denied. (17) (SQLDriverConnect); ...
```

**`LastTaskResult : 1` is the correct result here, and it is the interesting one.**
There is no SQL Server on this machine, so the agent failed at `connect()` and exited
1 — and that 1 came back through `run_agent.ps1` into `LastTaskResult`. The whole
chain is proven: scheduler → hidden PowerShell → wrapper → env → `python agent.py
--log` → exit code back out. `agent.log` was created, which is what VERIFY.md step 8
reads. On site, with a real POS, the same chain gives `0`.

Uninstall, and uninstall again:

```
  [ OK ] removed 'thirdeyev' (checked: it is gone)
--- exit = 0 ---
### is it gone? ###
ABSENT - clean

### UNINSTALL again (nothing to remove) ###
  [ OK ] no task named 'thirdeyev' is registered - nothing to remove
--- exit = 0 ---
```

### Evidence — the environment actually reaches python

The tests only grep `run_agent.ps1` for the two variables, which proves the source and
not the behaviour. So: both variables **explicitly unset**, started from `C:\Windows`,
with a stand-in `agent.py` that reports what it received and exits `7`.

```
### this shell, before: ###
PYTHONUTF8=[] PYTHONIOENCODING=[] cwd=C:\Windows

### what run_agent.ps1 hands to python: ###
cwd                = C:\Users\mahmo\.claude\jobs\e26f7f1b\tmp\envproof
PYTHONUTF8         = 1
PYTHONIOENCODING   = utf-8
sys.stdout.encoding= utf-8
argv               = ['--log', '...\\envproof\\agent.log']
--- wrapper exit = 7   (agent returned 7; must come back as 7) ---
```

Working directory forced, both variables set, `--log` passed, exit code preserved.

### Evidence — tests

```
> python -m pytest -q test_install_agent.py
19 passed in 2.05s

> python -m pytest -q
206 passed in 2.42s

> python -m pytest -q test_golden.py
31 passed in 0.05s
```

The 19 read properties out of the real `-ShowXml` output: `PT3M`, no `<Duration>`,
`LeastPrivilege` / `InteractiveToken`, no `SYSTEM`/`S-1-5-18`/`LOCALSERVICE` anywhere
in the principal, `-WindowStyle Hidden`, the wrapper and not `agent.py` directly,
`-ExecutionPolicy Bypass`, both battery settings `false`, `IgnoreNew`, `PT15M` tied to
`LOCK_STALE_SECONDS`, `Hidden=false`, `StartWhenAvailable=true`. Plus: `-ShowXml`
registers nothing, and the script refuses to install without `config.json`.

Windows-only, so they `skipif` when there is no PowerShell — pytest reports the skip
rather than passing quietly.

### A bug my own test caught, worth recording

The first version of `install_agent.ps1` would not parse at all:

```
The ampersand (&) character is not allowed...
Write-Host '  POSentine â€” install the scheduled task...
Unexpected token 'POSentine' in expression or statement.
```

**Windows PowerShell 5.1 reads a BOM-less `.ps1` as the system ANSI code page.** One
em-dash in a comment turns into `â€"` and shreds the parse, at lines that look fine.
All three scripts are now **ASCII-only and saved with a UTF-8 BOM**.

Second one, same session: PowerShell strips inner double quotes when building a native
command line, so `python -c 'print("%d.%d" % ...)'` reached Python as
`print(%d.%d % ...)` and died of a `SyntaxError` that reads like a broken interpreter.
The version probe now contains no double quotes.

Neither would have shown up before the visit. Both were found by running the file.

### Two things I changed outside the scripts

- **`VERIFY.md` step 7 now says `powershell -ExecutionPolicy Bypass -File ...`.**
  `.\install\install_agent.ps1` fails on a machine left at the Windows default
  (`Restricted`), before printing anything. Same for the uninstall line.
- **The logon-trigger trade-off is written down** rather than worked around: the task
  runs at logon and only while the till user is logged on. Running while logged off
  needs a stored password or an admin-granted logon right, and this account has
  neither. If the till is logged out, cycles stop and resume on logon; nothing is
  lost, because the watermark only moves forward. It is in step 7 and the install
  script prints it.

### What is still unproven

The scheduler accepted the XML, ran the task, and propagated the exit code **on this
machine**. What has not been proven anywhere: a `LastTaskResult : 0`, which needs a
POS that answers. And this machine is Windows 11; the customer's runs SQL Server 2014
Express and may be older. I kept the task to schema **1.2** (Windows 7+) and left out
`UseUnifiedSchedulingEngine` (Windows 8+) for that reason, but I cannot prove the
older path from here.

---

## 🟡 Priority 2 — I am not removing `mint_agent_token.py`, and here is why

The reasoning in your note is right about the *minting*: the customer machine never
mints a token, and that CLI is never used there. But the file is not only a minter.
`agent.py` imports it at module level and `Config.load` calls it:

```
agent.py:48:import mint_agent_token
agent.py:142:        mint_agent_token.assert_is_agent_token(raw["supabase_agent_token"],
```

`assert_is_agent_token` **is the check that refuses a service_role key** — the one
your own review called out as load-bearing. Removing the file from `ship/` does this:

```
### ship/ with mint_agent_token.py removed, as Priority 2 asks: ###
Traceback (most recent call last):
  File "...\trimtest\agent.py", line 48, in <module>
    import mint_agent_token
ModuleNotFoundError: No module named 'mint_agent_token'
```

That is the agent failing to start on the till, at install time.

**On the surface that genuinely is unused** — `mint()`, `_read_secret()`, `main()`:
they are inert on that machine. `mint()` cannot produce a token without
`SUPABASE_JWT_SECRET`, which by rule 5 is never there. And anyone who has the machine
already has the agent token in `config.json`; they do not need a minter.

I considered splitting the file — `token_check.py` (decode + assert, shipped) and
`mint_agent_token.py` (mint + CLI, not shipped). That achieves your intent exactly.
I did not do it because it means editing `agent.py`'s imports and `Config.load`'s call
path days before a site visit, to remove code that cannot be executed. That is the same
trade you agreed with on the exit code, pointing the same way — and there the change
would at least have fixed a real defect.

**What I did instead,** so this is decided mechanically rather than by review next
time — a test that derives the requirement from the source:

```python
@pytest.mark.parametrize("entry", ["agent.py", "preflight.py", "test_golden.py"])
def test_the_ship_list_is_closed_under_import(entry):
```

It walks each entry point's **module-level** imports and fails if any repository module
is missing from `SHIPPED`. Proof it bites — I removed the line from `make_ship.py` and
ran it:

```
E  AssertionError: agent.py imports ['mint_agent_token.py'], which ship/ does not
   contain. The agent would raise ImportError on the customer machine.
1 failed, 2 passed
make_ship.py restored
```

Module-level only, deliberately: `agent.py` imports `fake_adapter` *inside* `main()`
under `--fake`, and that one stays excluded — synthetic data has no place on a
production machine. The first draft of this test walked every import and correctly
flagged `fake_adapter`, which is how I noticed the distinction mattered.

**If you still want it gone after reading this, say so and I will do the split.** It
is about five minutes and I will re-run everything.

---

## `ship/` now

17 files + `MANIFEST.txt`. `make_ship.py` no longer prints the incomplete banner:

```
ship/adapter_hdsoft.py          ship/preflight.bat
ship/agent.py                   ship/preflight.py
ship/config.example.json        ship/report.py
ship/events.py                  ship/requirements.txt
ship/install/install_agent.ps1  ship/rows.py
ship/install/run_agent.ps1      ship/supa.py
ship/install/uninstall_agent.ps1  ship/test_golden.py
ship/metrics.py                 ship/VERIFY.md
ship/mint_agent_token.py        ship/MANIFEST.txt

  ✔ Complete for VERIFY.md steps 1–8: preflight, the agent,
    the golden baseline, and the scheduled task.
    Step 9 (go-live) is two SQL statements run from our side,
    not on this machine, and is deliberately not in here.
```

`MANIFEST.txt` now carries subdirectory paths, which step 0 had never seen. Checked
both ways:

```
### step 0 against a manifest with subdirectories ###
code integrity   OK - 17 files match MANIFEST.txt

### now corrupt a file INSIDE install/ ###
STOP: files differ from the versions we verified: ['install/run_agent.ps1']
```

Shipped `.ps1` encoding, verified after the copy:

```
install_agent.ps1      BOM=True  CRLF=317  ascii=True
run_agent.ps1          BOM=True  CRLF=60   ascii=True
uninstall_agent.ps1    BOM=True  CRLF=65   ascii=True
```

---

## Priority 3 — starting next

`orchestrator.py`, `notifier/telegram.py`, both workflows with `audit_privileges.py`
wired into keepalive. Agreed none of it needs the POS. Same gate discipline.

One thing I want to flag before I start, because it changes the shape of the work:
**`report.py` and `metrics.py` are locked**, and the orchestrator is the thing that
calls them. So the orchestrator has to fit their existing signatures exactly rather
than the other way round. I will read both closely and build to what they already
expect; if I find a place where a locked signature makes the orchestrator wrong rather
than merely awkward, I will stop and say so here instead of working around it.
