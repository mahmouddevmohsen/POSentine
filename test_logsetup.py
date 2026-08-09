# -*- coding: utf-8 -*-
"""
test_logsetup.py — the log is bounded, and carries no secret
================================================================
Two claims that fail silently if nobody checks them, and both of them are
faults we would have caused ourselves:

  • A log that fills a till's disk. The cap is arithmetic, so it is
    checked as arithmetic — write far more than the ceiling and measure
    what is left on disk.

  • A credential in a file we ask the customer to send us. Redaction is
    done at the formatter rather than at the call sites, because the call
    sites are where remembering happens. The test that matters here is the
    one the architect asked for by name: produce real logs from a real
    cycle and grep them for every secret in config.json.
================================================================
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

import logsetup

HERE = Path(__file__).resolve().parent

# Distinctive enough that a match cannot be a coincidence, and shaped like
# the real things: a JWT, a JWT, and a password with punctuation in it.
TOKEN = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoiYXV0aGVudGljYXRlZCI"
         "sInRlbmFudF9pZCI6IjU3YjYxYjQ3LWE1OTAtNDlmZS04MDNjLTBjMTc0YTA3YjdlYyJ9"
         ".ZZZZsignatureZZZZ_agent_token_marker")
ANON = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoiYW5vbiJ9"
        ".YYYYsignatureYYYY_anon_key_marker")
PASSWORD = "Tr0ub4dor&3-monitor-ro-marker"


@pytest.fixture(autouse=True)
def clean_secret_registry():
    logsetup.forget_secrets()
    yield
    logsetup.forget_secrets()


def config_dict() -> dict:
    return {
        "tenant_id": "57b61b47-a590-49fe-803c-0c174a07b7ec",
        "source_id": "93f8d146-ba68-4d58-8eda-f797f3e28bd4",
        "supabase_url": "https://example.supabase.co",
        "supabase_anon_key": ANON,
        "supabase_agent_token": TOKEN,
        "sql": {"server": "localhost\\HDSOFT", "database": "HD_Rest_Cashier",
                "user": "monitor_ro", "password": PASSWORD},
    }


def every_secret() -> list[str]:
    return [TOKEN, ANON, PASSWORD]


# ════════════════════════════════════════════════════════════════
# what counts as a secret
# ════════════════════════════════════════════════════════════════

def test_it_finds_every_secret_in_a_config_including_nested_ones():
    found = dict(logsetup.secret_values(config_dict()))
    assert found["supabase_agent_token"] == TOKEN
    assert found["supabase_anon_key"] == ANON
    assert found["sql.password"] == PASSWORD


def test_it_does_not_treat_the_url_or_the_tenant_id_as_secret():
    """Over-masking has a cost too: a log that hides the tenant_id cannot
    be used to work out which shop it came from."""
    labels = dict(logsetup.secret_values(config_dict()))
    assert "supabase_url" not in labels
    assert "tenant_id" not in labels


def test_a_field_added_later_is_covered_without_anyone_remembering():
    """The key-name heuristic is the point: `service_token` was never
    written into a list here, and it is still masked."""
    found = dict(logsetup.secret_values({"service_token": "abcdef123456"}))
    assert found == {"service_token": "abcdef123456"}


# ════════════════════════════════════════════════════════════════
# masking
# ════════════════════════════════════════════════════════════════

def test_a_whole_secret_is_replaced_and_named():
    logsetup.register_config_secrets(config_dict())
    masked = logsetup.mask(f"Authorization: Bearer {TOKEN}")
    assert TOKEN not in masked
    assert "***supabase_agent_token***" in masked


def test_a_truncated_secret_is_still_masked():
    """🔴 The one that matters. Error text is cut at 500 characters on its
    way to the cloud and wrapped by terminals, and half a token is exactly
    as leaked as a whole one."""
    logsetup.register_config_secrets(config_dict())
    half = TOKEN[:60]
    masked = logsetup.mask(f"failed with token {half}")
    assert half not in masked
    assert "***supabase_agent_token***" in masked


def test_a_connection_string_loses_its_password():
    logsetup.register_config_secrets(config_dict())
    masked = logsetup.mask(
        f"DRIVER={{ODBC Driver 17}};SERVER=x;UID=monitor_ro;PWD={PASSWORD};")
    assert PASSWORD not in masked
    assert "***sql.password***" in masked


def test_masking_survives_an_exception_traceback(tmp_path):
    """LOG.exception renders its traceback inside the formatter, which is
    why redaction lives there and not at the call sites."""
    logsetup.register_config_secrets(config_dict())
    log_path = tmp_path / "agent.log"
    logsetup.configure(log_path)
    try:
        raise RuntimeError(f"connect failed for PWD={PASSWORD}")
    except RuntimeError:
        logging.getLogger("t").exception("cycle failed")
    logging.shutdown()

    text = log_path.read_text(encoding="utf-8")
    assert PASSWORD not in text
    assert "***sql.password***" in text
    assert "Traceback" in text, "the traceback itself must still be there"


def test_a_secret_too_short_to_mask_is_reported_loudly(caplog):
    """A two-character password cannot be blanked without shredding the
    log. Refusing to start would strand an install at the counter, so it
    is loud instead — and never silent."""
    assert not logsetup.register_secret("sql.password", "ab")
    logsetup.configure(None)
    assert "sql.password" in logsetup._SHORT_SECRETS


def test_the_longest_secret_is_masked_first():
    """If one credential contains another, masking the short one first
    would leave the remainder of the long one on the line."""
    logsetup.register_secret("short", "abcdef1234")
    logsetup.register_secret("long", "abcdef1234567890XYZ")
    masked = logsetup.mask("value=abcdef1234567890XYZ")
    assert "abcdef1234567890XYZ" not in masked
    assert "567890XYZ" not in masked


# ════════════════════════════════════════════════════════════════
# the cap
# ════════════════════════════════════════════════════════════════

def test_the_size_cap_actually_holds(tmp_path, monkeypatch):
    """Write far past the ceiling and measure what survives.

    Scaled down so the test is fast; the arithmetic is what is being
    checked, and it is the same arithmetic at 2 MiB.
    """
    monkeypatch.setattr(logsetup, "LOG_MAX_BYTES", 4096)
    monkeypatch.setattr(logsetup, "LOG_BACKUP_COUNT", 3)

    log_path = tmp_path / "agent.log"
    logsetup.configure(log_path)
    log = logging.getLogger("cap")
    record = "x" * 200
    for n in range(2000):                       # ~400 KB of records
        log.info("%d %s", n, record)
    logging.shutdown()

    files = logsetup.log_files(log_path)
    total = logsetup.total_log_bytes(log_path)

    assert len(files) == 4, [f.name for f in files]      # live + 3 backups
    # Each file can exceed maxBytes by at most the record that triggered
    # the rollover, so the ceiling is (backups + 1) * (max + one record).
    ceiling = 4 * (4096 + 1024)
    assert total <= ceiling, f"{total} bytes on disk, ceiling {ceiling}"
    assert total > 4096, "nothing was written; the test proved nothing"


def test_the_newest_records_are_the_ones_kept(tmp_path, monkeypatch):
    """A cap that discarded the newest lines would be worse than no cap:
    the last thing the agent did before it stopped is the whole point."""
    monkeypatch.setattr(logsetup, "LOG_MAX_BYTES", 2048)
    monkeypatch.setattr(logsetup, "LOG_BACKUP_COUNT", 1)

    log_path = tmp_path / "agent.log"
    logsetup.configure(log_path)
    log = logging.getLogger("newest")
    for n in range(500):
        log.info("record %04d %s", n, "y" * 100)
    logging.shutdown()

    assert "record 0499" in log_path.read_text(encoding="utf-8")


def test_a_rollover_that_cannot_happen_does_not_lose_the_record(
        tmp_path, monkeypatch):
    """Two cycles can overlap after a stale-lock takeover, and on Windows a
    rename fails while another process holds the file. Taking down a cycle
    to tidy a log file would be the wrong trade."""
    monkeypatch.setattr(logsetup, "LOG_MAX_BYTES", 512)
    monkeypatch.setattr(logsetup, "LOG_BACKUP_COUNT", 2)

    log_path = tmp_path / "agent.log"
    logsetup.configure(log_path)
    handler = next(h for h in logging.getLogger().handlers
                   if isinstance(h, logsetup.CappedRotatingFileHandler))

    def refuse(*_args, **_kwargs):
        raise PermissionError("another process holds the log")

    monkeypatch.setattr(logsetup.RotatingFileHandler, "doRollover", refuse)

    log = logging.getLogger("contended")
    for n in range(50):
        log.info("still writing %d %s", n, "z" * 100)
    logging.shutdown()

    text = log_path.read_text(encoding="utf-8")
    assert "still writing 49" in text, "records were lost to a failed rollover"
    assert "rotation skipped" in text, "the skipped rotation was not recorded"


# ════════════════════════════════════════════════════════════════
# 🔴 the one the architect asked for by name
# ════════════════════════════════════════════════════════════════

PRODUCE_LOGS = r'''
import datetime, json, pathlib, sys
sys.path.insert(0, sys.argv[2])
import agent, fake_adapter, logsetup, supa

work = pathlib.Path(sys.argv[1])
agent.configure_output(work / "agent.log")
cfg = agent.Config.load(work / "config.json")

class Cloud:
    """Fails on purpose. A healthy run has few chances to leak a secret;
    a failing one builds error strings out of whatever is to hand."""
    def upsert(self, t, rows, on_conflict):
        raise supa.SupaError(
            "POST %s failed: HTTP 401 {\"message\":\"JWS: invalid\"} "
            "apikey=%s Authorization=Bearer %s"
            % (t, cfg.supabase_anon_key, cfg.supabase_agent_token))
    def update(self, t, f, p, returning=True): return [p]
    def insert(self, t, rows, returning=True):
        pathlib.Path(sys.argv[1], "sent_to_cloud.json").write_text(
            json.dumps(rows, ensure_ascii=False, default=str), encoding="utf-8")
        return []
    def select(self, t, params=None, paginate=True):
        return [{"watermark_salid": 1000, "rescan_from_salid": 1000}]
    def count(self, t, params=None): return 0

agent.run_once(cfg, agent.State(initialised=True), work / "state.json",
               fake_adapter, Cloud(),
               datetime.datetime(2026, 8, 9, 3, 0, tzinfo=datetime.timezone.utc),
               False)

# And a connection-string style failure, which is where a SQL password
# would surface if one ever did.
import logging
try:
    raise RuntimeError(
        "[08001] SQLDriverConnect DRIVER={ODBC Driver 17};UID=%s;PWD=%s"
        % (cfg.sql["user"], cfg.sql["password"]))
except RuntimeError as exc:
    logging.getLogger("posentine.agent").exception("connect failed: %s", exc)
logging.shutdown()
print("###DONE###")
'''


def test_no_secret_from_config_json_appears_in_any_produced_log(tmp_path):
    """Run a real cycle that fails in the ways most likely to leak, then
    grep every file it produced for every secret in config.json.

    Not a unit test of mask() — that is above. This runs the actual agent,
    through the actual logging configuration, and reads the actual files.
    """
    work = tmp_path / "install"
    work.mkdir()
    (work / "config.json").write_text(json.dumps(config_dict()),
                                      encoding="utf-8")

    script = tmp_path / "produce.py"
    script.write_text(PRODUCE_LOGS, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script), str(work), str(HERE)],
        cwd=str(work), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    assert "###DONE###" in result.stdout, result.stdout + result.stderr

    produced = [p for p in work.rglob("*") if p.is_file()
                and p.name != "config.json"]
    assert produced, "nothing was produced; this test proved nothing"

    offenders: list[str] = []
    for path in produced:
        text = path.read_text(encoding="utf-8", errors="replace")
        for secret in every_secret():
            if secret in text:
                offenders.append(f"{path.name}: whole secret")
            # Truncation is how a secret usually escapes, so a leading
            # fragment counts as a leak too.
            elif len(secret) > 24 and secret[:24] in text:
                offenders.append(f"{path.name}: leading fragment")
    assert not offenders, (
        "secrets from config.json reached the logs:\n  "
        + "\n  ".join(sorted(set(offenders))))

    # The falsifier: if the grep found nothing because nothing was logged,
    # this test would pass while proving nothing.
    log_text = (work / "agent.log").read_text(encoding="utf-8")
    assert "upload failed" in log_text, "the failing path never ran"
    assert "***supabase_agent_token***" in log_text, (
        "the token was never in the log to begin with, so its absence "
        "proves nothing about masking")


def test_the_leak_test_would_notice_an_unmasked_secret(tmp_path):
    """Falsifier for the test above. With redaction disabled, the same run
    must leave the token in the file."""
    work = tmp_path / "install"
    work.mkdir()
    (work / "config.json").write_text(json.dumps(config_dict()),
                                      encoding="utf-8")

    # Neuter mask() in the child only.
    disabled = PRODUCE_LOGS.replace(
        "import agent, fake_adapter, logsetup, supa",
        "import logsetup\nlogsetup.mask = lambda text: text\n"
        "import agent, fake_adapter, supa")
    script = tmp_path / "produce_leaky.py"
    script.write_text(disabled, encoding="utf-8")

    subprocess.run([sys.executable, str(script), str(work), str(HERE)],
                   cwd=str(work), capture_output=True, text=True,
                   encoding="utf-8", errors="replace")

    text = (work / "agent.log").read_text(encoding="utf-8", errors="replace")
    assert TOKEN in text, (
        "with masking disabled the token still did not reach the log, so "
        "the test above is not exercising the path it claims to")
