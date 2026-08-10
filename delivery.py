# -*- coding: utf-8 -*-
"""
delivery.py — Phase 2 Task 4: the cloud execution layer
========================================================
GitHub Actions calls THIS module (see .github/workflows/delivery.yml). It is
the composition root of the delivery half:

    orchestrator (decide what to build, enqueue into outbox)
        → notifier (claim outbox rows, send via Telegram, mark results)

Both components run in ONE process, so one run is one unit of truth: if
either fails, the whole run fails loudly and the workflow reports red. A
delivery system that fails silently is worse than one that does not exist,
because it is trusted.

Environment — GitHub Secrets, referenced by NAME only. Values never appear
in source, YAML, logs, reports, or this file:
    SUPABASE_URL                https://<project>.supabase.co
    SUPABASE_SERVICE_ROLE_KEY   the service_role key (the delivery role)
    TELEGRAM_BOT_TOKEN          the bot token
    TENANT_ID                   the tenant uuid
    SOURCE_ID                   the source uuid

Modes (workflow_dispatch inputs map to flags):
    --dry-run        build + print the full Arabic report; enqueue NOTHING,
                     send NOTHING. Never writes to outbox/events/shift_reports
                     and never calls the Telegram API.
    --force-shift    morning | evening
    --shift-date     YYYY-MM-DD

Clock rules (unchanged from the Phase 2 contract): the orchestrator resolves
"what time is it for this tenant" with zoneinfo.ZoneInfo(tenants.timezone).
UTC+3 is never hardcoded — Egypt observes DST (UTC+3 summer, UTC+2 winter).

Cloud-side only. The import closure is orchestrator + notifier + supa, all
closure-checked for pyodbc / adapter_hdsoft / agent / sqlguard — the
delivery system never connects to the customer POS (mechanically enforced
in test_delivery.py). No SQL Server connection exists anywhere in this
module or in what it imports.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
from typing import Any

import metrics as M
import orchestrator as ORCH
import supa
from notifier import telegram as NOTIFY


def _require_env(name: str) -> str:
    """Loud config check: a run without its configuration must not succeed."""
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(
            f"FATAL: required environment variable {name} is empty — add the "
            f"GitHub Secret named {name} (Settings → Secrets and variables → "
            f"Actions) and re-run. A delivery run without configuration must "
            f"not silently succeed."
        )
    return value


def _safe_plan_summary(out: ORCH.Plan) -> list[str]:
    """What the orchestrator decided, with chat ids masked like the notifier.

    Never prints a full chat id: the notifier's mask_chat_id convention is
    the project rule for every log line. The report bodies are fixed Arabic
    templates and contain no credentials.
    """
    lines: list[str] = []
    if out.shift_row is not None:
        sr = out.shift_row
        lines.append(f"  shift report    {sr['shift_date']} {sr['shift_name']} "
                     f"grand_total={sr['grand_total']} is_partial={sr['is_partial']}")
    for e in out.envelopes:
        lines.append("-" * 62)
        lines.append(f"  [{e.kind}] recipient={NOTIFY.mask_chat_id(e.recipient)} "
                     f"dedup={e.dedup_key}")
        lines.append("")
        lines.extend(f"    {line}" for line in e.body.splitlines())
        lines.append("")
    if not out.envelopes:
        lines.append("  (no messages to enqueue)")
    lines.append("-" * 62)
    lines.append(f"  events to record : {len(out.event_rows)} "
                 f"(queued: {len(out.queue_keys)})")
    lines.append(f"  internal anomalies: {len(out.anomalies)}")
    return lines


def main(argv: list[str] | None = None, *,
         client: Any | None = None,
         now_utc: _dt.datetime | None = None,
         session: Any | None = None,
         sleep: Any | None = None) -> int:
    """
    One cloud delivery pass.

    Production path (GitHub Actions): client/session/sleep/now_utc stay
    None — a real supa.Supa is built from the environment, real
    requests.Session / time.sleep are used, and the clock is now.
    Tests inject fakes through the keyword arguments (same convention as
    orchestrator.run and notifier.run).
    """
    ap = argparse.ArgumentParser(
        description="POSentine cloud delivery: orchestrator then notifier.")
    ap.add_argument("--tenant-id", default=os.environ.get("TENANT_ID"))
    ap.add_argument("--source-id", default=os.environ.get("SOURCE_ID"))
    ap.add_argument("--dry-run", action="store_true",
                    help="build and print reports; enqueue and send nothing")
    ap.add_argument("--force-shift", choices=(M.MORNING, M.EVENING), default=None)
    ap.add_argument("--shift-date", type=_dt.date.fromisoformat, default=None)
    args = ap.parse_args(argv)

    # All five secrets are checked up front — in dry-run mode too — so a
    # missing configuration is always a loud, obvious failure.
    url = _require_env("SUPABASE_URL")
    key = _require_env("SUPABASE_SERVICE_ROLE_KEY")
    token = _require_env("TELEGRAM_BOT_TOKEN")
    tenant_id = args.tenant_id or _require_env("TENANT_ID")
    source_id = args.source_id or _require_env("SOURCE_ID")

    if client is None:
        # The gateway admits any valid project JWT in the apikey header; the
        # role that matters travels in Authorization. service_role bypasses
        # RLS — exactly what the delivery side needs — and it lives only in
        # GitHub Secrets, never in a file or a log.
        client = supa.Supa(url, anon_key=key, token=key)
    now_utc = now_utc or _dt.datetime.now(_dt.timezone.utc)

    print("=" * 62)
    if args.dry_run:
        print("POSentine cloud delivery — DRY RUN: reports are built and printed;")
        print("NOTHING is enqueued and NOTHING is sent.")
    else:
        print("POSentine cloud delivery — LIVE: orchestrator then notifier.")
    print("=" * 62)

    if args.dry_run:
        # Read-only: the orchestrator's own dry-run branch — load live state,
        # decide, print. No insert_ignore/insert/update calls anywhere.
        ctx = ORCH._load_context(client, tenant_id, source_id)
        state = ORCH._load_state(client, ctx, now_utc)
        decided = ORCH.plan(now_utc, ctx, state,
                            force_shift=args.force_shift,
                            shift_date=args.shift_date)
        print("\n[orchestrator] decided (dry run):")
        for line in _safe_plan_summary(decided):
            print(line)
        print("\n[notifier] would-send view (dry run):")
        summary = NOTIFY.run(client, tenant_id=tenant_id, source_id=source_id,
                             token=token, now_utc=now_utc, dry_run=True)
        for line in summary.lines:
            print(line)
        print("-" * 62)
        print(f"  dry-run totals: claimed={summary.claimed} sent={summary.sent} "
              f"gate_blocked={summary.gate_blocked} "
              f"cap_deferred={summary.cap_deferred}")
        return 0

    decided = ORCH.run(client, tenant_id=tenant_id, source_id=source_id,
                       now_utc=now_utc, force_shift=args.force_shift,
                       shift_date=args.shift_date)
    print("\n[orchestrator] enqueued:")
    for line in _safe_plan_summary(decided):
        print(line)

    print("\n[notifier] delivery pass:")
    summary = NOTIFY.run(client, tenant_id=tenant_id, source_id=source_id,
                         token=token, now_utc=now_utc,
                         session=session, sleep=sleep)
    for line in summary.lines:
        print(line)
    print("-" * 62)
    print(f"  delivery totals: claimed={summary.claimed} sent={summary.sent} "
          f"failed={summary.failed} dead={summary.dead} "
          f"gate_blocked={summary.gate_blocked} "
          f"cap_deferred={summary.cap_deferred} "
          f"truncated={summary.truncated} telegram_403={summary.telegram_403}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
