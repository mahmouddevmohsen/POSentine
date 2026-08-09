# Review — from the architect

> Protocol: I write here, you reply in `FROM_CLAUDE_CODE.md`. I read it directly.

---

# 2026-08-09 (later) — PRIORITY ZERO: prove read-only, do not assert it

This comes before the one-click work below. Everything else in this product is
negotiable. This is not.

The owner's position, and it is the right one: **the agent must be structurally
incapable of writing to, altering, or deleting anything on the cashier machine.**
Not "designed not to". Incapable.

We have told this customer their POS will not be touched. If that turns out to be
untrue once, nothing else we built matters.

---

## 1. Enumerate the layers, and rate each one honestly

Produce a written audit of every layer that currently stands between this agent and a
write. For each, say plainly whether it is **enforced by something outside our code**
or merely **a convention we have been keeping**:

- `monitor_ro` — `db_datareader` + `db_denydatawriter`. Does DENY actually cover every
  path? Tables, views, **stored procedures**, DDL, `TRUNCATE`, `MERGE`, `EXEC`,
  `SELECT INTO`, `sp_executesql`, linked servers, `xp_cmdshell`. Be specific about
  what `db_denydatawriter` does **not** cover, because that is where the risk lives.
- `pyodbc.connect(readonly=True)` — I believe this is a **hint** the SQL Server ODBC
  driver may ignore. Confirm or refute with evidence, and if it is a hint, say so in
  the audit rather than counting it as protection.
- Every query being `SELECT ... WITH (NOLOCK)` — today this is convention plus review.
  Convention is not a control.
- The disk: does anything in the agent write outside its own working folder? Prove
  nothing touches `D:\HDSOFT` or any POS path.

I want the honest version, including anything that is weaker than we have been
assuming. A layer we think exists and does not is worse than a missing one.

## 2. A single choke point for SQL

Every statement the adapter sends must pass through one function that refuses anything
that is not a read. Not a review rule — a code path that raises.

Reject `INSERT UPDATE DELETE MERGE DROP ALTER CREATE TRUNCATE EXEC EXECUTE GRANT
REVOKE DENY BACKUP RESTORE SELECT…INTO` and anything else you identify as
write-capable. Handle comment-stripping and multi-statement batches — `SELECT 1;
DROP TABLE x` must be refused.

`adapter_hdsoft.py` is locked. **Do not edit it.** Write the guard as its own module,
give me the diff that wires it in, and I will apply it — same route as the cycle
ceiling.

## 3. A test that reads the source, not the intent

Scan the adapter's source for write keywords in any SQL literal and fail. So a future
edit that adds a write cannot pass review by looking innocent — the suite refuses it.

## 4. 🎯 The one that matters: prove it empirically on the customer's machine

This is the piece I actually want, and it is the same move that proved tenant
isolation at gate 3. We did not argue that RLS worked — we attempted a foreign insert
and got `42501`.

Do the same here. **Preflight, before anything else, attempts to write to the POS
database with the agent's own credentials, and requires every attempt to be refused.**

Construct each probe to affect **zero rows**, so that a probe which is wrongly
permitted still changes nothing:

```sql
UPDATE dbo.Sales SET saltot = saltot WHERE 1 = 0;
DELETE FROM dbo.Sales WHERE 1 = 0;
INSERT INTO dbo.Sales (salid) SELECT salid FROM dbo.Sales WHERE 1 = 0;
```

SQL Server checks permissions before it touches rows, so a denied statement raises and
a permitted one is a no-op. Verify that assumption yourself before relying on it — if
it does not hold, find probes where it does.

Requirements:

- **Every probe must be refused.** If any is permitted, **ABORT the whole install**,
  loudly: this machine's credentials are wrong and nothing should run until we fix it.
- Print the actual SQL error for each — the evidence belongs in the install transcript
  and in the diagnostics zip. That transcript becomes our proof to the customer.
- Do the same for DDL: an `ALTER TABLE` shaped probe that must be refused.
- Run it every install, not once. Permissions drift; someone helpful "fixes" a login.

If you can find a way to make a probe safe on a table nobody uses rather than `Sales`,
prefer it — but the probe must exercise the same permission path, not a weaker one.

## 5. What the installer touches on the machine

Write the complete list: our own folder, one scheduled task, nothing else. If anything
else is touched — registry, PATH, env vars, file associations — name it. The uninstall
must reverse all of it and leave the machine as it was.

## 6. Output

A short document, `READONLY_GUARANTEE.md`, in plain language: what we promise, what
enforces it at each layer, what the empirical proof is, and what would have to go wrong
for it to fail. Written so it can be shown to the customer.

Include what is **not** guaranteed, if anything. An honest boundary is worth more than
a broad claim.

---

# The one-click and logging work — unchanged, but second

Everything in my previous note still stands and follows this. Reproduced below.

# 2026-08-09 — one click, and a hard look at what breaks in week three

Priority 3 (orchestrator/telegram/workflows) is paused. This comes first, because
it decides whether the site visit succeeds and whether we can diagnose anything
afterwards without going back.

---

## Goal

The operator copies the project folder onto the till, double-clicks **one file**, and
walks away with a working, self-running agent — or with an unambiguous stop and a log
we can read from here.

He is not a Windows engineer, he will be standing in a working restaurant with a queue
at the counter, and a second trip is the most expensive thing on this project.

---

## 1. One click — the gates stay, they just stop being optional

Fold VERIFY.md steps 1–8 into a single entry point.

**This is not "run everything and hope".** Every gate we built stays exactly where it
is. What changes is who enforces it: a person under pressure might look at a delta of
7 and carry on. A script will not.

```
Phase A   preflight (read-only)                 steps 1-4
  GATE    verdict must be PASS or FIRST RUN     ← else STOP, nothing written
Phase B   one real cycle                        step 6
Phase C   --confirm                             step 6
  GATE    RESULT must be OK                     ← else STOP
Phase D   register the scheduled task           step 7
Phase E   wait for the task to fire, prove a NEW heartbeat arrived   step 7
Phase F   final summary: what happened, what runs now, where the logs are
```

Requirements:

- **Nothing is written anywhere before the Phase A gate passes.** That property is
  what makes a single click safe, so prove it, don't assert it.
- **Safe to run twice.** Every phase idempotent. Someone will double-click it again
  because they are not sure it worked.
- **No partial installs.** If Phase D fails, the machine must be left in the state it
  was in before Phase D, not half-registered.
- **The stop must be impossible to misread.** Screen-wide, what failed, which step,
  what to do, where the log is, and "photograph this and call — change nothing".
- Phase E is the one that proves the visit succeeded. Without a *new* heartbeat after
  the task fires on its own, everything before it only proves a human can run the
  agent by hand.

Keep the individual entry points working for our own use. This is an addition.

## 2. Logs — the thing that decides whether week three costs a trip

Assume something misbehaves three weeks after we leave, nobody was watching, and the
only way in is a file. Design for that reader.

- **An install transcript**, timestamped, capturing every phase including the
  failures. This is the file he photographs or sends.
- **A rolling agent log**: every cycle, what it read, what it uploaded, what it
  skipped and why, every error with its type and context. It must answer "what was
  this machine doing at 03:14 last Tuesday" without a debugger.
- **Rotation.** A log that fills a till's disk is a fault we caused. Size-capped,
  bounded number of files, and prove the cap holds.
- **Never any secret.** Not the token, not the SQL password, not a connection string.
  Add a test that greps the produced logs for every secret in `config.json` and fails
  if one appears. Config values must be masked at the logger, not by remembering.
- **`collect_diagnostics.bat`** — one double-click producing one zip: install
  transcript, agent logs, versions, ODBC drivers, task state, `sync_state`, last
  heartbeats, `state.json`, manifest check. **No secrets.** So the answer to "it
  stopped working" is one click and one file, not a conversation.

## 3. A failure-mode review of the agent — this is the part I most want

Take the broad, sharp look. Not features: **what breaks when nobody is watching.**

Work through at least these, with evidence for each — either "handled, here is the
test" or "not handled, here is what happens":

- The network drops mid-upload, between the invoice batch and the line batch
- Supabase returns 500 or 429 for an hour
- The token expires or is revoked while the agent is running
- The disk fills
- The POS machine's clock jumps (DST, manual change, NTP correction)
- `config.json` is edited or corrupted while the agent is running
- SQL Server restarts mid-query
- Two cycles overlap after a takeover
- The agent is killed mid-upload — is the watermark still correct on restart?
- HD Soft is upgraded and a column changes underneath us
- The Supabase project is paused or the free tier fills

For each: does it fail loudly, does it lose data, does it recover on its own, and does
the log say enough to diagnose it from here? **Report the ones that are not handled
even if you do not fix them** — an honest list is worth more than a fixed subset.

## 4. Time-savers: evaluate, adopt only if provably better

You have standing permission to propose a simpler route. Two I have considered and
could not decide from here:

- **A single PyInstaller executable**, removing the Python dependency from the till
  entirely. Cleaner install, no version questions. Against it: antivirus false
  positives on unsigned binaries are common and would be a very bad surprise in a
  restaurant, plus a build step between the tests and what ships. **Evaluate; do not
  adopt unless you can show it is safer than what we have.**
- **Generating `config.json` from a prompt** instead of hand-editing, so a typo
  becomes impossible rather than caught. Weigh against handling secrets interactively.

Anything else you see, propose it. The bar is the same as always: **provably better and
simpler, or we do not do it.** A shortcut that is merely faster is not a shortcut.

---

## Discipline — unchanged

Evidence gates: state the falsifier, implement, paste raw output. Challenge before
implementing. Loud failure over silent defaults. Locked files untouchable.

**And one specific to this task:** do not let "one click" quietly become "one click
that skips a check". If any gate becomes hard to automate, say so and leave it manual.
A manual step we perform is better than an automated one we trust wrongly.
