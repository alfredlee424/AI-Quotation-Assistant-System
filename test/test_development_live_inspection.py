"""檢查仍持有真實讀交易時的三層程序故障驗收；不提供恢復工具。"""
from contextlib import closing
import json
import os
import selectors
import signal
import sqlite3

import pytest

import development_inspection_receive as receive
from test_development_atomic_snapshot import database
from test_development_inspection_archive import destination
from test_development_integrity_inspection import dump
from test_development_snapshot_comparison import pair, clear
from test_development_active_reception import event, setup_sources, files, assert_incomplete
from test_development_reception_integration import (
    child, ready, finish, kill_group, receive_args, published, checked,
)


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX 真實檢查中止驗收")
PHASE = "active_inspection_read"


def inspection_ready(handle):
    ready(handle)
    pid = int.from_bytes(event(handle, 8), "big")
    assert pid not in (handle[0].pid, handle[3])
    assert os.getpgid(pid) == handle[3] != handle[0].pid
    assert handle[0].poll() is None
    return pid


def kill_receiver(handle, inspection_pid):
    handle[0].kill()
    result = finish(handle[0])
    assert result.returncode == -signal.SIGKILL
    assert result.stdout == result.stderr == b""
    assert os.getpgid(inspection_pid) == handle[3]


def control_eof(handle):
    with selectors.DefaultSelector() as selector:
        selector.register(handle[1], selectors.EVENT_READ)
        assert selector.select(10), "檢查後代仍持有控制管線"
        assert os.read(handle[1], 1) == b""


def exclusive_probe(path, *, blocked=False):
    # 不寫資料；DELETE 真實讀者會阻擋 EXCLUSIVE，死亡後鎖須釋放。
    with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as connection:
        if blocked:
            with pytest.raises(sqlite3.OperationalError) as error:
                connection.execute("BEGIN EXCLUSIVE")
            assert error.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        else:
            connection.execute("BEGIN EXCLUSIVE")
            connection.rollback()


@pytest.mark.parametrize("operation,code", [
    ("schema", 0), ("integrity", 0), ("registry", 0), ("snapshots", 0), ("snapshots", 1),
])
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_live_read_hook_preserves_real_result(pair, tmp_path, destination, operation, code, journal):
    before = setup_sources(pair, journal, code)
    with child(tmp_path, "receive", receive_args(operation, pair, destination), PHASE) as handle:
        inspection_ready(handle)
        assert files(destination) == {}
        if journal == "DELETE":
            exclusive_probe(pair[0], blocked=True)
        os.write(handle[2], b"G")
        assert event(handle, 2) == b"D" + bytes([code])
        assert not (destination / "reception.json").exists()
        os.write(handle[2], b"X")
        result = finish(handle[0])
    assert result.returncode == code and result.stderr == b""
    checked(tmp_path, destination / "report.json", destination / "reception.json",
            json.loads(result.stdout), code)
    assert tuple(dump(path) for path in pair) == before


@pytest.mark.parametrize("action", ["group_stop", "continue", "inspection_stop", "retry_wins"])
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("code", [0, 1])
def test_receiver_dies_during_real_read(pair, tmp_path, destination, action, journal, code):
    before = setup_sources(pair, journal, code)
    with child(tmp_path, "receive", receive_args("snapshots", pair, destination), PHASE) as handle:
        pid = inspection_ready(handle)
        kill_receiver(handle, pid)
        assert files(destination) == {}
        if journal == "DELETE":
            exclusive_probe(pair[0], blocked=True)
        if action == "group_stop":
            kill_group(handle[3])
            control_eof(handle)  # 存檔與檢查均持寫端；不可只等 leader 死亡。
            assert files(destination) == {}
        elif action == "inspection_stop":
            os.kill(pid, signal.SIGKILL)
            assert event(handle, 2) == b"D\x03"
            assert files(destination) == {}
        else:
            if action == "retry_wins":
                winner = published(tmp_path, pair, destination, "snapshots", code=code)
                persisted = files(destination)
            os.write(handle[2], b"G")
            assert event(handle, 2) == b"D\x03"
            if action == "retry_wins":
                assert files(destination) == persisted
                checked(tmp_path, *winner, code=code)
            else:
                assert_incomplete(tmp_path, destination, pair, code)
                assert (destination / "report.json").exists()
        assert not list(destination.glob(".inspection-*.tmp"))
    exclusive_probe(pair[0])
    if action in ("group_stop", "inspection_stop"):
        assert_incomplete(tmp_path, destination, pair, code)
    new = published(tmp_path, pair, destination, "snapshots", "new.json", "new-record.json", code)
    checked(tmp_path, *new, code=code)
    assert tuple(dump(path) for path in pair) == before


@pytest.mark.parametrize("receiver_alive", [False, True])
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_real_inspection_deadline_reaps_blocked_reader(pair, tmp_path, destination, receiver_alive, journal):
    before = setup_sources(pair, journal, 0)
    arguments = receive_args("snapshots", pair, destination) + ["--process-timeout", "3"]
    with child(tmp_path, "receive", arguments, PHASE) as handle:
        pid = inspection_ready(handle)
        if not receiver_alive:
            kill_receiver(handle, pid)
        # 不放行，不 sleep；原收集器期限到期後必須 kill/wait 真實檢查子程序。
        assert event(handle, 2) == b"D\x03"
        with pytest.raises(ProcessLookupError):
            os.getpgid(pid)
        exclusive_probe(pair[0])
        assert files(destination) == {}
        if receiver_alive:
            os.write(handle[2], b"X")
            result = finish(handle[0])
            assert result.returncode == 3 and result.stdout == b""
            assert result.stderr == (receive.EXECUTION_ERROR + "\n").encode()
    assert tuple(dump(path) for path in pair) == before


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_archive_leader_death_receiver_cleans_live_descendant(pair, tmp_path, destination, journal):
    before = setup_sources(pair, journal, 0)
    arguments = receive_args("snapshots", pair, destination) + ["--archive-timeout", "3"]
    with child(tmp_path, "receive", arguments, PHASE) as handle:
        inspection_ready(handle)
        os.kill(handle[3], signal.SIGKILL)
        # 後代仍持 stdout/stderr，原接收期限必須清理整群組，而非只 wait leader。
        result = finish(handle[0])
        assert result.returncode == 3 and result.stdout == b""
        assert result.stderr == (receive.EXECUTION_ERROR + "\n").encode()
        control_eof(handle)
        exclusive_probe(pair[0])
        assert files(destination) == {}
    assert tuple(dump(path) for path in pair) == before


@pytest.mark.parametrize("receiver_alive", [False, True])
def test_wal_reader_keeps_old_snapshot_after_source_change(pair, tmp_path, destination, receiver_alive):
    setup_sources(pair, "WAL", 0)
    with child(tmp_path, "receive", receive_args("snapshots", pair, destination), PHASE) as handle:
        pid = inspection_ready(handle)
        if not receiver_alive:
            kill_receiver(handle, pid)
        # 原第一端讀交易已釘住，外部刪除其整組內容仍能提交；檢查不是停寫鎖。
        clear(pair[0])
        changed = tuple(dump(path) for path in pair)
        os.write(handle[2], b"G")
        assert event(handle, 2) == b"D" + bytes([0 if receiver_alive else 3])
        old = json.loads((destination / "report.json").read_bytes())
        assert old["inspection_exit_code"] == 0
        if receiver_alive:
            os.write(handle[2], b"X")
            result = finish(handle[0])
            assert result.returncode == 0 and result.stderr == b""
            checked(tmp_path, destination / "report.json", destination / "reception.json",
                    json.loads(result.stdout))
        else:
            assert_incomplete(tmp_path, destination, pair, 0)
    new = published(tmp_path, pair, destination, "snapshots", "new.json", "new-record.json", 1)
    checked(tmp_path, *new, code=1)
    assert new[2]["run_id"] != old["run_id"]
    assert tuple(dump(path) for path in pair) == changed
