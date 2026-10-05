"""T43 開發備份／還原演練；不是應用備份工具或正式恢復授權。"""
from contextlib import closing, contextmanager
from pathlib import Path
import shutil
import sqlite3
import sys
from time import monotonic

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool

from test_development_process_integration import (
    assert_complete, database, finish, rows, run, worker,
)


@pytest.fixture
def backup_copy(tmp_path):
    """僅在本例暫存目錄內，以 SQLite backup API 建立全新目的檔。"""
    def copy(source, destination):
        source = Path(source).resolve(strict=True)
        destination = Path(destination)
        assert source.is_relative_to(tmp_path.resolve()) and source.is_file()
        assert destination.parent.resolve() == tmp_path.resolve()
        # 不覆寫既存備份／原庫，也不跟隨目的 symlink。
        with destination.open("xb"):
            pass
        deadline = monotonic() + 10

        def progress(status, remaining, total):
            if monotonic() >= deadline:
                raise TimeoutError("隔離備份逾時，不留下可誤認成功的目的檔")

        try:
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
                with closing(sqlite3.connect(destination.as_uri() + "?mode=rw", uri=True)) as writer:
                    reader.backup(writer, pages=8, progress=progress, sleep=0.01)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination

    return copy


@contextmanager
def restored_engine(path):
    # 必須已由 backup 建立；這裡不建表、不補欄或搬移資料。
    path = Path(path).resolve(strict=True)
    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)
    event.listen(engine, "connect", lambda dbapi, _: dbapi.execute("PRAGMA foreign_keys=ON"))
    try:
        yield engine
    finally:
        engine.dispose()


def assert_integrity(path):
    with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@contextmanager
def journal_connection(database, journal):
    # 保持連線供測試控制日誌；需要阻止 checkpoint 時另明確固定讀取交易。
    with closing(sqlite3.connect(database.url.database)) as connection:
        assert connection.execute(f"PRAGMA journal_mode={journal}").fetchone()[0].upper() == journal
        connection.execute("PRAGMA wal_autocheckpoint=0")
        yield connection


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_complete_backup_restores_exact_exports_and_idempotency(database, tmp_path, backup_copy, journal):
    with journal_connection(database, journal) as keeper:
        # 合成舊版 opaque 資料：全庫備份應保留，不由新服務解碼／重算。
        keeper.execute("CREATE TABLE synthetic_legacy_snapshot (id TEXT PRIMARY KEY, payload BLOB)")
        keeper.execute("INSERT INTO synthetic_legacy_snapshot VALUES (?, ?)", ("old-v1", b"legacy\x00unchanged"))
        keeper.commit()
        original = run(database, "full")
        saved = assert_complete(original)
        before = rows(database)
        if journal == "WAL":
            assert Path(database.url.database + "-wal").stat().st_size > 32
        backup = backup_copy(database.url.database, tmp_path / "backup.sqlite")
        assert_integrity(backup)
        # 再從備份還原到另一新檔，不把備份本身當可寫工作庫。
        restored = backup_copy(backup, tmp_path / "restored.sqlite")
        backup_bytes = backup.read_bytes()
        with restored_engine(restored) as target:
            assert rows(target) == before
            viewed = run(target, "view")
            assert viewed["exports"] == original["exports"]
            assert assert_complete(viewed)["created_at"] == saved["created_at"]
            assert run(target, "full", forbid_clock=True) == original
            assert rows(target) == before
            with target.connect() as connection:
                assert connection.exec_driver_sql("SELECT id, payload FROM synthetic_legacy_snapshot").all() == [
                    ("old-v1", b"legacy\x00unchanged")]
        assert rows(database) == before
        assert backup.read_bytes() == backup_bytes
        assert_integrity(restored)


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_reservation_only_backup_preserves_orphans_on_restore(database, tmp_path, backup_copy, journal):
    with journal_connection(database, journal):
        reservation = run(database, "reserve")["reservation"]
        before = rows(database)
        backup = backup_copy(database.url.database, tmp_path / "reserved.sqlite")
        restored = backup_copy(backup, tmp_path / "restored.sqlite")
        with restored_engine(restored) as target:
            assert rows(target) == before
            assert run(target, "view")["exports"] is None
            assert run(target, "reserve", forbid_clock=True)["reservation"] == reservation
            completed = run(target, "full")
            assert_complete(completed)
            assert completed["reservation"] == reservation
            for table in ("quote_number_reservation", "quote_number_reserved_child"):
                assert rows(target)[table] == before[table]
        assert rows(database) == before  # 還原演練不回寫來源庫。


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("mode,gate", [
    ("reserve", "after:quote_number_reserved_child:2"),
    ("save", "after:development_atomic_child:2"),
])
@pytest.mark.parametrize("outcome", ["commit", "kill"])
def test_online_backup_excludes_uncommitted_batch(database, tmp_path, backup_copy, journal, mode, gate, outcome):
    with journal_connection(database, journal):
        if mode == "save":
            run(database, "reserve")
        committed = rows(database)
        with worker(database, mode, gate=gate) as writer:
            # 真實子程序停在第二列，持有寫交易；備份使用另一唯讀連線。
            backup = backup_copy(database.url.database, tmp_path / "during-write.sqlite")
            if outcome == "commit":
                finish(writer, release=True)
            else:
                writer.kill()
                writer.wait(timeout=20)
        assert_integrity(backup)
        restored = backup_copy(backup, tmp_path / "restored.sqlite")
        with restored_engine(restored) as target:
            assert rows(target) == committed
            assert run(target, "view")["exports"] is None
            if mode == "save":
                # 只在已完整保留的隔離 clone 重送合成請求，不能重新分配號碼。
                original_reservation = run(database, "reserve", forbid_clock=True)["reservation"]
                assert run(target, "reserve", forbid_clock=True)["reservation"] == original_reservation
                assert_complete(run(target, "full"))
            else:
                # 備份不含後來提交的保留；不能當作權威 registry 復原後繼續配號。
                assert all(not values for values in rows(target).values())


@pytest.mark.parametrize("table", [
    "quote_number_reserved_child", "development_atomic_child",
    "development_atomic_source_link", "development_atomic_cost",
])
def test_structurally_valid_but_incomplete_backup_rejected(database, tmp_path, backup_copy, table):
    original = run(database, "full")
    source_before = rows(database)
    backup = backup_copy(database.url.database, tmp_path / "healthy.sqlite")
    restored = backup_copy(backup, tmp_path / "damaged.sqlite")
    with restored_engine(restored) as target:
        # 只破壞 clone，不改原始庫或備份；刪 leaf 不一定產生 FK 錯誤。
        with target.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.exec_driver_sql(f"DELETE FROM {table} WHERE rowid=(SELECT min(rowid) FROM {table})")
            connection.commit()
        with closing(sqlite3.connect(restored)) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        damaged = rows(target)
        assert run(target, "view")["error"] == "AtomicSnapshotConflict"
        assert run(target, "save")["error"] == "AtomicSnapshotConflict"
        assert rows(target) == damaged  # 不嘗試補列／重算／重配號。
    assert rows(database) == source_before
    assert run(database, "view")["exports"] == original["exports"]
    with restored_engine(backup) as healthy:
        assert rows(healthy) == source_before


def test_old_valid_backup_is_not_proof_of_latest_registry(database, tmp_path, backup_copy):
    # 舊备份在第一筆保留之前；新 registry 提交後，舊備份仍可通過結構檢查。
    backup = backup_copy(database.url.database, tmp_path / "stale.sqlite")
    original = run(database, "full")
    assert_complete(original)
    assert_integrity(backup)
    restored = backup_copy(backup, tmp_path / "restored.sqlite")
    with restored_engine(restored) as target:
        assert run(target, "view")["exports"] is None
        assert rows(target) != rows(database)
        assert all(not values for values in rows(target).values())
        # 刻意不呼叫 reserve/full：舊庫無法偵測外界已使用的號碼，不可恢復開立。
    assert run(database, "full", forbid_clock=True) == original


@pytest.mark.parametrize("destination", ["source", "existing"])
def test_backup_helper_never_overwrites_existing_files(database, tmp_path, backup_copy, destination):
    run(database, "full")
    target = Path(database.url.database) if destination == "source" else tmp_path / "existing.sqlite"
    if destination == "existing":
        target.write_bytes(b"existing-backup-do-not-overwrite")
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        backup_copy(database.url.database, target)
    assert target.read_bytes() == before
    assert_complete(run(database, "view"))


def test_copying_only_live_wal_main_file_loses_committed_batch(database, tmp_path, backup_copy):
    with journal_connection(database, "WAL") as keeper:
        keeper.execute("BEGIN")
        assert keeper.execute("SELECT count(*) FROM development_atomic_batch").fetchone() == (0,)
        # 固定提交前的讀取版本，阻止新批次完整 checkpoint 至主檔。
        original = run(database, "full")
        assert Path(database.url.database + "-wal").stat().st_size > 32
        # 刻意的負例：只複製主檔，沒有 WAL；只能用於測試，不能作操作建議。
        incomplete = tmp_path / "unsafe-main-only.sqlite"
        shutil.copyfile(database.url.database, incomplete)
        assert_integrity(incomplete)
        with restored_engine(incomplete) as target:
            assert run(target, "view")["exports"] is None
            assert rows(target) != rows(database)
        complete = backup_copy(database.url.database, tmp_path / "online-backup.sqlite")
        with restored_engine(complete) as target:
            assert run(target, "view")["exports"] == original["exports"]
            assert rows(target) == rows(database)


def test_backup_timeout_discards_partial_destination(database, tmp_path, backup_copy, monkeypatch):
    run(database, "full")
    before = rows(database)
    times = iter((0, 11))  # 第一次 progress callback 超過明確期限。
    monkeypatch.setattr(sys.modules[__name__], "monotonic", lambda: next(times))
    destination = tmp_path / "interrupted.sqlite"
    with pytest.raises(TimeoutError, match="備份逾時"):
        backup_copy(database.url.database, destination)
    assert not destination.exists()
    assert rows(database) == before
    assert_complete(run(database, "view"))


def test_invalid_backup_source_is_not_published_as_restored_database(tmp_path, backup_copy):
    source = tmp_path / "corrupt.sqlite"
    source.write_bytes(b"not a SQLite database" * 1024)
    destination = tmp_path / "not-restored.sqlite"
    with pytest.raises(sqlite3.DatabaseError):
        backup_copy(source, destination)
    assert not destination.exists()
    assert source.read_bytes() == b"not a SQLite database" * 1024
