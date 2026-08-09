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

# Verdicts. Only REFUSED is a pass; INCONCLUSIVE is deliberately not one.
REFUSED = "REFUSED"
PERMITTED = "PERMITTED"
INCONCLUSIVE = "INCONCLUSIVE"

# SQL Server errors that mean "you are not allowed to do that".
#   229  The <perm> permission was denied on object ...
#   230  The <perm> permission was denied on column ...
#   262  <perm> permission denied in database ...
#   300  VIEW SERVER STATE permission was denied ...
PERMISSION_DENIED_NATIVE = frozenset({229, 230, 262, 300})

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
    def passed(self) -> bool:
        """Every write refused, every dangerous permission answered 'no'.

        An inconclusive answer is a failure. "We could not tell" and
        "it is refused" must never produce the same outcome — that is the
        whole failure mode this product exists to avoid.
        """
        return (self.failure is None
                and not self.permitted
                and not self.inconclusive
                and bool(self.writes)
                and bool(self.permissions))


# ════════════════════════════════════════════════════════════════
# the probes
# ════════════════════════════════════════════════════════════════

def write_probes(tables: tuple[str, ...] = POS_TABLES) -> list[Probe]:
    """Zero-row writes. Every one must be refused.

    Built rather than listed so the same three shapes cover every table we
    read: a login denied on Sales but not on Items would otherwise pass.
    """
    probes: list[Probe] = []
    for table in tables:
        probes.append(Probe(
            name=f"UPDATE {table}",
            # `SET <col> = <col>` needs no literal and changes nothing even
            # if a row somehow qualified.
            sql=f"UPDATE {table} SET {_self_assign(table)} WHERE 1 = 0",
            permitted_means="this login can modify existing rows"))
        probes.append(Probe(
            name=f"DELETE {table}",
            sql=f"DELETE FROM {table} WHERE 1 = 0",
            permitted_means="this login can remove rows"))
        probes.append(Probe(
            name=f"INSERT {table}",
            # Sourced from the table itself, so no value is invented and
            # no column type has to be guessed.
            sql=(f"INSERT INTO {table} ({_key(table)}) "
                 f"SELECT {_key(table)} FROM {table} WHERE 1 = 0"),
            permitted_means="this login can add rows"))
    return probes


def _key(table: str) -> str:
    return {"dbo.Sales": "salid", "dbo.SalesDe": "saledeid",
            "dbo.Items": "Itid"}.get(table, "id")


def _self_assign(table: str) -> str:
    column = _key(table)
    return f"{column} = {column}"


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


def run_write_probes(raw_cursor_factory, probes: list[Probe]) -> list[ProbeResult]:
    """Attempt each write. Refusal is the pass."""
    results: list[ProbeResult] = []
    for probe in probes:
        cursor = raw_cursor_factory()
        try:
            cursor.execute(probe.sql)
        except Exception as exc:                       # noqa: BLE001
            sqlstate, native, message = _error_parts(exc)
            verdict = (REFUSED
                       if _looks_like_permission_denied(sqlstate, native, message)
                       else INCONCLUSIVE)
            results.append(ProbeResult(probe, verdict, sqlstate, native,
                                       message.strip()))
        else:
            # It ran. Zero rows changed by construction, and that is the
            # only reason this is survivable.
            results.append(ProbeResult(
                probe, PERMITTED, None, None,
                "the statement was accepted and executed (0 rows, by "
                "construction) — the server did not refuse it"))
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

    report.writes = run_write_probes(lambda: raw.cursor(), write_probes(tables))
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
    w("  ATTEMPTED - each of these was actually sent to the POS")
    w("  " + "-" * (width - 4))
    for result in report.writes:
        w(f"    {result.verdict:<13} {result.probe.name}")
        w(f"      {result.probe.sql}")
        if result.message:
            w(f"      -> {_wrap(result.message, width - 10)}")
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
    w(f"    {', '.join(report.effective) or '(none)'}")
    w("")

    if report.passed:
        w("  VERDICT: READ-ONLY CONFIRMED")
        w("  Every write was refused by the server, and no permission that")
        w("  could alter or remove data is held by this login.")
    else:
        w("  VERDICT: NOT READ-ONLY — DO NOT INSTALL")
        for name in report.permitted:
            w(f"    !! PERMITTED: {name}")
        for name in report.inconclusive:
            w(f"    ?? COULD NOT ESTABLISH: {name}")
        w("")
        w("  These credentials can change the customer's POS database. That")
        w("  is the one thing this product promises cannot happen.")
        w("  Change nothing. Photograph this screen and call.")
    w("=" * width)
    return "\n".join(out)


def _wrap(text: str, width: int) -> str:
    """One line, trimmed. The full text is in the transcript file; the
    screen only has to be readable."""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[:width - 3] + "..."
