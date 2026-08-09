# -*- coding: utf-8 -*-
"""
fake_adapter.py — fixture adapter, same interface as adapter_hdsoft
================================================================
Lets the whole pipeline run on a machine with no SQL Server, so the agent
can be exercised end to end before anyone stands in the shop.

The data is synthetic and generated in code. Real menu item names are used
because the menu is public; nothing else from the customer's export is —
no staff names, no real cash-count values, no receipt numbers tied to a
named individual. `data UN/` stays out of the repository entirely.

    python agent.py --fake --dry-run --config config.example.json

This is not an acceptance test. It proves the plumbing, never the numbers.
================================================================
"""

from __future__ import annotations

import datetime as _dt

from adapter_hdsoft import (AGENT_VERSION, AdapterError, CashCount,  # noqa: F401
                            Invoice, InvoiceLine, PullResult, RestoreSuspected,
                            SchemaDrift, classify_cash_count, classify_invoice,
                            clock_drift_seconds)

# Real names from a public menu. Prices are plausible, not exported.
MENU = [
    (1, "طعمية", 15.0),
    (2, "فول صغير", 15.0),
    (3, "بطاطس كاتشب", 25.0),
    (4, "حمص صغير", 25.0),
    (5, "بابا غنوج صغير", 25.0),
    (6, "عدد 7 طعمية", 10.0),
    (7, "باكت بطاطس صغير", 20.0),
    # Kitchen notes: list_price 0 makes is_modifier true in the database,
    # which is what keeps them out of the top-items ranking.
    (80, "شطة حمراء", 0.0),
    (81, "بدون دقة", 0.0),
]

BASE_DAY = _dt.date(2026, 8, 8)
FIRST_SALID = 1000

# Deliberately not in MENU: exercises the unknown-item path, where a line
# must upload with list_price NULL and raise an anomaly rather than 0.
UNKNOWN_ITID = 4242


class _Cursor:
    def close(self) -> None:
        pass


class _Connection:
    def cursor(self):
        return _Cursor()

    def close(self) -> None:
        pass


def pick_driver() -> str:
    return "fixture (no ODBC)"


def connect(**_kwargs) -> _Connection:
    return _Connection()


def verify_schema(_cn) -> None:
    return None


def _invoice(salid: int, hour: int, minute: int, total: float, kind: str,
             uid: int = 2, delivery: float = 0.0) -> Invoice:
    sal_t = {"cash": 1, "external": 2, "return": 1, "other": 3}[kind]
    return Invoice(
        salid=salid,
        receipt_num=salid - FIRST_SALID + 1,
        sold_at=_dt.datetime.combine(BASE_DAY, _dt.time(hour, minute)),
        total=total,
        delivery_cost=delivery,
        user_uid=uid,
        cust_code=None,
        sal_t=sal_t,
        sal_type=1 if kind == "return" else 0,
        sale_rtype=1 if kind == "return" else 0,
        kind=kind,
    )


def _dataset() -> tuple[list[Invoice], list[InvoiceLine]]:
    invoices: list[Invoice] = []
    lines: list[InvoiceLine] = []
    saledeid = 90000

    for n in range(40):                      # ordinary morning trade
        salid = FIRST_SALID + n
        inv = _invoice(salid, 8 + n // 6, (n * 7) % 60, 45.0, "cash")
        invoices.append(inv)
        for itid, _name, price in MENU[:3]:
            saledeid += 1
            lines.append(InvoiceLine(saledeid=saledeid, salid=salid, itid=itid,
                                     qty=1.0, line_total=price,
                                     sold_at=inv.sold_at))

    delivery = _invoice(FIRST_SALID + 100, 13, 5, 142.0, "external",
                        delivery=27.0)
    invoices.append(delivery)
    saledeid += 1
    lines.append(InvoiceLine(saledeid=saledeid, salid=delivery.salid, itid=3,
                             qty=1.0, line_total=115.0, sold_at=delivery.sold_at))

    refund = _invoice(FIRST_SALID + 101, 14, 20, 40.0, "return")
    invoices.append(refund)

    # A zero-total receipt whose items are priced on the menu: the exact
    # shape detect_zero_invoices exists to find.
    free = _invoice(FIRST_SALID + 102, 15, 40, 0.0, "cash")
    invoices.append(free)
    for itid, _name, price in MENU[:5]:
        saledeid += 1
        lines.append(InvoiceLine(saledeid=saledeid, salid=free.salid, itid=itid,
                                 qty=1.0, line_total=0.0, sold_at=free.sold_at))

    # Kitchen notes only — must NOT be reported.
    notes = _invoice(FIRST_SALID + 103, 16, 10, 0.0, "cash")
    invoices.append(notes)
    for itid in (80, 81):
        saledeid += 1
        lines.append(InvoiceLine(saledeid=saledeid, salid=notes.salid, itid=itid,
                                 qty=1.0, line_total=0.0, sold_at=notes.sold_at))

    # An item absent from the snapshot.
    odd = _invoice(FIRST_SALID + 104, 17, 0, 33.0, "cash")
    invoices.append(odd)
    saledeid += 1
    lines.append(InvoiceLine(saledeid=saledeid, salid=odd.salid,
                             itid=UNKNOWN_ITID, qty=1.0, line_total=33.0,
                             sold_at=odd.sold_at))

    return invoices, lines


def pull(cn, watermark_salid: int, rescan_from_salid: int,
         do_rescan: bool, do_reference: bool) -> PullResult:
    invoices, lines = _dataset()
    res = PullResult()
    res.pos_clock = _dt.datetime.combine(BASE_DAY, _dt.time(18, 0))
    res.max_salid = max(i.salid for i in invoices)
    res.max_saledeid = max(ln.saledeid for ln in lines)

    if watermark_salid and res.max_salid < watermark_salid:
        raise RestoreSuspected(
            f"MAX(salid)={res.max_salid} < watermark={watermark_salid}")

    fresh = [i for i in invoices if i.salid > watermark_salid]
    res.new_invoices = fresh
    fresh_ids = {i.salid for i in fresh}
    res.new_lines = [ln for ln in lines if ln.salid in fresh_ids]

    if do_rescan:
        res.window_from_salid = FIRST_SALID
        res.rescanned_invoices = list(invoices)
        res.window_salids = [i.salid for i in invoices]
        res.rescanned_lines = list(lines)

    res.cash_counts = [
        CashCount(srid=1, counted_at=_dt.datetime.combine(BASE_DAY, _dt.time(19, 5)),
                  user_uid=2, user_value=0.0, app_value=1800.0,
                  diff_value=-1800.0, pc_name="FIXTURE-PC",
                  kind=classify_cash_count(0.0)),
        CashCount(srid=2, counted_at=_dt.datetime.combine(BASE_DAY, _dt.time(19, 6)),
                  user_uid=2, user_value=1750.0, app_value=1800.0,
                  diff_value=-50.0, pc_name="FIXTURE-PC",
                  kind=classify_cash_count(1750.0)),
    ]
    res.date_change_rows = 0
    res.unknown_sal_t = [i.sal_t for i in invoices if i.kind == "other"]

    if do_reference:
        res.products = [{"itid": itid, "itcode": f"F{itid}", "itname": name,
                         "list_price": price, "main_cat": 1, "sub_cat": 1}
                        for itid, name, price in MENU]
        res.users = [{"uid": 2, "name": "مستخدم تجريبي"}]
        res.db_size_mb = 512.0

    return res
