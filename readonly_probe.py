# -*- coding: utf-8 -*-
"""
readonly_probe.py — prove the POS refuses us, do not assert it
================================================================
The same move that proved tenant isolation: we did not argue that RLS
worked, we attempted a foreign insert and got `42501`. This does that to
the customer's SQL Server.

Before anything else in an install, this **attempts to write** to the POS
database with the agent's own credentials and requires every attempt to be
refused. If any is permitted, the install aborts: those credentials are
wrong and nothing should run until they are fixed. It runs on every
install, not once — permissions drift, and someone helpful "fixes" a login.

⚠️ This is the ONE module in the repository allowed to contain write SQL.
   `test_readonly.py` enforces that, and enforces that every statement in
   here is zero-row by construction.

---- two kinds of probe, and why ---------------------------------

**Attempted** (WRITE_PROBES): UPDATE, DELETE and INSERT, each carrying
`WHERE 1 = 0`. SQL Server checks permissions when it compiles a statement,
before it touches a row, so a denied one raises and a permitted one is a
no-op. The `WHERE 1 = 0` means we are not relying on that: even if the
check happened late, zero rows qualify. Two independent reasons the probe
is safe, which is the right number for a statement we are pointing at a
live restaurant's sales table.

**Interrogated** (PERMISSION_CHECKS): TRUNCATE, ALTER, CREATE TABLE,
BACKUP, CONTROL. These are asked about, never attempted, and the reason
is not caution — it is that there is no harmless version of them:

  * `TRUNCATE TABLE dbo.Sales` takes no WHERE clause. A probe that is
    wrongly permitted empties the customer's sales history. There is no
    world in which we run that on site.
  * `ALTER TABLE dbo.Sales ADD ...` permanently changes their live table.
  * Wrapping either in a transaction and rolling back would take a
    schema-modification lock on `dbo.Sales` during service, which blocks
    the POS itself. Refused.

`HAS_PERMS_BY_NAME` answers the same question exactly — it accounts for
DENY, role membership and ownership — and it is a SELECT. TRUNCATE
requires ALTER on the table, so "can this login ALTER dbo.Sales" *is*
"can this login TRUNCATE dbo.Sales", with no risk attached.

Both kinds land in the install transcript and in the diagnostics zip.
That transcript is what we show the customer.
================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Verdicts. Only REFUSED is a pass; nothing else is.
REFUSED = "REFUSED"
PERMITTED = "PERMITTED"
INCONCLUSIVE = "INCONCLUSIVE"
# Our probe was malformed for this schema. Blocks the install like any other
# non-REFUSED result, but it is OUR fault and the report must say so — telling
# a customer their credentials are unsafe when our SQL was wrong is its own
# kind of wrong answer.
PROBE_DEFECT = "PROBE DEFECT"
NO_PROBEABLE_COLUMN = "NO PROBEABLE COLUMN"

# SQL Server errors that mean "you are not allowed to do that".
#   229  The <perm> permission was denied on object ...
#   230  The <perm> permission was denied on column ...
#   262  <perm> permission denied in database ...
#   300  VIEW SERVER STATE permission was denied ...
PERMISSION_DENIED_NATIVE = frozenset({229, 230, 262, 300})

# 🔴 Errors that mean "this statement is impossible", NOT "you lack permission".
#
# This is the defect that produced a false NOT READ-ONLY verdict on the
# customer's machine and blocked a correct install. The three UPDATE probes
# targeted the primary keys — salid, saledeid, Itid — and all three are
# IDENTITY columns. SQL Server refuses to update an identity column whatever
# permissions you hold, so the probe could not tell "denied" from
# "impossible", returned INCONCLUSIVE, and the aggregate failed closed.
#
# Failing closed was right. The probe was wrong. These are named so that a
# structural refusal can never again be mistaken for either a permission
# refusal OR evidence that writing is allowed.
STRUCTURAL_NATIVE: dict[int, str] = {
    8102: "cannot update an IDENTITY column - SQL Server refuses this whatever "
          "permissions the login holds, so it proves nothing either way",
    544:  "cannot insert an explicit value into an IDENTITY column while "
          "IDENTITY_INSERT is OFF - refused whatever permissions are held",
    271:  "cannot update a COMPUTED column - refused whatever permissions "
          "are held",
    272:  "cannot update a TIMESTAMP/ROWVERSION column - refused whatever "
          "permissions are held",
}

POS_TABLES = ("dbo.Sales", "dbo.SalesDe", "dbo.Items")


@dataclass(frozen=True)
class Probe:
    name: str
    sql: str
    permitted_means: str


@dataclass
class ProbeResult:
    probe: Probe
    verdict: str
    sqlstate: str | None = None
    native: int | None = None
    message: str = ""
    # Why this was not a clean REFUSED. Mandatory for every verdict other
    # than REFUSED: the customer bundle that exposed the identity defect
    # said only "INCONCLUSIVE" with a truncated message, and the one fact
    # that would have settled it in seconds — the error number — had been
    # cut off the end of the line.
    reason: str = ""


@dataclass
class PermissionAnswer:
    name: str
    securable: str
    permission: str
    held: bool | None          # None = the server would not say
    permitted_means: str


@dataclass
class Report:
    identity: dict[str, Any] = field(default_factory=dict)
    writes: list[ProbeResult] = field(default_factory=list)
    permissions: list[PermissionAnswer] = field(default_factory=list)
    effective: list[str] = field(default_factory=list)
    guard_wired: bool = False
    failure: str | None = None
    # Which column each write probe targeted, discovered at runtime. On the
    # report so the next person can see what was actually written to without
    # reading the SQL.
    probe_columns: dict[str, str | None] = field(default_factory=dict)
    column_errors: dict[str, str] = field(default_factory=dict)

    # Roles that make every other check on this page irrelevant.
    #   sysadmin  — SQL Server skips permission checks entirely, so the
    #               DENY is never evaluated.
    #   db_owner  — can remove the DENY itself.
    # The others are not that absolute, but no read-only login should hold
    # any of them, and finding one means the login is not what we think.
    DANGEROUS_ROLES = ("is_sysadmin", "is_db_owner", "is_ddladmin",
                       "is_datawriter")

    @property
    def dangerous_roles(self) -> list[str]:
        return [role for role in self.DANGEROUS_ROLES
                if self.identity.get(role)]

    @property
    def permitted(self) -> list[str]:
        """Everything that was allowed and must not have been."""
        names = [r.probe.name for r in self.writes if r.verdict == PERMITTED]
        names += [p.name for p in self.permissions if p.held is True]
        names += [f"member of {role.removeprefix('is_')}"
                  for role in self.dangerous_roles]
        return names

    @property
    def inconclusive(self) -> list[str]:
        names = [r.probe.name for r in self.writes if r.verdict == INCONCLUSIVE]
        names += [p.name for p in self.permissions if p.held is None]
        return names

    @property
    def probe_defects(self) -> list[str]:
        """Probes that could not run correctly. Our bug, not their login.

        Kept apart from `inconclusive` and `permitted` on purpose. All three
        block the install, but only one of them justifies telling a customer
        their credentials are unsafe — and saying that when our own SQL was
        malformed is a wrong answer delivered with confidence, which is the
        exact failure this product exists to prevent.
        """
        return [r.probe.name for r in self.writes
                if r.verdict in (PROBE_DEFECT, NO_PROBEABLE_COLUMN)]

    @property
    def passed(self) -> bool:
        """Every write refused, every dangerous permission answered 'no'.

        Anything other than REFUSED is a failure. "We could not tell" and
        "it is refused" must never produce the same outcome — that is the
        whole failure mode this product exists to avoid.

        🔴 This deliberately still fails closed on a probe defect. Failing
        closed was the CORRECT behaviour on the customer machine; the probe
        was wrong, not the gate. Nothing here has been relaxed — only the
        wording of the report distinguishes the causes.
        """
        return (self.failure is None
                and not self.permitted
                and not self.inconclusive
                and not self.probe_defects
                and bool(self.writes)
                and bool(self.permissions))


# ════════════════════════════════════════════════════════════════
# the probes
# ════════════════════════════════════════════════════════════════

# A column we can legally write to, chosen by asking the server rather than
# by us knowing the schema. Deliberately NOT hardcoded: hardcoding the primary
# key is exactly what produced the false verdict, and the next customer's
# schema is not this one.
#
# Excluded, because each is refused for a structural reason that would make
# the probe meaningless again:
#   is_identity   → Msg 8102 on UPDATE, Msg 544 on INSERT
#   is_computed   → Msg 271
#   rowversion    → Msg 272
#   is_rowguidcol → ROWGUIDCOL has its own restrictions
PROBEABLE_COLUMN_SQL = """
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
"""


def probeable_column(cursor, table: str) -> str | None:
    """The first column on `table` that a write probe can legally target.

    None when the table has no such column — which is a limitation of the
    probe, never evidence that writing is permitted. The caller must keep
    those two apart.
    """
    schema, _, name = table.partition(".")
    if not name:
        schema, name = "dbo", schema
    cursor.execute(PROBEABLE_COLUMN_SQL, schema, name)
    row = cursor.fetchone()
    return row[0] if row and row[0] else None


def write_probes(columns: dict[str, str | None]) -> list[Probe]:
    """Zero-row writes, one set per table. Every one must be refused.

    `columns` maps each table to the column discovered by
    `probeable_column()`. It is a required argument rather than something
    this function works out for itself, because a default would be a
    hardcoded schema and a hardcoded schema is the bug.
    """
    probes: list[Probe] = []
    for table, column in columns.items():
        if column is None:
            # Recorded as a probe, so it appears in the report and blocks the
            # install, rather than silently reducing what was tested.
            probes.append(Probe(
                name=f"UPDATE {table}",
                sql=f"(no writable column found on {table})",
                permitted_means="not established — the probe could not run"))
            probes.append(Probe(
                name=f"INSERT {table}",
                sql=f"(no writable column found on {table})",
                permitted_means="not established — the probe could not run"))
        else:
            probes.append(Probe(
                name=f"UPDATE {table}",
                # `SET <col> = <col>` needs no literal and changes nothing
                # even if a row somehow qualified.
                sql=f"UPDATE {table} SET {column} = {column} WHERE 1 = 0",
                permitted_means="this login can modify existing rows"))
            probes.append(Probe(
                name=f"INSERT {table}",
                # Sourced from the table itself, so no value is invented and
                # no column type has to be guessed. Uses the same discovered
                # column: an identity column here would raise Msg 544, which
                # is the identical trap one statement over.
                sql=(f"INSERT INTO {table} ({column}) "
                     f"SELECT {column} FROM {table} WHERE 1 = 0"),
                permitted_means="this login can add rows"))
        # DELETE names no column, so it was never affected by any of this.
        probes.append(Probe(
            name=f"DELETE {table}",
            sql=f"DELETE FROM {table} WHERE 1 = 0",
            permitted_means="this login can remove rows"))
    return probes


# Asked, never attempted. See the module docstring for why.
def permission_checks(tables: tuple[str, ...] = POS_TABLES
                      ) -> list[tuple[str, str, str, str, str]]:
    """(name, has_perms_securable_expr, class, permission, what a yes means)"""
    checks: list[tuple[str, str, str, str, str]] = []
    for table in tables:
        checks += [
            (f"ALTER {table}", f"'{table}'", "OBJECT", "ALTER",
             "ALTER on a table is what TRUNCATE requires. A yes here means "
             "this login could empty the table in one statement"),
            (f"CONTROL {table}", f"'{table}'", "OBJECT", "CONTROL",
             "CONTROL implies every other permission on the object"),
            (f"REFERENCES {table}", f"'{table}'", "OBJECT", "REFERENCES",
             "not a write, but it should not be granted either"),
        ]
    checks += [
        ("CREATE TABLE in the database", "DB_NAME()", "DATABASE",
         "CREATE TABLE",
         "any new object could be created here, and a select-into would "
         "be permitted"),
        ("ALTER the database", "DB_NAME()", "DATABASE", "ALTER",
         "schema changes anywhere in the POS database"),
        ("BACKUP the database", "DB_NAME()", "DATABASE", "BACKUP DATABASE",
         "not a write to the data, but it copies the customer's whole "
         "database somewhere"),
        ("CONTROL the server", "NULL", "SERVER", "CONTROL SERVER",
         "everything, everywhere on this SQL Server instance"),
    ]
    return checks


IDENTITY_SQL = """
SELECT SUSER_NAME()                          AS login_name,
       USER_NAME()                           AS db_user,
       DB_NAME()                             AS database_name,
       IS_SRVROLEMEMBER('sysadmin')          AS is_sysadmin,
       IS_ROLEMEMBER('db_owner')             AS is_db_owner,
       IS_ROLEMEMBER('db_ddladmin')          AS is_ddladmin,
       IS_ROLEMEMBER('db_datawriter')        AS is_datawriter,
       IS_ROLEMEMBER('db_denydatawriter')    AS is_denydatawriter,
       IS_ROLEMEMBER('db_datareader')        AS is_datareader
"""

# What the server itself says this login may do to the sales table. An
# exhaustive answer rather than a list of things we thought to ask about.
EFFECTIVE_SQL = ("SELECT permission_name FROM fn_my_permissions(?, 'OBJECT') "
                 "ORDER BY permission_name")


# ════════════════════════════════════════════════════════════════
# running them
# ════════════════════════════════════════════════════════════════

def _error_parts(exc: BaseException) -> tuple[str | None, int | None, str]:
    """pyodbc raises (sqlstate, message); the native number is inside the
    message as `(229)`. Pulled out so a permission refusal can be told
    apart from a table that does not exist."""
    args = getattr(exc, "args", ())
    sqlstate = str(args[0]) if args else None
    message = str(args[1]) if len(args) > 1 else str(exc)
    native = None
    import re
    for candidate in re.findall(r"\((\d{3,5})\)", message):
        number = int(candidate)
        if number in PERMISSION_DENIED_NATIVE:
            native = number
            break
    if native is None:
        numbers = [int(n) for n in re.findall(r"\((\d{3,5})\)", message)]
        native = numbers[0] if numbers else None
    return sqlstate, native, message


def _looks_like_permission_denied(sqlstate: str | None, native: int | None,
                                  message: str) -> bool:
    if native in PERMISSION_DENIED_NATIVE:
        return True
    low = message.lower()
    return "permission" in low and "denied" in low


def classify(sqlstate: str | None, native: int | None, message: str) -> tuple[str, str]:
    """(verdict, reason) for a refused statement.

    The order matters. A structural refusal is checked FIRST, because
    "cannot update an identity column" arrives with the same SQLSTATE
    (42000) as a permission denial and would otherwise fall through to
    INCONCLUSIVE — which is what happened on the customer's machine.
    """
    if native in STRUCTURAL_NATIVE:
        return PROBE_DEFECT, (
            f"Msg {native}: {STRUCTURAL_NATIVE[native]}. This is a defect in "
            "our probe, not a finding about this login.")
    if _looks_like_permission_denied(sqlstate, native, message):
        return REFUSED, ""
    return INCONCLUSIVE, (
        f"the server refused it, but not with a permission error "
        f"(SQLSTATE {sqlstate or '?'}, Msg {native if native is not None else '?'}). "
        "Refusal for an unknown reason does not establish that writing is "
        "denied.")


def run_write_probes(raw_cursor_factory, probes: list[Probe]) -> list[ProbeResult]:
    """Attempt each write. Refusal is the pass."""
    results: list[ProbeResult] = []
    for probe in probes:
        if probe.sql.startswith("(no writable column"):
            # Never sent. Recorded so the report shows what was not tested.
            results.append(ProbeResult(
                probe, NO_PROBEABLE_COLUMN,
                reason="every column on this table is an identity, computed "
                       "or rowversion column, so there is nothing a write "
                       "probe can legally target. This is a limitation of "
                       "the probe. It is NOT evidence that writing is "
                       "permitted, and it is NOT evidence that it is denied."))
            continue
        cursor = raw_cursor_factory()
        try:
            cursor.execute(probe.sql)
        except Exception as exc:                       # noqa: BLE001
            sqlstate, native, message = _error_parts(exc)
            verdict, reason = classify(sqlstate, native, message)
            results.append(ProbeResult(probe, verdict, sqlstate, native,
                                       message.strip(), reason))
        else:
            # It ran. Zero rows changed by construction, and that is the
            # only reason this is survivable.
            results.append(ProbeResult(
                probe, PERMITTED, None, None,
                "the statement was accepted and executed (0 rows, by "
                "construction) - the server did not refuse it",
                "the server did not refuse a write"))
        finally:
            try:
                cursor.close()
            except Exception:                          # pragma: no cover
                pass
    return results


def run_permission_checks(cursor, checks) -> list[PermissionAnswer]:
    """Ask the server. Pure reads — these go through the guarded cursor."""
    answers: list[PermissionAnswer] = []
    for name, securable, klass, permission, meaning in checks:
        sql = (f"SELECT HAS_PERMS_BY_NAME({securable}, "
               f"{'NULL' if klass == 'SERVER' else repr(klass)}, ?)")
        try:
            cursor.execute(sql, permission)
            row = cursor.fetchone()
            value = None if row is None or row[0] is None else bool(row[0])
        except Exception as exc:                       # noqa: BLE001
            _, _, message = _error_parts(exc)
            answers.append(PermissionAnswer(name, securable, permission, None,
                                            f"could not ask: {message.strip()}"))
            continue
        answers.append(PermissionAnswer(name, securable, permission, value,
                                        meaning))
    return answers


def run(cn, tables: tuple[str, ...] = POS_TABLES) -> Report:
    """The whole audit against a live connection.

    `cn` is whatever `adapter.connect()` returned. If the sqlguard diff has
    been applied it is a GuardedConnection, and `.raw` is used for the
    write probes on purpose: the probe's job is to prove the *server*
    refuses, which says nothing if our own guard refused first.
    """
    import sqlguard

    report = Report(guard_wired=sqlguard.is_guarded(cn))
    raw = getattr(cn, "raw", cn)

    try:
        cursor = cn.cursor()
        cursor.execute(IDENTITY_SQL)
        row = cursor.fetchone()
        names = [d[0] for d in cursor.description]
        report.identity = dict(zip(names, row))
    except Exception as exc:                           # noqa: BLE001
        report.failure = f"could not read the connection's identity: {exc}"
        return report

    # Ask the server which column each probe may legally target, before
    # building a single statement. Read-only, and it goes through the
    # guarded cursor like every other read.
    columns: dict[str, str | None] = {}
    for table in tables:
        try:
            columns[table] = probeable_column(cn.cursor(), table)
        except Exception as exc:                       # noqa: BLE001
            report.column_errors[table] = f"{type(exc).__name__}: {exc}"
            columns[table] = None
    report.probe_columns = dict(columns)

    report.writes = run_write_probes(lambda: raw.cursor(), write_probes(columns))
    report.permissions = run_permission_checks(cn.cursor(),
                                               permission_checks(tables))

    try:
        cursor = cn.cursor()
        cursor.execute(EFFECTIVE_SQL, tables[0])
        report.effective = [r[0] for r in cursor.fetchall()]
    except Exception as exc:                           # noqa: BLE001
        report.effective = [f"(could not enumerate: {exc})"]

    return report


# ════════════════════════════════════════════════════════════════
# what goes on the transcript
# ════════════════════════════════════════════════════════════════

def format_report(report: Report, width: int = 66) -> str:
    """The block the operator sees and we read three weeks later.

    Every SQL error is printed verbatim. That evidence is the point: it is
    what we show the customer when we say their POS was never written to.
    """
    out: list[str] = []

    def w(text: str = "") -> None:
        out.append(text)

    w("=" * width)
    w("  READ-ONLY PROOF - attempting to write to the POS, and requiring")
    w("  every attempt to be refused")
    w("=" * width)

    if report.failure:
        w(f"  COULD NOT RUN: {report.failure}")
        w("=" * width)
        return "\n".join(out)

    ident = report.identity
    w(f"  login              {ident.get('login_name')}")
    w(f"  database user      {ident.get('db_user')}")
    w(f"  database           {ident.get('database_name')}")
    w("")
    for label, key in (("sysadmin", "is_sysadmin"),
                       ("db_owner", "is_db_owner"),
                       ("db_ddladmin", "is_ddladmin"),
                       ("db_datawriter", "is_datawriter"),
                       ("db_denydatawriter", "is_denydatawriter"),
                       ("db_datareader", "is_datareader")):
        value = ident.get(key)
        flag = "yes" if value else "no"
        w(f"  {label:<18} {flag}")
    w("")
    w(f"  sqlguard choke point  "
      f"{'ACTIVE' if report.guard_wired else 'NOT WIRED (see READONLY_GUARANTEE.md)'}")
    w("")

    w("  " + "-" * (width - 4))
    w("  COLUMN CHOSEN FOR EACH WRITE PROBE (asked of the server, not assumed)")
    w("  " + "-" * (width - 4))
    for table, column in report.probe_columns.items():
        if column:
            w(f"    {table:<16} {column}   (not identity, not computed)")
        else:
            w(f"    {table:<16} NONE FOUND - probes on this table could not run")
        if table in report.column_errors:
            w(f"                     could not ask: {report.column_errors[table]}")
    if report.probe_columns:
        w("")

    w("  " + "-" * (width - 4))
    w("  ATTEMPTED - each of these was actually sent to the POS")
    w("  " + "-" * (width - 4))
    for result in report.writes:
        w(f"    {result.verdict:<20} {result.probe.name}")
        w(f"      {result.probe.sql}")
        # The error NUMBER, on its own line and never truncated. The bundle
        # that exposed the identity defect showed only
        # "[42000] [Microsoft][ODBC Driver 11 for SQL Server][SQ..." — the
        # number had been cut off, and it was the one fact that would have
        # settled the whole thing in seconds. 229 = permission denied.
        # 8102 = identity refusal.
        if result.sqlstate or result.native is not None:
            w(f"      SQLSTATE {result.sqlstate or '?'}   "
              f"Msg {result.native if result.native is not None else '?'}")
        if result.message:
            for line in _fold(result.message, width - 8):
                w(f"      {line}")
        # Every verdict other than REFUSED has to say why it is not REFUSED.
        if result.reason:
            folded = _fold(result.reason, width - 11)
            w(f"      why: {folded[0]}")
            for line in folded[1:]:
                w(f"           {line}")
        if result.verdict == PERMITTED:
            w(f"      !! {result.probe.permitted_means}")
        w("")

    w("  " + "-" * (width - 4))
    w("  ASKED - never attempted; there is no harmless version of these")
    w("  " + "-" * (width - 4))
    for answer in report.permissions:
        if answer.held is None:
            state = "UNKNOWN"
        else:
            state = "HELD !!" if answer.held else "not held"
        w(f"    {state:<13} {answer.name}  ({answer.permission})")
        if answer.held is not False:
            w(f"      {_wrap(answer.permitted_means, width - 10)}")
    w("")

    w("  " + "-" * (width - 4))
    w(f"  Everything this login may do to {POS_TABLES[0]}, per the server:")
    w("  " + "-" * (width - 4))
    # Distinct, sorted. fn_my_permissions returns one row PER COLUMN, so the
    # customer's report printed "SELECT" 46 times and reading it to confirm
    # that UPDATE was absent meant scanning 46 identical tokens. The count is
    # kept because it is the evidence that the query ran at all.
    distinct = sorted(set(report.effective))
    w(f"    {', '.join(distinct) or '(none)'}")
    if len(report.effective) != len(distinct):
        w(f"    ({len(report.effective)} rows, one per column; shown distinct)")
    w("")

    if report.passed:
        w("  VERDICT: READ-ONLY CONFIRMED")
        w("  Every write was refused by the server, and no permission that")
        w("  could alter or remove data is held by this login.")
    elif report.permitted:
        # The only case that justifies telling a customer their credentials
        # are unsafe.
        w("  VERDICT: NOT READ-ONLY - DO NOT INSTALL")
        for name in report.permitted:
            w(f"    !! PERMITTED: {name}")
        for name in report.inconclusive:
            w(f"    ?? COULD NOT ESTABLISH: {name}")
        for name in report.probe_defects:
            w(f"    ** OUR PROBE FAILED: {name}")
        w("")
        w("  These credentials can change the customer's POS database. That")
        w("  is the one thing this product promises cannot happen.")
        w("  Change nothing. Photograph this screen and call.")
    elif report.probe_defects and not report.inconclusive:
        # 🔴 Our bug. Still blocks the install — failing closed is correct
        # and has not been relaxed — but it must not be reported as a
        # finding about the customer's login. Saying "your credentials are
        # unsafe" when our own SQL was malformed is a confident wrong
        # answer, which is the failure this product exists to prevent.
        w("  VERDICT: CANNOT VERIFY - OUR PROBE IS AT FAULT, NOT THIS LOGIN")
        for name in report.probe_defects:
            w(f"    ** {name}")
        w("")
        w("  Every probe that did run was refused, and no dangerous")
        w("  permission is held. Nothing here suggests the login can write.")
        w("  But this tool could not construct a valid write for this")
        w("  schema, so it will not claim the POS is read-only either.")
        w("")
        w("  The install is blocked deliberately. Send this block and the")
        w("  diagnostics zip - this is fixable from our side, and it does")
        w("  NOT mean anything is wrong with this machine.")
    else:
        w("  VERDICT: COULD NOT ESTABLISH READ-ONLY - DO NOT INSTALL")
        for name in report.inconclusive:
            w(f"    ?? COULD NOT ESTABLISH: {name}")
        for name in report.probe_defects:
            w(f"    ** OUR PROBE FAILED: {name}")
        w("")
        w("  No write was permitted, but at least one could not be shown to")
        w("  be refused. 'We could not tell' and 'it is refused' must never")
        w("  produce the same outcome, so this blocks the install.")
        w("  Change nothing. Photograph this screen and call.")
    w("=" * width)
    return "\n".join(out)


def _wrap(text: str, width: int) -> str:
    """One line, trimmed. Kept for the ASKED section, where the text is our
    own prose and losing the tail costs nothing."""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[:width - 3] + "..."


def _fold(text: str, width: int) -> list[str]:
    """Wrap onto as many lines as it takes, losing nothing.

    🔴 Replaces the truncation that destroyed the evidence. The customer's
    report showed `[42000] [Microsoft][ODBC Driver 11 for SQL Server][SQ...`
    — every SQL Server error message puts its number near the END, so
    trimming the tail removed the only part that mattered. Server error text
    is never truncated again.
    """
    words = " ".join(text.split()).split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]
