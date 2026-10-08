"""真實檢查→原子存檔→回條→全新離線程序；故障只作用於暫存合成資料。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import uuid

import pytest

import development_archive_validation as validator
import development_inspection as inspection
import development_inspection_archive as archive
from engine import development_archive_validation as validation
from development_atomic_fixtures import request
from test_development_atomic_snapshot import database, reserve, service
from test_development_integrity_inspection import dump, mutate
from test_development_snapshot_comparison import clear, clone, pair
from test_development_inspection_archive import destination, options
from test_development_inspection_cli import assert_private


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX 程序／管線驗收")
WORKER = Path(__file__).with_name("development_evidence_worker.py")


@contextmanager
def child(tmp_path, mode, arguments, phase="normal"):
    ready_read, ready_write = os.pipe()
    resume_read, resume_write = os.pipe()
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(WORKER), mode, phase,
             str(ready_write), str(resume_read), *arguments],
            cwd=tmp_path, env={}, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(ready_write, resume_read), start_new_session=True)
        os.close(ready_write)
        ready_write = None
        os.close(resume_read)
        resume_read = None
        yield process, ready_read, resume_write
    finally:
        if process is not None:
            # 只清理本測試建立的獨立程序群組，包含超時時可能尚存的檢查子程序。
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                stream.close()
        for fd in (ready_read, ready_write, resume_read, resume_write):
            if fd is not None:
                os.close(fd)


def ready(handle):
    with selectors.DefaultSelector() as selector:
        selector.register(handle[1], selectors.EVENT_READ)
        assert selector.select(20), "未到達指定故障點"
        assert os.read(handle[1], 1) == b"R"


def finish(process):
    stdout, stderr = process.communicate(timeout=25)
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def run(tmp_path, mode, arguments):
    with child(tmp_path, mode, arguments) as handle:
        return finish(handle[0])


def private_file(path, payload):
    # 測試接收端排他保存真實 stdout；不是新增正式回條原子存檔服務。
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(payload)
    return path


def validation_args(path, receipt, operation, run_id):
    return ["--development-only", "--operation", operation, "--run-id", run_id,
            "--archive", str(path), "--receipt", str(receipt)]


def published(tmp_path, paths, destination, operation="integrity", name="report.json", code=0):
    result = run(tmp_path, "archive", options(operation, paths, destination, name))
    assert result.returncode == code and result.stderr == b""
    path = destination / name
    payload = path.read_bytes()
    receipt = json.loads(result.stdout)
    document = json.loads(payload)
    assert receipt["run_id"] == document["run_id"]
    assert receipt["sha256"] == hashlib.sha256(payload).hexdigest()
    assert receipt["byte_count"] == len(payload)
    assert receipt["inspection_exit_code"] == document["inspection_exit_code"] == code
    assert path.stat().st_mode & 0o777 == 0o600
    assert_private(payload.decode(), paths)
    assert_private(result.stdout.decode(), (*paths, destination))
    receipt_path = private_file(destination / (path.stem + "-receipt.json"), result.stdout)
    return path, receipt_path, receipt


def checked(tmp_path, path, receipt_path, receipt, code=0):
    before = (path.read_bytes(), receipt_path.read_bytes())
    result = run(tmp_path, "validate", validation_args(
        path, receipt_path, receipt["operation"], receipt["run_id"]))
    assert result.returncode == code and result.stderr == b""
    summary = json.loads(result.stdout)
    assert summary["validation_version"] == validation.VALIDATION_VERSION
    assert summary["inspection_exit_code"] == code
    assert summary["operation"] == receipt["operation"]
    assert summary["warning"] == validation.WARNING
    assert all(summary[flag] is False for flag in archive.FLAGS)
    assert not {"run_id", "sha256", "inspection"} & summary.keys()
    assert_private(result.stdout.decode(), (path, receipt_path))
    assert before == (path.read_bytes(), receipt_path.read_bytes())
    return result


def rejected(result, code=3):
    assert result.returncode == code and result.stdout == b""
    assert result.stderr == (validator.VALIDATION_ERROR + "\n").encode()


@pytest.mark.parametrize("operation", inspection.COMMANDS)
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("state", ["empty", "reserved", "saved"])
def test_full_command_chain_survives_source_removal(database, tmp_path, destination, operation, journal, state):
    source = Path(database.url.database)
    mutate(source, f"PRAGMA journal_mode={journal}")
    if state != "empty":
        reserve(database, request())
    if state == "saved":
        service(database).save(request())
    paths = source, clone(source, tmp_path / "candidate.sqlite")
    before = tuple(dump(p) for p in paths)
    path, receipt_path, receipt = published(tmp_path, paths, destination, operation)
    assert tuple(dump(p) for p in paths) == before
    assert not list(destination.glob(".inspection-*.tmp"))
    database.dispose()
    for p in paths:
        p.unlink()
    first = checked(tmp_path, path, receipt_path, receipt)
    # 每次全新程序；合法舊文件仍可核驗，不是新檢查或防重放。
    assert checked(tmp_path, path, receipt_path, receipt).stdout == first.stdout
    assert all(not p.exists() for p in paths)


@pytest.mark.parametrize("operation", ["schema", "registry", "snapshots"])
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_difference_exit_is_preserved_across_processes(pair, tmp_path, destination, operation, journal):
    for p in pair:
        mutate(p, f"PRAGMA journal_mode={journal}")
    if operation == "schema":
        mutate(pair[0], "DROP TABLE development_atomic_cost")
    else:
        clear(pair[1])
    before = tuple(dump(p) for p in pair)
    path, receipt_path, receipt = published(tmp_path, pair, destination, operation, code=1)
    checked(tmp_path, path, receipt_path, receipt, code=1)
    assert tuple(dump(p) for p in pair) == before


@pytest.mark.parametrize("operation", ["integrity", "snapshots"])
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_corrupt_sources_never_publish_or_reuse_old_success(pair, tmp_path, destination, operation, journal):
    for p in pair:
        mutate(p, f"PRAGMA journal_mode={journal}")
    old = published(tmp_path, pair, destination, operation)
    for p in pair:
        mutate(p, "DELETE FROM development_atomic_cost")
    before = tuple(dump(p) for p in pair)
    result = run(tmp_path, "archive", options(operation, pair, destination, "failed.json"))
    assert result.returncode == 3 and result.stdout == b""
    assert result.stderr == (archive.EXECUTION_ERROR + "\n").encode()
    assert not (destination / "failed.json").exists()
    assert not list(destination.glob(".inspection-*.tmp"))
    checked(tmp_path, *old)  # 舊證據仍自洽，不能代表目前已損壞來源。
    assert tuple(dump(p) for p in pair) == before


@pytest.mark.parametrize("operation", inspection.COMMANDS)
def test_two_real_runs_cannot_swap_receipts_or_expected_identity(pair, tmp_path, destination, operation):
    first = published(tmp_path, pair, destination, operation, "first.json")
    second = published(tmp_path, pair, destination, operation, "second.json")
    assert first[2]["run_id"] != second[2]["run_id"]
    for path, receipt_path, expected in ((first[0], second[1], first[2]),
                                         (second[0], first[1], second[2]),
                                         (second[0], second[1], first[2])):
        rejected(run(tmp_path, "validate", validation_args(
            path, receipt_path, operation, expected["run_id"])))
    checked(tmp_path, *first)
    checked(tmp_path, *second)


@pytest.mark.parametrize("phase", ["before_link", "after_link", "before_receipt",
                                   "partial_receipt", "after_receipt"])
def test_hard_stop_receipt_and_exit_are_distinct(pair, tmp_path, destination, phase):
    before = tuple(dump(p) for p in pair)
    with child(tmp_path, "archive", options("integrity", pair, destination), phase) as handle:
        ready(handle)
        assert handle[0].poll() is None
        handle[0].kill()
        result = finish(handle[0])
    assert result.returncode == -signal.SIGKILL and result.stderr == b""
    path = destination / "report.json"
    leftovers = list(destination.glob(".inspection-*.tmp"))
    if phase in ("before_link", "after_link"):
        assert len(leftovers) == 1
        payload = leftovers[0].read_bytes()
        assert path.exists() is (phase == "after_link")
        if path.exists():
            assert path.read_bytes() == payload
    else:
        assert not leftovers
        payload = path.read_bytes()
    document = json.loads(payload)
    receipt_path = private_file(destination / "interrupted-receipt.json", result.stdout)
    args = validation_args(path, receipt_path, "integrity", document["run_id"])
    if phase == "after_receipt":
        # 完整真實回條已刷新，但外層程序尚未成功退出就死亡；離線格式核驗仍會通過。
        checked(tmp_path, path, receipt_path, json.loads(result.stdout))
    else:
        assert result.stdout == (result.stdout[:19] if phase == "partial_receipt" else b"")
        if phase == "partial_receipt":
            assert len(result.stdout) == 19
        rejected(run(tmp_path, "validate", args))
    if path.exists():
        retry = run(tmp_path, "archive", options("integrity", pair, destination))
        assert retry.returncode == 2 and retry.stdout == b""
        assert retry.stderr == (archive.ARGUMENT_ERROR + "\n").encode()
        assert path.read_bytes() == payload
    # 另一次明確執行是新身份，不補回原回條、不清除舊暫存。
    new = published(tmp_path, pair, destination, name="new.json")
    assert new[2]["run_id"] != document["run_id"]
    rejected(run(tmp_path, "validate", validation_args(path, new[1], "integrity", document["run_id"])))
    checked(tmp_path, *new)
    assert list(destination.glob(".inspection-*.tmp")) == leftovers
    assert tuple(dump(p) for p in pair) == before


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_full_producers_race_only_winner_receipt_validates(pair, tmp_path, destination, journal):
    for p in pair:
        mutate(p, f"PRAGMA journal_mode={journal}")
    before = tuple(dump(p) for p in pair)
    args = options("snapshots", pair, destination)
    with child(tmp_path, "archive", args, "race") as first:
        with child(tmp_path, "archive", args, "race") as second:
            for handle in (first, second):
                ready(handle)
            assert not (destination / "report.json").exists()
            for handle in (first, second):
                os.write(handle[2], b"G")
            results = [finish(handle[0]) for handle in (first, second)]
    winner, loser = sorted(results, key=lambda result: result.returncode)
    assert winner.returncode == 0 and winner.stderr == b""
    assert loser.returncode == 3 and loser.stdout == b""
    assert loser.stderr == (archive.EXECUTION_ERROR + "\n").encode()
    assert not list(destination.glob(".inspection-*.tmp"))
    receipt_path = private_file(destination / "winner-receipt.json", winner.stdout)
    checked(tmp_path, destination / "report.json", receipt_path, json.loads(winner.stdout))
    assert tuple(dump(p) for p in pair) == before


@pytest.mark.parametrize("target", ["archive", "receipt"])
@pytest.mark.parametrize("change", ["truncate", "append", "reformat"])
def test_transport_damage_rejected_in_fresh_receiver(pair, tmp_path, destination, target, change):
    path, receipt_path, receipt = published(tmp_path, pair, destination)
    damaged = path if target == "archive" else receipt_path
    original = damaged.read_bytes()
    if change == "truncate":
        damaged.write_bytes(original[:len(original) // 2])
    elif change == "append":
        damaged.write_bytes(original + b"{}")
    else:
        damaged.write_text(json.dumps(json.loads(original), indent=2), encoding="utf-8")
    result = run(tmp_path, "validate", validation_args(path, receipt_path, "integrity", receipt["run_id"]))
    if target == "receipt" and change == "reformat":
        # 回條本身沒有外部位元組摘要；只改空白仍合法，不誤稱有簽章。
        assert result.returncode == 0 and not result.stderr
    else:
        rejected(result)
    damaged.write_bytes(original)
    checked(tmp_path, path, receipt_path, receipt)


def test_missing_consent_and_help_are_not_evidence(tmp_path, pair, destination):
    args = options("integrity", pair, destination)
    args.remove("--development-only")
    refused = run(tmp_path, "archive", args)
    assert refused.returncode == 2 and refused.stdout == b""
    help_result = run(tmp_path, "archive", ["--help"])
    assert help_result.returncode == 0 and help_result.stderr == b""
    assert not list(destination.iterdir())
    help_path = private_file(destination / "help.json", help_result.stdout)
    receipt_path = private_file(destination / "empty.json", b"{}")
    rejected(run(tmp_path, "validate", validation_args(
        help_path, receipt_path, "integrity", str(uuid.uuid4()))))
