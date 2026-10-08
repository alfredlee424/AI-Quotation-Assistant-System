"""T43 隔離加法式建表／相容／停用回退演練；不是正式 migration 工具。"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, create_engine, event, inspect, select
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from database import repository
from database.connection import Base
from database.development_atomic_schema import development_atomic_schema
from database.models import QuoteSnapshotDocument, ordqdt_ai
from database.quote_number_reservation_schema import reservation_schema
from development_atomic_fixtures import request
from engine.development_atomic_contract import identity_of
from engine.development_atomic_snapshot import DevelopmentAtomicSnapshotService
from test_development_backup_restore import (
    assert_integrity, backup_copy, journal_connection, restored_engine,
)
from test_development_process_integration import assert_complete, rows, run


LEGACY_TABLES = {"ordqdt_ai", "quote_snapshot_document"}
DEVELOPMENT_TABLES = set(development_atomic_schema()[0].tables)


def schema_image(engine):
    """比較實際 DDL（含索引／約束），不是只比較 ORM 宣告。"""
    with engine.connect() as connection:
        return connection.exec_driver_sql(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).all()


def table_image(engine, metadata):
    with engine.connect() as connection:
        return {
            table.name: connection.execute(select(table).order_by(*table.primary_key.columns)).all()
            for table in metadata.sorted_tables
        }


def legacy_read(sessions):
    """真正既有 repository 查成本；完整文件則沿用 ORM 原字串讀取。"""
    with sessions() as session:
        payloads = [row.payload for row in session.query(QuoteSnapshotDocument).order_by(
            QuoteSnapshotDocument.workgroup, QuoteSnapshotDocument.preview_id)]
    return {
        "documents": payloads,
        "snapshots": {
            (group, ref): repository.get_quote_snapshot(ref, workgroup=group)
            for group, ref in (("103", "LEGACY"), ("104", "LEGACY"), ("103", "ROWS-ONLY"))
        },
    }


@pytest.fixture
def legacy_database(tmp_path, monkeypatch, isolated_db):
    engine = create_engine(f"sqlite:///{tmp_path / 'schema-lifecycle.sqlite'}", poolclass=NullPool)
    event.listen(engine, "connect", lambda dbapi, _: dbapi.execute("PRAGMA foreign_keys=ON"))
    metadata = MetaData()
    documents = QuoteSnapshotDocument.__table__.to_metadata(metadata)
    costs = ordqdt_ai.__table__.to_metadata(metadata)
    metadata.create_all(engine)  # 只在暫存庫複製兩張既有表；沒有主檔可供重算。
    sessions = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(repository, "_session", sessions)
    try:
        with engine.begin() as connection:
            # 合成歷史內容，不宣稱這是工程核准的正式預覽。
            for group in ("103", "104"):
                connection.execute(documents.insert().values(
                    workgroup=group, preview_id="legacy-preview", ref_no="LEGACY",
                    payload=json.dumps({"legacy_fixture": True, "workgroup": group,
                                        "text": "歷史原文\r\n保留空白  \u0000",
                                        "amount": "123.450000000000000001"}, ensure_ascii=False, indent=2),
                ))
                # 插入順序刻意與五位序號相反；相同路徑不得去重。
                for seq, price in (("00002", 0.0), ("00001", 123.45)):
                    connection.execute(costs.insert().values(
                        workgroup=group, ref_no="LEGACY", seq_no=seq, path=r"LEGACY\A001",
                        part_code="A001", part_desc="保留舊說明", spdsc="原色號  ",
                        qty=2, stdqty=1.25, stdpar=3, compri=price, unit_price=price,
                        amount=price, unit="PCS", status="C", transferred="N",
                        adddate="2026-09-01", addusrno="TEST", abndat=None,
                    ))
            # 更早的列式歷史不要求回填完整文件或新增關聯。
            connection.execute(costs.insert().values(
                workgroup="103", ref_no="ROWS-ONLY", seq_no="99999",
                path=r"LEGACY\B001", part_code="B001", qty=None, compri=None,
            ))
        baseline = legacy_read(sessions)
        assert [r["seq_no"] for r in baseline["snapshots"][("103", "LEGACY")]] == ["00001", "00002"]
        assert baseline["snapshots"][("103", "ROWS-ONLY")][0]["compri"] is None
        yield SimpleNamespace(engine=engine, metadata=metadata, sessions=sessions,
                              data=table_image(engine, metadata), schema=schema_image(engine),
                              read=baseline)
    finally:
        engine.dispose()


def assert_legacy_unchanged(db):
    assert table_image(db.engine, db.metadata) == db.data
    assert [row for row in schema_image(db.engine) if row.tbl_name in LEGACY_TABLES] == db.schema
    assert legacy_read(db.sessions) == db.read


def add_development_tables(engine, *, atomic_only=False):
    """本檔測試專用：明確實體交易，不作為應用啟動或可發布的遷移入口。"""
    metadata, *atomic = development_atomic_schema()
    with engine.connect() as connection:
        # SQLite legacy driver 的 SQLAlchemy begin 本身不會替 DDL 發出 BEGIN。
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            metadata.create_all(connection, tables=atomic if atomic_only else None)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("reserved_first", [False, True])
def test_additive_schema_preserves_legacy_and_existing_reservations(legacy_database, journal, reserved_first):
    db = legacy_database
    with journal_connection(db.engine, journal):
        assert set(inspect(db.engine).get_table_names()) == LEGACY_TABLES
        assert not DEVELOPMENT_TABLES.intersection(Base.metadata.tables)
        reserved = None
        if reserved_first:
            registry = reservation_schema()[0]
            registry.create_all(db.engine)
            reserved = run(db.engine, "reserve")["reservation"]
            registry_before = table_image(db.engine, registry)
        add_development_tables(db.engine, atomic_only=reserved_first)
        assert set(inspect(db.engine).get_table_names()) == LEGACY_TABLES | DEVELOPMENT_TABLES
        assert_legacy_unchanged(db)
        if reserved_first:
            assert table_image(db.engine, registry) == registry_before
        original = run(db.engine, "full")
        assert_complete(original)
        if reserved is not None:
            assert original["reservation"] == reserved
        before, ddl = rows(db.engine), schema_image(db.engine)
        add_development_tables(db.engine)  # 重跑不改表、不重建索引、不重新配號。
        assert rows(db.engine) == before and schema_image(db.engine) == ddl
        assert run(db.engine, "full", forbid_clock=True) == original
        for child in original["saved"]["children"]:
            assert repository.get_quote_snapshot(child["ref_no"]) == []
        assert_legacy_unchanged(db)
        assert_integrity(db.engine.url.database)


@contextmanager
def legacy_only_reader(engine, monkeypatch):
    path = Path(engine.url.database).resolve(strict=True)
    readonly = create_engine(f"sqlite:///{path.as_uri()}?mode=ro&uri=true", poolclass=NullPool)

    def authorize(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if table in LEGACY_TABLES else sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION,
                      sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    event.listen(readonly, "connect", lambda dbapi, _: dbapi.set_authorizer(authorize))
    try:
        with monkeypatch.context() as patch:
            sessions = sessionmaker(bind=readonly, autoflush=False)
            patch.setattr(repository, "_session", sessions)
            yield readonly, sessions
    finally:
        readonly.dispose()


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("state", ["reserved", "saved"])
def test_soft_rollback_reads_only_legacy_then_reenables_original_batch(legacy_database, monkeypatch, journal, state):
    db = legacy_database
    with journal_connection(db.engine, journal):
        add_development_tables(db.engine)
        original = run(db.engine, "full" if state == "saved" else "reserve")
        before, ddl = rows(db.engine), schema_image(db.engine)
        # 模擬停用新功能：舊讀取連線甚至不具開發表讀權；不刪表或恢復舊備份。
        with legacy_only_reader(db.engine, monkeypatch) as (readonly, sessions):
            assert legacy_read(sessions) == db.read
            with readonly.connect() as connection:
                with pytest.raises(DatabaseError):
                    connection.exec_driver_sql("SELECT * FROM development_atomic_batch").all()
            with readonly.connect() as connection:
                with pytest.raises(DatabaseError):
                    connection.exec_driver_sql("DELETE FROM ordqdt_ai")
        assert rows(db.engine) == before and schema_image(db.engine) == ddl
        assert_legacy_unchanged(db)
        if state == "saved":
            assert run(db.engine, "full", forbid_clock=True) == original
        else:
            assert run(db.engine, "view")["exports"] is None
            assert run(db.engine, "reserve", forbid_clock=True)["reservation"] == original["reservation"]
            completed = run(db.engine, "full")
            assert completed["reservation"] == original["reservation"]
            assert_complete(completed)
        assert_legacy_unchanged(db)


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("failure_position", range(1, 7))
def test_explicit_sqlite_ddl_transaction_rolls_back_every_new_table(legacy_database, journal, failure_position):
    db = legacy_database
    with journal_connection(db.engine, journal):
        created = []

        def fail_after_create(connection, cursor, statement, parameters, context, many):
            if statement.lstrip().startswith("CREATE TABLE"):
                created.append(statement.split()[2])
                if len(created) == failure_position:
                    raise RuntimeError("synthetic DDL failure")

        event.listen(db.engine, "after_cursor_execute", fail_after_create)
        try:
            with pytest.raises(RuntimeError, match="synthetic DDL failure"):
                add_development_tables(db.engine)
        finally:
            event.remove(db.engine, "after_cursor_execute", fail_after_create)
        assert len(created) == failure_position
        assert schema_image(db.engine) == db.schema
        assert_legacy_unchanged(db)
        add_development_tables(db.engine)
        assert_complete(run(db.engine, "full"))
        assert_legacy_unchanged(db)


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("failure_position", [2, 4])
def test_failed_four_table_upgrade_never_discards_committed_registry(legacy_database, journal, failure_position):
    db = legacy_database
    with journal_connection(db.engine, journal):
        registry = reservation_schema()[0]
        registry.create_all(db.engine)
        reserved = run(db.engine, "reserve")["reservation"]
        before, ddl = table_image(db.engine, registry), schema_image(db.engine)
        created = []

        def fail_after_create(connection, cursor, statement, parameters, context, many):
            if statement.lstrip().startswith("CREATE TABLE"):
                created.append(statement.split()[2])
                if len(created) == failure_position:
                    raise RuntimeError("synthetic upgrade failure")

        event.listen(db.engine, "after_cursor_execute", fail_after_create)
        try:
            with pytest.raises(RuntimeError, match="synthetic upgrade failure"):
                add_development_tables(db.engine, atomic_only=True)
        finally:
            event.remove(db.engine, "after_cursor_execute", fail_after_create)
        assert len(created) == failure_position
        assert schema_image(db.engine) == ddl and table_image(db.engine, registry) == before
        assert run(db.engine, "reserve", forbid_clock=True)["reservation"] == reserved
        assert_legacy_unchanged(db)
        add_development_tables(db.engine, atomic_only=True)
        completed = run(db.engine, "full")
        assert completed["reservation"] == reserved
        assert_complete(completed)


@pytest.mark.parametrize("stage", ["legacy", "registry", "partial"])
def test_missing_schema_is_an_error_not_empty_history_or_implicit_repair(legacy_database, stage):
    db = legacy_database
    if stage != "legacy":
        reservation_schema()[0].create_all(db.engine)
        run(db.engine, "reserve")
    if stage == "partial":
        _, batch, child, _, _ = development_atomic_schema()
        batch.create(db.engine)
        child.create(db.engine)
    before = schema_image(db.engine)
    service = DevelopmentAtomicSnapshotService(db.engine)
    if stage != "partial":
        with pytest.raises(OperationalError, match="no such table"):
            service.read(identity_of(request()))
    # 空 header 的 read 不能證明所有 schema 齊全；save 才會碰到缺少的表。
    with pytest.raises(OperationalError, match="no such table"):
        service.save(request())
    assert schema_image(db.engine) == before
    if stage == "partial":
        with db.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM development_atomic_batch").scalar() == 0
            assert connection.exec_driver_sql("SELECT count(*) FROM development_atomic_child").scalar() == 0
    assert_legacy_unchanged(db)


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_create_all_is_not_schema_drift_validation_or_repair(legacy_database, journal):
    db = legacy_database
    with journal_connection(db.engine, journal):
        add_development_tables(db.engine)
        original = run(db.engine, "full")
        # 僅暫存負例：表名仍存在，實際欄位已不相容。
        with db.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE development_atomic_cost RENAME COLUMN payload TO incompatible_payload")
        drifted = schema_image(db.engine)
        add_development_tables(db.engine)
        assert schema_image(db.engine) == drifted  # checkfirst 不驗欄位，也不修補。
        service = DevelopmentAtomicSnapshotService(db.engine)
        for operation in (lambda: service.read(identity_of(request())), lambda: service.save(request())):
            with pytest.raises(OperationalError, match="no such column"):
                operation()
        assert schema_image(db.engine) == drifted
        with db.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT payload FROM development_atomic_batch").scalar() == json.dumps(
                original["saved"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        assert_legacy_unchanged(db)


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_sqlalchemy_begin_alone_does_not_rollback_legacy_driver_ddl(legacy_database, journal):
    db = legacy_database
    with journal_connection(db.engine, journal):
        _, header, _ = reservation_schema()
        # 刻意反例，不能當成維運建議：legacy driver 未送實體 BEGIN 時 DDL 已提交。
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            with db.engine.begin() as connection:
                header.create(connection)
                raise RuntimeError("synthetic interruption")
        assert set(inspect(db.engine).get_table_names()) == LEGACY_TABLES | {header.name}
        assert_legacy_unchanged(db)


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_recreating_dropped_cost_table_cannot_recover_saved_content(legacy_database, tmp_path, backup_copy, journal):
    db = legacy_database
    with journal_connection(db.engine, journal):
        add_development_tables(db.engine)
        original = run(db.engine, "full")
        before = rows(db.engine)
        backup = backup_copy(db.engine.url.database, tmp_path / "before-downgrade.sqlite")
        clone = backup_copy(backup, tmp_path / "unsafe-downgrade.sqlite")
        with restored_engine(clone) as target:
            # 只破壞 clone：刪 leaf 表沒有 FK 錯誤，重建空表更不等於資料復原。
            with target.begin() as connection:
                connection.exec_driver_sql("DROP TABLE development_atomic_cost")
            add_development_tables(target)
            assert_integrity(clone)
            damaged = rows(target)
            assert damaged["development_atomic_cost"] == []
            assert run(target, "view")["error"] == "AtomicSnapshotConflict"
            assert run(target, "save")["error"] == "AtomicSnapshotConflict"
            assert rows(target) == damaged
        assert rows(db.engine) == before
        assert run(db.engine, "full", forbid_clock=True) == original
        assert_legacy_unchanged(db)


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("table", ["quote_number_reservation", "development_atomic_batch"])
def test_unknown_persisted_version_is_not_downgraded_or_rewritten(legacy_database, journal, table):
    db = legacy_database
    with journal_connection(db.engine, journal):
        add_development_tables(db.engine)
        run(db.engine, "full")
        # 合成未支援版本；只在本例暫存庫略過 CHECK 建立負例，不修改服務驗證。
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.exec_driver_sql(f"UPDATE {table} SET schema_version='unsupported-future-version'")
        before, ddl = rows(db.engine), schema_image(db.engine)
        add_development_tables(db.engine)
        assert run(db.engine, "view")["error"] == "AtomicSnapshotConflict"
        assert run(db.engine, "save")["error"] == "AtomicSnapshotConflict"
        if table == "quote_number_reservation":
            assert run(db.engine, "reserve", forbid_clock=True)["error"] == "ReservationConflict"
        assert rows(db.engine) == before and schema_image(db.engine) == ddl
        assert_legacy_unchanged(db)
