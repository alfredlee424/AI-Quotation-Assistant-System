"""T43 六表完整快照雙庫唯讀對帳；不是權威認證或復原配號授權。"""
from contextlib import closing
from dataclasses import dataclass
import math
from pathlib import Path
import sqlite3
import time

from sqlalchemy import UniqueConstraint

from database.development_atomic_schema import development_atomic_schema
from engine import development_integrity_inspection as integrity
from engine.development_schema_inspection import _digest, _expected_schema, _table_signature


REPORT_VERSION = "development-snapshot-comparison-v1"
WARNING = ("僅兩份隔離開發六表在各自讀交易內通過完整性核驗後的內容對帳；"
           "參考端未經權威認證，兩端不是跨庫同時快照或同一復原點。"
           "共同過期、整組刪除或一致重寫仍可能相等；不是來源真實性、最新性或正式核准。"
           "保留未保存不代表可回收；不授權正式報價、確認、保存或恢復配號。")
TABLES = integrity.TABLES


class SnapshotComparisonError(RuntimeError):
    """任一端結構／完整性／容量／讀取失敗，不產生部分報告。"""


@dataclass(frozen=True)
class SnapshotTableComparison:
    table: str
    reference_rows: int
    candidate_rows: int
    missing_rows: int
    extra_rows: int
    changed_rows: int
    reference_digest: str
    candidate_digest: str


@dataclass(frozen=True)
class SnapshotIdentityConflict:
    table: str
    columns: tuple[str, ...]
    count: int


@dataclass(frozen=True)
class SnapshotComparisonReport:
    tables: tuple[SnapshotTableComparison, ...]
    identity_conflicts: tuple[SnapshotIdentityConflict, ...]
    reference_integrity: integrity.IntegrityInspectionReport
    candidate_integrity: integrity.IntegrityInspectionReport

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


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise SnapshotComparisonError("開發六表對帳逾時；未產生完整報告")


def _summarize(rows, table, deadline):
    # 核驗後直接摘要原始欄位，不使用持久摘要代替內容，不正規化內嵌 JSON。
    # 各端只留下不可逆索引再讀另一端，不同時持有兩庫原文。
    keys = sorted({tuple(column.name for column in constraint.columns)
                   for constraint in table.constraints if isinstance(constraint, UniqueConstraint)})
    fingerprints, aliases = {}, {key: {} for key in keys}
    for row in rows:
        _check_deadline(deadline)
        owner = _digest([row[column.name] for column in table.primary_key.columns])
        if owner in fingerprints:
            raise SnapshotComparisonError("開發六表對帳身份摘要重複；不產生報告")
        fingerprints[owner] = _digest([row[column.name] for column in table.columns])
        for columns, owners in aliases.items():
            alias = _digest([row[column] for column in columns])
            if alias in owners:
                raise SnapshotComparisonError("開發六表對帳唯一值摘要重複；不產生報告")
            owners[alias] = owner
    return fingerprints, aliases


def _read_snapshot(path, expected, metadata, deadline):
    _check_deadline(deadline)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                 timeout=max(0, deadline - time.monotonic()),
                                 isolation_level=None)) as connection:
        connection.set_authorizer(integrity._authorize)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        connection.execute("BEGIN")
        try:
            for name in TABLES:
                _check_deadline(deadline)
                if _table_signature(connection, name) != expected[name]:
                    raise SnapshotComparisonError("開發六表對帳結構不符；停止並隔離調查")
            budget = integrity.MAX_TOTAL_BYTES
            for name in TABLES:
                budget = integrity._preflight(connection, metadata.tables[name], deadline, budget)
            rows = {name: integrity._read_table(connection, metadata.tables[name], deadline) for name in TABLES}
            saved, reserved_only = integrity._verify_contents(rows, deadline)
            report = integrity.IntegrityInspectionReport(
                tuple((name, len(rows[name])) for name in TABLES), saved, reserved_only, _digest(expected))
            summaries = {name: _summarize(rows[name], metadata.tables[name], deadline) for name in TABLES}
            _check_deadline(deadline)
            return summaries, report
        finally:
            connection.set_progress_handler(None, 0)
            connection.rollback()


def _compare(left, right, deadline):
    comparisons, conflicts = [], []
    for name in TABLES:
        _check_deadline(deadline)
        a, aliases_a = left[name]
        b, aliases_b = right[name]
        changed = 0
        for key in a.keys() & b.keys():
            _check_deadline(deadline)
            changed += a[key] != b[key]
        comparisons.append(SnapshotTableComparison(
            name, len(a), len(b), len(a.keys() - b.keys()), len(b.keys() - a.keys()),
            changed, _digest(a), _digest(b)))
        for columns, owners_a in aliases_a.items():
            owners_b = aliases_b[columns]
            count = 0
            for key in owners_a.keys() & owners_b.keys():
                _check_deadline(deadline)
                count += owners_a[key] != owners_b[key]
            if count:
                conflicts.append(SnapshotIdentityConflict(name, columns, count))
    return tuple(comparisons), tuple(conflicts)


def compare_development_snapshots(reference: Path, candidate: Path, *, timeout: float = 10.0
                                  ) -> SnapshotComparisonReport:
    """可信 caller 明確指定不同既存隔離檔。每端結構／內容／摘要共用一個讀交易。

    兩端依序獨立讀取，不 ATTACH、不停寫、不讀其他表、不信外部完整性報告。
    共用整次期限；各端沿用完整性檢查器容量上限，先預檢六表再讀取原文。
    不同資料的 SHA-256 摘要用於比對，不是簽章或授權憑證。
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
    except OSError:
        raise SnapshotComparisonError("開發六表對帳來源無法開啟；請隔離並調查") from None
    try:
        deadline = time.monotonic() + timeout
        expected = _expected_schema()
        metadata, *_ = development_atomic_schema()
        left, reference_report = _read_snapshot(reference, expected, metadata, deadline)
        right, candidate_report = _read_snapshot(candidate, expected, metadata, deadline)
        comparisons, conflicts = _compare(left, right, deadline)
        _check_deadline(deadline)
        return SnapshotComparisonReport(comparisons, conflicts, reference_report, candidate_report)
    except integrity.IntegrityInspectionError:
        raise SnapshotComparisonError("開發六表對帳完整性、容量或期限檢查失敗；不產生部分報告") from None
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise SnapshotComparisonError("開發六表對帳失敗；請隔離兩端並由開發／DBA 調查") from None
