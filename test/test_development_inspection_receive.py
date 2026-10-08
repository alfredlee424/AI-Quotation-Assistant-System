"""受控接收必須觀察真實退出；回條、核驗、原子發布各自保留失敗界線。"""
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys

import pytest

import development_inspection_receive as receive
from development_atomic_fixtures import request
from test_development_atomic_snapshot import database, reserve, service
from test_development_integrity_inspection import dump, mutate
from test_development_snapshot_comparison import clear, clone, pair
from test_development_inspection_archive import destination, document, options
from test_development_inspection_cli import assert_private


def args(operation, paths, directory, name="reception.json"):
    return options(operation, paths, directory) + ["--reception-name", name]


def check(directory, output, operation, code, paths):
    report = (directory / "report.json").read_bytes()
    payload = (directory / "reception.json").read_bytes()
    record = json.loads(payload)
    summary = json.loads(output)
    receipt = record["receipt_utf8"].encode("utf-8")
    assert record["reception_version"] == summary["reception_version"] == receive.RECEPTION_VERSION
    assert record["archive_process_exit_code"] == record["inspection_exit_code"] == code
    assert summary["archive_process_exit_code"] == summary["inspection_exit_code"] == code
    assert summary["publication"] == "published"
    assert summary["byte_count"] == len(payload)
    assert summary["sha256"] == hashlib.sha256(payload).hexdigest()
    assert record["receipt_byte_count"] == len(receipt)
    assert record["receipt_sha256"] == hashlib.sha256(receipt).hexdigest()
    verified = receive.validation.validate_development_archive(
        report, receipt, expected_operation=operation, expected_run_id=record["run_id"])
    assert receive.asdict(verified) == record["validation"]
    for value in (record, summary):
        assert value["warning"] == receive.WARNING
        assert all(value[key] is False for key in receive.archive.FLAGS)
    for name in ("report.json", "reception.json"):
        assert (directory / name).stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in directory.iterdir()) == ["reception.json", "report.json"]
    assert_private(payload.decode(), (*paths, directory))
    assert_private(output, (*paths, directory))
    return report, receipt, record


@pytest.mark.parametrize("operation", receive.inspection.COMMANDS)
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("state", ["empty", "reserved", "saved"])
def test_real_chain(database, tmp_path, destination, capsys, operation, journal, state):
    path = Path(database.url.database)
    mutate(path, f"PRAGMA journal_mode={journal}")
    if state != "empty":
        reserve(database, request())
    if state == "saved":
        service(database).save(request())
    paths = path, clone(path, tmp_path / "candidate.sqlite")
    before = tuple(dump(p) for p in paths)
    assert receive.main(args(operation, paths, destination)) == 0
    output = capsys.readouterr()
    assert not output.err
    report, receipt, record = check(destination, output.out, operation, 0, paths)
    assert tuple(dump(p) for p in paths) == before
    for p in paths:
        p.unlink()
    assert receive.validation.validate_development_archive(
        report, receipt, expected_operation=operation, expected_run_id=record["run_id"])


@pytest.mark.parametrize("operation", ["schema", "registry", "snapshots"])
def test_real_difference(pair, destination, capsys, operation):
    if operation == "schema":
        mutate(pair[0], "DROP TABLE development_atomic_cost")
    else:
        clear(pair[1])
    assert receive.main(args(operation, pair, destination)) == 1
    output = capsys.readouterr()
    assert not output.err
    check(destination, output.out, operation, 1, pair)


def test_real_corruption_then_new_success_no_old_result(pair, destination, capsys):
    mutate(pair[0], "DELETE FROM development_atomic_cost")
    assert receive.main(args("integrity", pair, destination)) == 3
    assert capsys.readouterr() == ("", receive.EXECUTION_ERROR + "\n")
    assert not list(destination.iterdir())
    assert receive.main(args("schema", pair, destination)) == 0
    check(destination, capsys.readouterr().out, "schema", 0, pair)
    assert receive.main(args("schema", pair, destination)) == 2
    assert capsys.readouterr() == ("", receive.ARGUMENT_ERROR + "\n")


@pytest.mark.parametrize("name", ["report.json", "REPORT.json", "../r.json", ".env.json", "r.JSON",
                                  "a/b.json", "r.json\n", "a" * 81 + ".json"])
def test_invalid_names_before_spawn(pair, destination, capsys, monkeypatch, name):
    monkeypatch.setattr(receive, "_collect", lambda *a: pytest.fail("spawn"))
    assert receive.main(args("schema", pair, destination, name)) == 2
    assert capsys.readouterr() == ("", receive.ARGUMENT_ERROR + "\n")
    assert not list(destination.iterdir())


@pytest.mark.parametrize("extra", [["--archive-timeout", "nan"], ["--archive-timeout", "0"],
    ["--archive-timeout", "121"], ["--archive-timeout", "inf"], ["--archive-timeout", "abc"],
    ["--reception-name", "other.json"], ["--archive-time", "2"], ["--script", "secret"],
    ["--archive-process-exit-code", "0"], ["--receipt", "secret"], ["--process-timeout", "61"]])
def test_invalid_options(pair, destination, capsys, monkeypatch, extra):
    monkeypatch.setattr(receive, "_collect", lambda *a: pytest.fail("spawn"))
    assert receive.main(args("schema", pair, destination) + extra) == 2
    assert capsys.readouterr() == ("", receive.ARGUMENT_ERROR + "\n")


@pytest.mark.parametrize("target", ["report.json", "reception.json"])
@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "hardlink", "dangling"])
def test_existing_either_target_rejects_without_spawn(pair, destination, monkeypatch, capsys, target, kind):
    path = destination / target
    if kind == "file":
        path.write_bytes(b"existing")
    elif kind == "directory":
        path.mkdir()
    elif kind == "hardlink":
        path.hardlink_to(pair[0])
    else:
        path.symlink_to(pair[0] if kind == "symlink" else destination / "absent")
    info = path.lstat()
    monkeypatch.setattr(receive, "_collect", lambda *a: pytest.fail("spawn"))
    assert receive.main(args("schema", pair, destination)) == 2
    assert path.lstat() == info
    assert capsys.readouterr() == ("", receive.ARGUMENT_ERROR + "\n")


@pytest.fixture
def synthetic(document):
    payload = receive.archive._encode(document, "schema", 0)
    receipt = {"archive_version": receive.archive.ARCHIVE_VERSION, "run_id": document["run_id"],
               "operation": "schema", "status": document["status"], "inspection_exit_code": 0,
               "publication": "published", "byte_count": len(payload),
               "sha256": hashlib.sha256(payload).hexdigest(), "warning": receive.archive.WARNING,
               **dict.fromkeys(receive.archive.FLAGS, False)}
    return payload, (json.dumps(receipt) + "\n").encode()


def fake_archive(monkeypatch, destination, synthetic, code=0):
    def collect(*a):
        (destination / "report.json").write_bytes(synthetic[0])
        (destination / "report.json").chmod(0o600)
        return synthetic[1], code
    monkeypatch.setattr(receive, "_collect", collect)


@pytest.mark.parametrize("kind", ["empty", "truncated", "extra", "duplicate", "unknown", "flag",
                                  "id", "digest", "count", "inner", "code", "oversize"])
def test_invalid_pair_never_publishes_reception(synthetic, pair, destination, monkeypatch, capsys, kind):
    payload, receipt = synthetic
    doc = json.loads(receipt)
    code = 0
    if kind == "empty":
        receipt = b""
    elif kind == "truncated":
        receipt = receipt[:-5]
    elif kind == "extra":
        receipt += b"{}"
    elif kind == "duplicate":
        receipt = receipt.replace(b'{', b'{"run_id":"duplicate",', 1)
    elif kind == "oversize":
        receipt += b" " * receive.validation.MAX_RECEIPT_BYTES
    elif kind == "code":
        code = 1
    elif kind == "inner":
        changed = json.loads(payload)
        changed["inspection"]["unexpected"] = "secret"
        payload = json.dumps(changed).encode()
        doc.update(byte_count=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        receipt = json.dumps(doc).encode()
    else:
        field, value = {"unknown": ("unknown", "secret"), "flag": ("can_save", True),
                        "id": ("run_id", "invalid"), "digest": ("sha256", "0" * 64),
                        "count": ("byte_count", True)}[kind]
        doc[field] = value
        receipt = json.dumps(doc).encode()
    fake_archive(monkeypatch, destination, (payload, receipt), code)
    assert receive.main(args("schema", pair, destination)) == 3
    assert capsys.readouterr() == ("", receive.EXECUTION_ERROR + "\n")
    assert (destination / "report.json").read_bytes() == payload
    assert not (destination / "reception.json").exists()


def test_raw_receipt_bytes_and_capacity(synthetic, monkeypatch):
    payload, receipt = synthetic
    receipt += b" \t\r\n"
    encoded, _ = receive._encode(payload, receipt, "schema", 0)
    assert json.loads(encoded)["receipt_utf8"].encode() == receipt
    monkeypatch.setattr(receive, "MAX_RECEPTION_BYTES", len(encoded))
    assert receive._encode(payload, receipt, "schema", 0)[0] == encoded
    monkeypatch.setattr(receive, "MAX_RECEPTION_BYTES", len(encoded) - 1)
    with pytest.raises(ValueError):
        receive._encode(payload, receipt, "schema", 0)
    for code in (False, "0", -9, 2):
        with pytest.raises(ValueError):
            receive._encode(payload, receipt, "schema", code)


@pytest.mark.parametrize("phase", ["write", "zero", "short", "file_sync", "link", "dir_sync", "race"])
def test_reception_publication_failure_keeps_report(synthetic, pair, destination, monkeypatch, capsys, phase):
    fake_archive(monkeypatch, destination, synthetic)
    original_publish = receive.archive._publish
    def publish(fd, name, payload):
        with monkeypatch.context() as patch:
            def fail(*a, **kw):
                raise OSError("secret SQL path")
            if phase == "write":
                patch.setattr(os, "write", fail)
            elif phase == "zero":
                patch.setattr(os, "write", lambda *a: 0)
            elif phase == "short":
                original = os.write
                patch.setattr(os, "write", lambda f, p: original(f, p[:7]))
            elif phase in ("file_sync", "dir_sync"):
                original = os.fsync
                def sync(f):
                    if (f == fd) == (phase == "dir_sync"):
                        fail()
                    return original(f)
                patch.setattr(os, "fsync", sync)
            elif phase == "link":
                patch.setattr(os, "link", fail)
            else:
                (destination / name).write_bytes(b"winner")
            original_publish(fd, name, payload)
    monkeypatch.setattr(receive.archive, "_publish", publish)
    assert receive.main(args("schema", pair, destination)) == (0 if phase == "short" else 3)
    output = capsys.readouterr()
    assert (destination / "report.json").read_bytes() == synthetic[0]
    if phase == "short":
        check(destination, output.out, "schema", 0, pair)
    else:
        assert output == ("", receive.EXECUTION_ERROR + "\n")
        if phase == "dir_sync":
            assert json.loads((destination / "reception.json").read_bytes())["archive_process_exit_code"] == 0
        elif phase == "race":
            assert (destination / "reception.json").read_bytes() == b"winner"
        else:
            assert not (destination / "reception.json").exists()
    assert not list(destination.glob(".inspection-*"))


@pytest.fixture
def processes(monkeypatch):
    created = []
    original = subprocess.Popen
    def popen(*a, **kw):
        process = original(*a, **kw)
        created.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", popen)
    yield created
    for process in created:
        assert process.poll() is not None
        assert process.stdout.closed and process.stderr.closed


@pytest.mark.parametrize("source", [
    "import os; os.write(1, b'{}'); raise SystemExit(3)",
    "import os,signal; os.write(1, b'{}'); os.kill(os.getpid(),signal.SIGKILL)",
    "import os,time; os.write(1, b'{}'); time.sleep(20)",
    "import os,time; os.close(1); os.close(2); time.sleep(20)",
    "import os; os.write(2,b'secret')",
    "import os; os.write(1,b'x'*8193)",
    "import os; os.write(2,b'x'*4097)",
])
def test_collector_rejects_and_reaps(processes, source):
    with pytest.raises((ValueError, TimeoutError, subprocess.TimeoutExpired)):
        receive._collect([sys.executable, "-I", "-B", "-c", source], 0.5)
    assert len(processes) == 1


@pytest.mark.parametrize("size", [1, receive.validation.MAX_RECEIPT_BYTES])
@pytest.mark.parametrize("code", [0, 1])
def test_collector_exact_boundaries(processes, size, code):
    source = f"import os; os.write(1, b'x'*{size}); raise SystemExit({code})"
    assert receive._collect([sys.executable, "-I", "-B", "-c", source], 5) == (b"x" * size, code)


@pytest.mark.parametrize("phase", ["death", "hang", "closed_hang", "stderr", "truncated"])
def test_real_archive_complete_receipt_does_not_replace_exit(pair, destination, capsys, monkeypatch, phase):
    tail = {
        "death": "os.kill(os.getpid(), 9)",
        "hang": "import time; time.sleep(20)",
        "closed_hang": "os.close(1); os.close(2); import time; time.sleep(20)",
        "stderr": "sys.stderr.write('secret'); sys.stderr.flush(); raise SystemExit(code)",
        "truncated": "raise SystemExit(code)",
    }[phase]
    if phase == "truncated":
        source = """
class Truncated:
    def write(self, text):
        return original.write(text[:19])
    def flush(self):
        original.flush()
original = sys.stdout
sys.stdout = Truncated()
code = archive.main(sys.argv[2:])
"""
    else:
        source = "code = archive.main(sys.argv[2:])\n"
    worker = receive._WORKER.replace("raise SystemExit(archive.main(sys.argv[2:]))", source + tail)
    monkeypatch.setattr(receive, "_WORKER", worker)
    assert receive.main(args("schema", pair, destination) + ["--archive-timeout", "2"]) == 3
    assert capsys.readouterr() == ("", receive.EXECUTION_ERROR + "\n")
    assert json.loads((destination / "report.json").read_bytes())
    assert not (destination / "reception.json").exists()


@pytest.mark.parametrize("action", ["open(root / '.env')", "open(root / 'logs' / 'requirements.txt')",
    "import sqlite3", "import database.connection", "import sqlalchemy", "import dotenv",
    "import socket; socket.socket()", "import subprocess; subprocess.Popen([sys.executable, '-c', 'pass'])"])
def test_worker_isolation(pair, destination, capsys, monkeypatch, action):
    worker = receive._WORKER.replace("import development_inspection_archive as archive",
                                     action + "\nimport development_inspection_archive as archive")
    monkeypatch.setattr(receive, "_WORKER", worker)
    assert receive.main(args("schema", pair, destination)) == 3
    assert capsys.readouterr() == ("", receive.EXECUTION_ERROR + "\n")
    assert not list(destination.iterdir())


def test_real_script_relative_paths(pair, destination, tmp_path):
    argv = args("snapshots", tuple(p.relative_to(tmp_path) for p in pair), destination.relative_to(tmp_path))
    result = subprocess.run([sys.executable, "-B", str(receive.runner.ROOT / "development_inspection_receive.py"),
                             *argv], cwd=tmp_path, capture_output=True, text=True, timeout=20, env={})
    assert result.returncode == 0 and not result.stderr
    check(destination, result.stdout, "snapshots", 0, pair)


@pytest.mark.parametrize("argv", [[], ["--help"], ["schema", "--help"]])
def test_inert_import_and_help(tmp_path, argv):
    source = r'''
import sys
from pathlib import Path
root = Path(sys.argv[1])
def audit(event, args):
    if event.startswith(("sqlite3.", "socket.", "subprocess.")):
        raise AssertionError("external operation")
    if event == "import" and args[0].split(".")[0] in {"database", "sqlalchemy", "config", "dotenv", "openai", "streamlit"}:
        raise AssertionError("dependency")
    if event == "open" and isinstance(args[0], (str, bytes)):
        p = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).resolve()
        if p.name.startswith(".env") or p.is_relative_to(root / "logs"):
            raise AssertionError("private")
sys.addaudithook(audit)
sys.path.insert(0, str(root))
import development_inspection_receive
raise SystemExit(development_inspection_receive.main(sys.argv[2:]))
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", source, str(receive.runner.ROOT), *argv],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10, env={})
    assert result.returncode == (0 if argv else 2)
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("kind", ["missing", "shared", "symlink", "workspace", "private_parent"])
def test_bad_directory_before_spawn(pair, destination, tmp_path, monkeypatch, capsys, kind):
    target = destination
    if kind == "missing":
        target = destination / "absent"
    elif kind == "shared":
        destination.chmod(0o750)
    elif kind == "symlink":
        target = tmp_path / "link"
        target.symlink_to(destination, target_is_directory=True)
    elif kind == "workspace":
        target = receive.runner.ROOT
    else:
        target = tmp_path / ".env-private" / "reports"
        target.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(receive, "_collect", lambda *a: pytest.fail("spawn"))
    assert receive.main(args("schema", pair, target)) == 2
    assert capsys.readouterr() == ("", receive.ARGUMENT_ERROR + "\n")


@pytest.mark.parametrize("kind", ["move", "permissions"])
def test_directory_changed_after_child_rejects(synthetic, pair, destination, monkeypatch, capsys, kind):
    fake_archive(monkeypatch, destination, synthetic)
    original = receive._collect
    def collect(*a):
        result = original(*a)
        if kind == "move":
            destination.rename(destination.with_name("moved"))
            destination.mkdir(mode=0o700)
        else:
            destination.chmod(0o750)
        return result
    monkeypatch.setattr(receive, "_collect", collect)
    assert receive.main(args("schema", pair, destination)) == 3
    assert capsys.readouterr() == ("", receive.EXECUTION_ERROR + "\n")
    assert not (destination / "reception.json").exists()
    if kind == "move":
        assert (destination.with_name("moved") / "report.json").read_bytes() == synthetic[0]


@pytest.mark.parametrize("kind", ["missing", "shared", "fifo", "symlink", "oversize", "replace"])
def test_bad_report_file_after_child(synthetic, pair, destination, monkeypatch, capsys, kind):
    fake_archive(monkeypatch, destination, synthetic)
    original = receive._collect
    def collect(*a):
        result = original(*a)
        target = destination / "report.json"
        if kind in ("missing", "fifo", "symlink"):
            target.unlink()
            if kind == "fifo":
                os.mkfifo(target, 0o600)
            elif kind == "symlink":
                target.symlink_to(pair[0])
        elif kind == "shared":
            target.chmod(0o640)
        elif kind == "oversize":
            with target.open("wb") as stream:
                stream.truncate(receive.validation.MAX_ARCHIVE_BYTES + 1)
        return result
    monkeypatch.setattr(receive, "_collect", collect)
    if kind == "replace":
        original_read = receive.reader._read_input
        def read(*a):
            result = original_read(*a)
            target = destination / "report.json"
            target.rename(destination / "original.json")
            target.write_bytes(synthetic[0])
            target.chmod(0o600)
            return result
        monkeypatch.setattr(receive.reader, "_read_input", read)
    assert receive.main(args("schema", pair, destination)) == 3
    assert capsys.readouterr() == ("", receive.EXECUTION_ERROR + "\n")
    assert not (destination / "reception.json").exists()


@pytest.mark.parametrize("phase", ["short", "write", "flush", "interrupt"])
def test_final_output_failure_keeps_complete_reception(synthetic, pair, destination, monkeypatch, capsys, phase):
    fake_archive(monkeypatch, destination, synthetic)
    class Output:
        def write(self, text):
            if phase == "short":
                return len(text) - 1
            if phase == "write":
                raise OSError("secret")
            if phase == "interrupt":
                raise KeyboardInterrupt()
            return len(text)
        def flush(self):
            raise OSError("secret")
    with monkeypatch.context() as patch:
        patch.setattr(receive.sys, "stdout", Output())
        assert receive.main(args("schema", pair, destination)) == (130 if phase == "interrupt" else 3)
    assert capsys.readouterr() == ("", receive.EXECUTION_ERROR + "\n")
    assert json.loads((destination / "reception.json").read_bytes())["archive_process_exit_code"] == 0
    assert (destination / "report.json").read_bytes() == synthetic[0]


@pytest.mark.parametrize("phase", ["spawn", "read", "interrupt", "selector"])
def test_collection_errors_reap_and_no_reception(pair, destination, monkeypatch, capsys, processes, phase):
    def fail(*a, **kw):
        raise KeyboardInterrupt() if phase == "interrupt" else OSError("secret")
    if phase == "spawn":
        monkeypatch.setattr(receive.subprocess, "Popen", fail)
    elif phase == "selector":
        original_selector = receive.selectors.DefaultSelector
        def selector():
            instance = original_selector()
            instance.select = fail
            return instance
        monkeypatch.setattr(receive.selectors, "DefaultSelector", selector)
    else:
        # Popen 本身亦使用 os.read；只在子程序完成啟動後注入。
        original_popen = receive.subprocess.Popen
        def popen(*a, **kw):
            result = original_popen(*a, **kw)
            monkeypatch.setattr(receive.os, "read", fail)
            return result
        monkeypatch.setattr(receive.subprocess, "Popen", popen)
    assert receive.main(args("schema", pair, destination)) == (130 if phase == "interrupt" else 3)
    assert capsys.readouterr() == ("", receive.EXECUTION_ERROR + "\n")
    assert not (destination / "reception.json").exists()


def test_descendant_holding_pipes_is_killed_after_leader_exits(tmp_path, monkeypatch, processes):
    # 額外控制管線只供測試驗證群組清理，EOF 表示後代已關閉；不以 sleep 猜清理結果。
    read_fd, write_fd = os.pipe()
    original = subprocess.Popen
    def popen(*a, **kw):
        kw["pass_fds"] = (write_fd,)
        process = original(*a, **kw)
        os.close(write_fd)
        return process
    monkeypatch.setattr(subprocess, "Popen", popen)
    descendant = f"import os,time; os.write({write_fd}, b'ready'); time.sleep(20)"
    source = ("import subprocess,sys,os\n"
              f"p=subprocess.Popen([sys.executable,'-I','-c',{descendant!r}],pass_fds=({write_fd},))\n"
              "os._exit(0)\n")
    try:
        with pytest.raises(TimeoutError):
            receive._collect([sys.executable, "-I", "-B", "-c", source], 1)
        with selectors.DefaultSelector() as selector:
            selector.register(read_fd, selectors.EVENT_READ)
            assert selector.select(3)
            assert os.read(read_fd, 5) == b"ready"
            assert selector.select(3)
            assert os.read(read_fd, 1) == b""
    finally:
        os.close(read_fd)


def test_two_real_receivers_compete_for_reception_only(pair, destination, tmp_path):
    # 兩個報告名稱不同；兩端都完成核驗後才競爭相同接收紀錄名稱。
    worker = r'''
import sys,os
sys.path.insert(0,sys.argv[1])
import development_inspection_receive as receive
original=receive.archive._publish
def publish(fd,name,payload):
    os.write(int(sys.argv[2]),b"ready")
    if os.read(int(sys.argv[3]),1)!=b"x":
        raise RuntimeError()
    original(fd,name,payload)
receive.archive._publish=publish
raise SystemExit(receive.main(sys.argv[4:]))
'''
    handles = []
    try:
        for name in ("report-a.json", "report-b.json"):
            ready_r, ready_w = os.pipe()
            go_r, go_w = os.pipe()
            argv = args("schema", pair, destination)
            argv[argv.index("--report-name") + 1] = name
            process = subprocess.Popen([sys.executable, "-I", "-B", "-c", worker,
                                        str(receive.runner.ROOT), str(ready_w), str(go_r), *argv],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={}, cwd=tmp_path,
                                       pass_fds=(ready_w, go_r), start_new_session=True)
            os.close(ready_w)
            os.close(go_r)
            handles.append((process, ready_r, go_w))
        for process, ready_r, _ in handles:
            with selectors.DefaultSelector() as selector:
                selector.register(ready_r, selectors.EVENT_READ)
                assert selector.select(15)
                assert os.read(ready_r, 5) == b"ready"
            assert process.poll() is None
        for _, _, go_w in handles:
            os.write(go_w, b"x")
        results = [(p, p.communicate(timeout=10)) for p, _, _ in handles]
        assert sorted(p.returncode for p, _ in results) == [0, 3]
        record = json.loads((destination / "reception.json").read_bytes())
        for process, (out, err) in results:
            if process.returncode == 0:
                assert json.loads(out)["run_id"] == record["run_id"] and not err
            else:
                assert not out and err.decode() == receive.EXECUTION_ERROR + "\n"
        assert (destination / "report-a.json").exists() and (destination / "report-b.json").exists()
        assert not list(destination.glob(".inspection-*"))
    finally:
        for process, ready_r, go_w in handles:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()
            os.close(ready_r)
            os.close(go_w)
