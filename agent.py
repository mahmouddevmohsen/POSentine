# -*- coding: utf-8 -*-
"""
agent.py — runs on the POS machine
================================================================
One cycle per invocation, then exit. The Windows Scheduled Task's
3-minute repetition is the loop — not a `while True` in here. A dead
process is replaced on the next tick, a reboot recovers by itself, and
nothing long-lived can leak or wedge on a machine we cannot reach.

The agent is deliberately dumb: read and upload. No business logic, no
scheduling decisions, and no Telegram token on the customer's machine.

Ordering rules that exist because breaking them is silent:

  • The watermark advances only after every upload has returned 2xx.
    A half-written upload that looked successful would skip real
    invoices permanently — they are never re-read.

  • last_rescan_at advances only after every rescan batch succeeded.
    The cloud detects deletions by absence relative to that timestamp,
    so a partial rescan upload would read as mass deletion and fire
    hundreds of alerts naming real receipts.

  • An item missing from the product snapshot uploads as NULL, never 0.
    list_price = 0 means "kitchen note, ignore" to the zero-invoice
    detector, so a coerced 0 would delete the finding rather than
    report it.

  --dry-run reads and prints. It writes nothing: no heartbeat, no
  sync_state, no watermark, not even the local cycle counter.
================================================================
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import adapter_hdsoft as default_adapter
import mint_agent_token
import rows as R
import supa

LOCK_STALE_SECONDS = 15 * 60

# A cycle never uploads more than this many new invoices. It matters most
# after an outage: an agent down for a month comes back to a five-figure
# backlog, and pushing it in one cycle during service is its own incident.
# The backlog drains over subsequent cycles and the cap is logged each time
# it bites, so a persistent one is visible rather than merely survived.
MAX_INVOICES_PER_CYCLE = 5000
LOG = logging.getLogger("posentine.agent")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ANOTHER_INSTANCE = 0        # not a failure; the task simply overlapped
EXIT_HALTED = 3                  # restore suspected / schema drift


# ════════════════════════════════════════════════════════════════
# output encoding — a crash here is invisible
# ════════════════════════════════════════════════════════════════

def configure_output(log_path: Path | None = None) -> None:
    """
    Arabic item names on a default Windows console are cp1252, and printing
    one raises UnicodeEncodeError. That would surface mid-install, on the
    console of the person deciding whether to trust this.

    errors='replace' on the console deliberately: garbled output can be
    read past, a crash during verification cannot.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # The Scheduled Task runs hidden, so this file is the only output
        # that survives. It is opened UTF-8 explicitly for the same reason.
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=handlers,
        force=True,
    )


# ════════════════════════════════════════════════════════════════
# configuration and state
# ════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Config:
    tenant_id: str
    source_id: str
    supabase_url: str
    supabase_anon_key: str
    supabase_agent_token: str
    sql: dict[str, Any]
    sync_seconds: int = 180
    rescan_every_n_cycles: int = 5
    reference_every_n_cycles: int = 20

    @staticmethod
    def load(path: Path) -> "Config":
        raw = json.loads(path.read_text(encoding="utf-8"))
        required = ("tenant_id", "source_id", "supabase_url",
                    "supabase_anon_key", "supabase_agent_token", "sql")
        missing = [k for k in required if not raw.get(k)]
        if missing:
            raise ValueError(f"config is missing required keys: {missing}")

        # config.example.json ships angle-bracket placeholders. Copied over
        # unedited they reach the HTTP layer and die as UnicodeEncodeError
        # on an em-dash — an error that points at encoding rather than at
        # the field nobody filled in.
        placeholders = [k for k in required
                        if isinstance(raw[k], str) and raw[k].startswith("<")]
        if placeholders:
            raise ValueError(
                f"config still has unfilled placeholder values: {placeholders} "
                "— replace them with real values before running the agent"
            )

        # Decode the token and read its claims. Refusing here means a
        # service_role key or a wrong-tenant token cannot reach a live
        # request at all.
        mint_agent_token.assert_is_agent_token(raw["supabase_agent_token"],
                                               raw["tenant_id"])
        return Config(
            tenant_id=raw["tenant_id"],
            source_id=raw["source_id"],
            supabase_url=raw["supabase_url"],
            supabase_anon_key=raw["supabase_anon_key"],
            supabase_agent_token=raw["supabase_agent_token"],
            sql=raw["sql"],
            sync_seconds=int(raw.get("sync_seconds", 180)),
            rescan_every_n_cycles=int(raw.get("rescan_every_n_cycles", 5)),
            reference_every_n_cycles=int(raw.get("reference_every_n_cycles", 20)),
        )


@dataclass
class State:
    """Local mirror of sync_state, plus the product snapshot."""
    # False until the first run has adopted MAX(salid). "No backfill" has
    # to mean something in practice: a fresh install must not drag 218,207
    # invoices and 481,293 lines across during service.
    initialised: bool = False
    cycle_index: int = 0
    watermark_salid: int = 0
    watermark_saledeid: int = 0
    rescan_from_salid: int = 0
    restore_suspected: bool = False
    schema_ok: bool = True
    last_date_change_rows: int = 0
    products: dict[int, dict[str, Any]] = field(default_factory=dict)

    @staticmethod
    def load(path: Path) -> "State":
        if not path.exists():
            return State()
        raw = json.loads(path.read_text(encoding="utf-8"))
        # JSON object keys are strings; itid lookups are ints.
        raw["products"] = {int(k): v for k, v in (raw.get("products") or {}).items()}
        known = {f for f in State().__dict__}
        return State(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path) -> None:
        """Atomic: a torn state file would restart the watermark at zero."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = asdict(self)
        payload["products"] = {str(k): v for k, v in self.products.items()}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)


# ════════════════════════════════════════════════════════════════
# single instance
# ════════════════════════════════════════════════════════════════

class SingleInstanceLock:
    """
    Guards against a cycle that overruns the 3-minute tick.

    A crashed process releases its OS lock automatically, so failing to
    acquire means a live process holds it. If that process has been holding
    for longer than LOCK_STALE_SECONDS it is hung rather than working, and
    we proceed anyway: a permanently stalled product is worse than an
    overlapping read. Every upload is an idempotent upsert, so overlap
    costs duplicate work, not wrong data. The takeover is recorded.
    """

    def __init__(self, path: Path, stale_after: int = LOCK_STALE_SECONDS):
        self.path = path
        self.stale_after = stale_after
        self.acquired = False
        self.took_over = False
        self._fh = None

    def __enter__(self) -> "SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fh = open(self.path, "a+", encoding="utf-8")
            _lock_file(self._fh)
            self.acquired = True
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(f"{os.getpid()} {_dt.datetime.now().isoformat()}")
            self._fh.flush()
        except OSError:
            age = self._age_seconds()
            if age is not None and age > self.stale_after:
                self.took_over = True
                LOG.warning(
                    "lock held for %.0f minutes — treating the holder as hung "
                    "and proceeding; a stalled agent is worse than an overlap",
                    age / 60.0)
            self._close()
        return self

    def _age_seconds(self) -> float | None:
        try:
            return time.time() - self.path.stat().st_mtime
        except OSError:
            return None

    @property
    def may_run(self) -> bool:
        return self.acquired or self.took_over

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def __exit__(self, *exc) -> None:
        self._close()


def _lock_file(fh) -> None:
    """Exclusive, non-blocking. Raises OSError when already held."""
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


# ════════════════════════════════════════════════════════════════
# one cycle
# ════════════════════════════════════════════════════════════════

def sql_max_salid(cn) -> int:
    """
    Deliberately dumb, and deliberately not adapter.pull(): first-run
    initialisation must learn MAX(salid) *without* reading anything behind
    it. SELECT only.
    """
    cur = cn.cursor()
    cur.execute("SELECT ISNULL(MAX(salid), 0) FROM dbo.Sales WITH (NOLOCK)")
    return int(cur.fetchone()[0])


def sql_raw_counts(cn, watermark_salid: int) -> tuple[int, int]:
    """
    The acceptance comparison, run here rather than in SSMS — neither SSMS
    nor sqlcmd exists on the POS machine, so the single most important
    check of the visit would otherwise be unrunnable.

    A separate, bare query against the same source: different code path,
    no joins, no classification, no guards. If it disagrees with what the
    adapter produced, something is wrong with how we read their data.
    """
    cur = cn.cursor()
    cur.execute("SELECT COUNT(*) FROM dbo.Sales WITH (NOLOCK) WHERE salid > ?",
                watermark_salid)
    invoices = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM dbo.SalesDe WITH (NOLOCK) WHERE saleid > ?",
                watermark_salid)
    lines = int(cur.fetchone()[0])
    return invoices, lines


def _raw_counts(adapter, cn, watermark_salid: int) -> tuple[int, int]:
    return getattr(adapter, "raw_counts", sql_raw_counts)(cn, watermark_salid)


def _max_salid(adapter, cn) -> int:
    return getattr(adapter, "max_salid", sql_max_salid)(cn)


@dataclass
class CycleResult:
    invoices: list[dict[str, Any]] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)
    cash: list[dict[str, Any]] = field(default_factory=list)
    products: list[dict[str, Any]] = field(default_factory=list)
    users: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    did_rescan: bool = False
    did_reference: bool = False
    unknown_itids: list[int] = field(default_factory=list)
    pull: Any = None
    # Highest salid we actually read. NOT pull.max_salid, which is MAX over
    # the whole table and includes rows the 30-second dirty-read guard held
    # back — advancing to that would step over invoices we never read.
    watermark_target: int = 0
    capped: bool = False
    new_invoice_count: int = 0


def _merge(primary: list, extra: list, key) -> list:
    """Rescanned rows win over the incremental ones: they are fresher."""
    merged = {key(item): item for item in primary}
    merged.update({key(item): item for item in extra})
    return list(merged.values())


def build_cycle(adapter, cn, cfg: Config, state: State,
                stamp: _dt.datetime, force_reference: bool = False) -> CycleResult:
    """
    Pull one cycle and turn it into upload payloads. No network writes here,
    so --dry-run and a real cycle take exactly the same path up to this
    point and the dry-run numbers mean something.
    """
    do_rescan = state.cycle_index % cfg.rescan_every_n_cycles == 0
    do_reference = (force_reference
                    or not state.products
                    or state.cycle_index % cfg.reference_every_n_cycles == 0)

    pulled = adapter.pull(cn,
                          watermark_salid=state.watermark_salid,
                          rescan_from_salid=state.rescan_from_salid,
                          do_rescan=do_rescan,
                          do_reference=do_reference)

    result = CycleResult(did_rescan=do_rescan, did_reference=do_reference,
                         pull=pulled)

    products = dict(state.products)
    if pulled.products:
        products = {int(p["itid"]): p for p in pulled.products}
        result.products = [R.product_payload(p, cfg.tenant_id, cfg.source_id)
                           for p in pulled.products]
        result.users = [R.user_payload(u, cfg.tenant_id, cfg.source_id)
                        for u in pulled.users]

    # ── the per-cycle ceiling ───────────────────────────────────
    new_invoices = sorted(pulled.new_invoices, key=lambda i: i.salid)
    result.new_invoice_count = len(new_invoices)
    if len(new_invoices) > MAX_INVOICES_PER_CYCLE:
        result.capped = True
        new_invoices = new_invoices[:MAX_INVOICES_PER_CYCLE]
        LOG.warning(
            "backlog: %d new invoices waiting, cap is %d — uploading the "
            "oldest %d this cycle and the rest will follow. A backlog that "
            "persists across cycles means the agent was down for a long time.",
            result.new_invoice_count, MAX_INVOICES_PER_CYCLE, len(new_invoices))
    kept = {i.salid for i in new_invoices}
    new_lines = [ln for ln in pulled.new_lines if ln.salid in kept]

    # Follow what was read, never MAX(salid) over the table.
    result.watermark_target = (max(i.salid for i in new_invoices)
                               if new_invoices else state.watermark_salid)

    invoices = _merge(new_invoices, pulled.rescanned_invoices,
                      lambda i: i.salid)
    lines = _merge(new_lines, pulled.rescanned_lines,
                   lambda ln: ln.saledeid)

    # An item we have never seen must not silently become list_price 0.
    unknown = sorted({ln.itid for ln in lines
                      if ln.itid is not None and ln.itid not in products})
    if unknown and not do_reference:
        # Refresh the snapshot immediately and retry once, rather than
        # freezing NULLs into rows we can never re-derive.
        LOG.info("%d unseen item(s) in this batch — refreshing the product "
                 "snapshot before freezing prices", len(unknown))
        refreshed = adapter.pull(cn,
                                 watermark_salid=pulled.max_salid,
                                 rescan_from_salid=state.rescan_from_salid,
                                 do_rescan=False, do_reference=True)
        if refreshed.products:
            products = {int(p["itid"]): p for p in refreshed.products}
            result.products = [R.product_payload(p, cfg.tenant_id, cfg.source_id)
                               for p in refreshed.products]
            result.users = [R.user_payload(u, cfg.tenant_id, cfg.source_id)
                            for u in refreshed.users]
            result.did_reference = True
        unknown = sorted({i for i in unknown if i not in products})

    result.unknown_itids = unknown
    for itid in unknown:
        result.anomalies.append(("unknown_item", {"itid": itid}))

    result.invoices = [R.invoice_payload(i, cfg.tenant_id, cfg.source_id, stamp)
                       for i in invoices]
    result.lines = [R.line_payload(ln, cfg.tenant_id, cfg.source_id,
                                   products.get(ln.itid))
                    for ln in lines]
    result.cash = [R.cash_payload(c, cfg.tenant_id, cfg.source_id)
                   for c in pulled.cash_counts]

    if pulled.unknown_sal_t:
        for sal_t in sorted(set(pulled.unknown_sal_t)):
            result.anomalies.append(("unknown_sal_t", {"sal_t": sal_t}))
    if pulled.date_change_rows > state.last_date_change_rows:
        result.anomalies.append(("date_change_log", {
            "rows": pulled.date_change_rows,
            "previous": state.last_date_change_rows}))

    result.products_snapshot = products          # type: ignore[attr-defined]
    return result


def upload(client: supa.Supa, cfg: Config, result: CycleResult) -> None:
    """
    Order matters: reference data first so a report built moments later has
    names, then invoices before their lines. Any failure raises, and the
    caller leaves every watermark exactly where it was.
    """
    if result.products:
        client.upsert("pos_products", result.products,
                      on_conflict="tenant_id,source_id,itid")
    if result.users:
        client.upsert("pos_users", result.users,
                      on_conflict="tenant_id,source_id,uid")
    if result.invoices:
        client.upsert("invoices", result.invoices,
                      on_conflict="tenant_id,source_id,salid")
    if result.lines:
        client.upsert("invoice_lines", result.lines,
                      on_conflict="tenant_id,source_id,saledeid")
    if result.cash:
        client.upsert("cash_counts", result.cash,
                      on_conflict="tenant_id,source_id,srid")


def advance_sync_state(client: supa.Supa, cfg: Config, state: State,
                       result: CycleResult, stamp: _dt.datetime) -> None:
    """
    Called only after `upload` returned without raising.

    Every write here is monotonic, enforced by the database rather than by
    ordering. Row data is protected by idempotent upserts, but sync_state
    is not: if the lock's stale-takeover ever lets a hung cycle resume
    behind a newer one, it would write its own older watermark over the
    newer one. Nothing would be lost — the next cycle re-pulls — but a
    watermark moving backwards is precisely the signature RestoreSuspected
    exists to detect, and it must never fire for a benign reason.

    A `lt` filter means the UPDATE matches zero rows when the stored value
    is already ahead. Safe under any interleaving, no locking needed.
    """
    pulled = result.pull
    base = {"tenant_id": f"eq.{cfg.tenant_id}", "source_id": f"eq.{cfg.source_id}"}
    now = R.utc_ts(stamp)

    target = result.watermark_target
    applied = client.update(
        "sync_state",
        {**base, "watermark_salid": f"lt.{target}"},
        {"watermark_salid": target,
         "watermark_saledeid": pulled.max_saledeid,
         "pos_max_salid": pulled.max_salid,
         "last_sync_at": now},
    )
    if not applied:
        LOG.info("watermark already at or beyond %d — a newer cycle got there "
                 "first; leaving it alone", target)

    # last_sync_at must move even on a cycle that found no new invoices,
    # otherwise a quiet shop looks identical to a stalled agent.
    client.update(
        "sync_state",
        {**base, "or": f"(last_sync_at.lt.{now},last_sync_at.is.null)"},
        {"last_sync_at": now}, returning=False)

    if result.did_rescan:
        # Every invoice in the window carries last_seen_at = stamp, so the
        # orchestrator reads absence as "not refreshed by this rescan". If
        # any batch above had failed we would not be here, and the older
        # last_rescan_at would keep the cloud from inferring deletions.
        client.update(
            "sync_state",
            {**base, "or": f"(last_rescan_at.lt.{now},last_rescan_at.is.null)"},
            {"rescan_from_salid": pulled.window_from_salid,
             "last_rescan_at": now}, returning=False)


def send_heartbeats(client: supa.Supa, cfg: Config, result: CycleResult,
                    pos_clock, drift, rows_pulled: int, ok: bool,
                    note: str | None, agent_version: str) -> None:
    """
    One heartbeat for the cycle, plus one per anomaly.

    The agent has no grant on internal_anomalies (RLS gives it no policy),
    so anomalies ride out here as structured JSON with ok=false. The
    orchestrator mirrors every unmirrored ok=false heartbeat into the real
    table under service_role, keyed on heartbeat id so re-runs cannot
    duplicate.
    """
    beats = [R.heartbeat_payload(cfg.tenant_id, cfg.source_id, agent_version,
                                 pos_clock, drift, rows_pulled, ok, note)]
    for kind, detail in result.anomalies:
        beats.append(R.heartbeat_payload(
            cfg.tenant_id, cfg.source_id, agent_version, pos_clock, drift,
            0, False, R.anomaly_note(kind, detail, agent_version)))
    client.insert("heartbeats", beats, returning=False)


# ════════════════════════════════════════════════════════════════
# dry run
# ════════════════════════════════════════════════════════════════

def print_dry_run(result: CycleResult, state: State, driver: str,
                  out=None) -> None:
    """Reads and prints. Writes nothing, anywhere."""
    out = out or sys.stdout
    pulled = result.pull
    sold = [i["sold_at"] for i in result.invoices if i["sold_at"]]
    kinds: dict[str, int] = {}
    for inv in result.invoices:
        kinds[inv["kind"]] = kinds.get(inv["kind"], 0) + 1

    def w(text: str = "") -> None:
        print(text, file=out)

    w("=" * 62)
    w("POSentine agent — DRY RUN (nothing is written, anywhere)")
    w("=" * 62)
    w(f"  ODBC driver          {driver}")
    w(f"  schema check         OK (all required columns present)")
    w(f"  watermark_salid      {state.watermark_salid}")
    w(f"  POS MAX(salid)       {pulled.max_salid}")
    w(f"  POS clock            {pulled.pos_clock}")
    w(f"  rescan this cycle    {result.did_rescan}")
    w(f"  reference this cycle {result.did_reference}")
    w("")
    w(f"  invoices to upload   {len(result.invoices)}")
    w(f"  lines to upload      {len(result.lines)}")
    w(f"  cash counts          {len(result.cash)}")
    w(f"  products in snapshot {len(getattr(result, 'products_snapshot', {}))}")
    if sold:
        w(f"  sold_at range        {min(sold)}  ->  {max(sold)}")
    w("")
    w("  invoice kinds")
    for kind in ("cash", "external", "return", "other"):
        w(f"    {kind:<10} {kinds.get(kind, 0)}")
    w("")
    if result.unknown_itids:
        w(f"  ⚠ items missing from the snapshot: {result.unknown_itids}")
        w("    their lines would upload with list_price = NULL, and each")
        w("    would record an unknown_item anomaly. Zero-invoice detection")
        w("    cannot see a NULL-priced line.")
    else:
        w("  ✔ every line matched a product; no NULL list_price")
    for kind, detail in result.anomalies:
        w(f"  ⚠ anomaly: {kind} {detail}")
    _print_comparison(result, state, w)
    w("=" * 62)


def _print_first_run(top_salid: int, out=None) -> None:
    out = out or sys.stdout
    print("=" * 62, file=out)
    print("POSentine agent — FIRST RUN (nothing is written, anywhere)",
          file=out)
    print("=" * 62, file=out)
    print(f"  This machine has no state yet. A real run would adopt", file=out)
    print(f"  watermark_salid = {top_salid} (the POS MAX(salid)) and read", file=out)
    print("  nothing behind it.", file=out)
    print("", file=out)
    print("  History is not backfilled, by design. Reading it would drag the", file=out)
    print("  entire sales table across during service, and no report is ever", file=out)
    print("  built from data recorded before we started watching.", file=out)
    print("", file=out)
    print("  Expected here: 'invoices to upload' is 0 on a first install.", file=out)
    print("  A number in the thousands means STOP AND CALL.", file=out)
    print("=" * 62, file=out)


TOLERANCE = 5


def _print_comparison(result: CycleResult, state: State, w) -> None:
    """
    The acceptance check, run here because the POS machine has neither SSMS
    nor sqlcmd. A bare COUNT(*) against the same tables, no joins and no
    classification, compared with what the adapter produced.
    """
    raw = getattr(result, "raw_counts", None)
    w("  Cross-check against a bare count of the same tables")
    w(f"    SELECT COUNT(*) FROM dbo.Sales   WITH (NOLOCK) "
      f"WHERE salid  > {state.watermark_salid}")
    w(f"    SELECT COUNT(*) FROM dbo.SalesDe WITH (NOLOCK) "
      f"WHERE saleid > {state.watermark_salid}")
    if raw is None:
        w("    (not run)")
        return

    raw_inv, raw_lines = raw
    new_inv = result.new_invoice_count
    delta = raw_inv - new_inv
    w("")
    w(f"    bare COUNT(*) invoices   {raw_inv}")
    w(f"    agent would read         {new_inv}")
    w(f"    delta                    {delta}")
    w(f"    bare COUNT(*) lines      {raw_lines}")
    w("")

    if result.capped:
        w(f"  VERDICT: CAPPED — {new_inv} of {raw_inv} this cycle "
          f"(ceiling {MAX_INVOICES_PER_CYCLE}).")
        w("  The remainder follows on later cycles. Expected only after an")
        w("  outage; on a fresh install it means Day Zero did not run.")
    elif delta < 0:
        w(f"  VERDICT: ABORT — the agent claims {-delta} more invoice(s) than")
        w("  the POS has. This is not a timing artefact. Photograph this")
        w("  screen, change nothing, and call.")
    elif delta > TOLERANCE:
        w(f"  VERDICT: ABORT — delta {delta} exceeds the tolerance of "
          f"{TOLERANCE}.")
        w(f"  Only invoices newer than "
          f"{default_adapter.DIRTY_READ_GUARD_SECONDS} seconds should be")
        w("  missing. Photograph this screen, change nothing, and call.")
    else:
        w(f"  VERDICT: PASS — delta {delta}, within the tolerance of "
          f"{TOLERANCE}.")
        w(f"  Invoices newer than "
          f"{default_adapter.DIRTY_READ_GUARD_SECONDS} seconds are held back")
        w("  on purpose: NOLOCK can read a write that later rolls back, which")
        w("  would raise a deletion alert for a receipt that never existed.")


# ════════════════════════════════════════════════════════════════
# confirmation — read back what landed, on this screen
# ════════════════════════════════════════════════════════════════

def confirm(client: supa.Supa, cfg: Config, out=None) -> int:
    """
    Reads the cloud back and prints a verdict here, so nobody has to open
    a browser on a second device while standing at a counter.

    Read-only. Uses the agent's own token, so it also proves the token that
    ships is the one that works.
    """
    out = out or sys.stdout
    scope = {"tenant_id": f"eq.{cfg.tenant_id}", "source_id": f"eq.{cfg.source_id}"}
    problems: list[str] = []

    def w(text: str = "") -> None:
        print(text, file=out)

    counts = {name: client.count(name, scope) for name in
              ("invoices", "invoice_lines", "cash_counts",
               "pos_products", "pos_users")}
    null_price = client.count("invoice_lines", {**scope, "list_price": "is.null"})
    state_rows = client.select("sync_state", {**scope, "select": "*"})
    beats = client.select("heartbeats", {
        **scope, "select": "at,ok,drift_seconds,rows_pulled,note",
        "order": "at.desc", "limit": "5"}, paginate=False)

    w("=" * 62)
    w("POSentine — what actually landed in the cloud")
    w("=" * 62)
    for name, n in counts.items():
        w(f"  {name:<20} {n}")
    w(f"  {'lines w/o price':<20} {null_price}")
    w("")

    if not state_rows:
        problems.append("no sync_state row for this tenant/source")
    else:
        st = state_rows[0]
        w(f"  watermark_salid      {st.get('watermark_salid')}")
        w(f"  last_sync_at         {st.get('last_sync_at')}")
        w(f"  last_rescan_at       {st.get('last_rescan_at')}")
        w(f"  restore_suspected    {st.get('restore_suspected')}")
        w(f"  schema_ok            {st.get('schema_ok')}")
        if st.get("restore_suspected"):
            problems.append("restore_suspected is true — syncing is halted")
        if not st.get("schema_ok", True):
            problems.append("schema_ok is false — HD Soft changed a column")
        if not st.get("last_rescan_at"):
            problems.append("last_rescan_at is null — no rescan has completed, "
                            "so deletions cannot be detected yet")
        if not counts["invoices"]:
            problems.append("no invoices landed at all")

    w("")
    w("  recent heartbeats")
    for b in beats:
        flag = "ok " if b.get("ok") else "ERR"
        note = (b.get("note") or "")[:70]
        w(f"    {flag} {b.get('at')}  drift={b.get('drift_seconds')}  "
          f"rows={b.get('rows_pulled')}  {note}")
    if not beats:
        problems.append("no heartbeats at all — nothing has reported in")
    elif not beats[0].get("ok"):
        problems.append("the most recent heartbeat is not ok — read its note")

    if null_price:
        problems.append(
            f"{null_price} line(s) have no list_price. Zero-invoice detection "
            "cannot see them. Send us the unknown_item notes above.")

    w("")
    if problems:
        w("  RESULT: NEEDS ATTENTION")
        for p in problems:
            w(f"    ✗ {p}")
        w("=" * 62)
        return EXIT_ERROR
    w("  RESULT: OK — data landed, watermark advanced, agent reporting in")
    w("=" * 62)
    return EXIT_OK


# ════════════════════════════════════════════════════════════════
# entry point
# ════════════════════════════════════════════════════════════════

def run_once(cfg: Config, state: State, state_path: Path, adapter,
             client: supa.Supa | None, now_utc: _dt.datetime,
             dry_run: bool) -> int:
    agent_version = getattr(adapter, "AGENT_VERSION", "unknown")

    if state.restore_suspected:
        LOG.error("halted: a restore is suspected. salid moved backwards, so "
                  "the watermark matches nothing and every report would be "
                  "zero. Manual review required before syncing resumes.")
        return EXIT_HALTED
    if not state.schema_ok:
        LOG.error("halted: schema drift recorded. HD Soft changed a column "
                  "we depend on; numbers would be wrong rather than absent.")
        return EXIT_HALTED

    driver = "n/a"
    cn = None
    try:
        driver = adapter.pick_driver()
        cn = adapter.connect(**cfg.sql)
        adapter.verify_schema(cn)

        # ── Day Zero ────────────────────────────────────────────
        # A fresh install adopts MAX(salid) and reads nothing behind it.
        # Without this, the first cycle pulls the entire history — on the
        # real database 218,207 invoices and 481,293 lines, during service.
        # There is no backfill by design; this is what that means.
        if not state.initialised:
            top = _max_salid(adapter, cn)
            if dry_run:
                _print_first_run(top)
                return EXIT_OK
            state.initialised = True
            state.watermark_salid = top
            state.rescan_from_salid = top
            state.save(state_path)
            client.update("sync_state",
                          {"tenant_id": f"eq.{cfg.tenant_id}",
                           "source_id": f"eq.{cfg.source_id}",
                           "watermark_salid": f"lt.{top}"},
                          {"watermark_salid": top, "rescan_from_salid": top,
                           "pos_max_salid": top, "last_sync_at": R.utc_ts(now_utc)},
                          returning=False)
            LOG.info("first run: adopted watermark %d and read nothing behind "
                     "it. History is not backfilled by design.", top)
            return EXIT_OK

        result = build_cycle(adapter, cn, cfg, state, now_utc)
        if dry_run:
            result.raw_counts = _raw_counts(adapter, cn,      # type: ignore[attr-defined]
                                            state.watermark_salid)
    except default_adapter.RestoreSuspected as exc:
        LOG.error("restore suspected: %s", exc)
        if not dry_run and client is not None:
            _halt(client, cfg, "restore_suspected", str(exc), agent_version)
            state.restore_suspected = True
            state.save(state_path)
        return EXIT_HALTED
    except default_adapter.SchemaDrift as exc:
        LOG.error("schema drift: %s", exc)
        if not dry_run and client is not None:
            _halt(client, cfg, "schema_drift", str(exc), agent_version)
            state.schema_ok = False
            state.save(state_path)
        return EXIT_HALTED
    except Exception as exc:
        LOG.exception("cycle failed before upload: %s", exc)
        if not dry_run and client is not None:
            _beat_failure(client, cfg, "pull_failed", str(exc), agent_version)
        return EXIT_ERROR
    finally:
        if cn is not None:
            try:
                cn.close()
            except Exception:                       # pragma: no cover
                pass

    if dry_run:
        print_dry_run(result, state, driver)
        return EXIT_OK

    pulled = result.pull
    drift = adapter.clock_drift_seconds(pulled.pos_clock,
                                        now_utc.astimezone().replace(tzinfo=None))
    rows_pulled = len(result.invoices) + len(result.lines)
    try:
        upload(client, cfg, result)
        advance_sync_state(client, cfg, state, result, now_utc)
    except Exception as exc:
        # No watermark moves. Nothing is lost — the same range is re-read
        # in three minutes.
        LOG.exception("upload failed, watermark held at %d: %s",
                      state.watermark_salid, exc)
        _beat_failure(client, cfg, "upload_failed", str(exc), agent_version)
        return EXIT_ERROR

    # The local mirror follows the same rule as the remote write: what we
    # actually read, never MAX(salid) over the table.
    state.watermark_salid = result.watermark_target
    state.watermark_saledeid = pulled.max_saledeid
    state.last_date_change_rows = pulled.date_change_rows
    if result.did_rescan:
        state.rescan_from_salid = pulled.window_from_salid
    if getattr(result, "products_snapshot", None):
        state.products = result.products_snapshot   # type: ignore[attr-defined]
    state.cycle_index += 1
    state.save(state_path)

    send_heartbeats(client, cfg, result, pulled.pos_clock, drift,
                    rows_pulled, True, None, agent_version)
    LOG.info("cycle ok — %d invoices, %d lines, watermark now %d",
             len(result.invoices), len(result.lines), state.watermark_salid)
    return EXIT_OK


def _halt(client: supa.Supa, cfg: Config, kind: str, detail: str,
          agent_version: str) -> None:
    client.update("sync_state",
                  {"tenant_id": f"eq.{cfg.tenant_id}",
                   "source_id": f"eq.{cfg.source_id}"},
                  {"restore_suspected": kind == "restore_suspected",
                   "schema_ok": kind != "schema_drift"}, returning=False)
    _beat_failure(client, cfg, kind, detail, agent_version)


def _beat_failure(client: supa.Supa, cfg: Config, kind: str, detail: str,
                  agent_version: str) -> None:
    try:
        client.insert("heartbeats", [R.heartbeat_payload(
            cfg.tenant_id, cfg.source_id, agent_version, None, None, 0, False,
            R.anomaly_note(kind, {"message": detail[:500]}, agent_version))],
            returning=False)
    except Exception:                               # pragma: no cover
        LOG.exception("could not record the failure heartbeat either")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="POSentine POS-side agent.")
    ap.add_argument("--config", default="config.json", type=Path)
    ap.add_argument("--state", default="state.json", type=Path)
    ap.add_argument("--log", default=None, type=Path)
    ap.add_argument("--dry-run", action="store_true",
                    help="read and print; write nothing, anywhere")
    ap.add_argument("--confirm", action="store_true",
                    help="read the cloud back and print a verdict; writes nothing")
    ap.add_argument("--fake", action="store_true",
                    help="use the fixture adapter instead of SQL Server")
    ap.add_argument("--loop", action="store_true",
                    help="debugging only; the Scheduled Task is the loop")
    args = ap.parse_args(argv)

    configure_output(args.log)

    try:
        cfg = Config.load(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOG.error("cannot load %s: %s", args.config, exc)
        return EXIT_ERROR

    adapter = default_adapter
    if args.fake:
        import fake_adapter
        adapter = fake_adapter

    if args.confirm:
        client = supa.Supa(cfg.supabase_url, anon_key=cfg.supabase_anon_key,
                           token=cfg.supabase_agent_token)
        try:
            return confirm(client, cfg)
        except supa.SupaError as exc:
            LOG.error("could not read the cloud back: %s", exc)
            return EXIT_ERROR

    while True:
        state = State.load(args.state)
        client = None
        if not args.dry_run:
            client = supa.Supa(cfg.supabase_url,
                               anon_key=cfg.supabase_anon_key,
                               token=cfg.supabase_agent_token)

        # A dry run takes no lock: it writes nothing, so it cannot conflict
        # with the scheduled cycle, and it must never block one either.
        if args.dry_run:
            code = run_once(cfg, state, args.state, adapter, client,
                            _dt.datetime.now(_dt.timezone.utc), True)
        else:
            with SingleInstanceLock(args.state.with_suffix(".lock")) as lock:
                if not lock.may_run:
                    LOG.info("another cycle is running; exiting quietly")
                    return EXIT_ANOTHER_INSTANCE
                code = run_once(cfg, state, args.state, adapter, client,
                                _dt.datetime.now(_dt.timezone.utc), False)

        if not args.loop:
            return code
        time.sleep(cfg.sync_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
