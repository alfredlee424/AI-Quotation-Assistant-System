"""T31 開發整合：真實獨立程序、硬中止、可見性、冪等及唯讀輸出。"""
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import select as pipe_select
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.pool import NullPool

from database.development_atomic_schema import development_atomic_schema
from development_atomic_fixtures import request, SHARED
from engine.development_atomic_contract import validate_request


WORKER = Path(__file__).with_name("development_process_worker.py")


@pytest.fixture
def database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'process.sqlite'}", poolclass=NullPool)
    event.listen(engine, "connect", lambda dbapi, _: dbapi.execute("PRAGMA foreign_keys=ON"))
    metadata, *_ = development_atomic_schema()
    metadata.create_all(engine)  # 僅此 fixture 明確建立六張開發表。
    yield engine
    engine.dispose()


@contextmanager
def worker(database, mode, *, req=None, gate=None, forbid_clock=False):
    process = subprocess.Popen(
        [sys.executable, "-I", str(WORKER)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        process.stdin.write(json.dumps({
            "database": database.url.database, "mode": mode,
            "payload": validate_request(request() if req is None else req),
            "gate": gate, "forbid_clock": forbid_clock,
        }) + "\n")
        process.stdin.flush()
        if gate is not None:
            ready, _, _ = pipe_select.select([process.stdout], [], [], 20)
            assert ready, "子程序同步點超時"
            line = process.stdout.readline()
            if not line:
                _, stderr = process.communicate(timeout=20)
                pytest.fail(f"worker 未到同步點：{stderr}")
            assert json.loads(line) == {"ready": gate}
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=20)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def finish(process, *, release=False):
    stdout, stderr = process.communicate(input="continue\n" if release else None, timeout=25)
    assert process.returncode == 0, stderr
    assert not stderr, stderr
    return json.loads(stdout)


def run(database, mode, **kwargs):
    with worker(database, mode, **kwargs) as process:
        return finish(process)


def rows(database):
    metadata, *_ = development_atomic_schema()
    with database.connect() as connection:
        return {
            table.name: [dict(row) for row in connection.execute(
                select(table).order_by(*table.primary_key.columns)).mappings()]
            for table in metadata.sorted_tables
        }


def assert_complete(result):
    assert "error" not in result
    exports = result["exports"]
    batch = json.loads(exports["None"]["json"])
    saved = batch["content"]["saved_snapshot"]
    summary = batch["content"]["summary"]
    assert summary["child_count"] == 4 and summary["product_qty"] == 11
    assert summary["batch_totals"]["after_discount"] == "1.36"
    assert summary["batch_totals"]["total_price"] == "1.40"
    assert saved["fixed_request"] == json.loads(validate_request(request()))
    for position, child in enumerate(saved["children"], 1):
        fixed = child["fixed_child"]
        assert child["ref_no"] == f"20261002083045_{position:03d}"
        assert [r["seq_no"] for r in fixed["rows"]] == ["00001", "00002", "00003", "00004"]
        assert fixed["shared_description"] == SHARED
        assert fixed["amounts"]["tax_rounding_difference"] == "-0.01"
        assert fixed["rows"][0]["original_qty"] == "1.000000000000000001"
        export = exports[fixed["child_quote_id"]]
        view = json.loads(export["json"])
        assert view["content"]["child"] == child
        assert view["content"]["fixed_batch"]["source"] == saved["fixed_request"]["source"]
        assert SHARED in export["txt"] and "不是正式報價" in export["txt"]
        assert all(view[key] is False for key in ("can_quote", "can_confirm", "can_save"))
    assert "面無孔有桌下走線" in saved["children"][0]["fixed_child"]["description"]
    return saved


@pytest.mark.parametrize("mode,gate", [
    ("reserve", "after:quote_number_reserved_child:2"),
    ("reserve", "after:quote_number_reserved_child:4"),
    ("reserve", "before_commit"),
    *[("save", f"after:{table}:{position}")
      for table in ("development_atomic_child", "development_atomic_source_link")
      for position in (2, 4)],
    # 每張四成本列：第 5／13 次 INSERT 是第二／第四張的首列。
    ("save", "after:development_atomic_cost:5"),
    ("save", "after:development_atomic_cost:13"),
    ("save", "before_commit"),
])
def test_hard_kill_before_commit_leaves_no_partial_batch(database, mode, gate):
    if mode == "save":
        run(database, "reserve")
    before = rows(database)
    with worker(database, mode, gate=gate) as process:
        process.kill()  # 真正中止程序，不經 context manager rollback 或 exception handler。
        process.wait(timeout=20)
        assert process.returncode < 0
    assert rows(database) == before
    assert run(database, "view")["exports"] is None
    completed = run(database, "full")
    assert_complete(completed)
    after = rows(database)
    assert {name: len(value) for name, value in after.items()} == {
        "quote_number_reservation": 1, "quote_number_reserved_child": 4,
        "development_atomic_batch": 1, "development_atomic_child": 4,
        "development_atomic_source_link": 4, "development_atomic_cost": 16,
    }
    if mode == "save":
        for name in ("quote_number_reservation", "quote_number_reserved_child"):
            assert before[name] == after[name]  # 孤兒保留不回收／不重新配號。


@pytest.mark.parametrize("gate", ["after_reserve", "after_save"])
def test_committed_response_lost_restarts_with_original_identity(database, gate):
    with worker(database, "full", gate=gate) as process:
        before = rows(database)
        process.kill()
        process.wait(timeout=20)
    assert rows(database) == before
    if gate == "after_reserve":
        assert run(database, "view")["exports"] is None
    recovered = run(database, "full", forbid_clock=gate == "after_save")
    saved = assert_complete(recovered)
    stable = rows(database)
    repeated = run(database, "full", forbid_clock=True)
    assert repeated == recovered
    assert assert_complete(repeated)["created_at"] == saved["created_at"]
    assert rows(database) == stable
    for name in ("quote_number_reservation", "quote_number_reserved_child"):
        assert before[name] == stable[name]


@pytest.mark.parametrize("mode,table", [
    ("reserve", "quote_number_reservation"), ("save", "development_atomic_batch"),
])
def test_two_processes_race_on_actual_unique_constraint(database, mode, table):
    if mode == "save":
        run(database, "reserve")
    # 兩程序均已完成初次不存在檢查；先提交 leader，follower 的 INSERT 必須撞唯一鍵。
    with worker(database, mode, gate="before:" + table) as leader:
        with worker(database, mode, gate="before:" + table) as follower:
            assert leader.pid != follower.pid
            first = finish(leader, release=True)
            second = finish(follower, release=True)
    assert first["collisions"] == []
    assert any(f"UNIQUE constraint failed: {table}." in error for error in second["collisions"])
    assert {k: v for k, v in first.items() if k != "collisions"} == {
        k: v for k, v in second.items() if k != "collisions"}
    assert_complete(run(database, "full"))
    before = rows(database)
    assert_complete(run(database, "full", forbid_clock=True))
    assert rows(database) == before


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("outcome", ["commit", "kill"])
def test_reader_never_sees_uncommitted_partial_batch(database, journal, outcome):
    with database.connect() as connection:
        assert connection.exec_driver_sql(f"PRAGMA journal_mode={journal}").scalar().upper() == journal
    run(database, "reserve")
    with worker(database, "save", gate="after:development_atomic_child:2") as writer:
        # 寫者確實持有未提交 header 與兩子張；獨立唯讀程序仍只能看見尚未保存。
        assert run(database, "view")["exports"] is None
        if outcome == "commit":
            saved = finish(writer, release=True)["saved"]
        else:
            writer.kill()
            writer.wait(timeout=20)
    if outcome == "kill":
        assert run(database, "view")["exports"] is None
        saved = run(database, "save")["saved"]
    assert assert_complete(run(database, "view")) == saved


def test_changed_content_after_restart_rejected_without_repair(database):
    original = run(database, "full")
    before = rows(database)
    changed = replace(request(), origin="different-development-origin")
    assert run(database, "reserve", req=changed)["error"] == "ReservationConflict"
    assert run(database, "save", req=changed)["error"] == "AtomicSnapshotConflict"
    assert rows(database) == before
    assert run(database, "full", forbid_clock=True) == original


@pytest.mark.parametrize("table", ["quote_number_reserved_child", "development_atomic_cost"])
def test_fresh_reader_rejects_persistent_damage_and_does_not_repair(database, table):
    run(database, "full")
    with database.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(f"DELETE FROM {table} WHERE rowid = (SELECT min(rowid) FROM {table})")
        connection.commit()
    before = rows(database)
    assert run(database, "view")["error"] == "AtomicSnapshotConflict"
    assert run(database, "save")["error"] == "AtomicSnapshotConflict"
    assert rows(database) == before


@pytest.mark.parametrize("table", ["quote_number_reserved_child", "development_atomic_cost"])
def test_wal_reader_uses_one_transaction_then_refresh_rejects_damage(database, table):
    with database.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar().upper() == "WAL"
    original = run(database, "full")
    with worker(database, "view", gate="after_read_batch") as reader:
        # 讀者已 SELECT header，但尚未讀 registry／成本列；此時另一程序（pytest）提交破壞。
        with database.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.exec_driver_sql(f"DELETE FROM {table} WHERE rowid = (SELECT min(rowid) FROM {table})")
            connection.commit()
        damaged = rows(database)
        same_transaction = finish(reader, release=True)
    assert same_transaction["exports"] == original["exports"]
    assert_complete(same_transaction)
    assert run(database, "view")["error"] == "AtomicSnapshotConflict"
    assert rows(database) == damaged  # 不藉讀取修補或保留舊版為目前有效。
