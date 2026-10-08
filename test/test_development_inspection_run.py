"""受控執行的真實子程序、退出碼綁定、有界管線及開發隔離驗收。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

import development_inspection as producer
import development_inspection_run as runner
from engine.development_report_validation import validate_development_report
from development_atomic_fixtures import request
from test_development_atomic_snapshot import database, reserve, service
from test_development_integrity_inspection import dump, mutate
from test_development_snapshot_comparison import clear, clone, pair
from test_development_inspection_cli import arguments, assert_private, FLAGS


def assert_result(text, operation, code, paths):
    result = json.loads(text)
    assert set(result) == {"run_version", "run_id", "operation", "status", "inspection_exit_code",
                           "validation_version", "warning", "inspection", *FLAGS}
    assert result["run_version"] == runner.RUN_VERSION
    assert str(uuid.UUID(result["run_id"])) == result["run_id"]
    assert result["warning"] == runner.WARNING
    assert all(result[key] is False for key in FLAGS)
    validated = validate_development_report(json.dumps(result["inspection"]).encode(),
        expected_operation=operation, expected_exit_code=code)
    assert result["operation"] == validated.operation
    assert result["status"] == validated.status
    assert result["inspection_exit_code"] == validated.inspection_exit_code
    assert result["validation_version"] == validated.validation_version
    assert_private(text, paths)
    return result


@pytest.mark.parametrize("operation", producer.COMMANDS)
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("state", ["empty", "reserved", "saved"])
def test_real_child_reports_and_readonly(database, tmp_path, capsys, operation, journal, state):
    left = Path(database.url.database)
    mutate(left, f"PRAGMA journal_mode={journal}")
    if state != "empty":
        reserve(database, request())
    if state == "saved":
        service(database).save(request())
    paths = left, clone(left, tmp_path / "candidate.sqlite")
    before = tuple(dump(path) for path in paths)
    assert runner.main(arguments(operation, paths)) == 0
    output = capsys.readouterr()
    assert output.err == ""
    result = assert_result(output.out, operation, 0, paths)
    assert producer.main(arguments(operation, paths)) == 0
    assert result["inspection"] == json.loads(capsys.readouterr().out)
    assert tuple(dump(path) for path in paths) == before


@pytest.mark.parametrize("operation", ["schema", "registry", "snapshots"])
def test_actual_difference_code_is_not_supplied_by_caller(pair, capsys, operation):
    if operation == "schema":
        mutate(pair[0], "DROP TABLE development_atomic_cost")
    else:
        clear(pair[1])
    assert runner.main(arguments(operation, pair)) == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert_result(output.out, operation, 1, pair)


@pytest.mark.parametrize("operation", ["integrity", "snapshots"])
def test_success_then_corruption_never_reuses_old_result(pair, capsys, operation):
    assert runner.main(arguments(operation, pair)) == 0
    capsys.readouterr()
    mutate(pair[0], "DELETE FROM development_atomic_cost")
    before = tuple(dump(path) for path in pair)
    assert runner.main(arguments(operation, pair)) == 3
    assert capsys.readouterr() == ("", runner.EXECUTION_ERROR + "\n")
    assert tuple(dump(path) for path in pair) == before


def test_each_run_has_fresh_identifier_but_same_report(pair, capsys):
    results = []
    for _ in range(2):
        assert runner.main(arguments("schema", pair)) == 0
        results.append(json.loads(capsys.readouterr().out))
    assert results[0].pop("run_id") != results[1].pop("run_id")
    assert results[0] == results[1]


@pytest.mark.parametrize("extra", [
    ["--process-timeout", "0"], ["--process-timeout", "-1"], ["--process-timeout", "60.1"],
    ["--process-timeout", "nan"], ["--process-timeout", "inf"], ["--process-timeout", "secret-input"],
    ["--process-timeout", "1", "--process-timeout", "2"], ["--process-time", "1"],
    ["--timeout", "31"], ["--timeout", "0"], ["--timeout", "nan"],
    ["--development-only"], ["--database", "secret-input"], ["--inspection-exit-code", "0"],
    ["--report", "secret-input"], ["--script", "secret-input"], ["--output", "secret-input"],
    ["--candidate", "secret-input"], ["--repair"], ["@secret-input"],
])
def test_invalid_arguments_do_not_start_process(pair, monkeypatch, capsys, extra):
    monkeypatch.setattr(runner, "_run", lambda *a: pytest.fail("unexpected run"))
    assert runner.main(arguments("schema", pair) + extra) == 2
    assert capsys.readouterr() == ("", runner.ARGUMENT_ERROR + "\n")


@pytest.mark.parametrize("operation", producer.COMMANDS)
def test_missing_consent_does_not_start_process(pair, monkeypatch, capsys, operation):
    monkeypatch.setattr(runner, "_run", lambda *a: pytest.fail("unexpected run"))
    args = arguments(operation, pair)
    args.remove("--development-only")
    assert runner.main(args) == 2
    assert capsys.readouterr() == ("", runner.ARGUMENT_ERROR + "\n")


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "private", "uri", "same", "hardlink"])
def test_invalid_paths_are_rejected_before_spawn(pair, tmp_path, monkeypatch, capsys, kind):
    link = tmp_path / "link.sqlite"
    link.symlink_to(pair[0])
    hard = tmp_path / "hard.sqlite"
    hard.hardlink_to(pair[0])
    private = tmp_path / ".env.secret-input"
    private.write_text("secret-input")
    choices = {"missing": tmp_path / "missing", "directory": tmp_path, "symlink": link,
               "private": private, "uri": pair[0].as_uri(), "same": pair[0], "hardlink": hard}
    monkeypatch.setattr(runner, "_collect", lambda *a: pytest.fail("unexpected process"))
    assert runner.main(arguments("snapshots", (pair[0], choices[kind]))) == 2
    assert capsys.readouterr() == ("", runner.ARGUMENT_ERROR + "\n")


def test_special_relative_paths_and_fixed_launch(pair, tmp_path, monkeypatch, capsys):
    paths = (clone(pair[0], tmp_path / "開發 ?#%; $(secret-input).sqlite"),
             clone(pair[1], tmp_path / "另一份.sqlite"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", "secret-input")
    monkeypatch.setenv("DB_CONN_STR", "secret-input")
    real = runner.subprocess.Popen
    calls = []
    def launch(command, **kwargs):
        calls.append((command, kwargs))
        return real(command, **kwargs)
    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    assert runner.main(arguments("snapshots", tuple(path.name for path in paths)) +
                       ["--timeout", "0.75", "--process-timeout", "20"]) == 0
    result = capsys.readouterr()
    assert_result(result.out, "snapshots", 0, paths)
    assert len(calls) == 1
    command, options = calls[0]
    assert command[:5] == [sys.executable, "-I", "-B", "-c", runner._WORKER]
    assert command[5] == str(runner.ROOT)
    assert command[-4:] == ["--reference", str(paths[0]), "--candidate", str(paths[1])]
    assert command[command.index("--timeout") + 1] == "0.75"
    assert options["env"] == {} and options["shell"] is False
    assert options["stdin"] == subprocess.DEVNULL and options["close_fds"] is True
    assert options["cwd"] == runner.ROOT


@pytest.fixture
def processes(monkeypatch):
    real = subprocess.Popen
    started = []
    def launch(*args, **kwargs):
        child = real(*args, **kwargs)
        started.append(child)
        return child
    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    yield started
    for child in started:
        assert child.poll() is not None
        assert child.stdout.closed and child.stderr.closed


def child_command(source):
    return [sys.executable, "-I", "-B", "-c", source]


@pytest.mark.parametrize("size", [0, 1, runner.MAX_REPORT_BYTES])
@pytest.mark.parametrize("code", [0, 1])
def test_collector_exact_stdout_boundary_and_exit_code(processes, size, code):
    payload, result = runner._collect(child_command(
        f"import sys; sys.stdout.buffer.write(b'x' * {size}); sys.exit({code})"), 5)
    assert payload == b"x" * size and result == code


@pytest.mark.parametrize("stream,size", [
    ("stdout", runner.MAX_REPORT_BYTES + 1), ("stderr", runner.MAX_STDERR_BYTES + 1),
    ("stderr", 1), ("stderr", runner.MAX_STDERR_BYTES),
])
def test_output_overflow_or_any_stderr_rejects(processes, stream, size):
    with pytest.raises(ValueError):
        runner._collect(child_command(f"import sys; sys.{stream}.buffer.write(b'x' * {size})"), 5)


@pytest.mark.parametrize("code", [2, 3, 130, 255])
def test_actual_unsuccessful_exit_is_never_accepted(processes, code):
    with pytest.raises(ValueError):
        runner._collect(child_command(f"import sys; print('{{}}'); sys.exit({code})"), 5)


@pytest.mark.parametrize("source", [
    "import time; time.sleep(30)",
    "import os,time; os.close(1); os.close(2); time.sleep(30)",
    "import sys,time; print('{}', flush=True); time.sleep(30)",
])
def test_deadline_including_eof_without_exit_kills_and_reaps(processes, source):
    started = time.monotonic()
    with pytest.raises((TimeoutError, subprocess.TimeoutExpired)):
        runner._collect(child_command(source), 0.15)
    assert time.monotonic() - started < 4
    assert processes[0].returncode != 0


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_unbounded_child_output_is_stopped_without_deadlock(processes, stream):
    with pytest.raises(ValueError):
        runner._collect(child_command(
            f"import sys\nwhile True: sys.{stream}.buffer.write(b'x' * 8192); sys.{stream}.flush()"), 5)


def test_signal_death_is_rejected(processes):
    with pytest.raises(ValueError):
        runner._collect(child_command("import os,signal; os.kill(os.getpid(), signal.SIGKILL)"), 5)
    assert processes[0].returncode < 0


@pytest.mark.parametrize("phase", ["register", "select", "read", "interrupt"])
def test_collection_failure_reaps_child(processes, monkeypatch, phase):
    def fail(*args, **kwargs):
        raise (KeyboardInterrupt if phase == "interrupt" else OSError)("secret-input")
    if phase == "read":
        original = runner.os.read
        def read(fd, size):
            # Popen 本身讀 exec 錯誤管線不能被攔截，等 child 已登錄後才注入。
            if processes:
                return fail()
            return original(fd, size)
        monkeypatch.setattr(runner.os, "read", read)
    else:
        target = "select" if phase == "interrupt" else phase
        monkeypatch.setattr(runner.selectors.DefaultSelector, target, fail)
    with pytest.raises(KeyboardInterrupt if phase == "interrupt" else OSError):
        runner._collect(child_command("import time; print('x',flush=True); time.sleep(30)"), 5)


@pytest.mark.parametrize("kind", ["empty", "truncated", "wrong_code", "wrong_operation", "unknown",
                                 "duplicate", "extra", "invalid_utf8", "oversized"])
def test_received_payload_must_match_this_process_result(pair, monkeypatch, capsys, kind):
    assert producer.main(arguments("schema", pair)) == 0
    payload = capsys.readouterr().out.encode()
    code = 0
    if kind == "empty":
        payload = b""
    elif kind == "truncated":
        payload = payload[:100]
    elif kind == "wrong_code":
        code = 1
    elif kind in ("wrong_operation", "unknown"):
        document = json.loads(payload)
        document["operation" if kind == "wrong_operation" else "secret-input"] = "integrity"
        payload = json.dumps(document).encode()
    elif kind == "duplicate":
        payload = b'{"operation":"schema",' + payload[1:]
    elif kind == "extra":
        payload += b"{}"
    elif kind == "invalid_utf8":
        payload += b"\xff"
    else:
        payload += b" " * runner.MAX_REPORT_BYTES
    monkeypatch.setattr(runner, "_collect", lambda *a: (payload, code))
    assert runner.main(arguments("schema", pair)) == 3
    assert capsys.readouterr() == ("", runner.EXECUTION_ERROR + "\n")


@pytest.mark.parametrize("phase", ["spawn", "validate", "serialize", "write", "flush", "interrupt"])
def test_main_failures_are_sanitized(pair, capsys, monkeypatch, phase):
    assert producer.main(arguments("schema", pair)) == 0
    payload = capsys.readouterr().out.encode()
    def fail(*args, **kwargs):
        raise (KeyboardInterrupt if phase == "interrupt" else OSError)("secret-input")
    with monkeypatch.context() as patch:
        patch.setattr(runner, "_collect", lambda *a: (payload, 0))
        if phase in ("spawn", "interrupt"):
            patch.setattr(runner, "_collect", fail)
        elif phase == "validate":
            patch.setattr(runner, "validate_development_report", fail)
        elif phase == "serialize":
            patch.setattr(runner.json, "dumps", fail)
        else:
            patch.setattr(runner.sys, "stdout", SimpleNamespace(
                write=fail if phase == "write" else lambda value: None,
                flush=fail if phase == "flush" else lambda: None))
        assert runner.main(arguments("schema", pair)) == (130 if phase == "interrupt" else 3)
    output = capsys.readouterr()
    assert output.out == "" and "secret-input" not in output.err and "Traceback" not in output.err


def test_unsupported_platform_refuses_before_spawn(monkeypatch):
    monkeypatch.setattr(runner.os, "name", "nt")
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: pytest.fail("spawn"))
    with pytest.raises(RuntimeError):
        runner._collect([], 1)


@pytest.mark.parametrize("args,code", [([], 2), (["--help"], 0), (["schema", "--help"], 0),
                                      (["schema"], 2), (["secret-input"], 2)])
def test_fresh_import_help_and_refusal_do_not_connect_or_spawn(args, code, tmp_path):
    source = r'''
import sys
from pathlib import Path
root = Path(sys.argv[1])
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        p = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).resolve()
        if p.name.startswith(".env") or p.is_relative_to(root / "logs"):
            raise AssertionError("private access")
    if event.startswith(("socket.", "sqlite3.", "subprocess.")):
        raise AssertionError("external access")
    if event == "import" and (args[0] in {"sqlalchemy", "config", "dotenv", "streamlit", "openai"}
                              or args[0].startswith("database.")):
        raise AssertionError("dependency")
sys.addaudithook(audit)
sys.path.insert(0, str(root))
import development_inspection_run as runner
raise SystemExit(runner.main(sys.argv[2:]))
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", source, str(runner.ROOT), *args],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == code
    if code == 2:
        assert result.stdout == "" and result.stderr == runner.ARGUMENT_ERROR + "\n"
    else:
        assert result.stderr == "" and "--help" in result.stdout


def test_direct_script_from_other_directory(pair, tmp_path):
    for operation, expected in (("schema", 0), ("snapshots", 1), ("integrity", 3)):
        if expected == 1:
            clear(pair[1])
        if expected == 3:
            mutate(pair[0], "DELETE FROM development_atomic_cost")
        result = subprocess.run([sys.executable, str(runner.ROOT / "development_inspection_run.py"),
                                 *arguments(operation, pair)], cwd=tmp_path,
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == expected
        if expected == 3:
            assert result.stdout == "" and result.stderr == runner.EXECUTION_ERROR + "\n"
        else:
            assert result.stderr == ""
            assert_result(result.stdout, operation, expected, pair)


@pytest.mark.parametrize("operation", producer.COMMANDS)
def test_main_process_deadline_is_failure_and_child_is_reaped(pair, processes, capsys, operation):
    assert runner.main(arguments(operation, pair) + ["--process-timeout", "0.000001"]) == 3
    assert capsys.readouterr() == ("", runner.EXECUTION_ERROR + "\n")
    assert len(processes) == 1


@pytest.mark.parametrize("action", [
    "open(root / '.env')",
    "open(root / 'logs' / 'requirements.txt')",
    "import socket; socket.socket()",
    "import database.connection",
    "import engine.pricing",
    "import dotenv",
    "import subprocess; subprocess.Popen([sys.executable, '-c', 'pass'])",
])
def test_worker_audit_rejects_private_network_production_and_spawn(pair, monkeypatch, processes, capsys, action):
    # 在同一真實 bootstrap 的稽核安裝後注入禁止操作；私人檔案在 open 前即拒絕。
    worker = runner._WORKER.replace("import development_inspection\n", action + "\nimport development_inspection\n")
    monkeypatch.setattr(runner, "_WORKER", worker)
    assert runner.main(arguments("schema", pair)) == 3
    assert capsys.readouterr() == ("", runner.EXECUTION_ERROR + "\n")
    assert len(processes) == 1 and processes[0].returncode != 0


def test_stdin_not_read_and_no_environment_secrets_in_worker(pair, monkeypatch, capsys):
    class NoInput:
        def read(self, *args):
            pytest.fail("stdin must not be read")
    monkeypatch.setattr(sys, "stdin", NoInput())
    monkeypatch.setenv("PRIVATE_TEST_TOKEN", "secret-input")
    worker = runner._WORKER.replace("import development_inspection\n",
        "import os\nassert 'PRIVATE_TEST_TOKEN' not in os.environ\nassert sys.stdin.read() == ''\n"
        "import development_inspection\n")
    monkeypatch.setattr(runner, "_WORKER", worker)
    assert runner.main(arguments("schema", pair)) == 0
    assert_result(capsys.readouterr().out, "schema", 0, pair)


def test_spawn_failure_is_sanitized_without_a_child(pair, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise OSError("secret-input")
    monkeypatch.setattr(runner.subprocess, "Popen", fail)
    assert runner.main(arguments("schema", pair)) == 3
    assert capsys.readouterr() == ("", runner.EXECUTION_ERROR + "\n")


def test_self_consistent_forgery_is_not_authenticated(pair, monkeypatch, capsys):
    assert producer.main(arguments("schema", pair)) == 0
    report = json.loads(capsys.readouterr().out)
    report["report"]["expected_digest"] = report["report"]["observed_digest"] = "a" * 64
    payload = json.dumps(report)
    # 受信任本機程式若被替換，格式自洽仍可能通過；run_id 不是簽章。
    monkeypatch.setattr(runner, "_WORKER", f"print({payload!r})")
    assert runner.main(arguments("schema", pair)) == 0
    result = assert_result(capsys.readouterr().out, "schema", 0, pair)
    assert result["inspection"] == report and "不是簽章" in result["warning"]
