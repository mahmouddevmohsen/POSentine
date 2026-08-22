# -*- coding: utf-8 -*-
"""
test_withdrawals.py — مسحوبات (withdrawals) contract tests
================================================================
The confirmed business semantics (user-confirmed, forensic reports):
    1. HD Soft "مسحوبات"  =  SUM(Personal.peramount) over the shift window.
    2. مسحوبات is SUBTRACTED from the daybook grand total:
           الإجمالي = مبيعات + مقبوضات − مرتجع − دليفري − مسحوبات
    3. UID 2 = حمص = morning shift · UID 1 = محمود = evening shift.
       Attribution is the WINDOW, never the user: peruser is preserved as
       metadata/evidence only, and must never split or re-add the total.

The rules under test live at every layer:
    adapter   — dbo.Personal is read whole (no watermark — rows can be
                deleted, Perid has gaps), SELECT-only + NOLOCK, with the
                same 30-second dirty-read guard as invoices.
    rows      — withdrawal_payload maps Perid→perid, peramount→peramount,
                perdate→pos_ts (POS-local, never converted).
    agent     — withdrawals ride the upload (upsert conflict on
                tenant_id,source_id,perid) and POS deletions are mirrored
                to the cloud (absence is the signal, same as invoices).
    metrics   — compute_shift totals the new component into grand_total.
    orchestrator — _sum_withdrawals filters by the exact shift window
                [start, end) — 06:59:59 belongs to the previous evening.

The live-evidence tests pin the forensic reference values (2026-08-11/12)
as DB-derived expectations — they assert the aggregation path, never a
claim that the value was observed on an HD Soft screen.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from types import SimpleNamespace

import adapter_hdsoft as AD
import agent as A
import metrics as M
import orchestrator as OR
import rows as R

TID = "57b61b47-a590-49fe-803c-0c174a07b7ec"
SID = "93f8d146-ba68-4d58-8eda-f797f3e28bd4"


def t(y, mo, d, h, mi=0, s=0):
    return _dt.datetime(y, mo, d, h, mi, s)          # naive POS local


@dataclass
class _Inv:
    salid: int
    kind: str
    total: float
    delivery_cost: float = 0.0
    user_uid: int | None = 2
    sold_at: _dt.datetime = t(2026, 8, 1, 12, 0)


def compute(sales=0.0, collections=0.0, returns=0.0, delivery=0.0,
            withdrawals=0.0, date=_dt.date(2026, 8, 10), name="evening"):
    """metrics.compute_shift on a tiny constructed shift."""
    invs: list[_Inv] = []
    i = 0
    if sales:
        i += 1
        invs.append(_Inv(i, "cash", sales,
                         delivery_cost=delivery if not collections else 0.0))
    if collections:
        i += 1
        invs.append(_Inv(i, "external", collections))
    if returns:
        i += 1
        invs.append(_Inv(i, "return", returns))
    # delivery on its own line when no cash invoice carries it
    if delivery and collections:
        i += 1
        invs.append(_Inv(i, "cash", 0.0, delivery_cost=delivery))
    return M.compute_shift(date, name, invs, withdrawals=withdrawals)


def wd(perid, perdate, amount, user=2, branch=0, per_type=0, note=None):
    return OR.Withdrawal(perid=perid, perdate=perdate, amount=amount,
                         user_uid=user, branch_id=branch,
                         per_type=per_type, note=note)


# ════════════════════════════════════════════════════════════════
# 1) the formula — مسحوبات is subtracted, never added
# ════════════════════════════════════════════════════════════════

def test_no_withdrawals_formula():
    m = compute(sales=1000.0, collections=100.0, returns=50.0, delivery=20.0,
                withdrawals=0.0)
    assert m.withdrawals == 0.0
    assert m.grand_total == 1030.0          # 1000 + 100 − 50 − 20 − 0


def test_single_withdrawal_is_subtracted():
    m = compute(sales=1000.0, collections=100.0, returns=50.0, delivery=20.0,
                withdrawals=50.0)
    assert m.grand_total == 980.0           # 1030 − 50


def test_multiple_withdrawals_are_summed_then_subtracted():
    m = compute(sales=1000.0, collections=100.0, returns=50.0, delivery=20.0,
                withdrawals=400.0)          # 50 + 100 + 250
    assert m.grand_total == 630.0


def test_grand_total_is_never_sales_plus_withdrawals():
    # a regression tripwire for the double-count failure shape: مسحوبات
    # must only ever appear on the MINUS side
    m = compute(sales=1000.0, collections=0.0, returns=0.0, delivery=0.0,
                withdrawals=100.0)
    assert m.grand_total == 900.0


# ════════════════════════════════════════════════════════════════
# 2) shift-window attribution — same boundaries as invoices
# ════════════════════════════════════════════════════════════════

def test_morning_boundary_065959_belongs_to_previous_evening():
    """نافذة المساء [19:00, 07:00) — 06:59:59 يوم 8/11 لسه المساء 8/10."""
    start, end = M.shift_window(_dt.date(2026, 8, 10), "evening")
    w = wd(1, t(2026, 8, 11, 6, 59, 59), amount=10.0)
    assert OR._sum_withdrawals([w], start, end) == 10.0


def test_morning_boundary_070000_belongs_to_morning():
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    w = wd(1, t(2026, 8, 11, 7, 0, 0), amount=10.0)
    assert OR._sum_withdrawals([w], start, end) == 10.0


def test_evening_boundary_185959_belongs_to_morning():
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    w = wd(1, t(2026, 8, 11, 18, 59, 59), amount=10.0)
    assert OR._sum_withdrawals([w], start, end) == 10.0


def test_evening_boundary_190000_belongs_to_evening():
    start, end = M.shift_window(_dt.date(2026, 8, 11), "evening")
    w = wd(1, t(2026, 8, 11, 19, 0, 0), amount=10.0)
    assert OR._sum_withdrawals([w], start, end) == 10.0


def test_resolve_shift_agrees_for_withdrawal_timestamps():
    """المسحوبات بتستخدم نفس resolve_shift بتاع الفواتير بالظبط."""
    assert M.resolve_shift(t(2026, 8, 11, 7, 50, 50)) == \
        (_dt.date(2026, 8, 11), "morning")
    assert M.resolve_shift(t(2026, 8, 11, 6, 59, 59)) == \
        (_dt.date(2026, 8, 10), "evening")


def test_withdrawal_outside_the_window_is_ignored():
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    inside = wd(1, t(2026, 8, 11, 9, 0), amount=5.0)
    outside = wd(2, t(2026, 8, 11, 19, 1), amount=99.0)   # next evening
    assert OR._sum_withdrawals([inside, outside], start, end) == 5.0


# ════════════════════════════════════════════════════════════════
# 3) two cashiers — window is authoritative, peruser is metadata
# ════════════════════════════════════════════════════════════════

def test_two_cashiers_summed_by_window_not_by_user():
    # وردية صباح: مسحوبة لحمص (uid=2) — والمفروض كلها بتتحسب
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    rows = [
        wd(1, t(2026, 8, 11, 7, 50), amount=50.0, user=2),    # حمص صباح
        wd(2, t(2026, 8, 11, 8, 30), amount=150.0, user=2),   # حمص صباح
    ]
    assert OR._sum_withdrawals(rows, start, end) == 200.0


def test_a_morning_withdrawal_by_the_evening_cashier_still_counts():
    # حتى لو الكاشير المسائي (محمود uid=1) سحب صباحاً — التجميع بالنافذة
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    w = wd(1, t(2026, 8, 11, 10, 0), amount=75.0, user=1)     # محمود صباح
    assert OR._sum_withdrawals([w], start, end) == 75.0


def test_user_attribution_is_preserved_as_metadata():
    # peruser/PerBRID/pertype/pernote ride along on the payload — evidence,
    # never a second aggregation axis
    row = SimpleNamespace(Perid=3, peramount=50.0,
                          perdate=t(2026, 8, 11, 7, 50, 50),
                          peruser=2, PerBRID=0, pertype=0, pernote="عيش")
    w = AD._row_to_withdrawal(row)
    p = R.withdrawal_payload(w, TID, SID)
    assert p["peruser"] == 2
    assert p["perbr_id"] == 0
    assert p["pertype"] == 0
    assert p["pernote"] == "عيش"


# ════════════════════════════════════════════════════════════════
# 4) adapter read path — dbo.Personal, whole-table, read-only
# ════════════════════════════════════════════════════════════════

class _CannedCursor:
    def __init__(self, fetchall_map, fetchone_map=None):
        self.fetchall_map = fetchall_map
        self.fetchone_map = fetchone_map or {}
        self.executed: list[str] = []
        self.params: list[tuple] = []
        self._pending_all = []
        self._pending_one = None

    def execute(self, sql, *params):
        self.executed.append(sql)
        self.params.append((sql, params))
        self._pending_all = []
        self._pending_one = None
        for key, rows in self.fetchall_map.items():
            if key in sql:
                self._pending_all = rows
                break
        for key, row in self.fetchone_map.items():
            if key in sql:
                self._pending_one = row
                break

    def fetchone(self):
        return self._pending_one

    def fetchall(self):
        out = self._pending_all
        self._pending_all = []
        return out

    def close(self):
        pass


class _CannedConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _personal_row(perid, amount, perdate, peruser=2, perbr=0, pertype=0,
                  note=None):
    return SimpleNamespace(Perid=perid, peramount=amount, perdate=perdate,
                           peruser=peruser, PerBRID=perbr, pertype=pertype,
                           pernote=note)


def _pull_with_personal(rows, do_rescan=False):
    cur = _CannedCursor(
        fetchall_map={
            "FROM dbo.Personal": rows,
            "FROM dbo.Sales s": [],
            "FROM dbo.SalesDe d": [],
            "FROM dbo.safeR": [],
            "FROM dbo.Items": [],
            "FROM dbo.Users": [],
        },
        fetchone_map={
            "SELECT GETDATE()": (t(2026, 8, 11, 18, 0),),
            "MAX(salid)": (100,),
            "MAX(saledeid)": (200,),
            "COUNT(*) FROM dbo.DateChangeLog": (0,),
            "SUM(size)": (512.0,),
        })
    return AD.pull(_CannedConn(cur), watermark_salid=0, rescan_from_salid=0,
                   do_rescan=do_rescan, do_reference=False), cur


def test_pull_reads_personal_whole_and_parses_rows():
    rows = [
        _personal_row(1, 50.0, t(2026, 8, 11, 7, 50, 50)),
        _personal_row(2, 100.0, t(2026, 8, 11, 20, 15), peruser=1),
        _personal_row(3, 0.0, t(2026, 8, 11, 9, 0)),          # zero stays
    ]
    res, cur = _pull_with_personal(rows)
    assert [w.perid for w in res.withdrawals] == [1, 2, 3]
    assert res.withdrawals[0].amount == 50.0
    assert res.withdrawals[1].user_uid == 1
    assert res.withdrawals[2].amount == 0.0
    # whole table read — no perid watermark filter (rows can be deleted)
    sql = next(s for s in cur.executed if "FROM dbo.Personal" in s)
    assert "Perid" in sql and "peramount" in sql and "perdate" in sql
    assert "peruser" in sql and "PerBRID" in sql and "pertype" in sql
    assert "pernote" in sql
    assert ">" not in sql.replace("perdate <", "")           # no watermark


def test_personal_query_is_select_only_with_nolock_and_dirty_read_guard():
    res, cur = _pull_with_personal([])
    sql = next(s for s in cur.executed if "FROM dbo.Personal" in s)
    assert sql.lstrip().upper().startswith("SELECT")
    assert "WITH (NOLOCK)" in sql
    assert "perdate < DATEADD(second, -?, GETDATE())" in sql
    # the 30-second guard parameter is passed (same as invoices)
    sql, params = next((s, p) for s, p in cur.params
                       if "FROM dbo.Personal" in s)
    assert params == (AD.DIRTY_READ_GUARD_SECONDS,)


def test_empty_personal_table_yields_no_withdrawals():
    res, _cur = _pull_with_personal([])
    assert res.withdrawals == []


def test_large_personal_table_sums_deterministically():
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    rows = [wd(i, t(2026, 8, 11, 7 + (i % 11), (i * 7) % 60),
               amount=float(i % 5 + 1)) for i in range(1, 1001)]
    first = OR._sum_withdrawals(rows, start, end)
    second = OR._sum_withdrawals(rows, start, end)
    assert first == second and first > 0          # pure + idempotent


class _SchemaCursor:
    """verify_schema executes one query per table with the table name as a
    parameter — this cursor answers sys.columns by that parameter."""

    def __init__(self, columns_by_table: dict[str, list[str]]):
        self.columns_by_table = columns_by_table
        self._pending = []

    def execute(self, sql, *params):
        table = params[0] if params else ""
        self._pending = [(c,) for c in self.columns_by_table.get(table, [])]

    def fetchone(self):
        return self._pending[0] if self._pending else None

    def fetchall(self):
        out = self._pending
        self._pending = []
        return out

    def close(self):
        pass


_GOOD = {
    "Sales": list(AD._REQUIRED["Sales"]),
    "SalesDe": list(AD._REQUIRED["SalesDe"]),
    "Items": list(AD._REQUIRED["Items"]),
    "Users": list(AD._REQUIRED["Users"]),
    "safeR": list(AD._REQUIRED["safeR"]),
    "Personal": list(AD._REQUIRED["Personal"]),
}


def test_schema_verification_requires_personal_columns():
    # verify_schema reads sys.columns as rows and indexes r[0]
    class _Conn:
        def __init__(self, cur):
            self._cur = cur
        def cursor(self):
            return self._cur
        def close(self):
            pass

    AD.verify_schema(_Conn(_SchemaCursor(_GOOD)))          # must not raise

    bad = {tbl: cols for tbl, cols in _GOOD.items()}
    bad["Personal"] = [c for c in bad["Personal"] if c != "pernote"]
    import pytest
    with pytest.raises(AD.SchemaDrift) as ei:
        AD.verify_schema(_Conn(_SchemaCursor(bad)))
    assert "Personal.pernote" in str(ei.value)


# ════════════════════════════════════════════════════════════════
# 5) rows / payload — pos_ts, never converted
# ════════════════════════════════════════════════════════════════

def test_withdrawal_payload_maps_every_field():
    w = wd(perid=9, perdate=t(2026, 8, 11, 20, 15), amount=150.0,
           user=1, branch=2, per_type=0, note="مصاريف")
    p = R.withdrawal_payload(w, TID, SID)
    assert p["tenant_id"] == TID and p["source_id"] == SID
    assert p["perid"] == 9
    assert p["perdate"] == "2026-08-11T20:15:00"      # POS-local, no zone
    assert p["peramount"] == 150.0
    assert p["peruser"] == 1
    assert p["perbr_id"] == 2
    assert p["pertype"] == 0
    assert p["pernote"] == "مصاريف"


def test_withdrawal_payload_refuses_aware_timestamps():
    import pytest
    aware = t(2026, 8, 11, 20, 15).replace(tzinfo=_dt.timezone.utc)
    w = wd(1, aware, amount=10.0)
    with pytest.raises(ValueError):
        R.withdrawal_payload(w, TID, SID)


def test_branch_field_round_trips_through_adapter_and_payload():
    row = _personal_row(7, 60.0, t(2026, 8, 11, 9, 0), perbr=3)
    p = R.withdrawal_payload(AD._row_to_withdrawal(row), TID, SID)
    assert p["perbr_id"] == 3


# ════════════════════════════════════════════════════════════════
# 6) agent — uploads and mirrors deletions
# ════════════════════════════════════════════════════════════════

class _FakeSupa:
    def __init__(self, cloud_perids: list[int] | None = None):
        self.cloud_perids = cloud_perids
        self.upserts: list[tuple[str, int, str]] = []
        self.deletes: list[tuple[str, dict]] = []
        self.selects: list[tuple[str, dict]] = []

    def upsert(self, table, rows, on_conflict):
        self.upserts.append((table, len(rows), on_conflict))
        return len(rows)

    def select(self, table, params=None, paginate=True):
        self.selects.append((table, dict(params or {})))
        if table == "withdrawals" and self.cloud_perids is not None:
            return [{"perid": p} for p in self.cloud_perids]
        return []

    def delete(self, table, filters):
        self.deletes.append((table, dict(filters)))
        return None


CFG = A.Config(
    tenant_id=TID, source_id=SID, supabase_url="https://example.supabase.co",
    supabase_anon_key="anon", supabase_agent_token="tok",
    sql={"server": "x", "database": "y", "user": "z", "password": "p"},
)


def _result_with(withdrawals):
    res = A.CycleResult()
    res.withdrawals = withdrawals
    return res


def test_upload_sends_withdrawals_with_the_dedup_conflict():
    payloads = [R.withdrawal_payload(wd(1, t(2026, 8, 11, 9, 0), 50.0), TID, SID),
                R.withdrawal_payload(wd(2, t(2026, 8, 11, 10, 0), 100.0), TID, SID)]
    client = _FakeSupa(cloud_perids=[])
    A.upload(client, CFG, _result_with(payloads))
    assert ("withdrawals", 2, "tenant_id,source_id,perid") in client.upserts


def test_upload_with_no_withdrawals_skips_the_table():
    client = _FakeSupa(cloud_perids=[])
    A.upload(client, CFG, _result_with([]))
    assert not any(t == "withdrawals" for t, _n, _c in client.upserts)


def test_mirror_deletes_pos_rows_missing_from_the_latest_snapshot():
    # cloud has 1,2,3 but the latest pull saw 1,2 → 3 was deleted on the POS
    payloads = [R.withdrawal_payload(wd(1, t(2026, 8, 11, 9, 0), 50.0), TID, SID),
                R.withdrawal_payload(wd(2, t(2026, 8, 11, 10, 0), 100.0), TID, SID)]
    client = _FakeSupa(cloud_perids=[1, 2, 3])
    A._mirror_withdrawal_deletions(client, CFG, _result_with(payloads))
    assert len(client.deletes) == 1
    table, filters = client.deletes[0]
    assert table == "withdrawals"
    assert filters["perid"] == "in.(3)"


def test_mirror_deletes_nothing_when_snapshot_matches_cloud():
    payloads = [R.withdrawal_payload(wd(1, t(2026, 8, 11, 9, 0), 50.0), TID, SID)]
    client = _FakeSupa(cloud_perids=[1])
    A._mirror_withdrawal_deletions(client, CFG, _result_with(payloads))
    assert client.deletes == []


def test_mirror_is_idempotent_across_cycles():
    payloads = [R.withdrawal_payload(wd(1, t(2026, 8, 11, 9, 0), 50.0), TID, SID)]
    client = _FakeSupa(cloud_perids=[1])
    A._mirror_withdrawal_deletions(client, CFG, _result_with(payloads))
    A._mirror_withdrawal_deletions(client, CFG, _result_with(payloads))
    assert client.deletes == []


def test_mirror_only_deletes_stale_perids_keeps_current_ones():
    payloads = [R.withdrawal_payload(wd(1, t(2026, 8, 11, 9, 0), 50.0), TID, SID),
                R.withdrawal_payload(wd(4, t(2026, 8, 11, 12, 0), 30.0), TID, SID)]
    client = _FakeSupa(cloud_perids=[1, 2, 3, 4])
    A._mirror_withdrawal_deletions(client, CFG, _result_with(payloads))
    assert len(client.deletes) == 1
    assert client.deletes[0][1]["perid"] == "in.(2,3)"


def test_mirror_self_heals_a_row_edited_within_the_dirty_read_window():
    """حارس الـdirty-read بيستثني الصفوف الأحدث من 30 ثانية من اللقطة —
    لو الصف كان موجود على السحاب قبل كده، المرآة هتمسحه مؤقتاً والدورة
    الجاية هترفعه تاني بالقيمة الصح (self-healing خلال دورة واحدة)."""
    # الدورة الأولى: الصف edited (مش في اللقطة) → المرآة بتمسحه من السحاب
    client = _FakeSupa(cloud_perids=[5])
    A._mirror_withdrawal_deletions(client, CFG, _result_with([]))
    assert len(client.deletes) == 1
    assert client.deletes[0][1]["perid"] == "in.(5)"
    # الدورة الجاية: الصف بقى أقدم من 30 ثانية → رجع للقطة → بيترفع تاني
    payloads = [R.withdrawal_payload(wd(5, t(2026, 8, 11, 9, 0), 50.0), TID, SID)]
    client2 = _FakeSupa(cloud_perids=[])
    A.upload(client2, CFG, _result_with(payloads))
    assert ("withdrawals", 1, "tenant_id,source_id,perid") in client2.upserts


# ════════════════════════════════════════════════════════════════
# 7) pertype — no evidence-backed exclusion exists
# ════════════════════════════════════════════════════════════════

def test_pertype_zero_and_one_are_both_summed():
    # forensic report: pertype=0 → 116 rows, pertype=1 → 1 row. The owner
    # confirmed SUM(Personal.peramount) with no filter — so without
    # application-logic evidence for excluding pertype=1, we sum all rows.
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    rows = [wd(1, t(2026, 8, 11, 8, 0), amount=50.0, per_type=0),
            wd(2, t(2026, 8, 11, 9, 0), amount=25.0, per_type=1)]
    assert OR._sum_withdrawals(rows, start, end) == 75.0


def test_pertype_is_never_used_to_skip_a_row_in_the_adapter():
    row1 = _personal_row(1, 50.0, t(2026, 8, 11, 8, 0), pertype=0)
    row2 = _personal_row(2, 25.0, t(2026, 8, 11, 9, 0), pertype=1)
    res, _cur = _pull_with_personal([row1, row2])
    assert len(res.withdrawals) == 2


# ════════════════════════════════════════════════════════════════
# 8) live-evidence golden values (2026-08-11/12, user-confirmed)
# ════════════════════════════════════════════════════════════════

def test_live_perid3_row_lands_in_the_morning_shift():
    """Perid 3 · 50 ج · 2026-08-11 07:50:50 · peruser=2 · pertype=0."""
    row = _personal_row(3, 50.0, t(2026, 8, 11, 7, 50, 50), peruser=2,
                        perbr=0, pertype=0, note="عيش")
    w = AD._row_to_withdrawal(row)
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    assert OR._sum_withdrawals([w], start, end) == 50.0
    # وليست من صباح اليوم التالي ولا مساء 8/10
    assert M.resolve_shift(w.perdate) == (_dt.date(2026, 8, 11), "morning")


def test_documented_per_shift_withdrawal_sums():
    """Reference totals from the client forensic report (DB-derived, not
    screen-observed): 8/11 صباح 9,910 · 8/11 مساء 6,260 · 8/12 صباح 245 ·
    8/12 مساء 3,145 · 8/13 صباح 0."""
    sums = {
        ("2026-08-11", "morning"): 9910.0,
        ("2026-08-11", "evening"): 6260.0,
        ("2026-08-12", "morning"): 245.0,
        ("2026-08-12", "evening"): 3145.0,
        ("2026-08-13", "morning"): 0.0,
    }
    for (dstr, name), expected in sums.items():
        date = _dt.date.fromisoformat(dstr)
        start, end = M.shift_window(date, name)
        rows = [wd(1, start + _dt.timedelta(minutes=30), amount=expected)]
        assert OR._sum_withdrawals(rows, start, end) == expected, (dstr, name)


def test_live_morning_2026_08_11_grand_total_after_withdrawals():
    """16,305 + 3,365 − 250 − 635 = 18,785 (قبل المسحوبات)
    ثم − 9,910 مسحوبات صباح 8/11 → 8,875.
    8,875 = القيمة المتوقعة المشتقة من الداتا بعد المسحوبات المؤكدة —
    مش ادعاء إنها اتعرضت على شاشة HD Soft."""
    before = compute(sales=16305.0, collections=3365.0, returns=250.0,
                     delivery=635.0, withdrawals=0.0,
                     date=_dt.date(2026, 8, 11), name="morning")
    assert before.grand_total == 18785.0
    after = compute(sales=16305.0, collections=3365.0, returns=250.0,
                    delivery=635.0, withdrawals=9910.0,
                    date=_dt.date(2026, 8, 11), name="morning")
    assert after.grand_total == 8875.0


def test_live_2026_08_11_morning_formula_is_visible_in_report():
    """The report must carry مسحوبات in the breakdown and the formula."""
    m = compute(sales=16305.0, collections=3365.0, returns=250.0,
                delivery=635.0, withdrawals=9910.0,
                date=_dt.date(2026, 8, 11), name="morning")
    import report as R
    text = R.build_shift_report(
        m, M.Comparison(False, reason="مفيش بيانات للأسبوع اللي فات"),
        withdrawals=m.withdrawals)
    assert "مسحوبات" in text
    assert "−9,910" in text
    assert "8,875" in text
    assert ("الإجمالي = مبيعات + مقبوضات − مرتجع مبيعات − دليفري − مسحوبات"
            in text)
    E = __import__("events")
    E.assert_no_accusation(text)               # the guard must never trip


# ════════════════════════════════════════════════════════════════
# 9) nothing else changed
# ════════════════════════════════════════════════════════════════

def test_existing_verified_formula_unchanged_with_zero_withdrawals():
    # the golden daybook check: 19,205 = sales 17,580 + collections 2,150
    # − returns 0 − delivery 445 − withdrawals 0
    m = compute(sales=17580.0, collections=2150.0, returns=0.0, delivery=445.0,
                withdrawals=0.0, date=_dt.date(2026, 8, 12), name="morning")
    assert m.grand_total == 19285.0


def test_withdrawals_never_leak_into_sales_or_collections():
    m = compute(sales=1000.0, collections=100.0, withdrawals=250.0)
    assert m.sales == 1000.0
    assert m.collections == 100.0
    assert m.returns == 0.0 and m.delivery == 0.0


def test_zero_withdrawals_renders_a_zero_line():
    import report as R
    m = compute(sales=1000.0, collections=0.0, withdrawals=0.0)
    text = R.build_shift_report(
        m, M.Comparison(False, reason="مفيش بيانات للأسبوع اللي فات"),
        withdrawals=0.0)
    assert "مسحوبات         −0 ج" in text


# ════════════════════════════════════════════════════════════════
# 10) _group_withdrawals_by_user — display metadata only (2026-08-22)
# ════════════════════════════════════════════════════════════════

def test_two_users_grouped_with_correct_totals():
    # Test 1 — حمص 1000، محمود 400 → two entries, metadata total 1400
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    rows = [
        wd(1, t(2026, 8, 11, 7, 30), amount=1000.0, user=2),   # حمص
        wd(2, t(2026, 8, 11, 8, 0), amount=400.0, user=1),     # محمود
    ]
    users = OR._group_withdrawals_by_user(rows, start, end, {2: "حمص", 1: "محمود"})
    assert len(users) == 2
    assert sum(u["amount"] for u in users) == 1400.0
    # doesn't touch the authoritative financial sum
    assert OR._sum_withdrawals(rows, start, end) == 1400.0


def test_same_user_multiple_withdrawals_are_counted_and_summed():
    # Test 2 — حمص سحب 3 مرات: count=3, amount=1000
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    rows = [
        wd(1, t(2026, 8, 11, 7, 10), amount=500.0, user=2),
        wd(2, t(2026, 8, 11, 8, 10), amount=300.0, user=2),
        wd(3, t(2026, 8, 11, 9, 10), amount=200.0, user=2),
    ]
    users = OR._group_withdrawals_by_user(rows, start, end, {2: "حمص"})
    assert len(users) == 1
    assert users[0]["count"] == 3
    assert users[0]["amount"] == 1000.0


def test_grouping_key_is_uid_not_display_name():
    # Test 3 — two different uids that happen to resolve to different names
    # must stay two separate groups; a uid absent from the map never merges
    # into a uid that IS present just because both display as text.
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    rows = [
        wd(1, t(2026, 8, 11, 7, 10), amount=50.0, user=2),
        wd(2, t(2026, 8, 11, 8, 10), amount=75.0, user=5),   # uid 5: not in the map
    ]
    users = OR._group_withdrawals_by_user(rows, start, end, {2: "حمص"})
    assert {u["uid"] for u in users} == {2, 5}
    deleted = next(u for u in users if u["uid"] == 5)
    assert deleted["name"] == "مستخدم محذوف #5"     # not fabricated, not merged


def test_withdrawal_outside_window_excluded_from_breakdown():
    # Test 4 — same exclusion _sum_withdrawals already proves, for the
    # per-user breakdown too.
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    inside = wd(1, t(2026, 8, 11, 7, 0), amount=5.0, user=2)
    outside = wd(2, t(2026, 8, 11, 19, 1), amount=99.0, user=2)
    users = OR._group_withdrawals_by_user([inside, outside], start, end, {2: "حمص"})
    assert len(users) == 1
    assert users[0]["amount"] == 5.0


def test_breakdown_boundary_matches_sum_withdrawals_exactly():
    # Test 5 — perdate == start included, perdate == end excluded (same
    # boundary _sum_withdrawals uses — proven identical here).
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    at_start = wd(1, start, amount=10.0, user=2)
    at_end = wd(2, end, amount=20.0, user=2)
    users = OR._group_withdrawals_by_user([at_start, at_end], start, end, {2: "حمص"})
    assert len(users) == 1
    assert users[0]["amount"] == 10.0
    assert OR._sum_withdrawals([at_start, at_end], start, end) == 10.0


def test_withdrawal_user_differs_from_shift_owner():
    # Test 6 — the exact CONTEXT.md §8 scenario: evening shift owned by
    # محمود, a withdrawal at 19:10 recorded by حمص. Financial attribution
    # stays timestamp-based (evening); user attribution shows حمص, never
    # silently reassigned to the shift's primary user.
    start, end = M.shift_window(_dt.date(2026, 8, 11), "evening")
    w = wd(1, t(2026, 8, 11, 19, 10), amount=1400.0, user=2)   # حمص, uid 2
    assert start <= w.perdate < end                            # financially: evening shift
    users = OR._group_withdrawals_by_user([w], start, end, {2: "حمص", 1: "محمود"})
    assert len(users) == 1
    assert users[0]["name"] == "حمص"
    assert users[0]["uid"] == 2
    # never silently reassigned to محمود (uid 1), the shift's usual owner
    assert users[0]["name"] != "محمود"


def test_missing_user_uid_is_not_fabricated():
    # Test 7 — peruser NULL → "غير محدد", never a guessed name.
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    w = wd(1, t(2026, 8, 11, 7, 30), amount=60.0, user=None)
    users = OR._group_withdrawals_by_user([w], start, end, {2: "حمص"})
    assert len(users) == 1
    assert users[0]["uid"] is None
    assert users[0]["name"] == "غير محدد"


def test_grouping_never_changes_the_authoritative_financial_total():
    # Test 8 — the financial invariant: _sum_withdrawals on the same rows
    # returns exactly the same value whether or not the breakdown is ever
    # computed, before and after this feature.
    start, end = M.shift_window(_dt.date(2026, 8, 11), "evening")
    rows = [
        wd(1, t(2026, 8, 11, 19, 10), amount=1000.0, user=2),
        wd(2, t(2026, 8, 11, 20, 0), amount=400.0, user=1),
    ]
    before = OR._sum_withdrawals(rows, start, end)
    OR._group_withdrawals_by_user(rows, start, end, {2: "حمص", 1: "محمود"})   # side-effect-free
    after = OR._sum_withdrawals(rows, start, end)
    assert before == after == 1400.0
    m = M.compute_shift(_dt.date(2026, 8, 11), "evening", [], withdrawals=after)
    assert m.grand_total == -1400.0                # sales/collections/etc. all 0 here


def test_deterministic_ordering_amount_desc_name_asc_uid_asc():
    start, end = M.shift_window(_dt.date(2026, 8, 11), "morning")
    rows = [
        wd(1, t(2026, 8, 11, 7, 10), amount=100.0, user=3),
        wd(2, t(2026, 8, 11, 7, 20), amount=300.0, user=1),
        wd(3, t(2026, 8, 11, 7, 30), amount=300.0, user=2),   # tie with uid=1 on amount
    ]
    names = {1: "ب", 2: "أ", 3: "ج"}
    users = OR._group_withdrawals_by_user(rows, start, end, names)
    # amount DESC first; the 300/300 tie breaks by name ASC ("أ" before "ب")
    assert [u["amount"] for u in users] == [300.0, 300.0, 100.0]
    assert [u["uid"] for u in users] == [2, 1, 3]
    # deterministic across repeated calls on the same input
    assert OR._group_withdrawals_by_user(rows, start, end, names) == users


def test_report_shows_breakdown_only_for_more_than_one_user():
    # Test 9 (Telegram) — single-user shifts stay simple (no redundant
    # section); multi-user shifts show the breakdown; the aggregate total
    # line is unaffected either way; no accusatory wording is introduced
    # (assert_no_accusation runs inside build_shift_report itself).
    import report as R
    m = compute(sales=1000.0, collections=0.0, withdrawals=1400.0)
    comparison = M.Comparison(False, reason="مفيش بيانات للأسبوع اللي فات")

    single = R.build_shift_report(m, comparison, withdrawals=1400.0,
                                  withdrawal_users=[{"uid": 2, "name": "حمص",
                                                     "count": 1, "amount": 1400.0}])
    assert "💸 المسحوبات" not in single
    assert "مسحوبات         −1,400 ج" in single

    multi = R.build_shift_report(m, comparison, withdrawals=1400.0,
                                 withdrawal_users=[
                                     {"uid": 2, "name": "حمص", "count": 3, "amount": 1000.0},
                                     {"uid": 1, "name": "محمود", "count": 1, "amount": 400.0},
                                 ])
    assert "💸 المسحوبات" in multi
    assert "حمص — 1,000 ج" in multi
    assert "محمود — 400 ج" in multi
    # the aggregate line is the SAME value regardless of the breakdown above it
    assert "مسحوبات         −1,400 ج" in multi

    none_ = R.build_shift_report(m, comparison, withdrawals=1400.0, withdrawal_users=[])
    assert "💸 المسحوبات" not in none_
    assert "مسحوبات         −1,400 ج" in none_
