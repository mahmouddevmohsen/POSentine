# -*- coding: utf-8 -*-
"""
notifier/telegram.py — Phase 2: the only component that talks to Telegram
==========================================================================
Cloud-side only. This module never touches the POS: no pyodbc, no
adapter_hdsoft, and no SQL Server connection anywhere in its import
closure (enforced mechanically by test_notifier.py). It reads outbox
rows enqueued by orchestrator.py and sends them through the Telegram
Bot API sendMessage.

Outbox lifecycle (per row, from schema.sql):
    pending → sending → sent            (success; sent_at recorded)
    pending → sending → failed → dead   (failure; attempts + 1 each run,
                                          dead after MAX_ATTEMPTS)

Claim before sending: a crash between claim and mark cannot re-send —
the row sits in 'sending', which is never a claim surface.

The recipient/alert gates (active → go_live_at → alert_settings.notify,
with notify_before_golive bypassing ONLY the go-live gate) were applied
by orchestrator.py at enqueue time; they are re-checked here as
defense-in-depth, reusing orchestrator.eligible_recipients as the single
source of truth so the two components cannot drift apart.

Daily cap (Task 2 review note): a notifier outage accumulates pending
alert rows past the cap of 3. This sender counts what was actually SENT
today (sent_at-based, tenant-local day) and defers the overflow back to
'pending' in FIFO order — the cap is a day boundary, not a failure.

Secrets: the bot token lives in the environment (GitHub Secret) only.
It is redacted from every error surface, and chat ids are masked in
every log line and anomaly detail. Never print the token, the service
role key, or a full chat id.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import time
import zoneinfo
from dataclasses import dataclass, field
from typing import Any

import requests

import events as E
import orchestrator as ORCH
import supa

# ── limits ──────────────────────────────────────────────────────
MAX_MESSAGE_UNITS = 4096          # Telegram's hard cap, in UTF-16 units
MAX_ATTEMPTS = 3                  # outbox: failed(≤3) → dead
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 30.0
DEFAULT_TIMEOUT = 30

# The 4096 decision (Task 3 spec e): truncate with an explicit marker,
# never split. Reasons are spelled out in apply_4096_policy.
TRUNCATION_MARKER = "\n\n⚠️ الرسالة أطول من الحد المسموح — تم قصّها"


# ════════════════════════════════════════════════════════════════
# pure: masking, redaction, the 4096 policy
# ════════════════════════════════════════════════════════════════

def mask_chat_id(value: Any) -> str:
    """'9876543210' -> '98…10'. Never a full chat id in a log or anomaly."""
    s = str(value)
    if len(s) <= 4:
        return "****"
    return f"{s[:2]}…{s[-2:]}"


def redact(text: str, token: str) -> str:
    """Rule: never log the bot token. Applied to every error surface."""
    if token and token in text:
        text = text.replace(token, "***")
    return text


def _utf16_units(s: str) -> int:
    """Telegram counts UTF-16 code units, not code points (emoji = 2)."""
    return len(s.encode("utf-16-le")) // 2


def apply_4096_policy(body: str,
                      limit: int = MAX_MESSAGE_UNITS) -> tuple[str, bool]:
    """
    The 4096-char decision (Task 3 spec e): truncate with an explicit
    marker, never split. Returns (outgoing_text, was_truncated).

    Why not split into several sendMessage calls? The outbox row is the
    unit of delivery — one row, one message, one status. Splitting would
    either mark the row sent before all parts landed, or re-send the
    whole row on a mid-split failure (duplicate fragments). The fixed
    templates with at most 5 notes stay well under 4096 in practice; the
    only realistic over-limit content is a pathological report, and there
    the decision-relevant numbers (grand total, counts) sit in the first
    two thirds of the template, so truncation with a visible marker
    preserves the essential facts and never lies about having cut
    something.
    """
    if _utf16_units(body) <= limit:
        return body, False
    budget = limit - _utf16_units(TRUNCATION_MARKER)
    units = 0
    head: list[str] = []
    for ch in body:
        u = _utf16_units(ch)
        if units + u > budget:
            break
        units += u
        head.append(ch)
    return "".join(head) + TRUNCATION_MARKER, True


# ════════════════════════════════════════════════════════════════
# Telegram Bot API send, with bounded retry and explicit classification
# ════════════════════════════════════════════════════════════════

class TelegramError(RuntimeError):
    """
    A sendMessage failure with an explicit kind, so a 403 (the user never
    pressed Start) can never hide as a generic network error.
    """

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


def _backoff(attempt: int) -> float:
    return min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)


def _retry_after_seconds(resp) -> float:
    """Telegram puts retry_after in the JSON body (parameters.retry_after)."""
    try:
        data = resp.json()
        ra = (data.get("parameters") or {}).get("retry_after")
        if ra is not None:
            return float(ra)
    except (ValueError, AttributeError):
        pass
    return _backoff(1)


def _telegram_description(resp, token: str) -> str:
    raw = ""
    try:
        data = resp.json()
        raw = data.get("description") or json.dumps(data, ensure_ascii=False)
    except (ValueError, AttributeError):
        raw = getattr(resp, "text", "") or ""
    return redact(raw, token)[:300]


def send_message(token: str, chat_id: str, text: str, *,
                 session: Any, sleep: Any,
                 max_attempts: int = MAX_ATTEMPTS,
                 timeout: int = DEFAULT_TIMEOUT) -> dict:
    """
    One sendMessage with bounded retry.

    Retried:  429 (honouring Telegram's retry_after), 5xx (backoff),
              transport errors (backoff — the response may have been
              lost, so a retry can duplicate; documented and accepted:
              reports beat the rare duplicate).
    Never:    400 and 403 (permanent). A 403 is raised as its own kind
              so the caller records telegram_403 distinctly.
    """
    # The no-accusation guard runs before every send (events.py: "قبل كل
    # إرسال"). The orchestrator asserts at build time; this is the second
    # choke point, right before the text leaves the process.
    E.assert_no_accusation(text)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    last: TelegramError | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.post(url, json=payload, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            last = TelegramError("transport", redact(str(exc), token)[:300])
            if attempt == max_attempts:
                break
            sleep(_backoff(attempt))
            continue

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                raise TelegramError("permanent_400",
                                    "Telegram answered 200 with a non-JSON body")
            if isinstance(data, dict) and data.get("ok"):
                return data
            raise TelegramError("permanent_400",
                                f"Telegram ok=false: {redact(str(data), token)[:300]}")

        desc = _telegram_description(resp, token)
        if resp.status_code == 403:
            raise TelegramError("forbidden_403", f"403 {desc}")
        if resp.status_code == 400:
            raise TelegramError("permanent_400", f"400 {desc}")
        if resp.status_code == 429:
            last = TelegramError("rate_limited", f"429 {desc}")
            if attempt == max_attempts:
                break
            sleep(_retry_after_seconds(resp))
            continue
        # any 5xx — retryable
        last = TelegramError("server_error", f"{resp.status_code} {desc}")
        if attempt == max_attempts:
            break
        sleep(_backoff(attempt))

    raise last or TelegramError("exhausted", "sendMessage failed")


# ════════════════════════════════════════════════════════════════
# pure: gate re-check and context
# ════════════════════════════════════════════════════════════════

def _parse_alert_type(dedup_key: str) -> str | None:
    """'alert:refund:0' -> 'refund'; anything else -> None."""
    parts = dedup_key.split(":", 2)
    if len(parts) >= 2 and parts[0] == "alert":
        return parts[1]
    return None


def gate_check(ctx: ORCH.TenantContext, row: dict) -> tuple[bool, str]:
    """
    Defense-in-depth re-check, in the approved order:
        active → go_live_at (bypassed by notify_before_golive) →
        alert_settings.notify (alerts only)
    The orchestrator applied these at enqueue; this catches a config
    change between enqueue and claim (a deactivated recipient, a flipped
    notify flag). Eligible-recipient logic is orchestrator.eligible_
    recipients — the single source of truth, never re-implemented.
    """
    eligible = {r.address for r in ORCH.eligible_recipients(ctx)}
    if row.get("recipient") not in eligible:
        rcpt = next((r for r in ctx.recipients
                     if r.address == row.get("recipient")
                     and r.channel == row.get("channel")), None)
        if rcpt is None:
            return False, "recipient row not found"
        if not rcpt.active:
            return False, "recipient inactive"
        return False, "pre-go-live and notify_before_golive is false"
    if row.get("kind") == "alert":
        typ = _parse_alert_type(row.get("dedup_key") or "")
        cfg = ctx.alert_settings.get(typ) if typ else None
        if not cfg or not cfg.get("notify"):
            return False, f"alert_settings.notify is off for {typ or '?'}"
    return True, ""


def _parse_dt(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _load_context(client: supa.Supa, tenant_id: str) -> ORCH.TenantContext:
    t_rows = client.select("tenants",
                           {"id": f"eq.{tenant_id}",
                            "select": "timezone,shift_morning_start,"
                                      "shift_evening_start,go_live_at"},
                           paginate=False)
    if not t_rows:
        raise supa.SupaError(f"no tenants row for id {tenant_id}")
    t = t_rows[0]

    recipients = [
        ORCH.Recipient(address=r["address"], label=r.get("label"),
                       active=bool(r.get("active", True)),
                       notify_before_golive=bool(r.get("notify_before_golive", False)),
                       channel=r.get("channel") or "telegram")
        for r in client.select("recipients",
                               {"tenant_id": f"eq.{tenant_id}",
                                "select": "channel,address,label,active,"
                                          "notify_before_golive"})
    ]

    settings: dict[str, dict] = {}
    for row in client.select("alert_settings",
                             {"tenant_id": f"eq.{tenant_id}",
                              "select": "alert_type,detect,notify"}):
        settings[row["alert_type"]] = {"detect": bool(row.get("detect", True)),
                                       "notify": bool(row.get("notify", False))}

    return ORCH.TenantContext(
        tenant_id=tenant_id, source_id="",
        timezone=t.get("timezone") or "Africa/Cairo",
        shift_morning_start=_dt.time.fromisoformat((t.get("shift_morning_start") or "07:00")[:8]),
        shift_evening_start=_dt.time.fromisoformat((t.get("shift_evening_start") or "19:00")[:8]),
        go_live_at=_parse_dt(t.get("go_live_at")),
        recipients=recipients,
        alert_settings=settings,
    )


# ════════════════════════════════════════════════════════════════
# the delivery pass
# ════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class Summary:
    """What one pass did. Log lines are masked and secret-free."""
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    dead: int = 0
    gate_blocked: int = 0
    cap_deferred: int = 0
    truncated: int = 0
    telegram_403: int = 0
    sent_alerts_today: int = 0
    dry_run: bool = False
    lines: list[str] = field(default_factory=list)


def _claim(client: supa.Supa, tenant_id: str) -> list[dict]:
    """
    pending|failed → sending, in FIFO order. 'sending' is never claimed,
    so a crash between claim and mark cannot re-send — the accepted cost
    is that a row left in 'sending' by a crash is never sent (message
    loss, never duplication).
    """
    return client.update("outbox", {
        "tenant_id": f"eq.{tenant_id}",
        "channel": "eq.telegram",
        "or": "(status.eq.pending,status.eq.failed)",
        "order": "created_at.asc",
    }, {"status": "sending"}, returning=True)


def _mark_failed(client: supa.Supa, row: dict, error_text: str,
                 now_utc: _dt.datetime, dead_after: int) -> str:
    """failed (attempts+1) → dead once attempts reach the cap. Returns status."""
    attempts = int(row.get("attempts") or 0) + 1
    status = "dead" if attempts >= dead_after else "failed"
    client.update("outbox", {"id": f"eq.{row['id']}"},
                  {"status": status, "attempts": attempts,
                   "last_error": (error_text or "")[:500]},
                  returning=False)
    return status


def _record_403(client: supa.Supa, ctx: ORCH.TenantContext, row: dict,
                message: str, source_id: str | None,
                now_utc: _dt.datetime) -> None:
    """The single most likely go-live failure must be obvious, not generic."""
    detail = {"recipient": mask_chat_id(row["recipient"]),
              "dedup_key": row.get("dedup_key"),
              "error": message[:300],
              "at": now_utc.isoformat(timespec="seconds")}
    anomaly: dict[str, Any] = {
        "tenant_id": ctx.tenant_id,
        "kind": "telegram_403",
        "detail": json.dumps(detail, ensure_ascii=False),
    }
    if source_id:
        anomaly["source_id"] = source_id
    client.insert("internal_anomalies", [anomaly], returning=False)


def run(client: supa.Supa, *, tenant_id: str, token: str,
        source_id: str | None = None,
        now_utc: _dt.datetime | None = None,
        session: Any | None = None,
        sleep: Any | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        dry_run: bool = False) -> Summary:
    """
    One delivery pass: claim → gate → cap → send → mark.

    Raises loudly on infrastructure failure (missing context row, a count
    that refuses a total) — a delivery system that fails silently is
    worse than one that does not exist.
    """
    now_utc = now_utc or _dt.datetime.now(_dt.timezone.utc)
    ctx = _load_context(client, tenant_id)
    tz = zoneinfo.ZoneInfo(ctx.timezone)
    local_now = now_utc.astimezone(tz)
    local_midnight_utc = (_dt.datetime.combine(local_now.date(), _dt.time.min,
                                               tzinfo=tz)
                          .astimezone(_dt.timezone.utc))
    # The cap counts what actually went out today (sent_at), not what was
    # enqueued — the orchestrator's created_at-based count is its own
    # enqueue gate; this one is the final authority on the owner's inbox.
    sent_today = client.count("outbox", {
        "tenant_id": f"eq.{tenant_id}",
        "kind": "eq.alert",
        "status": "eq.sent",
        "sent_at": f"gte.{local_midnight_utc.isoformat(timespec='seconds')}"})
    cap_room = max(0, E.DAILY_ALERT_CAP - sent_today)

    summary = Summary(dry_run=dry_run, sent_alerts_today=sent_today)
    sess = session if session is not None else requests.Session()
    sleeper = sleep if sleep is not None else time.sleep

    if dry_run:
        # read-only: show what a pass would do; claim, send, write nothing
        pending = client.select("outbox", {
            "tenant_id": f"eq.{tenant_id}",
            "channel": "eq.telegram",
            "or": "(status.eq.pending,status.eq.failed)",
            "order": "created_at.asc"})
        summary.claimed = len(pending)
        for row in pending:
            ok, reason = gate_check(ctx, row)
            body, truncated = apply_4096_policy(row.get("body") or "")
            summary.truncated += int(truncated)
            flag = "" if ok else f"  [blocked: {reason}]"
            summary.lines.append(
                f"  would-send [{row['kind']}] "
                f"recipient={mask_chat_id(row['recipient'])} "
                f"dedup={row['dedup_key']}{flag}")
            if ok:
                summary.lines.append("")
                for line in body.splitlines():
                    summary.lines.append(f"    {line}")
        return summary

    claimed = _claim(client, tenant_id)
    # PostgREST's `order` on PATCH return is requested, but the FIFO cap
    # behaviour must not depend on it — sort here so the drain order is
    # guaranteed regardless of the gateway.
    claimed.sort(key=lambda r: r.get("created_at") or "")
    summary.claimed = len(claimed)
    alerts_sent = 0
    for row in claimed:
        kind = row.get("kind") or "?"
        masked = mask_chat_id(row["recipient"])

        if kind == "alert" and alerts_sent >= cap_room:
            # The cap is a day boundary, not a failure: stay in the queue
            # and be picked up tomorrow (FIFO).
            client.update("outbox", {"id": f"eq.{row['id']}"},
                          {"status": "pending"}, returning=False)
            summary.cap_deferred += 1
            summary.lines.append(
                f"  [deferred:cap] [{kind}] recipient={masked} "
                f"dedup={row['dedup_key']}")
            continue

        ok, reason = gate_check(ctx, row)
        if not ok:
            status = _mark_failed(client, row, reason, now_utc, max_attempts)
            summary.gate_blocked += 1
            summary.failed += int(status == "failed")
            summary.dead += int(status == "dead")
            summary.lines.append(
                f"  [failed:gate] [{kind}] recipient={masked} "
                f"dedup={row['dedup_key']} reason={reason}")
            continue

        body, truncated = apply_4096_policy(row.get("body") or "")
        summary.truncated += int(truncated)
        try:
            result = send_message(token, row["recipient"], body,
                                  session=sess, sleep=sleeper,
                                  max_attempts=max_attempts)
        except TelegramError as exc:
            if exc.kind == "forbidden_403":
                _record_403(client, ctx, row, str(exc), source_id, now_utc)
                summary.telegram_403 += 1
            status = _mark_failed(client, row, redact(str(exc), token),
                                  now_utc, max_attempts)
            summary.failed += int(status == "failed")
            summary.dead += int(status == "dead")
            summary.lines.append(
                f"  [failed:{exc.kind}] [{kind}] recipient={masked} "
                f"dedup={row['dedup_key']} err={redact(str(exc), token)[:200]}")
            continue

        message_id = ""
        try:
            message_id = str(result.get("result", {}).get("message_id", ""))
        except AttributeError:
            pass
        client.update("outbox", {"id": f"eq.{row['id']}"},
                      {"status": "sent",
                       "sent_at": now_utc.isoformat(timespec="seconds")},
                      returning=False)
        if kind == "alert":
            alerts_sent += 1
        summary.sent += 1
        summary.lines.append(
            f"  [sent] [{kind}] recipient={masked} "
            f"dedup={row['dedup_key']} message_id={message_id or '?'}")

    return summary


# ════════════════════════════════════════════════════════════════
# CLI — for local runs and the GitHub Actions workflow
# ════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="POSentine notifier — sends pending outbox rows via "
                    "Telegram (cloud-side only).")
    ap.add_argument("--tenant-id", default=os.environ.get("TENANT_ID"))
    ap.add_argument("--source-id", default=os.environ.get("SOURCE_ID"))
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be sent; claim and send nothing")
    ap.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    args = ap.parse_args(argv)

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not args.tenant_id:
        ap.error("--tenant-id is required (or TENANT_ID env)")
    if not (url and key and token):
        ap.error("SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and "
                 "TELEGRAM_BOT_TOKEN are required")

    # Same delivery role as the orchestrator: service_role, GitHub Secrets
    # only, never in a file or a log.
    client = supa.Supa(url, anon_key=key, token=key)
    summary = run(client, tenant_id=args.tenant_id, source_id=args.source_id,
                  token=token, dry_run=args.dry_run,
                  max_attempts=args.max_attempts)
    _print_summary(summary)
    return 0


def _print_summary(s: Summary) -> None:
    print("=" * 62)
    mode = "DRY RUN — nothing claimed, nothing sent" if s.dry_run \
        else "delivery pass"
    print(f"POSentine notifier — {mode}")
    print("=" * 62)
    for line in s.lines:
        print(line)
    print("-" * 62)
    print(f"  claimed={s.claimed} sent={s.sent} failed={s.failed} "
          f"dead={s.dead} gate_blocked={s.gate_blocked} "
          f"cap_deferred={s.cap_deferred} truncated={s.truncated} "
          f"telegram_403={s.telegram_403}")
    print(f"  sent alerts today (cap {E.DAILY_ALERT_CAP}): "
          f"{s.sent_alerts_today}")
    print("=" * 62)


if __name__ == "__main__":
    raise SystemExit(main())
