"""先用 T29 合成保存，再以檔案唯讀連線＋SQLite authorizer 驗證 T30。"""
from types import SimpleNamespace
import sqlite3

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool

from development_atomic_fixtures import request
from test_development_atomic_snapshot import database, reserve, registry_rows, service
from engine.development_atomic_contract import identity_of
from engine.development_snapshot_view import DevelopmentSnapshotReader


def readonly_engine(database):
    # 只使用 pytest tmp_path 已存在的檔案，mode=ro 不會建立不存在的 DB。
    engine = create_engine(f"sqlite:///file:{database.url.database}?mode=ro&uri=true", poolclass=NullPool)
    actions, statements = [], []

    def authorize(action, arg1, arg2, db, trigger):
        actions.append((action, arg1))
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                      sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA and arg1 in ("foreign_keys", "busy_timeout", "read_uncommitted"):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY  # 拒絕 INSERT／UPDATE／DELETE／全部 DDL／ATTACH 等。

    @event.listens_for(engine, "connect")
    def connect(dbapi, _):
        dbapi.execute("PRAGMA foreign_keys=ON")
        dbapi.set_authorizer(authorize)

    @event.listens_for(engine, "before_cursor_execute")
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)  # 測試只記 SQL 類型／表，無 payload／parameters。

    return engine, actions, statements


@pytest.fixture
def saved(database):
    req = request()
    reserve(database, req)
    document = service(database).save(req)
    before = registry_rows(database)
    engine, actions, statements = readonly_engine(database)
    reader = DevelopmentSnapshotReader(engine, source_context="synthetic-test")
    yield SimpleNamespace(database=database, req=req, identity=identity_of(req), document=document,
                          reader=reader, engine=engine, actions=actions, statements=statements,
                          registry_before=before)
    engine.dispose()
