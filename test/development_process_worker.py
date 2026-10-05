"""T31 專用子程序：只接受測試父程序的暫存庫及合成請求，不是應用入口。"""
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {
    "config", "database.connection", "database.models", "database.repository",
    "engine.pricing", "engine.preview", "engine.snapshot", "agent.core",
}


def isolation(event, args):
    # 子程序不繼承 pytest monkeypatch；必須在第三方／專案匯入之前自行隔離。
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).resolve()
        if path.name.startswith(".env") or path.is_relative_to(ROOT / "logs"):
            raise AssertionError("worker 不得讀私人環境檔／日誌")
    if event in ("socket.__new__", "socket.connect", "socket.getaddrinfo"):
        raise AssertionError("worker 禁止網路")
    if event == "import" and args[0] in FORBIDDEN:
        raise AssertionError("worker 禁止正式服務匯入")


sys.addaudithook(isolation)
# -I 不依賴工作目錄或 PYTHONPATH；只加入已知專案與純合成 fixture 位置。
sys.path[:0] = [str(ROOT), str(ROOT / "test")]

from datetime import datetime, timezone
from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool

from engine.development_atomic_contract import (
    build_development_reservation_request, decode_request, identity_of,
)
from engine.development_atomic_snapshot import (
    AtomicSnapshotConflict, AtomicSnapshotTimeout, DevelopmentAtomicSnapshotService,
)
from engine.development_snapshot_view import DevelopmentSnapshotReader, export_snapshot
from engine.quote_number_reservation import (
    QuoteNumberReservationService, ReservationConflict, ReservationTimeout,
)


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    options = json.loads(sys.stdin.readline())
    path = Path(options["database"]).resolve(strict=True)
    if path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError("只接受工作區外已存在的測試暫存庫")
    req = decode_request(options["payload"])
    collisions, gate_used = [], False

    def gate(stage):
        nonlocal gate_used
        if not gate_used and options.get("gate") == stage:
            gate_used = True
            emit({"ready": stage})
            if sys.stdin.readline().strip() != "continue":
                raise RuntimeError("父程序未明確釋放同步點")

    def clock():
        if options.get("forbid_clock"):
            raise AssertionError("冪等重試不得重新取時間／配號")
        return datetime(2026, 10, 2, 0, 30, 45, tzinfo=timezone.utc)

    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def connect(dbapi, _):
        dbapi.execute("PRAGMA foreign_keys=ON")

    @event.listens_for(engine, "before_cursor_execute")
    def before(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO "):
            gate("before:" + statement.split()[2])

    occurrences = {}

    @event.listens_for(engine, "after_cursor_execute")
    def after(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO "):
            table = statement.split()[2]
            occurrences[table] = occurrences.get(table, 0) + 1
            gate(f"after:{table}:{occurrences[table]}")

    @event.listens_for(engine, "commit")
    def commit(connection):
        gate("before_commit")  # SQLAlchemy commit 事件在實際 DBAPI commit 前。

    @event.listens_for(engine, "handle_error")
    def error(context):
        collisions.append(str(context.original_exception))  # 只含合成測試錯誤，無 SQL 參數。

    try:
        mode = options["mode"]
        result = {}
        if mode in ("reserve", "full"):
            result["reservation"] = QuoteNumberReservationService(
                engine, clock=clock, timeout=15, max_attempts=300,
            ).reserve(build_development_reservation_request(req))
            gate("after_reserve")
        if mode in ("save", "full"):
            result["saved"] = DevelopmentAtomicSnapshotService(
                engine, clock=clock, timeout=15, max_attempts=300,
            ).save(req)
            gate("after_save")
        if mode in ("view", "full"):
            readonly = create_engine(f"sqlite:///file:{path}?mode=ro&uri=true", poolclass=NullPool)

            def authorize(action, arg1, arg2, db, trigger):
                if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                              sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_PRAGMA and arg1 in ("foreign_keys", "busy_timeout", "read_uncommitted"):
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            @event.listens_for(readonly, "connect")
            def readonly_connect(dbapi, _):
                dbapi.execute("PRAGMA foreign_keys=ON")
                dbapi.set_authorizer(authorize)

            @event.listens_for(readonly, "after_cursor_execute")
            def readonly_after(connection, cursor, statement, parameters, context, many):
                if "FROM development_atomic_batch " in statement:
                    gate("after_read_batch")

            try:
                reader = DevelopmentSnapshotReader(readonly)
                snapshot = reader.read(identity_of(req))
                result["exports"] = None if snapshot is None else {
                    str(child): {
                        fmt: export_snapshot(reader, snapshot, child_quote_id=child, format=fmt).data.decode("utf-8")
                        for fmt in ("json", "txt")
                    }
                    for child in (None, *(c.child_quote_id for c in req.children))
                }
            finally:
                readonly.dispose()
        assert not FORBIDDEN.intersection(sys.modules)
        emit({**result, "collisions": collisions})
    except (AtomicSnapshotConflict, AtomicSnapshotTimeout, ReservationConflict, ReservationTimeout) as exc:
        emit({"error": type(exc).__name__, "collisions": collisions})
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
