# -*- coding: utf-8 -*-
"""
test_preflight.py — the checks that decide whether an install is safe
================================================================
preflight.bat is thin on purpose; everything it decides lives in
preflight.py, and this is why. The judgements below are the ones that
would otherwise be made by a person reading a screen with a queue behind
them.

Two of them do not exist in VERIFY.md as automatic checks at all, and are
the reason this file is worth its weight:

  • agent.py exits 0 after printing an ABORT block. Treating exit 0 as a
    pass would turn "we are reading their data wrong" into a green tick.
  • A dry run at watermark 0 with invoices behind it prints VERDICT: PASS,
    because it compares the whole table against the whole table. The
    verdict is real; it is also two identical wrong answers.
================================================================
"""

from __future__ import annotations

import hashlib
import re

import pytest

import preflight as P


# ════════════════════════════════════════════════════════════════
# blocks agent.py actually prints
# ════════════════════════════════════════════════════════════════

def dry_run_block(watermark: int = 1041, invoices: int = 42,
                  verdict: str = "PASS", delta: int = 2,
                  capped: bool = False) -> str:
    """The shape print_dry_run() produces, kept faithful to it."""
    cap_lines = ""
    if capped:
        cap_lines = ("  ⚠ CEILING HIT       this cycle read its maximum of "
                     "5000 new invoices;\n"
                     "                       a backlog is still waiting behind it\n\n")
    return (
        "=" * 62 + "\n"
        f"POSentine agent — {P.DRY_RUN_MARK}\n"
        + "=" * 62 + "\n"
        "  ODBC driver          ODBC Driver 17 for SQL Server\n"
        "  schema check         OK (all required columns present)\n"
        f"  watermark_salid      {watermark}\n"
        "  POS MAX(salid)       218207\n"
        "  POS clock            2026-08-09 03:00:00\n"
        "  rescan this cycle    False\n"
        "  reference this cycle False\n"
        "\n"
        + cap_lines +
        f"  invoices to upload   {invoices}\n"
        "  lines to upload      96\n"
        "  cash counts          2\n"
        "  products in snapshot 412\n"
        "  sold_at range        2026-08-09 07:02:00  ->  2026-08-09 18:55:00\n"
        "\n"
        "  invoice kinds\n"
        "    cash       40\n"
        "    external   1\n"
        "    return     1\n"
        "    other      0\n"
        "\n"
        "  ✔ every line matched a product; no NULL list_price\n"
        "  Cross-check against a bare count of the same tables\n"
        f"    bare COUNT(*) invoices   {invoices + delta}\n"
        f"    agent would read         {invoices}\n"
        f"    delta                    {delta}\n"
        "    bare COUNT(*) lines      2310\n"
        "\n"
        f"  VERDICT: {verdict} — delta {delta}, within the tolerance of 5.\n"
        + "=" * 62 + "\n"
    )


FIRST_RUN_BLOCK = (
    "=" * 62 + "\n"
    f"POSentine agent — {P.FIRST_RUN_MARK}\n"
    + "=" * 62 + "\n"
    "  This machine has no state yet. A real run would adopt\n"
    "  watermark_salid = 218207 (the POS MAX(salid)) and read\n"
    "  nothing behind it.\n"
    "\n"
    "  Expected here: 'invoices to upload' is 0 on a first install.\n"
    "  A number in the thousands means STOP AND CALL.\n"
    + "=" * 62 + "\n"
)


# ════════════════════════════════════════════════════════════════
# step 4 — the verdict is the block, not the exit code
# ════════════════════════════════════════════════════════════════

def test_a_clean_dry_run_passes():
    passed, what, _do = P.classify_dry_run(dry_run_block())
    assert passed
    assert "VERDICT: PASS" in what


def test_an_abort_verdict_fails():
    passed, what, do = P.classify_dry_run(
        dry_run_block(verdict="ABORT", delta=97))
    assert not passed
    assert "ABORT" in what
    assert "call" in do.lower()


def test_a_capped_verdict_fails():
    passed, what, _do = P.classify_dry_run(
        dry_run_block(verdict="CAPPED", capped=True))
    assert not passed
    assert "CAPPED" in what


def test_a_first_run_block_passes_and_is_not_read_as_a_dry_run():
    passed, what, do = P.classify_dry_run(FIRST_RUN_BLOCK)
    assert passed
    assert "FIRST RUN" in what
    assert "step 6" in do


def test_a_first_run_block_that_also_reports_uploads_fails():
    """Impossible by construction in agent.py — so if it ever appears, the
    agent is not behaving as verified and nothing here should be believed."""
    passed, what, _do = P.classify_dry_run(
        FIRST_RUN_BLOCK + "  invoices to upload   218207\n")
    assert not passed
    assert "invoices to upload" in what


def test_the_zero_watermark_trap_fails_even_though_the_verdict_says_pass():
    """The one failure VERIFY.md warns the step-5 verdict cannot catch.

    watermark 0 means the cross-check compares the whole table against the
    whole table. It agrees with itself and prints PASS. Both numbers are
    wrong, and the agent is about to pull 218,207 invoices during service.
    """
    block = dry_run_block(watermark=0, invoices=218207, delta=0)
    assert "VERDICT: PASS" in block          # the trap is real
    passed, what, do = P.classify_dry_run(block)
    assert not passed
    assert "watermark_salid is 0" in what
    assert "entire history" in do
    assert "state.json" in do


def test_a_zero_watermark_with_nothing_behind_it_is_not_the_trap():
    """A shop whose POS is genuinely empty adopts watermark 0 legitimately.
    Failing that install would be a false alarm at the counter."""
    passed, _what, _do = P.classify_dry_run(
        dry_run_block(watermark=0, invoices=0, delta=0))
    assert passed


def test_a_block_with_no_verdict_line_fails():
    block = dry_run_block().replace("  VERDICT: PASS — delta 2, "
                                    "within the tolerance of 5.\n", "")
    passed, what, _do = P.classify_dry_run(block)
    assert not passed
    assert "no VERDICT" in what


def test_unrecognised_output_fails_rather_than_passing_quietly():
    passed, what, _do = P.classify_dry_run("Traceback (most recent call last):")
    assert not passed
    assert "no recognisable block" in what


def test_a_block_missing_its_fields_fails():
    block = dry_run_block().replace("  watermark_salid      1041\n", "")
    passed, what, _do = P.classify_dry_run(block)
    assert not passed
    assert "watermark_salid" in what


def test_fields_are_read_from_the_block():
    block = dry_run_block(watermark=99, invoices=7)
    assert P._field(block, "watermark_salid") == 99
    assert P._field(block, "invoices to upload") == 7
    assert P._field(block, "not a field") is None


# ════════════════════════════════════════════════════════════════
# step 3 — the sql block Config.load does not inspect
# ════════════════════════════════════════════════════════════════

def test_a_complete_sql_block_passes():
    P.check_sql_block({"server": "localhost\\HDSOFT", "database": "HD_Rest_Cashier",
                       "user": "monitor_ro", "password": "s3cret"})


@pytest.mark.parametrize("dropped", P.SQL_KEYS)
def test_a_missing_sql_field_stops_at_step_3(dropped):
    sql = {"server": "s", "database": "d", "user": "u", "password": "p"}
    del sql[dropped]
    with pytest.raises(P.Stop) as exc:
        P.check_sql_block(sql)
    assert exc.value.step == P.STEP_3
    assert dropped in exc.value.what


def test_an_unfilled_sql_placeholder_stops_at_step_3():
    """Config.load only inspects top-level strings, so this one reaches
    connect() and reads like a network fault during step 4."""
    with pytest.raises(P.Stop) as exc:
        P.check_sql_block({"server": "s", "database": "d", "user": "u",
                           "password": "<monitor_ro password>"})
    assert "placeholder" in exc.value.what
    assert "password" in exc.value.what


# ════════════════════════════════════════════════════════════════
# step 3 — token failures say which claim is wrong
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("message, expected", [
    ("agent token has role='service_role', must be 'authenticated'.",
     "DO NOT run the agent"),
    ("agent token is for tenant X, but config says Y.", "zero rows"),
    ("agent token carries no tenant_id claim", "Re-mint"),
    ("not a readable JWT: bad padding", "truncated"),
    ("config still has unfilled placeholder values: ['supabase_agent_token']",
     "angle bracket"),
    ("config is missing required keys: ['source_id']", "missing keys"),
])
def test_each_token_failure_gets_its_own_instruction(message, expected):
    assert expected in P._token_guidance(message)


def test_an_unmapped_message_still_gets_an_instruction():
    assert P._token_guidance("something new") == "Fix config.json and run this again."


# ════════════════════════════════════════════════════════════════
# step 0 — is this the code we verified?
# ════════════════════════════════════════════════════════════════

def _ship(tmp_path, files: dict[str, str], manifest: bool = True):
    # write_bytes, not write_text: on Windows write_text turns "\n" into
    # "\r\n", and the manifest would then describe bytes that are not on
    # disk. make_ship.py copies binary for the same reason.
    for name, body in files.items():
        (tmp_path / name).write_bytes(body.encode("utf-8"))
    if manifest:
        lines = ["# built by make_ship.py"]
        lines += [f"{hashlib.sha256(body.encode('utf-8')).hexdigest()}  {name}"
                  for name, body in files.items()]
        (tmp_path / P.MANIFEST_NAME).write_text("\n".join(lines) + "\n",
                                                encoding="utf-8")
    return tmp_path


def test_an_intact_folder_verifies(tmp_path):
    root = _ship(tmp_path, {"agent.py": "print(1)\n", "rows.py": "x = 2\n"})
    assert "OK — 2 files" in P.verify_manifest(root)


def test_an_edited_file_stops_before_anything_else_runs(tmp_path):
    root = _ship(tmp_path, {"agent.py": "print(1)\n"})
    (root / "agent.py").write_bytes(b"print(2)\n")
    with pytest.raises(P.Stop) as exc:
        P.verify_manifest(root)
    assert "agent.py" in exc.value.what
    assert "Re-copy the ship folder" in exc.value.do


def test_a_missing_file_stops(tmp_path):
    root = _ship(tmp_path, {"agent.py": "print(1)\n", "supa.py": "y = 1\n"})
    (root / "supa.py").unlink()
    with pytest.raises(P.Stop) as exc:
        P.verify_manifest(root)
    assert "supa.py" in exc.value.what


def test_files_created_on_the_customer_machine_are_not_treated_as_drift(tmp_path):
    """config.json, state.json and agent.log all appear after packaging."""
    root = _ship(tmp_path, {"agent.py": "print(1)\n"})
    (root / "config.json").write_text("{}", encoding="utf-8")
    (root / "state.json").write_text("{}", encoding="utf-8")
    (root / "agent.log").write_text("cycle ok", encoding="utf-8")
    assert "OK — 1 files" in P.verify_manifest(root)


def test_a_folder_with_no_manifest_says_so_rather_than_passing(tmp_path):
    root = _ship(tmp_path, {"agent.py": "print(1)\n"}, manifest=False)
    status = P.verify_manifest(root)
    assert "NOT VERIFIED" in status
    assert "OK" not in status


def test_an_unreadable_manifest_stops(tmp_path):
    root = _ship(tmp_path, {"agent.py": "print(1)\n"})
    (root / P.MANIFEST_NAME).write_text("garbage-with-no-filename\n",
                                        encoding="utf-8")
    with pytest.raises(P.Stop) as exc:
        P.verify_manifest(root)
    assert "unreadable" in exc.value.what


def test_an_empty_manifest_is_a_failure_not_a_pass(tmp_path):
    root = _ship(tmp_path, {"agent.py": "print(1)\n"})
    (root / P.MANIFEST_NAME).write_text("# nothing here\n", encoding="utf-8")
    with pytest.raises(P.Stop) as exc:
        P.verify_manifest(root)
    assert "lists no files" in exc.value.what


# ════════════════════════════════════════════════════════════════
# step 4 — the failures VERIFY.md has a table for
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("marker", [m for m, _ in P.STEP_4_SYMPTOMS])
def test_every_known_step_4_symptom_carries_an_instruction(marker):
    guidance = next(do for mark, do in P.STEP_4_SYMPTOMS if mark == marker)
    assert guidance.strip()


# ════════════════════════════════════════════════════════════════
# the ship list
# ════════════════════════════════════════════════════════════════

def test_the_ship_list_never_carries_a_secret():
    import make_ship as S
    names = [name for name, _ in S.SHIPPED]
    for forbidden in S.FORBIDDEN:
        assert forbidden not in names


def test_everything_the_agent_imports_is_shipped():
    """A missing module here is an ImportError on the counter, mid-install."""
    import make_ship as S
    shipped = {name for name, _ in S.SHIPPED}
    for required in ("agent.py", "adapter_hdsoft.py", "rows.py", "supa.py",
                     "mint_agent_token.py", "test_golden.py", "metrics.py",
                     "events.py", "report.py", "preflight.py", "preflight.bat",
                     "requirements.txt", "VERIFY.md"):
        assert required in shipped


def _local_imports(path):
    """Repository modules a file imports at module level, plus every module
    it names in an `importlib.import_module("literal")` call.

    Module-level imports resolve the moment the file is loaded, so a missing
    one is an ImportError before the agent does anything. An import inside a
    function is reachable surface, not startup surface — `agent.py` imports
    `fake_adapter` that way, under `--fake`, and that module is excluded
    from ship/ on purpose: synthetic data has no place on a production
    machine.

    `importlib.import_module` is included wherever it appears, at any depth,
    because preflight.py loads `agent`, `mint_agent_token`, `adapter_hdsoft`
    and `readonly_probe` that way — deliberately late, after step 2 has
    installed their dependencies. Those are invisible to an import walk and
    would fail at the counter rather than here. This was a real gap: the
    module-level walk passed a ship list that had no `readonly_probe.py`
    in it.
    """
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        is_import_module = (
            (isinstance(target, ast.Attribute) and target.attr == "import_module")
            or (isinstance(target, ast.Name) and target.id == "import_module"))
        if is_import_module and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value.split(".")[0])

    return {n for n in names if (P.HERE / f"{n}.py").exists()}


def _reachable_imports(entry: str) -> set[str]:
    """Every repository module reachable from an entry point, transitively.

    🔴 This used to walk one level and was called "closed under import",
    which it was not. `sqlguard` is imported by `adapter_hdsoft.py`, not by
    any entry point, so removing it from the ship list passed this test —
    and would have put an agent on the till that raises
    `ModuleNotFoundError: No module named 'sqlguard'` at install time,
    with the SQL guard absent entirely.

    The architect asked for this to be verified rather than assumed. It was
    assumed, and it was wrong. A closure that only closes over the first
    level is not a closure.
    """
    seen: set[str] = set()
    queue = [entry]
    while queue:
        name = queue.pop()
        for module in _local_imports(P.HERE / name):
            if module not in seen:
                seen.add(module)
                queue.append(f"{module}.py")
    return seen


@pytest.mark.parametrize("entry", ["agent.py", "preflight.py", "test_golden.py"])
def test_the_ship_list_is_closed_under_transitive_import(entry):
    """The real closure: follow the import graph all the way down."""
    import make_ship as S
    shipped = {name for name, _ in S.SHIPPED}
    missing = sorted(f"{m}.py" for m in _reachable_imports(entry)
                     if f"{m}.py" not in shipped)
    assert not missing, (
        f"{entry} reaches {missing}, which ship/ does not contain. "
        "The agent would raise ImportError on the customer machine."
    )


def test_the_closure_test_would_notice_a_module_two_levels_down():
    """Falsifier. `sqlguard` is reachable only through `adapter_hdsoft`, and
    the single-level version of this test passed while it was missing."""
    assert "sqlguard" in _reachable_imports("agent.py"), (
        "sqlguard is no longer reachable from agent.py — either the wiring "
        "patch was reverted, or this test has stopped testing")
    assert "sqlguard" not in _local_imports(P.HERE / "agent.py"), (
        "sqlguard is now a direct import of agent.py, so this test no "
        "longer proves the walk is transitive; point it at a module that is "
        "still two levels down"
    )


@pytest.mark.parametrize("entry", ["agent.py", "preflight.py", "test_golden.py"])
def test_the_ship_list_is_closed_under_import(entry):
    """Derived from the source, not from a list someone maintains by hand.

    This exists because `mint_agent_token.py` reads like dead weight on the
    customer machine — the token is minted on our machine, and that file's
    CLI is never used there. It is not dead weight: agent.py imports it at
    module level (line ~48) and Config.load calls assert_is_agent_token, the
    check that refuses a service_role key. Deleting it from ship/ produces
    `ModuleNotFoundError: No module named 'mint_agent_token'` on the till,
    at install time. Whoever next decides that file looks unnecessary should
    be stopped here rather than on site.
    """
    import make_ship as S
    shipped = {name for name, _ in S.SHIPPED}
    missing = sorted(f"{m}.py" for m in _local_imports(P.HERE / entry)
                     if f"{m}.py" not in shipped)
    assert not missing, (
        f"{entry} imports {missing}, which ship/ does not contain. "
        "The agent would raise ImportError on the customer machine."
    )


def test_every_shipped_file_has_the_line_endings_a_checkout_produces():
    """A build once hashed a working copy that had picked up CRLF in three
    files. The manifest was right about that machine and wrong about the
    repository, and preflight agreed with it because both read the same
    working tree. Checked here so a dirty checkout cannot be shipped."""
    import make_ship as S
    S.check_line_endings()          # raises SystemExit with the offenders


def test_the_line_ending_rule_still_matches_gitattributes():
    """If .gitattributes changes, make_ship's CRLF_SUFFIXES must follow it,
    or the guard above starts enforcing a rule nobody agreed to."""
    import make_ship as S
    attrs = (P.HERE / ".gitattributes").read_text(encoding="utf-8")
    for suffix in S.CRLF_SUFFIXES:
        assert re.search(rf"^\*{re.escape(suffix)}\s+text eol=crlf", attrs,
                         re.MULTILINE), f"{suffix} is no longer pinned to CRLF"


def test_the_manifest_records_what_it_was_built_from():
    """A ship folder on a customer machine should be traceable to a commit,
    not to a date and a memory."""
    import make_ship as S
    revision = S.git_revision()
    assert revision and revision != "unknown (not a git checkout)"


def test_the_golden_baseline_needs_pytest_on_that_machine():
    """VERIFY.md step 3 and preflight both run pytest there, so it has to be
    a declared dependency of the customer machine, not an assumption."""
    text = (P.HERE / "requirements.txt").read_text(encoding="utf-8")
    assert "pytest" in text


def test_the_golden_count_matches_the_file_we_ship():
    """If test_golden.py gains or loses a test, '31 passed' stops being the
    right assertion and preflight would stop the install for the wrong
    reason. Pinned here so the two move together."""
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-m", "pytest", "-q",
                          "--collect-only", "test_golden.py"],
                         cwd=str(P.HERE), capture_output=True, text=True)
    assert f"{P.GOLDEN_TEST_COUNT} tests collected" in out.stdout, out.stdout


# ════════════════════════════════════════════════════════════════
# integrity for the folder the operator actually gets
# ════════════════════════════════════════════════════════════════

def _git(path, *args):
    import subprocess
    return subprocess.run(["git", *args], cwd=str(path), capture_output=True,
                          text=True)


def _tiny_repo(tmp_path):
    """A real git checkout, because this checks real git behaviour."""
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "agent.py").write_bytes(b"# agent\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "first")
    return repo


def test_a_clean_clone_is_verified_even_without_a_manifest(tmp_path):
    """🔴 The operator clones from GitHub at the shop. A clone has no
    MANIFEST.txt — ship/ is generated and never committed — so without this
    the strongest check in the procedure silently downgraded to NOT
    VERIFIED on the exact path he actually takes."""
    repo = _tiny_repo(tmp_path)
    status = P.verify_manifest(repo)
    assert status.startswith("code integrity   OK")
    assert "clean checkout of commit" in status


def test_an_edited_clone_stops(tmp_path):
    repo = _tiny_repo(tmp_path)
    (repo / "agent.py").write_bytes(b"# tampered\n")
    with pytest.raises(P.Stop) as caught:
        P.verify_manifest(repo)
    assert "agent.py" in caught.value.what
    assert "git checkout" in caught.value.do


def test_files_the_operator_creates_do_not_look_like_tampering(tmp_path):
    """config.json, state.json, agent.log and logs/ all appear after
    cloning. Treating them as drift would stop every real install."""
    repo = _tiny_repo(tmp_path)
    (repo / "config.json").write_bytes(b"{}")
    (repo / "state.json").write_bytes(b"{}")
    (repo / "agent.log").write_bytes(b"log")
    (repo / "logs").mkdir()
    assert P.verify_manifest(repo).startswith("code integrity   OK")


def test_a_folder_that_is_neither_says_so_rather_than_passing(tmp_path):
    plain = tmp_path / "loose"
    plain.mkdir()
    (plain / "agent.py").write_bytes(b"# agent\n")
    status = P.verify_manifest(plain)
    assert "NOT VERIFIED" in status


def test_a_ship_folder_still_uses_its_manifest(tmp_path):
    """The manifest must keep winning where it exists: a ship/ folder copied
    onto a machine has no .git at all."""
    shipped = tmp_path / "ship"
    shipped.mkdir()
    body = b"# agent\n"
    (shipped / "agent.py").write_bytes(body)
    (shipped / "MANIFEST.txt").write_bytes(
        (hashlib.sha256(body).hexdigest() + "  agent.py\n").encode())
    assert P.verify_manifest(shipped).startswith("code integrity   OK — 1 files")


def test_the_release_artifact_carries_its_own_manifest(tmp_path):
    """🔴 The customer installed from a GitHub ZIP of the repository:
    C:/Users/Techno/Downloads/POSentine-main. A ZIP has no `.git`,
    and `ship/` is gitignored so the ZIP had no `MANIFEST.txt` either —
    both integrity mechanisms were absent on the one path the operator
    actually took, and the install ran with `NOT VERIFIED`.

    The release artifact is the fix: the download IS the verified folder.
    """
    import zipfile
    import make_ship as S

    S.build()
    archive = S.make_zip()
    try:
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            zf.extractall(tmp_path)
        assert any(n.endswith("posentine/MANIFEST.txt") for n in names), names

        unpacked = tmp_path / "posentine"
        assert not (unpacked / ".git").exists(), "the point is that there is no .git"
        status = P.verify_manifest(unpacked)
        assert status.startswith("code integrity   OK"), status
        assert "MANIFEST.txt" in status

        # And it must still catch tampering after download.
        (unpacked / "agent.py").write_bytes(b"# tampered\n")
        with pytest.raises(P.Stop):
            P.verify_manifest(unpacked)
    finally:
        archive.unlink(missing_ok=True)


def test_the_release_artifact_does_not_carry_the_repository():
    """A repo ZIP puts fake_adapter.py, the whole test suite and our
    correspondence with the architect on the customer's till. The release
    artifact is ship/ and nothing else."""
    import zipfile
    import make_ship as S

    S.build()
    archive = S.make_zip()
    try:
        with zipfile.ZipFile(archive) as zf:
            names = "\n".join(zf.namelist())
        assert "fake_adapter" not in names, "synthetic data must not ship"
        assert "TO_CLAUDE_CODE" not in names and "FROM_CLAUDE_CODE" not in names
        assert "test_agent" not in names and "test_preflight" not in names
        assert "test_golden.py" in names, "the on-site baseline must still ship"
    finally:
        archive.unlink(missing_ok=True)
