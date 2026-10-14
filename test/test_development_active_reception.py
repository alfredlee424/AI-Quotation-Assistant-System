"""接收者 SIGKILL 不會自動取消獨立群組存檔；不補回條或恢復正式能力。"""
import json
import os
import selectors
import signal
import uuid

import pytest

import development_inspection_receive as receive
from test_development_atomic_snapshot import database
from test_development_inspection_archive import destination
from test_development_integrity_inspection import dump, mutate
from test_development_snapshot_comparison import pair, clear
from test_development_reception_integration import (
    child, ready, finish, kill_group, receive_args, run, published, checked,
    rejected, validation_args,
)


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX 存檔生命週期驗收")
PHASES = ("active_before_run", "active_before_link", "active_after_link", "active_after_receipt")


def event(handle, size):
    result = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(handle[1], selectors.EVENT_READ)
        while len(result) < size:
            assert selector.select(20), "存檔程序未到達指定同步點"
            chunk = os.read(handle[1], size - len(result))
            assert chunk, "存檔控制管線提早關閉"
            result.extend(chunk)
    return bytes(result)


def setup_sources(paths, journal, code):
    for path in paths:
        mutate(path, f"PRAGMA journal_mode={journal}")
    if code:
        clear(paths[1])
    return tuple(dump(path) for path in paths)


def files(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir()}


def stop_receiver(handle):
    ready(handle)
    receiver, _, _, archive_pid = handle
    assert receiver.poll() is None
    assert os.getpgid(archive_pid) == archive_pid != receiver.pid
    receiver.kill()
    result = finish(receiver)
    assert result.returncode == -signal.SIGKILL
    assert result.stdout == result.stderr == b""
    # 在明確屏障上仍存活，證明不是靠延遲猜測孤兒程序存在。
    assert os.getpgid(archive_pid) == archive_pid


def assert_incomplete(tmp_path, directory, paths, code):
    path, record = directory / "report.json", directory / "reception.json"
    expected = json.loads(path.read_bytes()) if path.exists() else {"run_id": str(uuid.uuid4())}
    assert not record.exists()
    rejected(run(tmp_path, "validate", validation_args(path, record, "snapshots", expected["run_id"])))
    if path.exists():
        assert expected["inspection_exit_code"] == code
        assert path.stat().st_mode & 0o777 == 0o600
        before = files(directory)
        retry = run(tmp_path, "receive", receive_args("snapshots", paths, directory))
        assert retry.returncode == 2 and retry.stdout == b""
        assert retry.stderr == (receive.ARGUMENT_ERROR + "\n").encode()
        assert files(directory) == before
    return expected


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("code", [0, 1])
def test_active_hooks_preserve_live_reception_contract(pair, tmp_path, destination, phase, code):
    before = setup_sources(pair, "WAL", code)
    with child(tmp_path, "receive", receive_args("snapshots", pair, destination), phase) as handle:
        ready(handle)
        os.write(handle[2], b"G")
        assert event(handle, 2) == b"D" + bytes([code])
        assert handle[0].poll() is None
        assert not (destination / "reception.json").exists()
        os.write(handle[2], b"X")  # 存檔 main 真正返回，接收者才可觀察退出及保存。
        result = finish(handle[0])
    assert result.returncode == code and result.stderr == b""
    summary = json.loads(result.stdout)
    checked(tmp_path, destination / "report.json", destination / "reception.json", summary, code)
    assert not list(destination.glob(".inspection-*.tmp"))
    assert tuple(dump(path) for path in pair) == before


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("code", [0, 1])
def test_external_group_stop_leaves_only_stage_appropriate_evidence(
        pair, tmp_path, destination, phase, journal, code):
    before = setup_sources(pair, journal, code)
    with child(tmp_path, "receive", receive_args("snapshots", pair, destination), phase) as handle:
        stop_receiver(handle)
        frozen = files(destination)
        kill_group(handle[3])  # 外部測試控制器清理，不是已死亡接收者的 finally。
        # 接收者已 wait；存檔控制寫端只有該群組持有，EOF 後不再有本次寫者。
        with selectors.DefaultSelector() as selector:
            selector.register(handle[1], selectors.EVENT_READ)
            assert selector.select(20)
            assert os.read(handle[1], 1) == b""
        assert files(destination) == frozen
    published_before_stop = phase in ("active_after_link", "active_after_receipt")
    assert (destination / "report.json").exists() is published_before_stop
    temporary = {k: v for k, v in frozen.items() if k.startswith(".inspection-")}
    assert len(temporary) == (1 if phase in ("active_before_link", "active_after_link") else 0)
    for name, payload in temporary.items():
        assert json.loads(payload)["inspection_exit_code"] == code
        assert (destination / name).stat().st_mode & 0o777 == 0o600
    old = assert_incomplete(tmp_path, destination, pair, code)
    if phase == "active_after_link":
        assert next(iter(temporary.values())) == (destination / "report.json").read_bytes()
    # 全部舊證據留存；使用新名稱是新執行，不是替舊執行補回條。
    new = published(tmp_path, pair, destination, "snapshots", "new.json", "new-record.json", code)
    checked(tmp_path, *new, code=code)
    assert new[2]["run_id"] != old["run_id"]
    assert all((destination / name).read_bytes() == payload for name, payload in frozen.items())
    assert tuple(dump(path) for path in pair) == before


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("code", [0, 1])
def test_orphan_archive_can_publish_after_receiver_death(
        pair, tmp_path, destination, phase, journal, code):
    before = setup_sources(pair, journal, code)
    with child(tmp_path, "receive", receive_args("snapshots", pair, destination), phase) as handle:
        stop_receiver(handle)
        os.write(handle[2], b"G")
        # 原 main 的函式返回值，不是 orphan 的 OS 退出狀態。
        expected_return = code if phase == "active_after_receipt" else 3
        assert event(handle, 2) == b"D" + bytes([expected_return])
        old = assert_incomplete(tmp_path, destination, pair, code)
        assert (destination / "report.json").exists()
        assert not list(destination.glob(".inspection-*.tmp"))
    new = published(tmp_path, pair, destination, "snapshots", "new.json", "new-record.json", code)
    checked(tmp_path, *new, code=code)
    rejected(run(tmp_path, "validate", validation_args(
        destination / "report.json", new[1], "snapshots", old["run_id"])))
    assert tuple(dump(path) for path in pair) == before


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("code", [0, 1])
def test_retry_can_win_while_orphan_waits_before_publication(pair, tmp_path, destination, journal, code):
    before = setup_sources(pair, journal, code)
    arguments = receive_args("snapshots", pair, destination)
    with child(tmp_path, "receive", arguments, "active_before_link") as handle:
        stop_receiver(handle)
        temporary = list(destination.glob(".inspection-*.tmp"))
        assert len(temporary) == 1
        orphan = json.loads(temporary[0].read_bytes())
        assert not (destination / "report.json").exists()
        # 人工明確的新執行；目的尚未發布時，前置檢查不會把暫存當保留鎖。
        winner = published(tmp_path, pair, destination, "snapshots", code=code)
        assert winner[2]["run_id"] != orphan["run_id"]
        persisted = winner[0].read_bytes(), winner[1].read_bytes()
        os.write(handle[2], b"G")
        assert event(handle, 2) == b"D\x03"
        assert (winner[0].read_bytes(), winner[1].read_bytes()) == persisted
        assert not temporary[0].exists()  # 活著的原存檔器只清理自己的暫存。
        checked(tmp_path, *winner, code=code)
        rejected(run(tmp_path, "validate", validation_args(
            winner[0], winner[1], "snapshots", orphan["run_id"])))
    assert not list(destination.glob(".inspection-*.tmp"))
    assert tuple(dump(path) for path in pair) == before
