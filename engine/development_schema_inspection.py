"""T43 隔離 SQLite 結構前置檢查；不遷移、不讀 payload、不授權寫入。"""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from database.development_atomic_schema import development_atomic_schema


REPORT_VERSION = "development-schema-inspection-v1"
WARNING = ("僅開發結構比對，不是正式部署、報價或恢復配號授權；"
           "未驗證資料完整性、持久文件版本、備份最新性或權威 registry。")


class SchemaInspectionError(RuntimeError):
    """無法完成整份檢查；不回傳部分成功或保留前次報告。"""


@dataclass(frozen=True)
class SchemaIssue:
    table: str  # 只輸出程式契約中的表名，不輸出現場任意名稱或 DDL。
    code: str


@dataclass(frozen=True)
class SchemaInspectionReport:
    expected_digest: str
    observed_digest: str
    checked_tables: tuple[str, ...]
    issues: tuple[SchemaIssue, ...]

    @property
    def schema_matches(self):
        return not self.issues

    @property
    def report_version(self):
        return REPORT_VERSION

    @property
    def warning(self):
        return WARNING

    @property
    def can_quote(self):
        return False

    @property
    def can_confirm(self):
        return False

    @property
    def can_save(self):
        return False

    @property
    def can_resume_numbering(self):
        return False


def _tokens(sql):
    # 只忽略引號外空白及大小寫；不改字串常值、不推論 DDL 語意等價。
    pattern = r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|\s+|\w+|."
    return tuple(token if token[0] in "'\"`[" else token.upper()
                 for token in re.findall(pattern, sql or "") if not token.isspace())


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def _table_signature(connection, name):
    objects = connection.execute(
        "SELECT type, name, sql FROM main.sqlite_master WHERE tbl_name=? ORDER BY type, name",
        (name,),
    ).fetchall()
    table = next((row for row in objects if row[1] == name and row[0] in ("table", "view")), None)
    if table is None:
        return {"kind": "missing"}
    if table[0] != "table":
        return {"kind": "not_table"}
    indexes = []
    for _, index, unique, origin, partial in connection.execute(
            f"PRAGMA main.index_list({_quoted(name)})").fetchall():
        # cid=-2 的 expression、排序、定序及 key 欄位都保留，不只看索引名稱。
        columns = connection.execute(f"PRAGMA main.index_xinfo({_quoted(index)})").fetchall()
        indexes.append((index, unique, origin, partial, columns))
    return {
        "kind": "table",
        "definition": _tokens(table[2]),
        "columns": connection.execute(f"PRAGMA main.table_xinfo({_quoted(name)})").fetchall(),
        "foreign_keys": sorted(connection.execute(f"PRAGMA main.foreign_key_list({_quoted(name)})").fetchall()),
        "indexes": sorted(indexes),
        "extra_objects": [(kind, obj, _tokens(sql)) for kind, obj, sql in objects
                          if kind in ("trigger", "index") and sql is not None],
    }


def _expected_schema():
    metadata, *_ = development_atomic_schema()
    # 只在私有、無資料的記憶體參考庫編譯契約；絕不在受檢檔案建表。
    with closing(sqlite3.connect(":memory:")) as reference:
        for table in metadata.sorted_tables:
            reference.execute(str(CreateTable(table).compile(dialect=dialect())))
            for index in sorted(table.indexes, key=lambda item: item.name):
                reference.execute(str(CreateIndex(index).compile(dialect=dialect())))
        return {name: _table_signature(reference, name) for name in sorted(metadata.tables)}


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _authorize(action, arg1, arg2, database, trigger):
    if action == sqlite3.SQLITE_READ:
        return (sqlite3.SQLITE_OK if database == "main" and arg1 == "sqlite_master"
                else sqlite3.SQLITE_DENY)
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_TRANSACTION and arg1 in ("BEGIN", "ROLLBACK"):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 in (
            "table_xinfo", "foreign_key_list", "index_list", "index_xinfo"):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def inspect_development_schema(path: Path, *, timeout: float = 2.0) -> SchemaInspectionReport:
    """明確指定既存隔離檔；每次新唯讀連線、單一實體交易，不接受 URL／外部交易。

    呼叫端須保證來源為可信開發庫。本報告不是 capability token，沒有快取或保存入口。
    timeout 限 busy 等候及 SQLite VM 工作，不保證中斷作業系統 I/O。
    """
    if not isinstance(path, Path):
        raise TypeError("須明確提供既存隔離 SQLite 的 Path，不接受連線、URL 或字串")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("timeout 須為大於零且至多 30 秒的有限數值")
    if path.is_symlink() or not path.is_file():
        raise ValueError("來源須為既存一般檔案，不接受目錄、缺檔或末端符號連結")
    try:
        resolved = path.resolve(strict=True)
        expected = _expected_schema()
        deadline = time.monotonic() + timeout
        # as_uri 會編碼 ?/# 等字元，不能藉檔名注入 mode=rw 或 ATTACH。
        with closing(sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True,
                                     timeout=timeout, isolation_level=None)) as connection:
            connection.set_authorizer(_authorize)
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            connection.execute("BEGIN")
            try:
                observed = {}
                for name in expected:
                    if time.monotonic() >= deadline:
                        raise SchemaInspectionError("開發結構檢查逾時；未產生完整報告")
                    observed[name] = _table_signature(connection, name)
            finally:
                connection.set_progress_handler(None, 0)
                connection.rollback()
    except (sqlite3.Error, OSError):
        # 不將 SQL、現場任意名稱、路徑或底層訊息帶到輸出／日誌。
        raise SchemaInspectionError("開發結構檢查失敗；請隔離來源並由開發／DBA 調查") from None
    issues = []
    for name, wanted in expected.items():
        actual = observed[name]
        if actual["kind"] != "table":
            issues.append(SchemaIssue(name, actual["kind"]))
            continue
        for section in ("definition", "columns", "foreign_keys", "indexes", "extra_objects"):
            if actual[section] != wanted[section]:
                issues.append(SchemaIssue(name, section + "_mismatch"))
    return SchemaInspectionReport(_digest(expected), _digest(observed), tuple(expected), tuple(issues))
