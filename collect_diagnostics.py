# -*- coding: utf-8 -*-
"""
collect_diagnostics.py — one click, one file, no conversation
================================================================
Three weeks after we leave, something misbehaves. Nobody was watching.
The answer to "it stopped working" has to be one double-click and one
file, not a series of questions asked over a phone to someone standing in
a restaurant.

So this collects everything that could matter into a single zip:

    install transcripts        every phase of every install attempt
    agent.log + rotations      every cycle, what it read, what it skipped
    config.redacted.json       the shape, with the secrets replaced
    versions.txt               Python, packages, Windows
    odbc.txt                   installed drivers, and which one we pick
    task.xml / task_info.txt   what the scheduler holds, and its last result
    state.json                 the local watermark
    cloud.txt                  sync_state + the last heartbeats
    manifest_check.txt         whether this machine runs the code we shipped
    readonly_proof.txt         the POS refusing our writes, freshly re-run
    folder.txt                 every file here, with size and mtime

🔴 **No secrets.** Not the token, not the SQL password, not a connection
   string. Two mechanisms, because one of them is remembering and that is
   not a mechanism:

     1. `config.json` is never copied. A redacted version is generated,
        which keeps every key and replaces every secret VALUE with its
        length and a short sha256 prefix — enough to answer "is this the
        token we issued?" without disclosing it.
     2. Every text file that goes in is passed through `logsetup.mask`
        on the way, and `test_diagnostics.py` unzips the result and greps
        it for every secret in config.json.

    double-click collect_diagnostics.bat
    python collect_diagnostics.py --out somewhere.zip
================================================================
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import platform
import subprocess
import sys
import zipfile
from pathlib import Path

import logsetup

HERE = Path(__file__).resolve().parent
TASK_NAME = "thirdeyev"

# Files copied verbatim (after masking). Anything not on this list is not
# collected — an allowlist, so a file dropped into this folder later
# cannot be swept up and sent without anyone deciding to send it.
COPY_TEXT = (
    "state.json",
    "MANIFEST.txt",
)

MAX_BYTES_PER_FILE = 8 * 1024 * 1024


def run(argv: list[str], timeout: int = 120) -> str:
    """A command's combined output, or the reason there is none."""
    try:
        done = subprocess.run(argv, cwd=str(HERE), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(could not run {argv[0]}: {type(exc).__name__}: {exc})"
    return (done.stdout or "") + (done.stderr or "")


def redacted_config(path: Path) -> str:
    """config.json with every secret value replaced.

    The length and a sha256 prefix are kept deliberately. "The token is 219
    characters and hashes to 4f2a9c1e" answers *is this the token we
    issued* — which is a real question when an install has gone wrong —
    while disclosing nothing that could be used.
    """
    if not path.exists():
        return "(no config.json on this machine)"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"(config.json is unreadable: {exc})"

    secrets = {label for label, _ in logsetup.secret_values(raw)}

    def walk(node, prefix: str = ""):
        out = {}
        for key, value in node.items():
            label = f"{prefix}{key}"
            if isinstance(value, dict):
                out[key] = walk(value, f"{label}.")
            elif label in secrets and isinstance(value, str):
                digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
                out[key] = (f"<redacted: {len(value)} chars, "
                            f"sha256:{digest}>")
            else:
                out[key] = value
        return out

    return json.dumps(walk(raw), indent=2, ensure_ascii=False)


def versions() -> str:
    lines = [
        f"collected           {_dt.datetime.now().isoformat(timespec='seconds')}",
        f"python              {sys.version}",
        f"python executable   {sys.executable}",
        f"platform            {platform.platform()}",
        f"machine             {platform.machine()}",
        f"folder              {HERE}",
        "",
        "--- pip freeze ---",
        run([sys.executable, "-m", "pip", "freeze",
             "--disable-pip-version-check"]),
    ]
    return "\n".join(lines)


def odbc() -> str:
    probe = (
        "import pyodbc, adapter_hdsoft as a;"
        "print('pyodbc', pyodbc.version);"
        "print('drivers:');"
        "[print('   ', d) for d in pyodbc.drivers()];"
        "print('picked', a.pick_driver())"
    )
    return run([sys.executable, "-c", probe])


def task_state() -> tuple[str, str]:
    """What the scheduler holds, and how its last run went.

    Both matter and they answer different questions: the XML says what we
    asked for, the info says what actually happened. A task that is
    registered perfectly and has never run is the exact failure this
    product hit once, and only the second file shows it.
    """
    xml = run(["schtasks.exe", "/Query", "/TN", TASK_NAME, "/XML", "ONE"])
    info = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"Get-ScheduledTask -TaskName '{TASK_NAME}' "
                "-ErrorAction SilentlyContinue | "
                "Get-ScheduledTaskInfo | Format-List * ; "
                f"Get-ScheduledTask -TaskName '{TASK_NAME}' "
                "-ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty Triggers | Format-List *"])
    return xml, info


def cloud(config_path: Path) -> str:
    """sync_state and the last heartbeats, read with the agent's own token.

    Read-only, and it fails soft: a machine that has lost its network is
    exactly the machine whose diagnostics we most need, so an unreachable
    cloud must not stop the zip from being produced.
    """
    try:
        import agent
        import supa
        cfg = agent.Config.load(config_path)
    except Exception as exc:                            # noqa: BLE001
        return f"(could not load config: {type(exc).__name__}: {exc})"

    try:
        client = supa.Supa(cfg.supabase_url, anon_key=cfg.supabase_anon_key,
                           token=cfg.supabase_agent_token)
        scope = {"tenant_id": f"eq.{cfg.tenant_id}",
                 "source_id": f"eq.{cfg.source_id}"}
        state = client.select("sync_state", {**scope, "select": "*"})
        beats = client.select("heartbeats", {
            **scope,
            "select": "at,ok,drift_seconds,rows_pulled,agent_version,note",
            "order": "at.desc", "limit": "50"}, paginate=False)
        counts = {name: client.count(name, scope) for name in
                  ("invoices", "invoice_lines", "cash_counts",
                   "pos_products", "pos_users")}
    except Exception as exc:                            # noqa: BLE001
        return (f"(could not reach the cloud: {type(exc).__name__}: {exc})\n"
                "This is itself a finding: the agent cannot upload either.")

    return "\n".join([
        "--- row counts ---",
        json.dumps(counts, indent=2),
        "",
        "--- sync_state ---",
        json.dumps(state, indent=2, ensure_ascii=False, default=str),
        "",
        "--- last 50 heartbeats (newest first) ---",
        json.dumps(beats, indent=2, ensure_ascii=False, default=str),
    ])


def manifest_check() -> str:
    try:
        import preflight
        return preflight.verify_manifest(HERE)
    except Exception as exc:                            # noqa: BLE001
        return f"code integrity   COULD NOT CHECK — {type(exc).__name__}: {exc}"


def readonly_proof(config_path: Path) -> str:
    """Re-run the write probes now, so the zip carries current evidence.

    Deliberately re-run rather than copied out of the install transcript:
    the question three weeks later is whether the POS still refuses us
    *today*, and an install transcript answers what was true on the day of
    the visit.
    """
    try:
        import adapter_hdsoft
        import agent
        import readonly_probe
        cfg = agent.Config.load(config_path)
    except Exception as exc:                            # noqa: BLE001
        return f"(could not load: {type(exc).__name__}: {exc})"

    try:
        cn = adapter_hdsoft.connect(**cfg.sql)
    except Exception as exc:                            # noqa: BLE001
        return (f"(could not connect to the POS: {type(exc).__name__}: {exc})\n"
                "This is itself a finding: the agent cannot read either.")
    try:
        return readonly_probe.format_report(readonly_probe.run(cn))
    finally:
        try:
            cn.close()
        except Exception:                               # pragma: no cover
            pass


def folder_listing() -> str:
    lines = [f"{'size':>12}  {'modified':<20}  name"]
    for path in sorted(HERE.rglob("*")):
        if any(part in ("__pycache__", ".git", ".pytest_cache")
               for part in path.parts):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        when = _dt.datetime.fromtimestamp(stat.st_mtime).isoformat(
            timespec="seconds")
        rel = path.relative_to(HERE)
        lines.append(f"{stat.st_size:>12}  {when:<20}  "
                     f"{rel}{'/' if path.is_dir() else ''}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# building the zip
# ════════════════════════════════════════════════════════════════

def collect(out_path: Path, config_name: str = "config.json") -> Path:
    config_path = HERE / config_name

    # Registers the secrets so mask() knows what to remove. Done before
    # anything is read, and it is why a config that cannot be parsed is
    # still safe: an unparseable config registers nothing, but it also
    # contributes nothing to the zip.
    try:
        logsetup.register_config_secrets(
            json.loads(config_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass

    generated: dict[str, str] = {
        "README.txt": (
            "POSentine diagnostics\n"
            "=====================\n"
            f"collected {_dt.datetime.now().isoformat(timespec='seconds')}\n"
            f"from      {HERE}\n\n"
            "This archive contains NO password, NO agent token and NO\n"
            "connection string. config.json is not included; a redacted\n"
            "version is, carrying each secret's length and a sha256 prefix\n"
            "so it can be identified without being disclosed.\n\n"
            "Start with readonly_proof.txt and agent_logs/agent.log.\n"),
        "versions.txt": versions(),
        "odbc.txt": odbc(),
        "config.redacted.json": redacted_config(config_path),
        "manifest_check.txt": manifest_check(),
        "cloud.txt": cloud(config_path),
        "readonly_proof.txt": readonly_proof(config_path),
        "folder.txt": folder_listing(),
    }
    generated["task.xml"], generated["task_info.txt"] = task_state()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in generated.items():
            zf.writestr(name, logsetup.mask(body))

        for name in COPY_TEXT:
            source = HERE / name
            if source.exists():
                zf.writestr(name, logsetup.mask(_read(source)))

        for log in logsetup.log_files(HERE / "agent.log"):
            zf.writestr(f"agent_logs/{log.name}", logsetup.mask(_read(log)))

        for transcript in sorted((HERE / "logs").glob("install_*.txt")):
            zf.writestr(f"install_logs/{transcript.name}",
                        logsetup.mask(_read(transcript)))

    return out_path


def _read(path: Path) -> str:
    """Text, bounded. A log we somehow failed to rotate must not turn the
    diagnostics zip into a second disk problem."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return f"(could not read {path.name}: {exc})"
    truncated = b""
    if len(raw) > MAX_BYTES_PER_FILE:
        truncated = (f"\n\n[... {len(raw) - MAX_BYTES_PER_FILE} bytes trimmed "
                     f"from the middle of this file ...]\n\n").encode("utf-8")
        half = MAX_BYTES_PER_FILE // 2
        raw = raw[:half] + truncated + raw[-half:]
    return raw.decode("utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Collect everything needed to diagnose this machine "
                    "into one zip. Contains no secrets.")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args(argv)

    logsetup.configure_streams()
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = args.out or (HERE / f"diagnostics_{stamp}.zip")

    print("=" * 66)
    print("  POSentine — collecting diagnostics")
    print("=" * 66)
    print("  This reads only. It changes nothing on this machine.")
    print("  It re-runs the read-only proof against the POS, which")
    print("  attempts writes that carry WHERE 1 = 0 and must be refused.")
    print()

    try:
        path = collect(out, args.config)
    except Exception as exc:                            # noqa: BLE001
        print(f"  FAILED to build the archive: {type(exc).__name__}: {exc}")
        return 1

    size_kb = path.stat().st_size / 1024
    print("=" * 66)
    print(f"  Wrote {path}")
    print(f"  {size_kb:,.0f} KB")
    print()
    print("  Send this one file. It contains no password and no token.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
