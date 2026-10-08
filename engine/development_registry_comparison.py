"""T43 開發 registry 雙庫唯讀對帳；內容相等不是資料有效或恢復配號授權。"""
from contextlib import closing
from dataclasses import dataclass
import math
from pathlib import Path
import sqlite3
import time

from database.quote_number_reservation_schema import REQUEST_VERSION, SCHEMA_VERSION, reservation_schema
from engine.development_schema_inspection import (
    _authorize as _schema_authorize, _digest, _expected_schema, _quoted, _table_signature,
)


REPORT_VERSION = "development-registry-comparison-v1"
WARNING = ("僅兩份開發 registry 的內容對帳；參考端未經權威認證。"
           "兩庫各自讀取快照，不是跨庫同一復原點；相等不代表最新、有效或未遭共同竄改。"
           "未驗證完整快照／業務內容，不授權正式報價、保存或恢復配號。")
HEADER = "quote_number_reservation"
CHILD = "quote_number_reserved_child"
TABLES = (HEADER, CHILD)
MAX_ROWS = {HEADER: 10000, CHILD: 100000}
MAX_ROW_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024  # 每庫；先查 byte length，再取內容，不截斷。
UNIQUE_KEYS = {HEADER: ("request_id", "preview_identity", "prefix"),
               CHILD: ("child_quote_id", "reserved_ref_no")}


class RegistryComparisonError(RuntimeError):
    """任一端無法完整讀取就失敗，不發布半份報告，不修補。"""


@dataclass(frozen=True)
class RegistryTableComparison:
    table: str
    reference_rows: int
    candidate_rows: int
    missing_rows: int  # 參考端有、候選端無；反向比較時語意反轉。
    extra_rows: int
    changed_rows: int  # 同主鍵、任一欄位不同，包含原始 payload 字串。
    reference_digest: str
    candidate_digest: str


@dataclass(frozen=True)
class RegistryIdentityConflict:
    table: str
    column: str
    count: int  # 同一全域唯一值在兩端屬於不同主鍵；不揭露實際識別或號碼。


@dataclass(frozen=True)
class RegistryComparisonReport:
    tables: tuple[RegistryTableComparison, ...]
    identity_conflicts: tuple[RegistryIdentityConflict, ...]
    schema_digest: str

    @property
    def contents_match(self):
        return not self.identity_conflicts and all(
            not (item.missing_rows or item.extra_rows or item.changed_rows) for item in self.tables)

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


def _authorize(action, arg1, arg2, database, trigger):
    if action == sqlite3.SQLITE_READ and database == "main" and arg1 in TABLES and trigger is None:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and arg2 == "length":
        return sqlite3.SQLITE_OK
    return _schema_authorize(action, arg1, arg2, database, trigger)


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise RegistryComparisonError("開發配號對帳逾時；未產生完整報告")


def _read_table(connection, table, deadline, budget):
    name = table.name
    columns = tuple(table.columns.keys())
    projection = ",".join(_quoted(column) for column in columns)
    sizes = "+".join(f"length(CAST({_quoted(column)} AS BLOB))" for column in columns)
    # SQLite 宣告容量不限制實際值；預先檢查全部列大小，同讀交易內才取 payload。
    count = 0
    for (size,) in connection.execute(
            f"SELECT {sizes} FROM main.{_quoted(name)} LIMIT ?", (MAX_ROWS[name] + 1,)):
        _check_deadline(deadline)
        count += 1
        if (count > MAX_ROWS[name] or size is None or size > MAX_ROW_BYTES or size > budget):
            raise RegistryComparisonError("開發配號對帳超過容量或含無效空值；不截斷或略過")
        budget -= size
    rows, aliases = {}, {column: {} for column in UNIQUE_KEYS[name]}
    integer_columns = {"child_count"} if name == HEADER else {"position"}
    for values in connection.execute(f"SELECT {projection} FROM main.{_quoted(name)}"):
        _check_deadline(deadline)
        row = dict(zip(columns, values))
        if any(type(value) is not (int if column in integer_columns else str)
               for column, value in row.items()):
            raise RegistryComparisonError("開發配號對帳遇到非契約儲存型別；請停止調查")
        if name == HEADER and (row["schema_version"], row["request_version"], row["state"]) != (
                SCHEMA_VERSION, REQUEST_VERSION, "RESERVED"):
            raise RegistryComparisonError("開發配號對帳遇到未知持久版本或狀態；不轉型")
        key = _digest([row[column.name] for column in table.primary_key.columns])
        if key in rows:
            raise RegistryComparisonError("開發配號對帳遇到重複身份；請停止調查")
        rows[key] = _digest(values)
        for column, owners in aliases.items():
            alias = _digest(row[column])
            if alias in owners:
                raise RegistryComparisonError("開發配號對帳遇到重複唯一值；請停止調查")
            owners[alias] = key
    return rows, aliases, budget


def _read_registry(path, expected, tables, deadline):
    _check_deadline(deadline)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                 timeout=max(0, deadline - time.monotonic()),
                                 isolation_level=None)) as connection:
        connection.set_authorizer(_authorize)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        connection.execute("BEGIN")
        try:
            # 結構與兩表內容在同一實體交易，不能用另一連線的舊結構報告當通行證。
            for name in TABLES:
                _check_deadline(deadline)
                if _table_signature(connection, name) != expected[name]:
                    raise RegistryComparisonError("開發配號登錄結構不符；請先隔離並檢查結構")
            result, budget = {}, MAX_TOTAL_BYTES
            for table in tables:
                rows, aliases, budget = _read_table(connection, table, deadline, budget)
                result[table.name] = (rows, aliases)
            _check_deadline(deadline)
            return result
        finally:
            connection.set_progress_handler(None, 0)
            connection.rollback()


def compare_development_registries(reference: Path, candidate: Path, *, timeout: float = 2.0
                                   ) -> RegistryComparisonReport:
    """明確指定兩個不同既存隔離檔案；reference 只是比較方向，不宣稱權威。

    caller 負責可信來源、停寫及同一復原點。兩庫依序各持有獨立讀交易，不 ATTACH，
    不接外部連線。不讀其他表、不解析或輸出 registry 內的原文／成本 payload。
    byte-for-byte payload 差異也算不同；一致仍須另做完整性及最新性驗證。
    """
    if not all(isinstance(path, Path) for path in (reference, candidate)):
        raise TypeError("須明確提供兩個隔離 SQLite 的 Path，不接受字串、URL 或外部連線")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("timeout 須為大於零且至多 30 秒的有限數值")
    try:
        if any(path.is_symlink() or not path.is_file() for path in (reference, candidate)):
            raise ValueError("對帳來源須為既存一般檔案，拒絕缺檔、目錄及末端符號連結")
        reference, candidate = reference.resolve(strict=True), candidate.resolve(strict=True)
        if reference.samefile(candidate):
            raise ValueError("對帳須為不同檔案，拒絕同路徑或硬連結自我比對")
        deadline = time.monotonic() + timeout
        expected = {name: signature for name, signature in _expected_schema().items() if name in TABLES}
        _, *tables = reservation_schema()
        left = _read_registry(reference, expected, tables, deadline)
        right = _read_registry(candidate, expected, tables, deadline)
        comparisons, conflicts = [], []
        for name in TABLES:
            _check_deadline(deadline)
            a, a_aliases = left[name]
            b, b_aliases = right[name]
            common = a.keys() & b.keys()
            comparisons.append(RegistryTableComparison(
                name, len(a), len(b), len(a.keys() - b.keys()), len(b.keys() - a.keys()),
                sum(a[key] != b[key] for key in common), _digest(a), _digest(b)))
            for column in UNIQUE_KEYS[name]:
                owners_a, owners_b = a_aliases[column], b_aliases[column]
                count = sum(owners_a[key] != owners_b[key] for key in owners_a.keys() & owners_b.keys())
                if count:
                    conflicts.append(RegistryIdentityConflict(name, column, count))
        _check_deadline(deadline)
        return RegistryComparisonReport(tuple(comparisons), tuple(conflicts), _digest(expected))
    except (sqlite3.Error, OSError, UnicodeError):
        # 不將資料庫錯誤中的路徑、SQL、參數或客戶資料傳到輸出／日誌。
        raise RegistryComparisonError("開發配號對帳失敗；請隔離兩端並由開發／DBA 調查") from None
