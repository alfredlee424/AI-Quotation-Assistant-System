"""開發報告原子發布、無覆寫、故障界線及真實受控檢查測試。"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import development_inspection as producer
import development_inspection_archive as archive
import development_inspection_run as runner
from development_atomic_fixtures import request
from test_development_atomic_snapshot import database, reserve, service
from test_development_integrity_inspection import dump, mutate
from test_development_snapshot_comparison import clear, clone, pair
from test_development_inspection_cli import arguments, assert_private
from test_development_inspection_run import assert_result


@pytest.fixture
def destination(tmp_path):
    path = tmp_path / "private-reports"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def document(database, capsys, monkeypatch):
    args = arguments("schema", (Path(database.url.database),) * 2)
    assert producer.main(args) == 0
    payload = capsys.readouterr().out.encode()
    monkeypatch.setattr(runner, "_collect", lambda *a: (payload, 0))
    return runner._run("schema", [], 5)[0]


def options(operation, paths, destination, name="report.json"):
    return arguments(operation, paths) + ["--output-directory", str(destination), "--report-name", name]


def check_receipt(output, destination, operation, code, paths):
    receipt = json.loads(output)
    payload = (destination / "report.json").read_bytes()
    result = assert_result(payload.decode("utf-8"), operation, code, paths)
    assert receipt == {"archive_version": archive.ARCHIVE_VERSION, "run_id": result["run_id"],
                       "operation": operation, "status": result["status"], "inspection_exit_code": code,
                       "publication": "published", "byte_count": len(payload),
                       "sha256": hashlib.sha256(payload).hexdigest(), "warning": archive.WARNING,
                       **dict.fromkeys(archive.FLAGS, False)}
    assert (destination / "report.json").stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in destination.iterdir()) == ["report.json"]
    assert_private(output, (*paths, destination))
    assert str(destination) not in output


@pytest.mark.parametrize("operation", producer.COMMANDS)
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("state", ["empty", "reserved", "saved"])
def test_real_archive(database, tmp_path, destination, capsys, operation, journal, state):
    path = Path(database.url.database)
    mutate(path, f"PRAGMA journal_mode={journal}")
    if state != "empty":
        reserve(database, request())
    if state == "saved":
        service(database).save(request())
    paths = path, clone(path, tmp_path / "candidate.sqlite")
    before = tuple(dump(p) for p in paths)
    assert archive.main(options(operation, paths, destination)) == 0
    output = capsys.readouterr()
    assert output.err == ""
    check_receipt(output.out, destination, operation, 0, paths)
    assert tuple(dump(p) for p in paths) == before


@pytest.mark.parametrize("operation", ["schema", "registry", "snapshots"])
def test_difference_is_published_but_remains_exit_one(pair, destination, capsys, operation):
    if operation == "schema":
        mutate(pair[0], "DROP TABLE development_atomic_cost")
    else:
        clear(pair[1])
    assert archive.main(options(operation, pair, destination)) == 1
    output = capsys.readouterr()
    assert output.err == ""
    check_receipt(output.out, destination, operation, 1, pair)


def test_failed_inspection_does_not_create_report(pair, destination, capsys):
    mutate(pair[0], "DELETE FROM development_atomic_cost")
    assert archive.main(options("integrity", pair, destination)) == 3
    assert capsys.readouterr() == ("", archive.EXECUTION_ERROR + "\n")
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("name", ["../report.json", "/report.json", ".env.json", ".report.json",
                                  "a/b.json", "a\\b.json", "a.json.tmp", "a.json\n", "a" * 81 + ".json"])
def test_bad_names_do_not_execute(pair, destination, monkeypatch, capsys, name):
    monkeypatch.setattr(runner, "_run", lambda *a: pytest.fail("executed"))
    assert archive.main(options("schema", pair, destination, name)) == 2
    assert capsys.readouterr() == ("", archive.ARGUMENT_ERROR + "\n")
    assert not list(destination.iterdir())


@pytest.mark.parametrize("extra", [["--report-name", "other.json"], ["--report", "x"],
                                    ["--output-directory", "x"], ["--process-timeout", "nan"],
                                    ["--inspection-exit-code", "0"], ["--script", "x"]])
def test_invalid_options(pair, destination, monkeypatch, capsys, extra):
    monkeypatch.setattr(runner, "_run", lambda *a: pytest.fail("executed"))
    assert archive.main(options("schema", pair, destination) + extra) == 2
    assert capsys.readouterr() == ("", archive.ARGUMENT_ERROR + "\n")


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling", "hardlink"])
def test_existing_target_never_opened_or_overwritten(pair, destination, monkeypatch, capsys, kind):
    target = destination / "report.json"
    if kind == "file":
        target.write_bytes(b"existing")
    elif kind == "directory":
        target.mkdir()
    elif kind in ("symlink", "dangling"):
        target.symlink_to(pair[0] if kind == "symlink" else destination / "absent")
    else:
        target.hardlink_to(pair[0])
    before = tuple(dump(p) for p in pair)
    info = target.lstat()
    monkeypatch.setattr(runner, "_run", lambda *a: pytest.fail("executed"))
    assert archive.main(options("schema", pair, destination)) == 2
    assert capsys.readouterr() == ("", archive.ARGUMENT_ERROR + "\n")
    assert target.lstat() == info
    assert tuple(dump(p) for p in pair) == before


@pytest.mark.parametrize("kind", ["missing", "file", "symlink", "shared", "workspace", "uri", "private"])
def test_invalid_directory(pair, destination, monkeypatch, capsys, kind):
    path = destination
    if kind == "missing":
        path = destination / "missing"
    elif kind == "file":
        path = pair[0]
    elif kind == "symlink":
        path = destination.parent / "link"
        path.symlink_to(destination, target_is_directory=True)
    elif kind == "shared":
        destination.chmod(0o750)
    elif kind == "workspace":
        path = runner.ROOT
    elif kind == "uri":
        path = destination.as_uri()
    elif kind == "private":
        path = destination / ".env-data"
        path.mkdir(mode=0o700)
    monkeypatch.setattr(runner, "_run", lambda *a: pytest.fail("executed"))
    assert archive.main(options("schema", pair, path)) == 2
    assert capsys.readouterr() == ("", archive.ARGUMENT_ERROR + "\n")


@pytest.mark.parametrize("field,value", [("run_version", "unknown"), ("run_id", "unknown"),
    ("warning", "secret"), ("status", "unknown"), ("operation", "snapshots"),
    ("inspection_exit_code", False), ("inspection_exit_code", 1), ("validation_version", "unknown"),
    *[(flag, True) for flag in archive.FLAGS], ("extra", "secret")])
def test_reject_invalid_envelope_before_writing(document, pair, destination, monkeypatch, capsys, field, value):
    document[field] = value
    monkeypatch.setattr(runner, "_run", lambda *a: (document, 0))
    assert archive.main(options("schema", pair, destination)) == 3
    assert capsys.readouterr() == ("", archive.EXECUTION_ERROR + "\n")
    assert not list(destination.iterdir())


def test_inner_report_and_capacity_rechecked(document, monkeypatch):
    invalid = deepcopy(document)
    invalid["inspection"]["extra"] = "secret"
    with pytest.raises(ValueError):
        archive._encode(invalid, "schema", 0)
    payload = archive._encode(document, "schema", 0)
    monkeypatch.setattr(archive, "MAX_ARCHIVE_BYTES", len(payload))
    assert archive._encode(document, "schema", 0) == payload
    monkeypatch.setattr(archive, "MAX_ARCHIVE_BYTES", len(payload) - 1)
    with pytest.raises(ValueError):
        archive._encode(document, "schema", 0)


@pytest.mark.parametrize("phase", ["create", "chmod", "write", "zero", "file_sync", "link", "unlink", "dir_sync"])
def test_failure_boundaries(document, pair, destination, monkeypatch, capsys, phase):
    monkeypatch.setattr(runner, "_run", lambda *a: (document, 0))
    def fail(*a, **kw):
        raise OSError("secret path SQL")
    if phase == "create":
        original = os.open
        def opening(path, flags, *a, **kw):
            return fail() if flags & os.O_CREAT else original(path, flags, *a, **kw)
        monkeypatch.setattr(os, "open", opening)
    elif phase in ("chmod", "write", "link"):
        monkeypatch.setattr(os, {"chmod": "fchmod", "write": "write", "link": "link"}[phase], fail)
    elif phase == "zero":
        monkeypatch.setattr(os, "write", lambda *a: 0)
    elif phase == "unlink":
        original = os.unlink
        calls = []
        def unlink(*a, **kw):
            calls.append(1)
            return fail() if len(calls) == 1 else original(*a, **kw)
        monkeypatch.setattr(os, "unlink", unlink)
    else:
        original = os.fsync
        calls = []
        def sync(fd):
            calls.append(1)
            return fail() if len(calls) == (2 if phase == "file_sync" else 3) else original(fd)
        monkeypatch.setattr(os, "fsync", sync)
    assert archive.main(options("schema", pair, destination)) == 3
    assert capsys.readouterr() == ("", archive.EXECUTION_ERROR + "\n")
    if phase in ("unlink", "dir_sync"):
        assert (destination / "report.json").read_bytes() == archive._encode(document, "schema", 0)
        assert len(list(destination.iterdir())) == 1
    else:
        assert not list(destination.iterdir())


def test_short_writes_and_no_clobber_race(document, pair, destination, monkeypatch, capsys):
    monkeypatch.setattr(runner, "_run", lambda *a: (document, 0))
    original = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: original(fd, data[:17]))
    assert archive.main(options("schema", pair, destination)) == 0
    check_receipt(capsys.readouterr().out, destination, "schema", 0, pair)
    original_link = os.link
    def competing(source, target, **kw):
        (destination / "second.json").write_bytes(b"winner")
        return original_link(source, target, **kw)
    monkeypatch.setattr(os, "link", competing)
    assert archive.main(options("schema", pair, destination, "second.json")) == 3
    assert capsys.readouterr() == ("", archive.EXECUTION_ERROR + "\n")
    assert (destination / "second.json").read_bytes() == b"winner"
    assert not list(destination.glob("*.tmp"))


@pytest.mark.parametrize("phase", ["write", "flush"])
def test_response_failure_keeps_published_report(document, pair, destination, monkeypatch, capsys, phase):
    monkeypatch.setattr(runner, "_run", lambda *a: (document, 0))
    class Output:
        def write(self, value):
            if phase == "write":
                raise OSError("secret")
        def flush(self):
            if phase == "flush":
                raise OSError("secret")
    with monkeypatch.context() as m:
        m.setattr(sys, "stdout", Output())
        assert archive.main(options("schema", pair, destination)) == 3
    assert capsys.readouterr() == ("", archive.EXECUTION_ERROR + "\n")
    assert (destination / "report.json").read_bytes() == archive._encode(document, "schema", 0)
    monkeypatch.setattr(runner, "_run", lambda *a: pytest.fail("retry executed"))
    assert archive.main(options("schema", pair, destination)) == 2


def test_pinned_directory_not_redirected(document, pair, destination, monkeypatch, capsys):
    moved = destination.with_name("moved")
    def run(*a):
        destination.rename(moved)
        destination.mkdir(mode=0o700)
        return document, 0
    monkeypatch.setattr(runner, "_run", run)
    assert archive.main(options("schema", pair, destination)) == 0
    check_receipt(capsys.readouterr().out, moved, "schema", 0, pair)
    assert not list(destination.iterdir())


_WORKER = r'''
import sys, os
sys.path.insert(0, sys.argv[1])
import development_inspection_archive as archive
directory, phase, payload = sys.argv[2], sys.argv[3], bytes.fromhex(sys.argv[4])
with archive._destination(directory, "report.json") as fd:
    if phase == "race":
        print("ready", flush=True)
        sys.stdin.readline()
    else:
        original = os.link
        def link(*a, **kw):
            if phase == "after":
                original(*a, **kw)
            print("ready", flush=True)
            sys.stdin.readline()
            if phase == "before":
                original(*a, **kw)
        os.link = link
    try:
        archive._publish(fd, "report.json", payload)
    except FileExistsError:
        raise SystemExit(4)
'''


def child(destination, phase, payload):
    return subprocess.Popen([sys.executable, "-I", "-B", "-c", _WORKER, str(runner.ROOT),
                             str(destination), phase, payload.hex()], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={})


def ready(process):
    # 有界管線同步，不使用任意 sleep 或等待無期限 readline。
    import selectors
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(10)
        assert process.stdout.readline() == "ready\n"


def close_child(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    for stream in (process.stdin, process.stdout, process.stderr):
        stream.close()


@pytest.mark.parametrize("phase", ["before", "after"])
def test_hard_stop_only_absent_or_complete_final(document, destination, phase):
    payload = archive._encode(document, "schema", 0)
    process = child(destination, phase, payload)
    try:
        ready(process)
        process.kill()
        process.wait(timeout=5)
        if phase == "before":
            assert not (destination / "report.json").exists()
        else:
            assert (destination / "report.json").read_bytes() == payload
        leftovers = list(destination.glob(".inspection-*.tmp"))
        assert len(leftovers) == 1
        assert leftovers[0].read_bytes() == payload
    finally:
        close_child(process)


def test_real_process_race_has_one_winner(document, destination):
    payload = archive._encode(document, "schema", 0)
    processes = [child(destination, "race", payload) for _ in range(2)]
    try:
        for process in processes:
            ready(process)
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.flush()
        assert sorted(p.wait(timeout=10) for p in processes) == [0, 4]
        assert (destination / "report.json").read_bytes() == payload
        assert len(list(destination.iterdir())) == 1
    finally:
        for process in processes:
            close_child(process)


def test_script_relative_paths_and_missing_consent(pair, destination, tmp_path):
    args = options("schema", pair, destination.relative_to(tmp_path))
    command = [sys.executable, "-B", str(runner.ROOT / "development_inspection_archive.py")]
    result = subprocess.run(command + args, cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and result.stderr == ""
    check_receipt(result.stdout, destination, "schema", 0, pair)
    args.remove("--development-only")
    result = subprocess.run(command + args, cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 2
    assert result.stdout == "" and result.stderr == archive.ARGUMENT_ERROR + "\n"


@pytest.mark.parametrize("args", [["--help"], []])
def test_import_help_and_rejection_are_isolated(tmp_path, args):
    source = r'''
import sys
from pathlib import Path
root = Path(sys.argv[1])
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.name.startswith(".env") or path.is_relative_to(root / "logs"):
            raise AssertionError("private access")
    if event.startswith(("sqlite3.", "socket.", "subprocess.")):
        raise AssertionError("external access")
    if event == "import" and (args[0] in {"sqlalchemy", "config", "dotenv", "openai", "streamlit"}
                              or args[0].startswith("database.")):
        raise AssertionError("dependency")
import os
sys.addaudithook(audit)
sys.path.insert(0, str(root))
import development_inspection_archive
raise SystemExit(development_inspection_archive.main(sys.argv[2:]))
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", source, str(runner.ROOT), *args],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10, env={})
    assert result.returncode == (0 if args else 2)
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("phase", ["run", "write", "file_sync", "after_link"])
def test_interrupt_cleanup(document, pair, destination, monkeypatch, capsys, phase):
    def interrupt(*a, **kw):
        raise KeyboardInterrupt()
    monkeypatch.setattr(runner, "_run", interrupt if phase == "run" else lambda *a: (document, 0))
    if phase == "write":
        monkeypatch.setattr(os, "write", interrupt)
    elif phase == "file_sync":
        original = os.fsync
        calls = []
        def sync(fd):
            calls.append(1)
            return interrupt() if len(calls) == 2 else original(fd)
        monkeypatch.setattr(os, "fsync", sync)
    elif phase == "after_link":
        original = os.link
        def link(*a, **kw):
            original(*a, **kw)
            interrupt()
        monkeypatch.setattr(os, "link", link)
    assert archive.main(options("schema", pair, destination)) == 130
    assert capsys.readouterr() == ("", archive.EXECUTION_ERROR + "\n")
    if phase == "after_link":
        assert (destination / "report.json").read_bytes() == archive._encode(document, "schema", 0)
    else:
        assert not list(destination.iterdir())
    assert not list(destination.glob(".inspection-*.tmp"))


@pytest.mark.parametrize("kind,code", [("owner", 2), ("platform", 3), ("sync", 3)])
def test_destination_preflight_before_execution(pair, destination, monkeypatch, capsys, kind, code):
    monkeypatch.setattr(runner, "_run", lambda *a: pytest.fail("executed"))
    if kind == "owner":
        monkeypatch.setattr(os, "geteuid", lambda: destination.stat().st_uid + 1)
    elif kind == "platform":
        # 局部取代模組引用，不修改全域 os.name 破壞 pathlib／pytest。
        from types import SimpleNamespace
        monkeypatch.setattr(archive, "os", SimpleNamespace(name="nt"))
    else:
        monkeypatch.setattr(os, "fsync", lambda *a: (_ for _ in ()).throw(OSError("sync failure")))
    assert archive.main(options("schema", pair, destination)) == code
    error = archive.ARGUMENT_ERROR if code == 2 else archive.EXECUTION_ERROR
    assert capsys.readouterr() == ("", error + "\n")
    assert not list(destination.iterdir())


def test_partial_temp_write_failure_never_publishes(document, pair, destination, monkeypatch, capsys):
    monkeypatch.setattr(runner, "_run", lambda *a: (document, 0))
    original = os.write
    calls = []
    def write(fd, data):
        calls.append(1)
        if len(calls) > 1:
            raise OSError("disk full")
        assert not (destination / "report.json").exists()
        return original(fd, data[:7])
    monkeypatch.setattr(os, "write", write)
    assert archive.main(options("schema", pair, destination)) == 3
    assert capsys.readouterr() == ("", archive.EXECUTION_ERROR + "\n")
    assert len(calls) == 2 and not list(destination.iterdir())


@pytest.mark.parametrize("code", [False, True, -1, 2, 3, 130, "0"])
def test_return_code_is_strict(document, code):
    with pytest.raises(ValueError):
        archive._encode(document, "schema", code)


def test_success_then_new_failure_preserves_old_only(pair, destination, capsys):
    assert archive.main(options("integrity", pair, destination)) == 0
    capsys.readouterr()
    previous = (destination / "report.json").read_bytes()
    mutate(pair[0], "DELETE FROM development_atomic_cost")
    assert archive.main(options("integrity", pair, destination, "new.json")) == 3
    assert capsys.readouterr() == ("", archive.EXECUTION_ERROR + "\n")
    assert (destination / "report.json").read_bytes() == previous
    assert not (destination / "new.json").exists()


def test_self_consistent_forgery_is_not_authenticated(document):
    # 固定文件與原子存檔不是簽章；可信程式／本機若失守，自洽偽造仍能通過。
    document["run_id"] = "00000000-0000-4000-8000-000000000001"
    payload = archive._encode(document, "schema", 0)
    assert json.loads(payload)["run_id"] == document["run_id"]
