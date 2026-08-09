# -*- coding: utf-8 -*-
"""
test_readonly.py — the POS is never written to, checked by machine
================================================================
"Read-only on the POS" was a rule people kept. These tests make it
something the suite refuses to let go of.

Four layers, tested in order of how much they are worth:

  1. `sqlguard.assert_read_only` refuses everything that is not a plain
     read — and every real query this product sends passes it. A guard
     that rejects the actual queries would be turned off within a day.

  2. The adapter's own source contains no write keyword in any SQL
     literal. A future edit that adds a write cannot pass review by
     looking innocent: this fails first.

  3. Every write statement in the repository lives in exactly one file,
     `readonly_probe.py`, and every one of them is zero-row by
     construction.

  4. The agent opens no file for writing outside its own folder, proven
     with an audit hook rather than by reading the code and believing it.
================================================================
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest

import readonly_probe
import sqlguard

HERE = Path(__file__).resolve().parent

# Modules that may talk to the POS database. The probe is deliberately
# absent — it is the one file allowed to contain writes, and test 3 pins
# that it stays the only one.
POS_FACING = ("adapter_hdsoft.py", "agent.py", "fake_adapter.py")

# The one exception, named so its exception is a decision rather than an
# oversight.
WRITE_SQL_IS_ALLOWED_ONLY_IN = "readonly_probe.py"


# ════════════════════════════════════════════════════════════════
# 1) the guard refuses writes — and permits our real reads
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("sql", [
    "UPDATE dbo.Sales SET saltot = 0",
    "DELETE FROM dbo.Sales",
    "INSERT INTO dbo.Sales (salid) VALUES (1)",
    "MERGE dbo.Sales AS t USING dbo.Sales AS s ON 1=1 WHEN MATCHED THEN DELETE",
    "DROP TABLE dbo.Sales",
    "ALTER TABLE dbo.Sales ADD x INT",
    "CREATE TABLE dbo.x (a INT)",
    "TRUNCATE TABLE dbo.Sales",
    "EXEC sp_executesql N'UPDATE dbo.Sales SET saltot = 0'",
    "EXECUTE dbo.SomeProc",
    "GRANT SELECT ON dbo.Sales TO public",
    "REVOKE SELECT ON dbo.Sales FROM public",
    "DENY SELECT ON dbo.Sales TO public",
    "BACKUP DATABASE HD_Rest_Cashier TO DISK = 'x'",
    "RESTORE DATABASE HD_Rest_Cashier FROM DISK = 'x'",
    "SELECT salid INTO dbo.copy FROM dbo.Sales",
    "BEGIN TRAN",
    "SET NOCOUNT ON",
    "WAITFOR DELAY '23:59:59'",
    "DBCC CHECKDB",
    "SHUTDOWN",
])
def test_every_write_shape_is_refused(sql):
    with pytest.raises(sqlguard.WriteAttempt):
        sqlguard.assert_read_only(sql)


def test_a_batch_hiding_a_write_behind_a_read_is_refused():
    """The shape the architect named. The first half is innocent."""
    with pytest.raises(sqlguard.WriteAttempt) as caught:
        sqlguard.assert_read_only("SELECT 1; DROP TABLE dbo.Sales")
    assert "batch" in str(caught.value)


def test_a_write_hidden_behind_a_line_comment_is_refused():
    """`--` runs to the end of the LINE, not to the end of the string. A
    guard that stripped from `--` onwards would drop the DROP."""
    with pytest.raises(sqlguard.WriteAttempt):
        sqlguard.assert_read_only("SELECT 1 -- harmless\n; DROP TABLE dbo.Sales")


def test_a_write_hidden_in_a_nested_block_comment_is_refused():
    with pytest.raises(sqlguard.WriteAttempt):
        sqlguard.assert_read_only(
            "SELECT 1 /* outer /* inner */ */ ; DELETE FROM dbo.Sales")


def test_a_write_keyword_inside_a_string_literal_is_not_a_write():
    """Over-refusing is its own failure: a guard that rejects legitimate
    reads gets disabled, and then nothing is guarded."""
    sqlguard.assert_read_only(
        "SELECT salid FROM dbo.Sales WHERE salreceiptnum = 'DELETE ME'")


def test_a_column_named_like_a_verb_is_not_a_write():
    sqlguard.assert_read_only("SELECT [Delete], [Update] FROM dbo.Sales")


def test_an_unterminated_literal_is_refused_rather_than_guessed():
    with pytest.raises(sqlguard.WriteAttempt) as caught:
        sqlguard.assert_read_only("SELECT * FROM dbo.Sales WHERE x = 'oops")
    assert "unterminated" in str(caught.value)


def test_a_bare_stored_procedure_call_is_refused():
    """`sp_who` on its own is a valid batch. The allowlist on the leading
    verb is what catches it, not the keyword list."""
    with pytest.raises(sqlguard.WriteAttempt):
        sqlguard.assert_read_only("sp_who")


_VERBS = ("SELECT", "UPDATE", "DELETE", "INSERT", "MERGE", "EXEC", "EXECUTE",
          "CREATE", "ALTER", "DROP", "TRUNCATE", "BACKUP", "RESTORE", "GRANT",
          "REVOKE", "DENY", "WITH")

# A statement, as opposed to a word. `'DELETE'` on its own is a PostgREST
# HTTP method in supa.py and a PostgreSQL privilege name in
# audit_privileges.py — neither is SQL, and a scanner that cannot tell the
# difference gets muted, which is worse than one that is slightly narrow.
_STRUCTURE = ("FROM", "INTO", "TABLE", " SET ", "VALUES", "DATABASE", "ON ")


def _sql_literals(path: Path) -> list[str]:
    """Every string constant in a module that is plausibly a SQL statement."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        text = node.value
        words = text.split()
        if not words:
            continue
        # Written in capitals, as every SQL keyword in this repository is.
        # supa.py's docstring "Insert or merge on the primary key" is prose
        # about an HTTP call, not a statement, and the capital is what
        # separates the two without having to enumerate English.
        if words[0] != words[0].upper():
            continue
        upper = " ".join(text.upper().split())
        if not upper.startswith(_VERBS):
            continue
        if not any(marker in f" {upper} " for marker in _STRUCTURE):
            continue
        found.append(text)
    return found


def test_every_real_query_this_product_sends_passes_the_guard():
    """The falsifier for the whole design: if the guard rejected one of
    our own reads, it would be relaxed or bypassed, and then it guards
    nothing. Placeholders are substituted the way the adapter builds them.
    """
    checked = 0
    for name in ("adapter_hdsoft.py", "agent.py", "readonly_probe.py"):
        for literal in _sql_literals(HERE / name):
            if not literal.upper().lstrip().startswith("SELECT"):
                continue                       # writes are covered elsewhere
            # The adapter interpolates its column lists and IN() markers.
            filled = (literal.replace("{_INV_COLS}", "s.salid, s.saltot")
                             .replace("{_LINE_COLS}", "d.saledeid, d.saleid")
                             .replace("{marks}", "?,?,?")
                             .replace("{securable}", "'dbo.Sales'")
                             .replace("{'NULL' if klass == 'SERVER' else repr(klass)}",
                                      "'OBJECT'"))
            if "{" in filled:
                continue                       # not a complete statement
            sqlguard.assert_read_only(filled)
            checked += 1
    assert checked >= 10, f"only {checked} real queries were checked"


# ════════════════════════════════════════════════════════════════
# 2) the source itself carries no write
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name", POS_FACING)
def test_no_pos_facing_module_contains_a_write_statement(name):
    """Reads the source, not the intent. A future edit that adds a write
    to the adapter fails here even if it looks innocent in review."""
    offenders: list[str] = []
    for literal in _sql_literals(HERE / name):
        words = {w.upper() for w in sqlguard._WORD.findall(sqlguard.normalise(literal))}
        hits = sorted(words & sqlguard.FORBIDDEN)
        if hits:
            offenders.append(f"{hits} in {literal.strip()[:70]!r}")
    assert not offenders, (
        f"{name} contains write SQL:\n  " + "\n  ".join(offenders) +
        f"\n\nOnly {WRITE_SQL_IS_ALLOWED_ONLY_IN} may contain write statements.")


def test_the_adapter_sends_only_select_and_only_with_nolock():
    """Both halves of the adapter's own rule 1, checked rather than read."""
    literals = _sql_literals(HERE / "adapter_hdsoft.py")
    assert literals, "no SQL found in the adapter — this test stopped testing"
    for literal in literals:
        assert literal.upper().lstrip().startswith("SELECT"), literal
    # Every query against a POS business table takes NOLOCK. sys.* and the
    # bare SELECT GETDATE() are catalogue reads and take no lock worth
    # naming.
    for literal in literals:
        if "dbo." in literal:
            assert "NOLOCK" in literal.upper(), (
                f"a read of a POS table without WITH (NOLOCK): {literal!r}")


# ════════════════════════════════════════════════════════════════
# 3) writes exist in exactly one file, and change nothing
# ════════════════════════════════════════════════════════════════

def test_write_sql_lives_in_exactly_one_file():
    """If a second file ever grows a write statement, this names it.

    Test files are excluded: they carry deliberate write SQL as fixtures —
    including the ones two tests up that prove the guard refuses it — and
    none of them ever holds a POS connection.
    """
    guilty: list[str] = []
    for path in sorted(HERE.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        if path.name in (WRITE_SQL_IS_ALLOWED_ONLY_IN, "sqlguard.py"):
            continue
        for literal in _sql_literals(path):
            words = {w.upper() for w in sqlguard._WORD.findall(
                sqlguard.normalise(literal))}
            if words & {"UPDATE", "DELETE", "INSERT", "MERGE", "TRUNCATE",
                        "DROP", "ALTER", "CREATE"}:
                guilty.append(f"{path.name}: {literal.strip()[:60]!r}")
    assert not guilty, "write SQL outside the probe:\n  " + "\n  ".join(guilty)


def test_every_attempted_write_probe_is_zero_row_by_construction():
    """The probes point at a live restaurant's sales table. Each one must
    be unable to change anything even if the server permits it."""
    probes = readonly_probe.write_probes()
    assert probes, "no write probes — the read-only proof stopped proving"
    for probe in probes:
        assert "WHERE 1 = 0" in probe.sql, (
            f"{probe.name} is not zero-row: {probe.sql!r}")


def test_no_probe_can_empty_or_alter_a_table():
    """TRUNCATE takes no WHERE clause and ALTER changes their live schema.
    Neither may ever be attempted — both are interrogated instead."""
    for probe in readonly_probe.write_probes():
        words = {w.upper() for w in sqlguard._WORD.findall(probe.sql)}
        assert "TRUNCATE" not in words, probe.sql
        assert "ALTER" not in words, probe.sql
        assert "DROP" not in words, probe.sql

    asked = {name for name, *_ in readonly_probe.permission_checks()}
    assert any("ALTER dbo.Sales" == name for name in asked), (
        "TRUNCATE is governed by ALTER on the table; if we no longer ask "
        "about ALTER, nothing covers TRUNCATE at all")


def test_an_inconclusive_answer_is_not_a_pass():
    """'We could not tell' and 'it is refused' must never produce the same
    outcome. That equivalence is this product's whole failure mode."""
    report = readonly_probe.Report()
    report.writes = [readonly_probe.ProbeResult(
        readonly_probe.write_probes()[0], readonly_probe.INCONCLUSIVE)]
    report.permissions = [readonly_probe.PermissionAnswer(
        "ALTER dbo.Sales", "'dbo.Sales'", "ALTER", None, "unknown")]
    assert not report.passed


def test_a_permitted_write_fails_the_report():
    report = readonly_probe.Report()
    report.writes = [readonly_probe.ProbeResult(
        readonly_probe.write_probes()[0], readonly_probe.PERMITTED)]
    report.permissions = [readonly_probe.PermissionAnswer(
        "ALTER dbo.Sales", "'dbo.Sales'", "ALTER", False, "")]
    assert not report.passed
    assert "NOT READ-ONLY" in readonly_probe.format_report(report)


def test_an_empty_report_is_not_a_pass():
    """A probe run that produced nothing at all — because it could not
    connect, or because someone emptied the list — must not read as clean."""
    assert not readonly_probe.Report().passed


def test_a_fully_refused_report_passes():
    report = readonly_probe.Report(identity={"login_name": "monitor_ro"})
    report.writes = [readonly_probe.ProbeResult(p, readonly_probe.REFUSED,
                                                "42000", 229,
                                                "The UPDATE permission was denied")
                     for p in readonly_probe.write_probes()]
    report.permissions = [
        readonly_probe.PermissionAnswer(name, sec, perm, False, why)
        for name, sec, _klass, perm, why in readonly_probe.permission_checks()]
    assert report.passed
    assert "READ-ONLY CONFIRMED" in readonly_probe.format_report(report)


# ════════════════════════════════════════════════════════════════
# 4) the disk — proven with an audit hook, not by reading the code
# ════════════════════════════════════════════════════════════════

class WriteRecorder:
    """Every path opened for writing, recorded by the interpreter itself.

    sys.addaudithook cannot be removed once installed, which is why this
    runs in a subprocess in the test below rather than in-process.
    """


AUDIT_PROBE = r'''
import json, os, sys, datetime, pathlib
# The probe lives in a temp folder, so the repository is not on sys.path
# by virtue of being the script's directory.
sys.path.insert(0, sys.argv[2])
opened = []

def _as_path(value):
    # The `open` audit event carries an int when a file object is built
    # from an already-open descriptor - io.FileIO(3), which is how the
    # bytecode compiler and the subprocess plumbing reopen handles. The
    # descriptor's file was opened by path earlier and recorded then, so
    # ignoring the int loses nothing; treating it as a path invents a file
    # called "3" in the current directory, which is what this test first
    # reported.
    if isinstance(value, bytes):
        return os.fsdecode(value)
    return value if isinstance(value, str) else None

def hook(event, args):
    if event == "open":
        path, mode, flags = args
        writing = False
        if isinstance(mode, str) and any(c in mode for c in "wa+x"):
            writing = True
        if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR |
                                               os.O_CREAT | os.O_APPEND):
            writing = True
        name = _as_path(path)
        if writing and name is not None:
            opened.append(("open", name))
    elif event in ("os.rename", "os.replace", "os.remove", "os.unlink",
                   "os.mkdir", "os.rmdir"):
        name = _as_path(args[0])
        if name is not None:
            opened.append((event, name))

sys.addaudithook(hook)

import agent, fake_adapter, supa

class Cloud:
    def __init__(self): self.calls = []
    def upsert(self, t, rows, on_conflict): return len(rows)
    def update(self, t, f, p, returning=True): return [p]
    def insert(self, t, rows, returning=True): return []
    def select(self, t, params=None, paginate=True):
        return [{"watermark_salid": 1000, "rescan_from_salid": 1000}]
    def count(self, t, params=None): return 0

cfg = agent.Config(
    tenant_id="57b61b47-a590-49fe-803c-0c174a07b7ec",
    source_id="93f8d146-ba68-4d58-8eda-f797f3e28bd4",
    supabase_url="https://example.supabase.co",
    supabase_anon_key="anon", supabase_agent_token="tok",
    sql={"server": "x", "database": "y", "user": "z", "password": "p"})

work = sys.argv[1]
agent.run_once(cfg, agent.State(initialised=True),
               pathlib.Path(work) / "state.json",
               fake_adapter, Cloud(),
               datetime.datetime(2026, 8, 9, 3, 0, tzinfo=datetime.timezone.utc),
               False)

print("###AUDIT###")
print(json.dumps(opened))
'''


def test_the_agent_writes_only_inside_its_own_folder(tmp_path):
    """A real cycle, with the interpreter reporting every write it makes.

    This is the claim "nothing in this product touches the POS machine
    outside our folder" turned into something that can fail. Reading the
    source and believing it is what this replaces.
    """
    import json
    import subprocess

    probe = tmp_path / "audit_probe.py"
    probe.write_text(AUDIT_PROBE, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    result = subprocess.run(
        [sys.executable, str(probe), str(work), str(HERE)],
        cwd=str(work), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    assert "###AUDIT###" in result.stdout, result.stdout + result.stderr

    events = json.loads(result.stdout.split("###AUDIT###", 1)[1].strip())
    assert events, "the audit hook recorded nothing — it is not working"

    stray: list[str] = []
    for event, path in events:
        resolved = Path(path).resolve()
        inside_work = str(resolved).startswith(str(work.resolve()))
        # The interpreter writes its own bytecode cache next to the source.
        # That is Python's, not ours, and it is inside the install folder.
        is_pycache = "__pycache__" in resolved.parts
        if not (inside_work or is_pycache):
            stray.append(f"{event} {resolved}")

    assert not stray, (
        "the agent wrote outside its own folder:\n  " + "\n  ".join(stray))


def test_the_audit_probe_would_notice_a_stray_write(tmp_path):
    """The falsifier for the test above: if the hook missed writes, the
    test would pass for the wrong reason. Make it write somewhere else on
    purpose and require it to be caught."""
    import json
    import subprocess

    outside = tmp_path / "somewhere_else" / "oops.txt"
    outside.parent.mkdir()
    naughty = AUDIT_PROBE.replace(
        'print("###AUDIT###")',
        f'open({str(outside)!r}, "w").write("x")\nprint("###AUDIT###")')

    probe = tmp_path / "audit_probe_bad.py"
    probe.write_text(naughty, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    result = subprocess.run(
        [sys.executable, str(probe), str(work), str(HERE)],
        cwd=str(work), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    assert "###AUDIT###" in result.stdout, result.stdout + result.stderr
    events = json.loads(result.stdout.split("###AUDIT###", 1)[1].strip())
    assert any("oops.txt" in path for _event, path in events), (
        "the audit hook did not notice a deliberate stray write, so the "
        "test above proves nothing")


# ════════════════════════════════════════════════════════════════
# 5) the wrapper, so the guard cannot be bypassed by accident
# ════════════════════════════════════════════════════════════════

class _RecordingCursor:
    def __init__(self): self.executed = []
    def execute(self, sql, *params): self.executed.append(sql)
    def fetchone(self): return (1,)
    def fetchall(self): return []
    def close(self): pass


class _RecordingConnection:
    def __init__(self): self.cursors = []
    def cursor(self):
        cur = _RecordingCursor()
        self.cursors.append(cur)
        return cur
    def close(self): pass


def test_a_guarded_connection_hands_out_guarded_cursors():
    raw = _RecordingConnection()
    guarded = sqlguard.guard(raw)
    cursor = guarded.cursor()
    cursor.execute("SELECT 1 FROM dbo.Sales WITH (NOLOCK)")
    with pytest.raises(sqlguard.WriteAttempt):
        cursor.execute("DELETE FROM dbo.Sales")
    assert raw.cursors[0].executed == ["SELECT 1 FROM dbo.Sales WITH (NOLOCK)"]


def test_guarding_twice_is_harmless():
    raw = _RecordingConnection()
    assert sqlguard.guard(sqlguard.guard(raw)) is not None
    assert sqlguard.is_guarded(sqlguard.guard(sqlguard.guard(raw)))


def test_the_raw_escape_hatch_is_only_used_by_the_probe():
    """`.raw` exists so the probe can prove the SERVER refuses. If any
    other module reaches for it, the guard has a hole with a name."""
    users: list[str] = []
    for path in sorted(HERE.glob("*.py")):
        if path.name in (WRITE_SQL_IS_ALLOWED_ONLY_IN, "sqlguard.py",
                         Path(__file__).name):
            continue
        source = path.read_text(encoding="utf-8")
        if re.search(r"\.raw(?![A-Za-z0-9_])", source):
            users.append(path.name)
    assert not users, f"modules reaching past the SQL guard: {users}"


def test_the_agent_would_report_an_unwired_guard():
    """adapter_hdsoft.py is locked, so the two-line diff that calls guard()
    is applied by the architect. An unapplied diff must be visible on the
    install transcript rather than assumed — never silently absent."""
    report = readonly_probe.Report(guard_wired=False)
    assert "NOT WIRED" in readonly_probe.format_report(report)
