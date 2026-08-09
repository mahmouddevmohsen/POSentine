# -*- coding: utf-8 -*-
"""
adapter_hdsoft.py — محوّل HD Soft (قراءة فقط)
================================================================
⚠️ ممنوع تعديل الملف ده من غير مراجعة. كل سطر فيه قرار اتاخد بعد
   تحليل داتا فعلية، والغلط فيه بيبقى **صامت** — الكود يشتغل والأرقام تطلع غلط.

قواعد غير قابلة للنقاش:
  1. SELECT فقط + WITH (NOLOCK). ولا أمر كتابة واحد.
  2. الربط: SalesDe.saleitcode → Items.Itid   (مش itcode — الغلط بيفقد 21%)
  3. saltot = مجموع السطور + saldelcost      (متحقق 155/155)
  4. SalesDe.saleprice = إجمالي السطر        (مش سعر الوحدة)
  5. تصنيف الفاتورة: المرتجع أولاً، بعدين salT
  6. الاستيعاب بـ salid فقط (مش saldate) — salid فيه قفزات 9,999 وده عادي
  7. إعادة المسح محدودة بـ salid (مفيش index على saldate)
  8. تجاهل الفواتير الأحدث من 30 ثانية (حماية من dirty read)
  9. ممنوع أي join بين Sales و SalesDe من غير شرط — cross join = تعليق السيرفر
================================================================
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

# pyodbc محتاجه الأجينت بس (على جهاز الكاشير).
# GitHub Actions بتشغّل الحساب والتقارير ومفيهاش ODBC drivers —
# فالاستيراد اختياري عشان الاختبارات والتقارير تشتغل من غيره.
try:
    import pyodbc
    _DbError: type[BaseException] = pyodbc.Error
except ImportError:                                   # pragma: no cover
    pyodbc = None                                     # type: ignore[assignment]
    _DbError = Exception

AGENT_VERSION = "1.0.0"

# الفواتير الأحدث من كده بنسيبها للدورة الجاية — NOLOCK ممكن يقرا كتابة لسه ماتمّتش
DIRTY_READ_GUARD_SECONDS = 30

# نافذة إعادة المسح لالتقاط التعديل والحذف
RESCAN_HOURS = 24

_PREFERRED_DRIVERS = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 11 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)

# الأعمدة اللي بنعتمد عليها — لو أي واحد اختفى بعد تحديث HD Soft نوقف بوضوح
_REQUIRED = {
    "Sales": ("salid", "salreceiptnum", "saldate", "saltot", "saldelcost",
              "saluser", "salcustcode", "salT", "saltype", "saleRtype"),
    "SalesDe": ("saledeid", "saleid", "saledate", "saleitcode",
                "salequan", "saleprice"),
    "Items": ("Itid", "itcode", "itname", "itsaleprice", "itmcat", "itscat"),
    "Users": ("UID", "Uname"),
    "safeR": ("SRID", "SrUID", "SrDate", "SrUserval", "SrAppVal", "SrDiffVal"),
}


class AdapterError(RuntimeError):
    """خطأ يوقف الدورة ويتسجل كـ internal anomaly."""


class RestoreSuspected(AdapterError):
    """MAX(salid) أقل من الـwatermark → على الأغلب اتعمل استرجاع نسخة احتياطية."""


class SchemaDrift(AdapterError):
    """عمود متوقع مش موجود → HD Soft اتحدّث."""


# ════════════════════════════════════════════════════════════════
# نماذج البيانات
# ════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class Invoice:
    salid: int
    receipt_num: int | None
    sold_at: _dt.datetime          # وقت الـPOS المحلي — ممنوع تحويله
    total: float
    delivery_cost: float
    user_uid: int | None
    cust_code: int | None
    sal_t: int | None
    sal_type: int | None
    sale_rtype: int | None
    kind: str                      # cash | external | return | other


@dataclass(slots=True)
class InvoiceLine:
    saledeid: int
    salid: int
    itid: int | None
    qty: float
    line_total: float              # saleprice = إجمالي السطر
    sold_at: _dt.datetime | None


@dataclass(slots=True)
class CashCount:
    srid: int
    counted_at: _dt.datetime
    user_uid: int | None
    user_value: float
    app_value: float
    diff_value: float
    pc_name: str | None
    kind: str                      # no_count | real_count


@dataclass(slots=True)
class PullResult:
    """مخرج دورة واحدة. الأجينت بيرفعها زي ما هي."""
    new_invoices: list[Invoice] = field(default_factory=list)
    new_lines: list[InvoiceLine] = field(default_factory=list)
    rescanned_invoices: list[Invoice] = field(default_factory=list)
    rescanned_lines: list[InvoiceLine] = field(default_factory=list)
    # كل الـsalid الموجودة في نافذة إعادة المسح — الغياب عنها = حذف
    window_salids: list[int] = field(default_factory=list)
    window_from_salid: int = 0
    cash_counts: list[CashCount] = field(default_factory=list)
    products: list[dict[str, Any]] = field(default_factory=list)
    users: list[dict[str, Any]] = field(default_factory=list)
    date_change_rows: int = 0
    db_size_mb: float | None = None
    pos_clock: _dt.datetime | None = None
    max_salid: int = 0
    max_saledeid: int = 0
    unknown_sal_t: list[int] = field(default_factory=list)


# ════════════════════════════════════════════════════════════════
# التصنيف — الترتيب إلزامي
# ════════════════════════════════════════════════════════════════

def classify_invoice(sal_t, sal_type, sale_rtype) -> tuple[str, int | None]:
    """
    بيرجّع (kind, unknown_salT_or_None).

    ⚠️ المرتجع لازم يتفحص **قبل** salT.
       في الداتا فاتورة salT=2 و saleRtype=1 (مرتجع دليفري) —
       لو فحصنا salT الأول هتتصنّف غلط.

    ⚠️ الـelse مش اختياري: فيه 204 فاتورة salT=3 في التاريخ ومعرفناش هي إيه.
       تتصنّف 'other' وتتسجل عندنا — ممنوع تسقط بصمت.
    """
    if sale_rtype == 1 or sal_type == 1:
        return "return", None
    if sal_t == 1:
        return "cash", None
    if sal_t == 2:
        return "external", None
    return "other", sal_t


def classify_cash_count(user_value: float) -> str:
    """
    🚨 أخطر قاعدة في النظام.

    17 صف من 21 في الداتا فيهم SrUserval = 0 — ودي معناها
    **الكاشير مدخلش رقم**، مش إن الخزينة فاضية.

    من غير الشرط ده، أول تنبيه هيقول "عجز 15,758 ج" وهو غير صحيح،
    ومعاه اتهام ضمني لموظف باسمه.
    """
    if not user_value:                       # 0 أو None
        return "no_count"
    return "real_count"


# ════════════════════════════════════════════════════════════════
# الاتصال
# ════════════════════════════════════════════════════════════════

def pick_driver() -> str:
    if pyodbc is None:
        raise AdapterError("pyodbc مش متسطّب — مطلوب على جهاز الأجينت فقط")
    available = set(pyodbc.drivers())
    for d in _PREFERRED_DRIVERS:
        if d in available:
            return d
    raise AdapterError(f"مفيش ODBC driver مناسب. المتاح: {sorted(available)}")


def connect(server: str, database: str, user: str, password: str,
            timeout: int = 15):
    driver = pick_driver()
    extra = "TrustServerCertificate=yes;" if "18" in driver else ""
    cn = pyodbc.connect(
        f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
        f"UID={user};PWD={password};{extra}",
        timeout=timeout, readonly=True,
    )
    cn.autocommit = True
    return cn


def verify_schema(cn) -> None:
    """فحص إقلاع — لو HD Soft اتحدّث وغيّر عمود، نوقف بوضوح بدل ما نطلّع أرقام غلط."""
    cur = cn.cursor()
    missing: list[str] = []
    for table, cols in _REQUIRED.items():
        cur.execute(
            "SELECT c.name FROM sys.columns c "
            "JOIN sys.tables t ON t.object_id = c.object_id WHERE t.name = ?",
            table,
        )
        present = {r[0].lower() for r in cur.fetchall()}
        if not present:
            missing.append(f"{table} (الجدول نفسه مش موجود)")
            continue
        for col in cols:
            if col.lower() not in present:
                missing.append(f"{table}.{col}")
    if missing:
        raise SchemaDrift("أعمدة ناقصة بعد تحديث محتمل لـHD Soft: " + ", ".join(missing))


# ════════════════════════════════════════════════════════════════
# استعلامات مساعدة
# ════════════════════════════════════════════════════════════════

_INV_COLS = """
    s.salid, s.salreceiptnum, s.saldate, s.saltot,
    ISNULL(s.saldelcost, 0) AS saldelcost,
    s.saluser, s.salcustcode, s.salT, s.saltype, s.saleRtype
"""

_LINE_COLS = """
    d.saledeid, d.saleid, d.saleitcode, d.salequan, d.saleprice, d.saledate
"""


def _row_to_invoice(r) -> tuple[Invoice, int | None]:
    kind, unknown = classify_invoice(r.salT, r.saltype, r.saleRtype)
    return Invoice(
        salid=int(r.salid),
        receipt_num=int(r.salreceiptnum) if r.salreceiptnum is not None else None,
        sold_at=r.saldate,
        total=float(r.saltot or 0),
        delivery_cost=float(r.saldelcost or 0),
        user_uid=int(r.saluser) if r.saluser is not None else None,
        cust_code=int(r.salcustcode) if r.salcustcode is not None else None,
        sal_t=r.salT, sal_type=r.saltype, sale_rtype=r.saleRtype,
        kind=kind,
    ), unknown


def _row_to_line(r) -> InvoiceLine:
    return InvoiceLine(
        saledeid=int(r.saledeid),
        salid=int(r.saleid),
        itid=int(r.saleitcode) if r.saleitcode is not None else None,
        qty=float(r.salequan or 0),
        line_total=float(r.saleprice or 0),   # إجمالي السطر
        sold_at=r.saledate,
    )


def _fetch_lines_for(cur, salids: list[int]) -> list[InvoiceLine]:
    """سحب السطور على دفعات — SQL Server حده 2100 parameter."""
    out: list[InvoiceLine] = []
    CHUNK = 900
    for i in range(0, len(salids), CHUNK):
        part = salids[i:i + CHUNK]
        marks = ",".join("?" * len(part))
        cur.execute(
            f"SELECT {_LINE_COLS} FROM dbo.SalesDe d WITH (NOLOCK) "
            f"WHERE d.saleid IN ({marks})",
            *part,
        )
        out.extend(_row_to_line(r) for r in cur.fetchall())
    return out


# ════════════════════════════════════════════════════════════════
# الدورة الرئيسية
# ════════════════════════════════════════════════════════════════

def pull(cn,
         watermark_salid: int,
         rescan_from_salid: int,
         do_rescan: bool,
         do_reference: bool) -> PullResult:
    """
    دورة قراءة واحدة.

    watermark_salid   : آخر salid اتسحب بنجاح
    rescan_from_salid : بداية نافذة الـ24 ساعة (بالـsalid مش بالتاريخ)
    do_rescan         : كل 15 دقيقة تقريباً
    do_reference      : لقطة الأصناف والمستخدمين (كل ساعة يكفي)
    """
    cur = cn.cursor()
    res = PullResult()

    # ── ساعة الـPOS ────────────────────────────────────────────
    cur.execute("SELECT GETDATE()")
    res.pos_clock = cur.fetchone()[0]

    # ⚠️ استعلامان منفصلان — ممنوع جمعهم في واحد.
    #    FROM Sales, SalesDe = cross join لـ 218 ألف × 481 ألف صف = تعليق السيرفر.
    cur.execute("SELECT ISNULL(MAX(salid), 0) FROM dbo.Sales WITH (NOLOCK)")
    res.max_salid = int(cur.fetchone()[0])
    cur.execute("SELECT ISNULL(MAX(saledeid), 0) FROM dbo.SalesDe WITH (NOLOCK)")
    res.max_saledeid = int(cur.fetchone()[0])

    # 🔴 حماية الاسترجاع — لو رجعوا نسخة احتياطية، salid بيرجع لورا
    #    والشرط salid > watermark مش هيرجّع ولا صف أبداً، والتقارير تبقى أصفار
    #    من غير أي رسالة خطأ. لازم نوقف ونستنى تدخل يدوي.
    if watermark_salid and res.max_salid < watermark_salid:
        raise RestoreSuspected(
            f"MAX(salid)={res.max_salid} أقل من الـwatermark={watermark_salid}. "
            "على الأغلب اتعمل استرجاع نسخة احتياطية. المزامنة وقفت — تدخل يدوي مطلوب."
        )

    # ── الفواتير الجديدة ──────────────────────────────────────
    # حارس الـdirty read: NOLOCK ممكن يقرا صف لسه بيتكتب وبعدين يترجع،
    # وده بيولّد تنبيه "فاتورة اتمسحت" وهي معملتش أصلاً.
    cur.execute(
        f"SELECT {_INV_COLS} FROM dbo.Sales s WITH (NOLOCK) "
        f"WHERE s.salid > ? AND s.saldate < DATEADD(second, -?, GETDATE()) "
        f"ORDER BY s.salid",
        watermark_salid, DIRTY_READ_GUARD_SECONDS,
    )
    for r in cur.fetchall():
        inv, unknown = _row_to_invoice(r)
        res.new_invoices.append(inv)
        if unknown is not None:
            res.unknown_sal_t.append(unknown)

    if res.new_invoices:
        res.new_lines = _fetch_lines_for(cur, [i.salid for i in res.new_invoices])

    # ── إعادة المسح: التعديلات والحذف ─────────────────────────
    if do_rescan:
        # ⚠️ محدودة بـ salid مش saldate.
        #    مفيش index على saldate → المسح الزمني = 218 ألف صف / 117 ميجا كل مرة.
        #    salid هو الـclustered PK → بحث نطاق على ~1,500 صف.
        window_from = _resolve_rescan_anchor(cur, rescan_from_salid)
        res.window_from_salid = window_from

        cur.execute(
            f"SELECT {_INV_COLS} FROM dbo.Sales s WITH (NOLOCK) "
            f"WHERE s.salid >= ? AND s.saldate < DATEADD(second, -?, GETDATE()) "
            f"ORDER BY s.salid",
            window_from, DIRTY_READ_GUARD_SECONDS,
        )
        for r in cur.fetchall():
            inv, unknown = _row_to_invoice(r)
            res.rescanned_invoices.append(inv)
            if unknown is not None:
                res.unknown_sal_t.append(unknown)

        res.window_salids = [i.salid for i in res.rescanned_invoices]
        if res.window_salids:
            res.rescanned_lines = _fetch_lines_for(cur, res.window_salids)

    # ── الخزينة ───────────────────────────────────────────────
    cur.execute(
        "SELECT SRID, SrUID, SrDate, SrUserval, SrAppVal, SrDiffVal, SrPcName "
        "FROM dbo.safeR WITH (NOLOCK) ORDER BY SRID"
    )
    for r in cur.fetchall():
        uv = float(r.SrUserval or 0)
        res.cash_counts.append(CashCount(
            srid=int(r.SRID),
            counted_at=r.SrDate,
            user_uid=int(r.SrUID) if r.SrUID is not None else None,
            user_value=uv,
            app_value=float(r.SrAppVal or 0),
            diff_value=float(r.SrDiffVal or 0),
            pc_name=r.SrPcName,
            kind=classify_cash_count(uv),
        ))

    # ── تغيير تاريخ الجهاز — لو ظهر صف، كل حسابات الورديات تبوظ ─
    try:
        cur.execute("SELECT COUNT(*) FROM dbo.DateChangeLog WITH (NOLOCK)")
        res.date_change_rows = int(cur.fetchone()[0])
    except _DbError:
        res.date_change_rows = 0

    # ── لقطة مرجعية ───────────────────────────────────────────
    if do_reference:
        cur.execute(
            "SELECT Itid, itcode, itname, itsaleprice, itmcat, itscat "
            "FROM dbo.Items WITH (NOLOCK)"
        )
        res.products = [{
            "itid": int(r.Itid),
            "itcode": (r.itcode or "").strip() or None,
            "itname": (r.itname or "").strip() or None,
            "list_price": float(r.itsaleprice or 0),
            "main_cat": r.itmcat,
            "sub_cat": r.itscat,
        } for r in cur.fetchall()]

        cur.execute("SELECT UID, Uname FROM dbo.Users WITH (NOLOCK)")
        res.users = [{
            "uid": int(r.UID),
            "name": (r.Uname or "").strip() or None,
        } for r in cur.fetchall()]

        try:
            cur.execute(
                "SELECT CAST(SUM(size) * 8.0 / 1024 AS decimal(12,1)) "
                "FROM sys.database_files"
            )
            res.db_size_mb = float(cur.fetchone()[0] or 0)
        except _DbError:
            res.db_size_mb = None

    return res


def _resolve_rescan_anchor(cur, previous_anchor: int) -> int:
    """
    أصغر salid وقع في آخر 24 ساعة.

    بنحسبه مرة واحدة كل دورة إعادة مسح ونخزّنه، عشان الاستعلام الأساسي
    يفضل بحث نطاق على الـclustered index بدل مسح زمني كامل.

    الاستعلام ده لوحده بيمسح، بس بيتنفّذ كل 15 دقيقة مش كل 3.
    """
    cur.execute(
        "SELECT ISNULL(MIN(salid), 0) FROM dbo.Sales WITH (NOLOCK) "
        "WHERE saldate >= DATEADD(hour, -?, GETDATE())",
        RESCAN_HOURS,
    )
    anchor = int(cur.fetchone()[0] or 0)
    # لو رجع صفر (مفيش مبيعات 24 ساعة) نفضل على آخر مرساة معروفة
    return anchor if anchor else previous_anchor


# ════════════════════════════════════════════════════════════════
# فحص انحراف الساعة
# ════════════════════════════════════════════════════════════════

def clock_drift_seconds(pos_clock: _dt.datetime | None,
                        our_local_now: _dt.datetime) -> int | None:
    """
    كل حسابات الورديات معتمدة على ساعة جهازهم.
    لو اتغيّرت، الأرقام تبوظ من غير أي رسالة خطأ.
    الانحراف > 5 دقايق → إنذار **لينا** مش للمالك.
    """
    if pos_clock is None:
        return None
    return int((pos_clock - our_local_now).total_seconds())
