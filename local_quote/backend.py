"""本機 MySQL 邊界與唯讀結構檢查。此模組沒有建表、修表或 seed 路徑。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import re

from dotenv import dotenv_values
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url


class SettingsError(ValueError):
    """設定未符合本機限定規則（訊息不得包含連線資訊）。"""


class SchemaError(ValueError):
    """結構不符合保存合約。"""


@dataclass(frozen=True)
class Settings:
    db_conn_str: str = field(repr=False)
    workgroup: str


def _validated_url(value):
    try:
        if not isinstance(value, str) or any(ord(c) < 32 for c in value):
            raise ValueError
        url = make_url(value)
        if (url.drivername != "mysql+pymysql"
                or url.host not in {"localhost", "127.0.0.1", "::1"}
                or not url.database or "#" in value
                or any(k != "charset" or v != "utf8mb4" for k, v in url.query.items())):
            raise ValueError
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError
        return url.update_query_dict({"charset": "utf8mb4"})
    except Exception:
        raise SettingsError("只允許本機 mysql+pymysql 與 utf8mb4 連線設定") from None


def _workgroup(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 3
            or value != value.strip() or any(ord(c) < 32 for c in value)):
        raise SettingsError("WORKGROUP 必須為 1 至 3 字元")
    return value


def load_settings(path: str | Path = ".env") -> Settings:
    """只取檔案內兩個值，不插值、不採用 process environment、不修改環境。"""
    try:
        values = dotenv_values(path, interpolate=False)
    except Exception:
        raise SettingsError("無法讀取本機設定") from None
    raw = values.get("DB_CONN_STR")
    _validated_url(raw)
    return Settings(raw, _workgroup(values.get("WORKGROUP")))


# (SQL type, exact character length, nullable). Only consumed master columns
# are required; every persistence column is checked before any save.
def _v(length, nullable=True):
    return ("varchar", length, nullable)


AUDIT = {"adddate": _v(10), "addusrno": _v(8), "abndat": _v(10),
         "abntim": _v(8), "usrno": _v(8), "prgno": _v(10)}
REQUIRED_COLUMNS = {
    "invdoc": {"workgroup": _v(3, False), "prodkind": _v(12, False),
               "codsc": _v(30), "ordkind": _v(1), "quo_rate": ("float", None, True)},
    "ordstr": {"workgroup": _v(3, False), "pathf": _v(100, False),
               "pathc": _v(100, False), "optnof": _v(10, False),
               "optnoc": _v(10, False), "must_chose": _v(1),
               "has_name": _v(1), "has_inname": _v(1),
               "seq": ("int", None, True), "dmark": ("text", None, True)},
    "ordspd": {"workgroup": _v(3, False), "optno": _v(10, False),
               "kind": _v(1), "optdesc": _v(30), "code": _v(10)},
    "ordspe": {"workgroup": _v(3, False), "path": _v(100, False),
               "code": _v(10, False), "codsc": _v(40, False),
               "compri": ("float", None, True)},
    "ordqty": {"workgroup": _v(3, False), "path": _v(100, False),
               "code": _v(10, False), "codsc": _v(40), "part_path": _v(100),
               "stdqty": ("float", None, True), "stdpar": ("float", None, True)},
    "ordqdt_ai": {
        "workgroup": _v(3, False), "ref_no": _v(21, False), "seq_no": _v(5, False),
        "part_code": _v(10, False), "part_desc": _v(40), "path": _v(100, False),
        "opt_code": _v(10), "opt_desc": _v(40), "spc_code": _v(10), "spdsc": _v(40),
        **{k: ("float", None, True) for k in
           ("qty", "stdqty", "stdpar", "compri", "unit_price", "amount")},
        "unit": _v(5), "status": _v(1), "transferred": _v(1), **AUDIT,
    },
    "quote_snapshot_document": {"workgroup": _v(3, False),
                                "preview_id": _v(32, False), "ref_no": _v(21, False),
                                "payload": ("text", None, False)},
}
PRIMARY_KEYS = {
    "invdoc": ("workgroup", "prodkind"), "ordstr": ("workgroup", "pathf", "pathc"),
    "ordspd": ("workgroup", "optno"), "ordspe": ("workgroup", "path", "code", "codsc"),
    "ordqty": ("workgroup", "path", "code"),
    "ordqdt_ai": ("workgroup", "ref_no", "seq_no"),
    "quote_snapshot_document": ("workgroup", "preview_id"),
}
WRITE_TABLES = {"ordqdt_ai", "quote_snapshot_document"}


def _check_schema(schema, *, testing=False):
    """Validate normalized inspector facts; fail closed, without emitting data."""
    errors = []
    for table, expected in REQUIRED_COLUMNS.items():
        actual = schema.get(table)
        if actual is None:
            errors.append(f"{table}: missing table")
            continue
        if not testing and (actual["engine"].lower() != "innodb"
                            or not actual["collation"].lower().startswith("utf8mb4_")):
            errors.append(f"{table}: engine/charset")
        if tuple(actual["pk"]) != PRIMARY_KEYS[table]:
            errors.append(f"{table}: primary key")
        if actual["fk"] or actual["triggers"]:
            errors.append(f"{table}: foreign key/trigger risk")
        unique = {tuple(x) for x in actual["unique"]}
        allowed = {PRIMARY_KEYS[table]}
        if table == "quote_snapshot_document":
            allowed.add(("workgroup", "ref_no"))
            if ("workgroup", "ref_no") not in unique:
                errors.append(f"{table}: missing unique reference")
        if table in WRITE_TABLES and unique - allowed:
            errors.append(f"{table}: unexpected unique constraint")
        for name, (kind, length, nullable) in expected.items():
            col = actual["columns"].get(name)
            if col is None:
                errors.append(f"{table}.{name}: missing column")
                continue
            allowed_types = {kind}
            if table not in WRITE_TABLES:
                # 本機 ERP 主檔為 CHAR / DOUBLE；保存表仍精確要求 VARCHAR / FLOAT。
                allowed_types |= {"char"} if kind == "varchar" else {"double"} if kind == "float" else set()
            if (col["type"] not in allowed_types or col["length"] != length
                    or col["nullable"] != nullable):
                errors.append(f"{table}.{name}: type/length/nullability")
            if not testing and kind in {"varchar", "text"} and col["charset"] != "utf8mb4":
                errors.append(f"{table}.{name}: charset")
            if table in WRITE_TABLES and col.get("generated"):
                errors.append(f"{table}.{name}: generated column")
        if table in WRITE_TABLES:
            for name, col in actual["columns"].items():
                if name not in expected and not col["nullable"] and col.get("default") is None:
                    errors.append(f"{table}: unexpected required column")
    if errors:
        raise SchemaError("結構檢查未通過：" + "; ".join(errors))
    return {"ok": True, "tables": tuple(REQUIRED_COLUMNS), "testing": testing}


class LocalDatabase:
    def __init__(self, settings: Settings, *, engine=None, testing: bool = False):
        self.workgroup = _workgroup(settings.workgroup)
        _validated_url(settings.db_conn_str)
        if engine is not None:
            if testing is not True or engine.dialect.name != "sqlite":
                raise SettingsError("注入引擎只限明確 testing=True 的隔離 SQLite")
            self.engine = engine
        else:
            if testing:
                raise SettingsError("測試必須注入隔離 SQLite 引擎")
            self.engine = create_engine(
                _validated_url(settings.db_conn_str), pool_pre_ping=True,
                hide_parameters=True, echo=False,
                connect_args={"connect_timeout": 5, "read_timeout": 15, "write_timeout": 15},
            )
        self.testing = testing

    @contextmanager
    def read(self):
        """Yield a SQLAlchemy Connection; transaction control/writes are forbidden."""
        with self.engine.connect() as conn:
            def no_transaction_control(connection):
                raise ValueError("唯讀連線不得自行結束交易")

            def guard(connection, cursor, statement, parameters, context, executemany):
                sql = statement.strip().rstrip(";")
                if (not re.match(r"^(SELECT|SHOW|DESCRIBE)\b", sql, re.I)
                        or re.search(r";|\b(INTO|FOR\s+UPDATE|LOCK\s+IN|GET_LOCK|RELEASE_LOCK)\b", sql, re.I)):
                    raise ValueError("唯讀連線只允許查詢")
            try:
                if self.testing:
                    conn.exec_driver_sql("PRAGMA query_only=ON")
                else:
                    conn.exec_driver_sql("START TRANSACTION READ ONLY")
                event.listen(conn, "before_cursor_execute", guard)
                event.listen(conn, "commit", no_transaction_control)
                event.listen(conn, "rollback", no_transaction_control)
                yield conn
            finally:
                if event.contains(conn, "before_cursor_execute", guard):
                    event.remove(conn, "before_cursor_execute", guard)
                for name in ("commit", "rollback"):
                    if event.contains(conn, name, no_transaction_control):
                        event.remove(conn, name, no_transaction_control)
                try:
                    conn.rollback()
                    if self.testing:
                        conn.exec_driver_sql("PRAGMA query_only=OFF")
                        conn.rollback()
                except Exception:
                    conn.invalidate()
                    raise

    def inspect_schema(self, connection=None):
        """No DDL. Save calls this again on its locked transaction connection."""
        if connection is None:
            if self.testing:
                # SQLite inspector requires read-only PRAGMA metadata statements.
                with self.engine.connect() as conn:
                    try:
                        return _check_schema(self._sqlite_schema(conn), testing=True)
                    finally:
                        conn.rollback()
            with self.read() as conn:
                return self.inspect_schema(conn)
        facts = self._sqlite_schema(connection) if self.testing else self._mysql_schema(connection)
        return _check_schema(facts, testing=self.testing)

    @staticmethod
    def _mysql_schema(conn):
        def rows(sql):
            return conn.execute(text(sql)).mappings().all()
        tables = rows("SELECT TABLE_NAME, ENGINE, TABLE_COLLATION FROM information_schema.TABLES "
                      "WHERE TABLE_SCHEMA = DATABASE()")
        facts = {r["TABLE_NAME"]: {"engine": r["ENGINE"] or "", "collation": r["TABLE_COLLATION"] or "",
                  "columns": {}, "pk": [], "unique": [], "fk": [], "triggers": []}
                 for r in tables if r["TABLE_NAME"] in REQUIRED_COLUMNS}
        for r in rows("SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, "
                      "IS_NULLABLE, CHARACTER_SET_NAME, COLUMN_DEFAULT, EXTRA "
                      "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"):
            if r["TABLE_NAME"] in facts:
                facts[r["TABLE_NAME"]]["columns"][r["COLUMN_NAME"]] = {
                    "type": r["DATA_TYPE"].lower(),
                    "length": r["CHARACTER_MAXIMUM_LENGTH"] if r["DATA_TYPE"].lower() in {"varchar", "char"} else None,
                    "nullable": r["IS_NULLABLE"] == "YES", "charset": r["CHARACTER_SET_NAME"],
                    "default": r["COLUMN_DEFAULT"], "generated": "GENERATED" in (r["EXTRA"] or "").upper()}
        indexes = {}
        for r in rows("SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME, SUB_PART, NON_UNIQUE "
                      "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
                      "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"):
            if r["TABLE_NAME"] in facts and not r["NON_UNIQUE"]:
                indexes.setdefault((r["TABLE_NAME"], r["INDEX_NAME"]), []).append(
                    r["COLUMN_NAME"] if r["SUB_PART"] is None else "<prefix>")
        for (table, name), cols in indexes.items():
            if name == "PRIMARY":
                facts[table]["pk"] = cols
            facts[table]["unique"].append(cols)
        for r in rows("SELECT TABLE_NAME, REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE "
                      "WHERE REFERENCED_TABLE_NAME IS NOT NULL AND "
                      "(TABLE_SCHEMA = DATABASE() OR REFERENCED_TABLE_SCHEMA = DATABASE())"):
            for table in (r["TABLE_NAME"], r["REFERENCED_TABLE_NAME"]):
                if table in facts:
                    facts[table]["fk"].append(True)
        for r in rows("SELECT EVENT_OBJECT_TABLE FROM information_schema.TRIGGERS "
                      "WHERE TRIGGER_SCHEMA = DATABASE()"):
            if r["EVENT_OBJECT_TABLE"] in facts:
                facts[r["EVENT_OBJECT_TABLE"]]["triggers"].append(True)
        return facts

    @staticmethod
    def _sqlite_schema(conn):
        inspector = inspect(conn)
        names = set(inspector.get_table_names())
        facts = {}
        for table in REQUIRED_COLUMNS.keys() & names:
            columns = {}
            for c in inspector.get_columns(table):
                kind = c["type"].__class__.__name__.lower()
                columns[c["name"]] = {
                    "type": {"integer": "int"}.get(kind, kind),
                    "length": getattr(c["type"], "length", None),
                    "nullable": c["nullable"], "charset": None, "default": c["default"],
                    "generated": bool(c.get("computed")),
                }
            facts[table] = {
                "engine": "sqlite", "collation": "", "columns": columns,
                "pk": inspector.get_pk_constraint(table)["constrained_columns"],
                "unique": [u["column_names"] for u in inspector.get_unique_constraints(table)],
                "fk": inspector.get_foreign_keys(table),
                "triggers": conn.execute(text("SELECT name FROM sqlite_master WHERE type='trigger' "
                                               "AND tbl_name=:name"), {"name": table}).all(),
            }
        return facts
