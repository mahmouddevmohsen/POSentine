# -*- coding: utf-8 -*-
"""
test_installer.py — the gates, and what the one click may not skip
================================================================
The risk this file exists for is not that the installer breaks. It is
that "one click" quietly becomes "one click that skips a check". Every
test below is a gate that must still refuse.

The read-only gate is exercised against a simulated SQL Server rather
than a mock of our own code: a fake connection that answers the probe's
questions the way a correctly configured server would, and a second one
that answers the way a dangerously configured server would. There is no
SQL Server on this machine, and the alternative — finding out on site —
is the thing this whole product is arranged to avoid.
================================================================
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import collect_diagnostics
import installer
import logsetup
import preflight
import readonly_probe
import sqlguard

HERE = Path(__file__).resolve().parent

TOKEN = ("eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYXV0aGVudGljYXRlZCIsInRlbmFudF9pZCI6"
         "IjU3YjYxYjQ3LWE1OTAtNDlmZS04MDNjLTBjMTc0YTA3YjdlYyJ9.SIGSIGSIG_marker")
PASSWORD = "Tr0ub4dor&3-installer-marker"


@pytest.fixture(autouse=True)
def clean_secret_registry():
    logsetup.forget_secrets()
    yield
    logsetup.forget_secrets()


# ════════════════════════════════════════════════════════════════
# a SQL Server that behaves, and one that does not
# ════════════════════════════════════════════════════════════════

class FakeOdbcError(Exception):
    """Shaped like pyodbc's: args are (sqlstate, message)."""


class FakeCursor:
    def __init__(self, server: "FakePos") -> None:
        self.server = server
        self._rows: list[tuple] = []
        self.description = None

    def execute(self, sql: str, *params):
        upper = sql.upper()
        self.server.executed.append(sql)

        if upper.lstrip().startswith("SELECT SUSER_NAME"):
            self.description = [(name,) for name in (
                "login_name", "db_user", "database_name", "is_sysadmin",
                "is_db_owner", "is_ddladmin", "is_datawriter",
                "is_denydatawriter", "is_datareader")]
            self._rows = [tuple(self.server.identity[k[0]]
                                for k in self.description)]
            return self

        if "HAS_PERMS_BY_NAME" in upper:
            permission = params[0] if params else ""
            self._rows = [(self.server.permissions.get(permission, 0),)]
            return self

        if "FN_MY_PERMISSIONS" in upper:
            self._rows = [(name,) for name in self.server.effective]
            return self

        # Anything else that is not a read is a write probe.
        verb = upper.lstrip().split(None, 1)[0]
        if verb in ("UPDATE", "DELETE", "INSERT"):
            if self.server.allows_writes:
                self._rows = []
                return self
            raise FakeOdbcError(
                "42000",
                f"[42000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]"
                f"The {verb} permission was denied on object 'Sales', "
                f"database 'HD_Rest_Cashier', schema 'dbo'. (229) "
                f"(SQLExecDirectW)")

        self._rows = [(1,)]
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class FakePos:
    """A SQL Server, as far as readonly_probe is concerned."""

    def __init__(self, allows_writes: bool = False,
                 permissions: dict | None = None,
                 sysadmin: bool = False) -> None:
        self.allows_writes = allows_writes
        self.permissions = permissions or {}
        self.executed: list[str] = []
        self.identity = {
            "login_name": "monitor_ro", "db_user": "monitor_ro",
            "database_name": "HD_Rest_Cashier",
            "is_sysadmin": 1 if sysadmin else 0,
            "is_db_owner": 0, "is_ddladmin": 0, "is_datawriter": 0,
            "is_denydatawriter": 1, "is_datareader": 1,
        }
        self.effective = ["SELECT"]

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


def guarded(server: FakePos):
    return sqlguard.guard(server)


# ════════════════════════════════════════════════════════════════
# the read-only gate
# ════════════════════════════════════════════════════════════════

def test_a_correctly_locked_down_server_passes():
    report = readonly_probe.run(guarded(FakePos()))
    assert report.passed, readonly_probe.format_report(report)
    assert "READ-ONLY CONFIRMED" in readonly_probe.format_report(report)
    assert report.guard_wired


def test_every_write_probe_is_actually_sent():
    """The falsifier for the whole proof: a probe run that quietly sent
    nothing would report success and mean nothing."""
    server = FakePos()
    readonly_probe.run(guarded(server))
    verbs = {sql.split(None, 1)[0].upper() for sql in server.executed}
    assert {"UPDATE", "DELETE", "INSERT"} <= verbs, sorted(verbs)


def test_a_server_that_permits_writes_fails_the_gate():
    report = readonly_probe.run(guarded(FakePos(allows_writes=True)))
    assert not report.passed
    assert report.permitted
    assert "NOT READ-ONLY" in readonly_probe.format_report(report)


def test_a_login_that_can_alter_the_table_fails_even_if_writes_are_denied():
    """ALTER is what TRUNCATE requires. A login that is denied INSERT and
    UPDATE but can ALTER dbo.Sales can still empty it in one statement,
    and every write probe would have said REFUSED."""
    report = readonly_probe.run(
        guarded(FakePos(permissions={"ALTER": 1})))
    assert not report.passed
    assert any("ALTER" in name for name in report.permitted)


def test_a_sysadmin_login_fails_even_when_everything_else_looks_clean():
    """sysadmin skips permission checks entirely, so the DENY is never
    evaluated. A server that answered every probe with a refusal while the
    login is sysadmin is answering about a state that does not bind."""
    server = FakePos(sysadmin=True)
    report = readonly_probe.run(guarded(server))
    assert not report.passed
    assert "member of sysadmin" in report.permitted


def test_a_refusal_for_the_wrong_reason_is_not_a_pass():
    """A probe that fails with something other than a permission error has
    not established anything. 'We could not tell' and 'it is refused' must
    never produce the same outcome."""
    class Odd(FakePos):
        def cursor(self):
            cursor = FakeCursor(self)
            original = cursor.execute

            def execute(sql, *params):
                if sql.upper().lstrip().startswith(("UPDATE", "DELETE",
                                                    "INSERT")):
                    raise FakeOdbcError("42S02", "Invalid object name 'Sales'")
                return original(sql, *params)

            cursor.execute = execute
            return cursor

    report = readonly_probe.run(guarded(Odd()))
    assert not report.passed
    assert report.inconclusive


def test_step_3b_stops_the_install_when_the_server_permits_a_write(
        monkeypatch, capsys):
    """The gate, at the level preflight enforces it."""
    import sys
    import types

    fake_adapter_module = types.ModuleType("adapter_hdsoft")
    fake_adapter_module.connect = lambda **_kw: guarded(
        FakePos(allows_writes=True))
    monkeypatch.setitem(sys.modules, "adapter_hdsoft", fake_adapter_module)

    cfg = types.SimpleNamespace(sql={"server": "x", "database": "y",
                                     "user": "z", "password": "p"})
    with pytest.raises(preflight.Stop) as caught:
        preflight.step_3b_readonly_proof(cfg)

    assert "3b" in caught.value.step
    assert "write to the POS" in caught.value.what
    assert "Do not install" in caught.value.do
    # The evidence must reach the transcript whether it passed or failed.
    assert "NOT READ-ONLY" in capsys.readouterr().out


def test_step_3b_passes_a_locked_down_server(monkeypatch, capsys):
    import sys
    import types

    fake_adapter_module = types.ModuleType("adapter_hdsoft")
    fake_adapter_module.connect = lambda **_kw: guarded(FakePos())
    monkeypatch.setitem(sys.modules, "adapter_hdsoft", fake_adapter_module)

    cfg = types.SimpleNamespace(sql={"server": "x", "database": "y",
                                     "user": "z", "password": "p"})
    report = preflight.step_3b_readonly_proof(cfg)
    assert report.passed
    assert "READ-ONLY CONFIRMED" in capsys.readouterr().out


# ════════════════════════════════════════════════════════════════
# the phases
# ════════════════════════════════════════════════════════════════

def test_phase_b_runs_a_second_cycle_on_a_first_install(monkeypatch):
    """A first install adopts the watermark and uploads nothing — that is
    the whole cycle, by design. Stopping there would hand Phase C a cloud
    with no invoices and stop a healthy machine."""
    calls: list[tuple] = []

    def fake_run(*args):
        calls.append(args)
        if len(calls) == 1:
            return 0, "first run: adopted watermark 218207 and read nothing"
        return 0, "cycle 1 ok  watermark 218207 -> 218250"

    monkeypatch.setattr(installer, "run_agent", fake_run)
    installer.phase_b_one_real_cycle()
    assert len(calls) == 2, "the second cycle never ran"


def test_phase_b_does_not_run_twice_on_a_normal_install(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(installer, "run_agent",
                        lambda *a: (calls.append(a), (0, "cycle ok"))[1])
    installer.phase_b_one_real_cycle()
    assert len(calls) == 1


def test_phase_b_does_not_accept_a_cycle_that_never_ran(monkeypatch):
    """🔴 agent.py exits 0 when another instance holds the lock, having
    done nothing. On a second double-click the scheduled task is already
    registered and its cycles overlap — so exit 0 alone would let Phase B
    pass without a cycle ever running."""
    calls: list[tuple] = []

    def fake_run(*args):
        calls.append(args)
        if len(calls) < 3:
            return 0, "another cycle is still running; exiting quietly"
        return 0, "cycle 4 ok  watermark 10 -> 20"

    monkeypatch.setattr(installer, "run_agent", fake_run)
    monkeypatch.setattr(installer.time, "sleep", lambda _s: None)
    installer.phase_b_one_real_cycle()
    assert len(calls) == 3


def test_phase_b_gives_up_rather_than_looping_forever(monkeypatch):
    monkeypatch.setattr(
        installer, "run_agent",
        lambda *a: (0, "another cycle is still running; exiting quietly"))
    monkeypatch.setattr(installer.time, "sleep", lambda _s: None)
    with pytest.raises(installer.Halt) as caught:
        installer.phase_b_one_real_cycle()
    assert "uninstall_agent.ps1" in caught.value.do


def test_phase_b_stops_on_a_failing_cycle(monkeypatch):
    monkeypatch.setattr(installer, "run_agent",
                        lambda *a: (1, "cycle failed before upload"))
    with pytest.raises(installer.Halt) as caught:
        installer.phase_b_one_real_cycle()
    assert caught.value.phase == "B"
    assert "No scheduled task" in caught.value.machine_state or \
           "not registered" in caught.value.machine_state


# ════════════════════════════════════════════════════════════════
# the stop screen
# ════════════════════════════════════════════════════════════════

def test_the_stop_screen_answers_every_question_in_order(capsys, tmp_path):
    halt = installer.Halt("C", "step 6 — confirm", "no invoices landed",
                          "Photograph and call.", "A cycle ran and uploaded.")
    installer.report_halt(halt, tmp_path / "install_x.txt")
    out = capsys.readouterr().out

    for required in ("S T O P P E D", "PHASE C", "WHAT FAILED", "WHAT TO DO",
                     "THE STATE OF THIS MACHINE", "THE LOG",
                     "PHOTOGRAPH THIS SCREEN AND CALL",
                     "CHANGE NOTHING ON THIS MACHINE",
                     "collect_diagnostics.bat"):
        assert required in out, required
    assert str(tmp_path / "install_x.txt") in out


def test_a_phase_a_stop_still_says_the_machine_is_untouched():
    """Phase A speaks VERIFY.md's language; the installer translates it
    rather than re-wording it, so the two cannot drift apart."""
    source = Path(installer.__file__).read_text(encoding="utf-8")
    assert "except preflight.Stop as stop:" in source
    assert "exactly as it was before you double-clicked" in source


# ════════════════════════════════════════════════════════════════
# the transcript
# ════════════════════════════════════════════════════════════════

def test_the_transcript_carries_no_secret(tmp_path):
    """The screen gets photographed and the photograph gets forwarded."""
    logsetup.register_secret("supabase_agent_token", TOKEN)
    path = tmp_path / "install.txt"

    class Sink:
        def __init__(self): self.text = ""
        def write(self, t): self.text += t
        def flush(self): pass

    sink = Sink()
    transcript = installer.Transcript(path, sink)
    transcript.write(f"Authorization: Bearer {TOKEN}\n")
    transcript.close()

    assert TOKEN not in path.read_text(encoding="utf-8")
    assert TOKEN not in sink.text, "the screen was not masked either"
    assert "***supabase_agent_token***" in path.read_text(encoding="utf-8")


def test_a_secret_split_across_two_writes_is_still_masked(tmp_path):
    """print() writes its argument and its newline separately, and any
    stream can be handed a secret in pieces. Masking per chunk would let
    the halves through."""
    logsetup.register_secret("supabase_agent_token", TOKEN)
    path = tmp_path / "install.txt"

    class Sink:
        def write(self, t): pass
        def flush(self): pass

    transcript = installer.Transcript(path, Sink())
    transcript.write(f"Bearer {TOKEN[:40]}")
    transcript.write(f"{TOKEN[40:]}\n")
    transcript.close()

    assert TOKEN not in path.read_text(encoding="utf-8")


def test_install_transcripts_are_bounded(tmp_path, monkeypatch):
    """An unbounded directory on a machine we cannot reach is a fault we
    caused, exactly like an unbounded log."""
    monkeypatch.setattr(installer, "LOG_DIR", tmp_path)
    monkeypatch.setattr(installer, "KEEP_TRANSCRIPTS", 5)
    for n in range(12):
        (tmp_path / f"install_2026080{n // 10}_{n:06d}.txt").write_text(
            "x", encoding="utf-8")

    installer.prune_transcripts()
    remaining = sorted(tmp_path.glob("install_*.txt"))
    assert len(remaining) == 5
    # The newest are the ones kept.
    assert remaining[-1].name.endswith("000011.txt")


# ════════════════════════════════════════════════════════════════
# the diagnostics zip
# ════════════════════════════════════════════════════════════════

def config_dict() -> dict:
    return {
        "tenant_id": "57b61b47-a590-49fe-803c-0c174a07b7ec",
        "source_id": "93f8d146-ba68-4d58-8eda-f797f3e28bd4",
        "supabase_url": "https://example.supabase.co",
        "supabase_anon_key": "eyJhbGciOiJIUzI1NiJ9.anonanonanon.SIG_anon_marker",
        "supabase_agent_token": TOKEN,
        "sql": {"server": "localhost\\HDSOFT", "database": "HD_Rest_Cashier",
                "user": "monitor_ro", "password": PASSWORD},
    }


def test_the_redacted_config_keeps_the_shape_and_loses_the_secrets(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_dict()), encoding="utf-8")

    text = collect_diagnostics.redacted_config(path)
    assert TOKEN not in text
    assert PASSWORD not in text
    # The shape has to survive, or the file answers no question at all.
    parsed = json.loads(text)
    assert parsed["tenant_id"] == "57b61b47-a590-49fe-803c-0c174a07b7ec"
    assert parsed["sql"]["user"] == "monitor_ro"
    assert parsed["sql"]["server"] == "localhost\\HDSOFT"
    # Identifiable without being disclosed.
    assert "redacted" in parsed["supabase_agent_token"]
    assert f"{len(TOKEN)} chars" in parsed["supabase_agent_token"]
    assert "sha256:" in parsed["supabase_agent_token"]


def test_config_json_itself_is_never_in_the_zip(tmp_path, monkeypatch):
    work = tmp_path / "install"
    (work / "logs").mkdir(parents=True)
    (work / "config.json").write_text(json.dumps(config_dict()),
                                      encoding="utf-8")
    (work / "agent.log").write_text(
        f"2026-08-09 INFO cycle ok\n2026-08-09 ERROR Bearer {TOKEN}\n",
        encoding="utf-8")
    (work / "logs" / "install_20260809_120000.txt").write_text(
        f"PWD={PASSWORD}\n", encoding="utf-8")

    monkeypatch.setattr(collect_diagnostics, "HERE", work)
    monkeypatch.setattr(collect_diagnostics, "COPY_TEXT", ())
    for name in ("versions", "odbc", "folder_listing"):
        monkeypatch.setattr(collect_diagnostics, name, lambda: "(skipped)")
    monkeypatch.setattr(collect_diagnostics, "manifest_check", lambda: "(skipped)")
    monkeypatch.setattr(collect_diagnostics, "task_state", lambda: ("", ""))
    monkeypatch.setattr(collect_diagnostics, "cloud", lambda _p: "(skipped)")
    monkeypatch.setattr(collect_diagnostics, "readonly_proof", lambda _p: "(skipped)")

    out = collect_diagnostics.collect(tmp_path / "diag.zip")

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "config.json" not in names
        assert "config.redacted.json" in names
        blob = "\n".join(zf.read(n).decode("utf-8", "replace") for n in names)

    for secret in (TOKEN, PASSWORD, config_dict()["supabase_anon_key"]):
        assert secret not in blob, "a secret reached the diagnostics zip"
        assert secret[:24] not in blob, "a secret fragment reached the zip"

    # Falsifier: the logs really did contain the secrets before masking.
    assert "***supabase_agent_token***" in blob
    assert "***sql.password***" in blob


def test_a_missing_config_does_not_stop_the_zip_being_built(tmp_path,
                                                            monkeypatch):
    """The machine whose config is broken is exactly the machine whose
    diagnostics we most need."""
    work = tmp_path / "install"
    (work / "logs").mkdir(parents=True)
    monkeypatch.setattr(collect_diagnostics, "HERE", work)
    monkeypatch.setattr(collect_diagnostics, "COPY_TEXT", ())
    for name in ("versions", "odbc", "folder_listing"):
        monkeypatch.setattr(collect_diagnostics, name, lambda: "(skipped)")
    monkeypatch.setattr(collect_diagnostics, "manifest_check", lambda: "(skipped)")
    monkeypatch.setattr(collect_diagnostics, "task_state", lambda: ("", ""))
    monkeypatch.setattr(collect_diagnostics, "cloud", lambda _p: "(skipped)")
    monkeypatch.setattr(collect_diagnostics, "readonly_proof", lambda _p: "(skipped)")

    out = collect_diagnostics.collect(tmp_path / "diag.zip")
    with zipfile.ZipFile(out) as zf:
        assert "no config.json" in zf.read("config.redacted.json").decode()


# ════════════════════════════════════════════════════════════════
# the one click must not skip a check
# ════════════════════════════════════════════════════════════════

def test_the_installer_runs_the_same_preflight_we_run_by_hand():
    """One implementation of the gate, two callers. A second
    implementation is a second thing that can disagree with the first."""
    source = Path(installer.__file__).read_text(encoding="utf-8")
    assert "preflight.run_steps_0_to_4" in source
    assert "classify_dry_run" not in source, (
        "the installer is re-deriving the step 4 verdict instead of using "
        "preflight's")


def test_phase_e_requires_both_a_task_run_and_a_new_heartbeat():
    """Either fact alone can lie: a task can run and upload nothing, and a
    heartbeat can be left over from the manual cycle in Phase B."""
    source = Path(installer.__file__).read_text(encoding="utf-8")
    body = source.split("def phase_e_prove_it_runs_itself")[1]
    assert "LastTaskResult" in body
    assert "latest_heartbeat" in body
    assert "newest != baseline" in body


def test_skip_wait_says_the_install_is_unverified():
    """--skip-wait exists for our own rehearsals. It must never look like
    a successful install."""
    source = Path(installer.__file__).read_text(encoding="utf-8")
    assert "THIS INSTALL IS NOT VERIFIED" in source


def test_everything_the_installer_needs_is_shipped():
    import make_ship as S
    shipped = {name for name, _ in S.SHIPPED}
    for required in ("INSTALL.bat", "installer.py", "collect_diagnostics.bat",
                     "collect_diagnostics.py", "logsetup.py", "sqlguard.py",
                     "readonly_probe.py"):
        assert required in shipped, f"{required} is not in ship/"
