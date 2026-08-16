# -*- coding: utf-8 -*-
"""PHASE 3 — REAL-DATA RECONCILIATION (read-only).

Reads the REAL invoices / withdrawals / pos_users from Supabase (agent
token, GET only), runs the LOCKED metrics.py compute_shift over the exact
shift windows of the three verified Telegram reports, and compares every
number the dashboard will display against what the Telegram reports say.

Nothing is written anywhere. Secrets are masked in output.
"""
import datetime as _dt
import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

sys.path.insert(0, ".")
import metrics as M  # the locked pure module — never modified

with open("Docs/config.json", encoding="utf-8") as f:
    cfg = json.load(f)

BASE = cfg["supabase_url"].rstrip("/")
ANON = cfg.get("supabase_anon_key", "")
TOKEN = cfg["supabase_agent_token"]
TENANT = cfg["tenant_id"]
SOURCE = cfg["source_id"]
SCOPE = {"tenant_id": f"eq.{TENANT}", "source_id": f"eq.{SOURCE}"}


def mask(text: str) -> str:
    for s in (ANON, TOKEN):
        if s and s in text:
            text = text.replace(s, "***")
    return text


def get_all(table: str, params: dict) -> list[dict]:
    out = []
    offset = 0
    while True:
        r = requests.get(
            BASE + "/rest/v1/" + table,
            headers={"apikey": ANON, "Authorization": "Bearer " + TOKEN,
                     "Accept": "application/json",
                     "Range-Unit": "items", "Range": f"{offset}-{offset + 999}"},
            params=params, timeout=60,
        )
        if r.status_code != 200:
            print(f"  ! GET {table} HTTP {r.status_code} {mask(r.text)[:200]}")
            return out
        chunk = r.json() or []
        out.extend(chunk)
        if len(chunk) < 1000:
            return out
        offset += 1000


class Inv:
    def __init__(self, r):
        self.salid = int(r["salid"])
        self.total = float(r.get("total") or 0)
        self.delivery_cost = float(r.get("delivery_cost") or 0)
        self.user_uid = int(r["user_uid"]) if r.get("user_uid") is not None else None
        self.kind = r.get("kind") or "other"
        self.sold_at = _dt.datetime.fromisoformat(r["sold_at"])


class Line:
    def __init__(self, r):
        self.itid = int(r["itid"]) if r.get("itid") is not None else None
        self.item_name = r.get("item_name")
        self.list_price = float(r["list_price"]) if r.get("list_price") is not None else None
        self.qty = float(r.get("qty") or 0)
        self.line_total = float(r.get("line_total") or 0)


W = float  # withdrawals are floats in the pipeline


def main() -> int:
    print("=" * 78)
    print("PHASE 3 RECONCILIATION — real Supabase data vs verified Telegram reports")
    print("=" * 78)

    win_start = _dt.datetime(2026, 8, 13, 19, 0)     # evening 13/14 begins
    win_end = _dt.datetime(2026, 8, 16, 7, 0)        # morning 15/16 ends

    inv_rows = get_all("invoices", {**SCOPE,
                                    "sold_at": f"gte.{win_start.isoformat()}",
                                    "select": "salid,total,delivery_cost,user_uid,kind,sold_at,deleted_at"})
    # PostgREST cannot take two sold_at filters under one key; do range in python.
    invoices = [Inv(r) for r in inv_rows
                if win_start <= _dt.datetime.fromisoformat(r["sold_at"]) < win_end
                and r.get("deleted_at") is None]
    print(f"  invoices in window (excl. deleted): {len(invoices)}")

    line_rows = get_all("invoice_lines", {**SCOPE,
                                          "select": "salid,itid,item_name,list_price,qty,line_total,sold_at"})
    lines_by_salid: dict[int, list] = {}
    for r in line_rows:
        if not r.get("sold_at"):
            continue
        t = _dt.datetime.fromisoformat(r["sold_at"])
        if win_start <= t < win_end:
            lines_by_salid.setdefault(int(r["salid"]), []).append(Line(r))

    wd_rows = get_all("withdrawals", {**SCOPE,
                                      "perdate": f"gte.{win_start.isoformat()}",
                                      "select": "perid,perdate,peramount"})
    withdrawals = [float(r["peramount"]) for r in wd_rows
                   if win_start <= _dt.datetime.fromisoformat(r["perdate"]) < win_end]

    users = {int(r["uid"]): r.get("name") for r in
             get_all("pos_users", {**SCOPE, "select": "uid,name"})
             if r.get("uid") is not None}

    # ── the three verified Telegram reports ──────────────────────
    reports = [
        {  # New مستند نصي.txt — الجمعة 14 أغسطس · الصباح · حمص
            "shift_date": _dt.date(2026, 8, 14), "shift_name": "morning",
            "sales": 16080, "collections": 4140, "delivery": 820,
            "withdrawals": 2795, "returns": 250, "grand_total": 16355,
            "n_cash": 240, "n_return": 1, "n_external": 28,
            "primary": "حمص", "other": [("محمود", 26, 1955)],
            "top5": [("طعمية", 90), ("بطاطس كاتشب", 79), ("فول", 67),
                     ("بطاطس ثومية", 53), ("فول وسط", 44)],
        },
        {  # New مستند نصي (2).txt — الجمعة 14 أغسطس · المساء · محمود
            "shift_date": _dt.date(2026, 8, 14), "shift_name": "evening",
            "sales": 15945, "collections": 1870, "delivery": 365,
            "withdrawals": 50, "returns": 0, "grand_total": 17400,
            "n_cash": 245, "n_return": 0, "n_external": 12,
            "primary": "محمود", "other": [("حمص", 2, 135)],
            "top5": [("طعمية", 134), ("بطاطس كاتشب", 77), ("بطاطس ثومية", 76),
                     ("فول", 73), ("بطاطس", 55)],
        },
        {  # New مستند نصي (3).txt — السبت 15 أغسطس · الصباح · حمص
            "shift_date": _dt.date(2026, 8, 15), "shift_name": "morning",
            "sales": 18550, "collections": 3880, "delivery": 765,
            "withdrawals": 0, "returns": 0, "grand_total": 21665,
            "n_cash": 281, "n_return": 0, "n_external": 26,
            "primary": "حمص", "other": [("محمود", 13, 730)],
            "top5": [("طعمية", 129), ("بطاطس كاتشب", 104), ("فول", 104),
                     ("بطاطس ثومية", 90), ("بطاطس", 44)],
        },
    ]

    failures = 0
    for rep in reports:
        start, end = M.shift_window(rep["shift_date"], rep["shift_name"])
        invs = [i for i in invoices if start <= i.sold_at < end]
        wd = sum(w for w in withdrawals if True)  # filtered by window below
        # withdrawals are windowed like the orchestrator does:
        wd_win = sum(w for w in withdrawals if _in_window(w, start, end, []))
        # simpler: recompute windowed withdrawals from rows
        wd_win = sum(_windowed_withdrawals(wd_rows, start, end))
        m = M.compute_shift(rep["shift_date"], rep["shift_name"], invs,
                            lines_by_salid=lines_by_salid,
                            user_names=users, withdrawals=wd_win)

        label = f"{rep['shift_date']} {rep['shift_name']}"
        print(f"\n── {label} ──")
        for field in ("sales", "collections", "delivery", "withdrawals",
                      "returns", "grand_total", "n_cash", "n_return", "n_external"):
            got = getattr(m, field)
            want = rep[field]
            ok = abs(float(got) - float(want)) < 0.01
            if not ok:
                failures += 1
            print(f"  {field:12s} report={want:>10,.0f}  metrics={got:>10,.0f}  "
                  f"{'✅' if ok else '❌ MISMATCH'}")

        prim = m.primary_user.name if m.primary_user else None
        okp = prim == rep["primary"]
        if not okp:
            failures += 1
        print(f"  {'primary':12s} report={rep['primary']!r}  metrics={prim!r}  "
              f"{'✅' if okp else '❌ MISMATCH'}")

        other = [(u.name, u.invoices, u.amount) for u in m.other_users]
        oko = other == rep["other"]
        if not oko:
            failures += 1
        print(f"  {'other_users':12s} report={rep['other']}  metrics={other}  "
              f"{'✅' if oko else '❌ MISMATCH'}")

        tops = m.top_items
        # metrics returns (name, qty) tuples; compare first 5 by name+qty
        tops5 = [(n, round(q, 3)) for n, q in tops[:5]]
        want5 = rep["top5"]
        # normalise int vs float
        want5n = [(n, float(q)) for n, q in want5]
        okt = tops5 == want5n
        if not okt:
            failures += 1
        print(f"  {'top_items':12s} report={want5}  metrics={tops5}  "
              f"{'✅' if okt else '❌ MISMATCH'}")

    print("\n" + "=" * 78)
    if failures:
        print(f"RECONCILIATION: {failures} MISMATCHES — investigate before wiring the dashboard")
        return 1
    print("RECONCILIATION PASS — real Supabase raw data through locked metrics.py")
    print("reproduces all three verified Telegram reports, number for number.")
    return 0


def _in_window(_w, _s, _e, _r):
    return True


def _windowed_withdrawals(wd_rows, start, end) -> list[float]:
    out = []
    for r in wd_rows:
        t = _dt.datetime.fromisoformat(r["perdate"])
        if start <= t < end:
            out.append(float(r["peramount"]))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
