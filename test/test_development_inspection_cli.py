"""開發 CLI 的操作閘門、去識別输出、退出碼與真實跨程序唯讀驗收。"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

import development_inspection as cli
from development_atomic_fixtures import SHARED, ident, request
from test_development_atomic_snapshot import database, reserve, service
from test_development_integrity_inspection import dump, mutate
from test_development_snapshot_comparison import clear, clone, pair


ROOT = Path(__file__).resolve().parents[1]
FLAGS = ("can_quote", "can_confirm", "can_save", "can_resume_numbering")


def arguments(command, pair):
    paths = (["--database", str(pair[0])] if command in ("schema", "integrity") else
             ["--reference", str(pair[0]), "--candidate", str(pair[1])])
    return [command, "--development-only", *paths]


def assert_private(text, pair):
    for secret in (str(pair[0]), str(pair[1]), SHARED, "SYNTHETIC-ORDER", ident(1),
                   "20261002083045_001", "secret-input", "Traceback"):
        assert secret not in text


def assert_report(text, command, code):
    data = json.loads(text)
    assert data["output_version"] == cli.OUTPUT_VERSION
    assert data["operation"] == command and data["exit_code"] == code
    assert data["status"] == ("completed" if code == 0 else "differences")
    assert data["warning"] == cli.WARNING
    for report in (data, data["report"]):
        assert all(report[flag] is False for flag in FLAGS)
        assert report["warning"]
    if command == "snapshots":
        for key in ("reference_integrity", "candidate_integrity"):
            assert all(data["report"][key][flag] is False for flag in FLAGS)
            assert data["report"][key]["warning"]
    return data["report"]


@pytest.mark.parametrize("command", cli.COMMANDS)
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("state", ["empty", "reserved", "saved"])
def test_complete_reports_are_readonly_private_and_repeatable(database, tmp_path, capsys, command, journal, state):
    left = Path(database.url.database)
    mutate(left, f"PRAGMA journal_mode={journal}")
    if state != "empty":
        reserve(database, request())
    if state == "saved":
        service(database).save(request())
    pair = left, clone(left, tmp_path / "candidate.sqlite")
    before = tuple(dump(path) for path in pair)
    assert cli.main(arguments(command, pair)) == 0
    first = capsys.readouterr()
    assert first.err == ""
    report = assert_report(first.out, command, 0)
    assert_private(first.out, pair)
    if command == "integrity":
        assert report["verified_saved_batches"] == (state == "saved")
        assert report["reservation_only_batches"] == (state == "reserved")
        assert len(report["table_counts"]) == 6
    if command == "snapshots":
        assert len(report["tables"]) == 6
        assert report["reference_integrity"] == report["candidate_integrity"]
    assert cli.main(arguments(command, pair)) == 0
    assert capsys.readouterr() == first
    assert tuple(dump(path) for path in pair) == before


@pytest.mark.parametrize("command", cli.COMMANDS)
def test_missing_consent_never_calls_service(pair, monkeypatch, capsys, command):
    monkeypatch.setattr(cli, "_run", lambda args: pytest.fail("must not run"))
    args = arguments(command, pair)
    args.remove("--development-only")
    assert cli.main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == cli.ARGUMENT_ERROR + "\n"


@pytest.mark.parametrize("extra", [
    ["--timeout", "nan"], ["--timeout", "inf"], ["--timeout", "0"],
    ["--timeout", "-1"], ["--timeout", "30.001"], ["--timeout", "secret-input"],
    ["--time", "1"], ["--timeout", "1", "--timeout", "2"],
    ["--development-only"], ["--database", "secret-input"],
    ["--unknown", "secret-input"], ["@secret-input"], ["--repair"],
    ["--output", "secret-input"], ["--candidate", "secret-input"],
])
def test_invalid_arguments_do_not_echo_or_run(pair, monkeypatch, capsys, extra):
    monkeypatch.setattr(cli, "_run", lambda args: pytest.fail("must not run"))
    assert cli.main(arguments("schema", pair) + extra) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == cli.ARGUMENT_ERROR + "\n"
    assert_private(captured.err, pair)


@pytest.mark.parametrize("args", [[], ["secret-input"], ["schema"], ["integrity"], ["registry"],
                                   ["snapshots", "--development-only"]])
def test_incomplete_command_is_fail_closed(monkeypatch, capsys, args):
    monkeypatch.setattr(cli, "_run", lambda args: pytest.fail("must not run"))
    assert cli.main(args) == 2
    assert capsys.readouterr() == ("", cli.ARGUMENT_ERROR + "\n")


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "uri", "url", "private"])
def test_invalid_source_never_connects(tmp_path, monkeypatch, capsys, kind):
    source = tmp_path / "source.sqlite"
    source.write_text("not a database", encoding="utf8")
    link = tmp_path / "link.sqlite"
    link.symlink_to(source)
    private = tmp_path / ".env.secret-input"
    private.write_text("secret-input", encoding="utf8")
    paths = {"missing": tmp_path / "missing.sqlite", "directory": tmp_path,
             "symlink": link, "uri": source.as_uri(), "url": "sqlite:///secret-input",
             "private": private}
    monkeypatch.setattr(cli, "_run", lambda args: pytest.fail("must not run"))
    assert cli.main(["schema", "--development-only", "--database", str(paths[kind])]) == 2
    assert capsys.readouterr() == ("", cli.ARGUMENT_ERROR + "\n")
    assert not (tmp_path / "missing.sqlite").exists()


@pytest.mark.parametrize("command", ["registry", "snapshots"])
@pytest.mark.parametrize("hardlink", [False, True])
def test_same_file_comparison_is_rejected(pair, tmp_path, capsys, command, hardlink):
    right = pair[0]
    if hardlink:
        right = tmp_path / "hardlink.sqlite"
        right.hardlink_to(pair[0])
    assert cli.main(arguments(command, (pair[0], right))) == 2
    assert capsys.readouterr() == ("", cli.ARGUMENT_ERROR + "\n")


@pytest.mark.parametrize("command", ["registry", "snapshots"])
def test_complete_group_loss_returns_difference_exit_code(pair, capsys, command):
    clear(pair[1])
    before = tuple(dump(path) for path in pair)
    assert cli.main(arguments(command, pair)) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    report = assert_report(captured.out, command, 1)
    assert not report["contents_match"] and any(row["missing_rows"] for row in report["tables"])
    assert_private(captured.out, pair)
    assert tuple(dump(path) for path in pair) == before


def test_schema_difference_is_not_execution_failure(pair, capsys):
    mutate(pair[0], "DROP TABLE development_atomic_cost")
    assert cli.main(arguments("schema", pair)) == 1
    captured = capsys.readouterr()
    report = assert_report(captured.out, "schema", 1)
    assert not report["schema_matches"] and report["issues"]
    assert captured.err == ""


@pytest.mark.parametrize("command", ["integrity", "snapshots"])
def test_corrupt_content_is_not_a_difference_or_partial_success(pair, capsys, command):
    assert cli.main(arguments(command, pair)) == 0
    capsys.readouterr()
    mutate(pair[0], "DELETE FROM development_atomic_cost WHERE rowid=(SELECT min(rowid) FROM development_atomic_cost)")
    before = dump(pair[0])
    assert cli.main(arguments(command, pair)) == 3
    assert capsys.readouterr() == ("", cli.EXECUTION_ERROR + "\n")
    assert dump(pair[0]) == before


def test_registry_match_does_not_claim_full_integrity(pair, capsys):
    mutate(pair[0], "DELETE FROM development_atomic_cost")
    assert cli.main(arguments("registry", pair)) == 0
    captured = capsys.readouterr()
    report = assert_report(captured.out, "registry", 0)
    assert "未驗證完整快照" in report["warning"]
    assert "reference_integrity" not in report


@pytest.mark.parametrize("command", cli.COMMANDS)
def test_non_database_and_locks_are_sanitized(pair, capsys, command):
    with closing(sqlite3.connect(pair[0])) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        assert cli.main(arguments(command, pair) + ["--timeout", "0.01"]) == 3
    assert capsys.readouterr() == ("", cli.EXECUTION_ERROR + "\n")
    pair[0].write_text("secret-input", encoding="utf8")
    assert cli.main(arguments(command, pair)) == 3
    assert capsys.readouterr() == ("", cli.EXECUTION_ERROR + "\n")


@pytest.mark.parametrize("failure", [RuntimeError, ImportError, ValueError, MemoryError, KeyboardInterrupt])
def test_unexpected_failure_never_exposes_traceback_or_input(pair, monkeypatch, capsys, failure):
    def fail(args):
        raise failure("secret-input " + str(pair[0]))
    monkeypatch.setattr(cli, "_run", fail)
    assert cli.main(arguments("schema", pair)) == (130 if failure is KeyboardInterrupt else 3)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err
    assert_private(captured.err, pair)


def test_serialization_failure_has_no_partial_output(pair, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_document", lambda *args: ({"secret-input": object()}, 0))
    assert cli.main(arguments("schema", pair)) == 3
    assert capsys.readouterr() == ("", cli.EXECUTION_ERROR + "\n")


def test_allowlist_does_not_export_new_report_fields(pair, monkeypatch, capsys):
    report = cli._run(cli._arguments(arguments("schema", pair)))
    extended = SimpleNamespace(**{name: getattr(report, name) for name in (
        "report_version", "warning", "schema_matches", "expected_digest", "observed_digest", "checked_tables", "issues")},
        payload="secret-input", database_path=str(pair[0]), can_quote=True)
    monkeypatch.setattr(cli, "_run", lambda args: extended)
    assert cli.main(arguments("schema", pair)) == 0
    captured = capsys.readouterr()
    assert_report(captured.out, "schema", 0)
    assert_private(captured.out, pair)


def test_broken_output_is_nonzero_without_traceback(pair, monkeypatch, capsys):
    def fail(text):
        raise BrokenPipeError("secret-input")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", SimpleNamespace(write=fail, flush=lambda: None))
        assert cli.main(arguments("schema", pair)) == 3
    assert capsys.readouterr() == ("", cli.EXECUTION_ERROR + "\n")


@pytest.mark.parametrize("command,module,function", [
    ("schema", "development_schema_inspection", "inspect_development_schema"),
    ("integrity", "development_integrity_inspection", "inspect_development_integrity"),
    ("registry", "development_registry_comparison", "compare_development_registries"),
    ("snapshots", "development_snapshot_comparison", "compare_development_snapshots"),
])
@pytest.mark.parametrize("timeout", [None, "0.125", "30"])
def test_dispatches_exactly_one_service_with_explicit_timeout(pair, monkeypatch, capsys, command, module, function, timeout):
    import importlib
    target = importlib.import_module("engine." + module)
    real = getattr(target, function)
    calls = []
    def checked(*paths, **kwargs):
        calls.append((paths, kwargs))
        return real(*paths, **kwargs)
    monkeypatch.setattr(target, function, checked)
    args = arguments(command, pair)
    if timeout is not None:
        args += ["--timeout", timeout]
    assert cli.main(args) == 0
    capsys.readouterr()
    expected_paths = (pair[0],) if command in ("schema", "integrity") else pair
    expected_timeout = float(timeout) if timeout else (2 if command == "schema" else 10)
    assert calls == [(expected_paths, {"timeout": expected_timeout})]


@pytest.mark.parametrize("command", cli.COMMANDS)
def test_special_filename_is_literal_not_connection_options(pair, tmp_path, capsys, command):
    left = clone(pair[0], tmp_path / "開發 ?mode=rw# &%.sqlite")
    right = clone(pair[1], tmp_path / "另一份 ?.sqlite")
    before = (dump(left), dump(right))
    assert cli.main(arguments(command, (left, right))) == 0
    captured = capsys.readouterr()
    assert_report(captured.out, command, 0)
    assert_private(captured.out, (left, right))
    assert (dump(left), dump(right)) == before


# 新程序於匯入入口前安裝隔離；不依賴父程序 conftest 的替身傳遞。
WORKER = r'''
from pathlib import Path
import sys
root = Path(sys.argv[1])
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).resolve()
        if path.name.startswith(".env") or path.is_relative_to(root / "logs"):
            raise AssertionError("private file access")
    if event.startswith("socket."):
        raise AssertionError("network access")
    if event == "import" and args[0] in {
        "config", "database.connection", "database.models", "database.repository",
        "engine.pricing", "engine.snapshot", "streamlit", "openai", "dotenv",
    }:
        raise AssertionError("production dependency")
sys.addaudithook(audit)
sys.path.insert(0, str(root))
import sqlite3
original = sqlite3.connect
def deny(*args, **kwargs):
    raise AssertionError("unexpected connection")
sqlite3.connect = deny
import development_inspection as cli
assert not any(name.startswith("engine.") for name in sys.modules)
if sys.argv[2] == "run":
    sqlite3.connect = original
code = cli.main(sys.argv[3:])
if sys.argv[2] != "run":
    assert not any(name.startswith("engine.") for name in sys.modules)
if sys.argv[3:4] in (["schema"], ["registry"]):
    assert "engine.development_atomic_snapshot" not in sys.modules
    assert "engine.quote_number_reservation" not in sys.modules
raise SystemExit(code)
'''


def process(args, *, enabled=True, cwd=None):
    return subprocess.run([sys.executable, "-c", WORKER, str(ROOT), "run" if enabled else "deny", *args],
                          capture_output=True, text=True, timeout=20, cwd=cwd)


@pytest.mark.parametrize("command", cli.COMMANDS)
def test_real_fresh_process_result_is_identical_and_readonly(pair, capsys, tmp_path, command):
    before = tuple(dump(path) for path in pair)
    assert cli.main(arguments(command, pair)) == 0
    expected = capsys.readouterr()
    result = process(arguments(command, pair), cwd=tmp_path)
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout == expected.out
    assert_report(result.stdout, command, 0)
    assert_private(result.stdout, pair)
    assert tuple(dump(path) for path in pair) == before


@pytest.mark.parametrize("args,code", [([], 2), (["--help"], 0), (["schema", "--help"], 0),
                                      (["secret-input"], 2), (["schema"], 2)])
def test_fresh_import_help_and_denial_never_connect(args, code):
    result = process(args, enabled=False)
    assert result.returncode == code
    assert "unexpected connection" not in result.stderr
    assert "secret-input" not in result.stdout + result.stderr
    if code == 0:
        assert "--help" in result.stdout and result.stderr == ""
    else:
        assert result.stdout == "" and result.stderr == cli.ARGUMENT_ERROR + "\n"


@pytest.mark.parametrize("command", cli.COMMANDS)
def test_fresh_process_missing_consent_never_loads_services(pair, command):
    args = arguments(command, pair)
    args.remove("--development-only")
    result = process(args, enabled=False)
    assert result.returncode == 2
    assert result.stdout == "" and result.stderr == cli.ARGUMENT_ERROR + "\n"


@pytest.mark.parametrize("command", ["integrity", "snapshots"])
def test_fresh_process_corruption_is_sanitized_and_readonly(pair, command):
    mutate(pair[0], "DELETE FROM development_atomic_cost")
    before = tuple(dump(path) for path in pair)
    result = process(arguments(command, pair))
    assert result.returncode == 3
    assert result.stdout == "" and result.stderr == cli.EXECUTION_ERROR + "\n"
    assert tuple(dump(path) for path in pair) == before


def test_direct_script_entrypoint_exit_codes(pair, tmp_path):
    for command, expected in (("schema", 0), ("snapshots", 1), ("integrity", 3)):
        if command == "snapshots":
            clear(pair[1])
        if command == "integrity":
            mutate(pair[0], "DELETE FROM development_atomic_cost")
        # 不經 runpy 或可注入 main；實際 Python 腳本的 SystemExit 契約。
        result = subprocess.run([sys.executable, str(ROOT / "development_inspection.py"),
                                 *arguments(command, pair)], cwd=tmp_path,
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == expected
        assert_private(result.stdout + result.stderr, pair)
        if expected == 3:
            assert result.stdout == "" and result.stderr == cli.EXECUTION_ERROR + "\n"
        else:
            assert_report(result.stdout, command, expected)
