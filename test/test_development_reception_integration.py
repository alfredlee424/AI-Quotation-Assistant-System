"""真實受控接收→全新離線核驗；不以內容一致冒充歷史程序成功。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid

import pytest

import development_inspection as inspection
import development_inspection_archive as archive
import development_inspection_receive as receive
import development_reception_validation as validator
from engine import development_reception_validation as validation
from development_atomic_fixtures import request
from test_development_atomic_snapshot import database, reserve, service
from test_development_evidence_integration import finish, private_file, ready
from test_development_inspection_archive import destination, options
from test_development_inspection_cli import assert_private
from test_development_integrity_inspection import dump, mutate
from test_development_snapshot_comparison import clear, clone, pair


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX 接收鏈程序驗收")
WORKER = Path(__file__).with_name("development_reception_worker.py")


def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@contextmanager
def child(tmp_path, mode, arguments, phase="normal"):
    ready_read, ready_write = os.pipe()
    resume_read, resume_write = os.pipe()
    child_read, child_write = os.pipe()
    process = None
    archive_pid = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(WORKER), mode, phase,
             str(ready_write), str(resume_read), str(child_write), *arguments],
            cwd=tmp_path, env={}, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(ready_write, resume_read, child_write), start_new_session=True)
        os.close(ready_write)
        ready_write = None
        os.close(resume_read)
        resume_read = None
        os.close(child_write)
        child_write = None
        if phase.startswith("active_"):
            with selectors.DefaultSelector() as selector:
                selector.register(child_read, selectors.EVENT_READ)
                assert selector.select(20), "未收到存檔程序識別"
            pids = os.read(child_read, 8)
            assert len(pids) == 8
            archive_pid = int.from_bytes(pids, "big")
        yield process, ready_read, resume_write, archive_pid
    finally:
        if process is not None:
            kill_group(process.pid)
            process.wait(timeout=5)
            # 原接收器另開存檔群組，不能只清理最外層；固定只允許一次 spawn。
            if archive_pid is not None:
                kill_group(archive_pid)
                # active worker 的控制管線由存檔群組持有；有界排空並等 EOF，
                # 不把已送達的函式返回標記當成群組已結束。
                deadline = time.monotonic() + 5
                with selectors.DefaultSelector() as selector:
                    selector.register(ready_read, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        assert remaining > 0 and selector.select(remaining), "存檔群組未關閉控制管線"
                        if not os.read(ready_read, 16):
                            break
            else:
                os.set_blocking(child_read, False)
                pids = os.read(child_read, 9)
                if pids:
                    assert len(pids) == 8
                    kill_group(int.from_bytes(pids, "big"))
            for stream in (process.stdout, process.stderr):
                stream.close()
        for fd in (ready_read, ready_write, resume_read, resume_write, child_read, child_write):
            if fd is not None:
                os.close(fd)


def run(tmp_path, mode, arguments):
    with child(tmp_path, mode, arguments) as handle:
        return finish(handle[0])


def receive_args(operation, paths, directory, name="report.json", record="reception.json"):
    return options(operation, paths, directory, name) + ["--reception-name", record]


def validation_args(path, record, operation, run_id):
    return ["--development-only", "--operation", operation, "--run-id", run_id,
            "--archive", str(path), "--reception", str(record)]


def published(tmp_path, paths, directory, operation="integrity", name="report.json",
              record="reception.json", code=0):
    result = run(tmp_path, "receive", receive_args(operation, paths, directory, name, record))
    assert result.returncode == code and result.stderr == b""
    summary = json.loads(result.stdout)
    path, record_path = directory / name, directory / record
    payload, record_bytes = path.read_bytes(), record_path.read_bytes()
    document, reception = json.loads(payload), json.loads(record_bytes)
    receipt_bytes = reception["receipt_utf8"].encode("utf-8")
    receipt = json.loads(receipt_bytes)
    assert summary["publication"] == "published"
    assert summary["run_id"] == document["run_id"] == reception["run_id"] == receipt["run_id"]
    assert summary["sha256"] == hashlib.sha256(record_bytes).hexdigest()
    assert summary["byte_count"] == len(record_bytes)
    assert reception["receipt_sha256"] == hashlib.sha256(receipt_bytes).hexdigest()
    assert reception["receipt_byte_count"] == len(receipt_bytes)
    assert receipt["sha256"] == hashlib.sha256(payload).hexdigest()
    assert receipt["byte_count"] == len(payload)
    assert reception["archive_process_exit_code"] == document["inspection_exit_code"] == code
    for p in (path, record_path):
        assert p.stat().st_mode & 0o777 == 0o600
        assert_private(p.read_text(), paths)
    assert_private(result.stdout.decode(), (*paths, directory))
    return path, record_path, summary


def checked(tmp_path, path, record, expected, code=0):
    before = path.read_bytes(), record.read_bytes()
    result = run(tmp_path, "validate", validation_args(
        path, record, expected["operation"], expected["run_id"]))
    assert result.returncode == code and result.stderr == b""
    summary = json.loads(result.stdout)
    assert summary["validation_version"] == validation.VALIDATION_VERSION
    assert summary["warning"] == validation.WARNING
    assert summary["operation"] == expected["operation"]
    assert summary["inspection_exit_code"] == summary["recorded_archive_process_exit_code"] == code
    assert all(summary[flag] is False for flag in archive.FLAGS)
    assert not {"run_id", "sha256", "receipt_utf8", "archive_process_exit_code"} & summary.keys()
    assert_private(result.stdout.decode(), (path, record))
    assert before == (path.read_bytes(), record.read_bytes())
    return result


def rejected(result):
    assert result.returncode == 3 and result.stdout == b""
    assert result.stderr == (validator.VALIDATION_ERROR + "\n").encode()


@pytest.mark.parametrize("operation", inspection.COMMANDS)
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("state", ["empty", "reserved", "saved"])
def test_reception_chain_survives_source_removal(database, tmp_path, destination, operation, journal, state):
    source = Path(database.url.database)
    mutate(source, f"PRAGMA journal_mode={journal}")
    if state != "empty":
        reserve(database, request())
    if state == "saved":
        service(database).save(request())
    paths = source, clone(source, tmp_path / "candidate.sqlite")
    before = tuple(dump(p) for p in paths)
    evidence = published(tmp_path, paths, destination, operation)
    assert tuple(dump(p) for p in paths) == before
    assert not list(destination.glob(".inspection-*.tmp"))
    database.dispose()
    for p in paths:
        p.unlink()
    first = checked(tmp_path, *evidence)
    assert checked(tmp_path, *evidence).stdout == first.stdout
    assert all(not p.exists() for p in paths)


@pytest.mark.parametrize("operation", ["schema", "registry", "snapshots"])
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_difference_preserves_three_exit_meanings(pair, tmp_path, destination, operation, journal):
    for p in pair:
        mutate(p, f"PRAGMA journal_mode={journal}")
    if operation == "schema":
        mutate(pair[0], "DROP TABLE development_atomic_cost")
    else:
        clear(pair[1])
    before = tuple(dump(p) for p in pair)
    evidence = published(tmp_path, pair, destination, operation, code=1)
    checked(tmp_path, *evidence, code=1)
    assert tuple(dump(p) for p in pair) == before


@pytest.mark.parametrize("operation", ["integrity", "snapshots"])
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_corruption_does_not_reuse_old_reception(pair, tmp_path, destination, operation, journal):
    for p in pair:
        mutate(p, f"PRAGMA journal_mode={journal}")
    old = published(tmp_path, pair, destination, operation)
    for p in pair:
        mutate(p, "DELETE FROM development_atomic_cost")
    before = tuple(dump(p) for p in pair)
    result = run(tmp_path, "receive", receive_args(
        operation, pair, destination, "failed.json", "failed-record.json"))
    assert result.returncode == 3 and result.stdout == b""
    assert result.stderr == (receive.EXECUTION_ERROR + "\n").encode()
    assert not (destination / "failed.json").exists()
    assert not (destination / "failed-record.json").exists()
    assert not list(destination.glob(".inspection-*.tmp"))
    checked(tmp_path, *old)  # 舊證據自洽不代表目前資料庫有效。
    assert tuple(dump(p) for p in pair) == before


@pytest.mark.parametrize("operation", inspection.COMMANDS)
def test_runs_cannot_swap_receptions_or_expected_identity(pair, tmp_path, destination, operation):
    first = published(tmp_path, pair, destination, operation, "first.json", "first-record.json")
    second = published(tmp_path, pair, destination, operation, "second.json", "second-record.json")
    assert first[2]["run_id"] != second[2]["run_id"]
    for path, record, expected in ((first[0], second[1], first[2]),
                                   (second[0], first[1], second[2]),
                                   (second[0], second[1], first[2])):
        rejected(run(tmp_path, "validate", validation_args(path, record, operation, expected["run_id"])))
    checked(tmp_path, *first)
    checked(tmp_path, *second)


@pytest.mark.parametrize("phase", ["after_collect", "before_link", "after_link", "before_summary",
                                   "partial_summary", "after_summary"])
@pytest.mark.parametrize("code", [0, 1])
def test_hard_stop_record_is_not_receiver_exit_proof(pair, tmp_path, destination, phase, code):
    if code:
        clear(pair[1])
    before = tuple(dump(p) for p in pair)
    arguments = receive_args("snapshots", pair, destination)
    with child(tmp_path, "receive", arguments, phase) as handle:
        ready(handle)
        assert handle[0].poll() is None
        handle[0].kill()
        result = finish(handle[0])
    assert result.returncode == -signal.SIGKILL and result.stderr == b""
    path, record = destination / "report.json", destination / "reception.json"
    payload = path.read_bytes()  # 所有故障點均在真實存檔程序成功退出之後。
    expected = json.loads(payload)
    assert expected["inspection_exit_code"] == code
    leftovers = list(destination.glob(".inspection-*.tmp"))
    temporary = {p: p.read_bytes() for p in leftovers}
    assert len(leftovers) == (1 if phase in ("before_link", "after_link") else 0)
    if phase in ("after_collect", "before_link"):
        assert not record.exists()
        rejected(run(tmp_path, "validate", validation_args(path, record, "snapshots", expected["run_id"])))
    else:
        checked(tmp_path, path, record, expected, code)
        if phase == "after_link":
            assert record.read_bytes() == leftovers[0].read_bytes()
    if phase == "after_summary":
        summary = json.loads(result.stdout)
        assert summary["sha256"] == hashlib.sha256(record.read_bytes()).hexdigest()
        assert summary["byte_count"] == len(record.read_bytes())
    elif phase == "partial_summary":
        assert len(result.stdout) == 19
    else:
        assert result.stdout == b""
    persisted = {p: p.read_bytes() for p in destination.iterdir()}
    retry = run(tmp_path, "receive", arguments)
    assert retry.returncode == 2 and retry.stdout == b""
    assert retry.stderr == (receive.ARGUMENT_ERROR + "\n").encode()
    assert persisted == {p: p.read_bytes() for p in destination.iterdir()}
    new = published(tmp_path, pair, destination, "snapshots", "new.json", "new-record.json", code)
    assert new[2]["run_id"] != expected["run_id"]
    rejected(run(tmp_path, "validate", validation_args(path, new[1], "snapshots", expected["run_id"])))
    checked(tmp_path, *new, code=code)
    assert temporary == {p: p.read_bytes() for p in destination.glob(".inspection-*.tmp")}
    assert tuple(dump(p) for p in pair) == before


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_two_reports_race_for_one_reception_without_overwrite(pair, tmp_path, destination, journal):
    for p in pair:
        mutate(p, f"PRAGMA journal_mode={journal}")
    before = tuple(dump(p) for p in pair)
    with child(tmp_path, "receive", receive_args("snapshots", pair, destination, "first.json"), "race") as first:
        with child(tmp_path, "receive", receive_args("snapshots", pair, destination, "second.json"), "race") as second:
            for handle in (first, second):
                ready(handle)
            assert not (destination / "reception.json").exists()
            reports = [destination / name for name in ("first.json", "second.json")]
            originals = [p.read_bytes() for p in reports]
            for handle in (first, second):
                os.write(handle[2], b"G")
            results = [finish(handle[0]) for handle in (first, second)]
    assert sorted(r.returncode for r in results) == [0, 3]
    record = destination / "reception.json"
    expected = json.loads(record.read_bytes())
    for path, result in zip(reports, results):
        if result.returncode == 0:
            assert result.stderr == b""
            assert json.loads(result.stdout)["run_id"] == expected["run_id"]
            checked(tmp_path, path, record, expected)
        else:
            assert result.stdout == b"" and result.stderr == (receive.EXECUTION_ERROR + "\n").encode()
            rejected(run(tmp_path, "validate", validation_args(path, record, "snapshots", expected["run_id"])))
    assert originals == [p.read_bytes() for p in reports]
    assert not list(destination.glob(".inspection-*.tmp"))
    assert tuple(dump(p) for p in pair) == before


@pytest.mark.parametrize("target", ["archive", "reception"])
@pytest.mark.parametrize("change", ["truncate", "append", "reformat"])
def test_transport_damage_and_unsigned_outer_format(pair, tmp_path, destination, target, change):
    path, record, expected = published(tmp_path, pair, destination)
    damaged = path if target == "archive" else record
    original = damaged.read_bytes()
    if change == "truncate":
        damaged.write_bytes(original[:len(original) // 2])
    elif change == "append":
        damaged.write_bytes(original + b"{}")
    else:
        damaged.write_text(json.dumps(json.loads(original), indent=2), encoding="utf-8")
    if target == "reception" and change == "reformat":
        assert hashlib.sha256(damaged.read_bytes()).hexdigest() != expected["sha256"]
        checked(tmp_path, path, record, expected)  # 核驗器不驗證外部發布摘要或簽章。
    else:
        rejected(run(tmp_path, "validate", validation_args(path, record, "integrity", expected["run_id"])))
    damaged.write_bytes(original)
    checked(tmp_path, path, record, expected)


def test_help_missing_consent_and_publication_summary_are_not_records(pair, tmp_path, destination):
    arguments = receive_args("integrity", pair, destination)
    arguments.remove("--development-only")
    refused = run(tmp_path, "receive", arguments)
    assert refused.returncode == 2 and refused.stdout == b""
    help_result = run(tmp_path, "receive", ["--help"])
    assert help_result.returncode == 0 and help_result.stderr == b""
    assert not list(destination.iterdir())
    path, record, expected = published(tmp_path, pair, destination)
    for name, payload in (("help.json", help_result.stdout),
                          ("summary.json", json.dumps(expected).encode()),
                          ("receipt.json", json.loads(record.read_bytes())["receipt_utf8"].encode())):
        wrong = private_file(destination / name, payload)
        rejected(run(tmp_path, "validate", validation_args(path, wrong, "integrity", expected["run_id"])))
    rejected(run(tmp_path, "validate", validation_args(path, record, "integrity", str(uuid.uuid4()))))
    checked(tmp_path, path, record, expected)
