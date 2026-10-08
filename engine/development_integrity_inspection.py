"""T43 六張開發表的有界唯讀完整性檢查；不是復原／最新性／授權認證。"""
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, fields
from datetime import datetime
import math
from pathlib import Path
import sqlite3
import time

from sqlalchemy import Integer, UniqueConstraint

from database.development_atomic_schema import development_atomic_schema
from engine.development_atomic_contract import (
    MAX_DOCUMENT_BYTES, build_development_reservation_request, canonical,
    decode_request, fixed_object,
)
from engine.development_atomic_snapshot import DevelopmentAtomicSnapshotService
from engine.development_schema_inspection import (
    _authorize as _schema_authorize, _digest, _expected_schema, _quoted, _table_signature,
)
from engine.quote_number_reservation import (
    DevelopmentReservationChild, DevelopmentReservationRequest, QuoteNumberReservationService,
    _child_rows, _result, _validate, taiwan_prefix,
)


REPORT_VERSION = "development-integrity-inspection-v1"
WARNING = ("僅當次單一讀交易的六張開發表內部一致性；不是業務、工程或身份核准。"
           "保留未保存不等於遺失或可回收；全部刪除、共同竄改或健康舊備份仍可能通過。"
           "未認證權威來源、最新性或同一復原點，不授權正式報價、保存或恢復配號。")
HEADER = "quote_number_reservation"
RESERVED_CHILD = "quote_number_reserved_child"
BATCH = "development_atomic_batch"
CHILD = "development_atomic_child"
LINK = "development_atomic_source_link"
COST = "development_atomic_cost"
TABLES = (HEADER, RESERVED_CHILD, BATCH, CHILD, LINK, COST)
MAX_ROWS = {HEADER: 10000, RESERVED_CHILD: 100000, BATCH: 10000,
            CHILD: 100000, LINK: 100000, COST: 100000}
MAX_ROW_BYTES = 128 * 1024 * 1024 + 4096
MAX_TOTAL_BYTES = 256 * 1024 * 1024


class IntegrityInspectionError(RuntimeError):
    """結構／內容／資源任一失敗均不產生部分成功報告，不修補。"""


@dataclass(frozen=True)
class IntegrityInspectionReport:
    table_counts: tuple[tuple[str, int], ...]
    verified_saved_batches: int
    reservation_only_batches: int
    schema_digest: str

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
        raise IntegrityInspectionError("開發完整性檢查逾時；未產生完整報告")


def _require(condition):
    if not condition:
        raise IntegrityInspectionError("開發六表內容不完整或不一致；停止調查，不修補或配號")


def _preflight(connection, table, deadline, budget):
    sizes = "+".join(f"length(CAST({_quoted(column.name)} AS BLOB))" for column in table.columns)
    for count, (size,) in enumerate(connection.execute(
            f"SELECT {sizes} FROM main.{_quoted(table.name)} LIMIT ?",
            (MAX_ROWS[table.name] + 1,)), 1):
        _check_deadline(deadline)
        if count > MAX_ROWS[table.name] or size is None or size > MAX_ROW_BYTES or size > budget:
            raise IntegrityInspectionError("開發完整性檢查超過容量或含空值；不截斷或略過")
        budget -= size
    return budget


def _read_table(connection, table, deadline):
    columns = tuple(table.columns)
    projection = ",".join(_quoted(column.name) for column in columns)
    keys = [tuple(column.name for column in table.primary_key.columns)]
    keys += [tuple(column.name for column in constraint.columns)
             for constraint in table.constraints if isinstance(constraint, UniqueConstraint)]
    seen = [set() for _ in keys]
    rows = []
    for values in connection.execute(f"SELECT {projection} FROM main.{_quoted(table.name)}"):
        _check_deadline(deadline)
        if any(type(value) is not (int if isinstance(column.type, Integer) else str)
               for column, value in zip(columns, values, strict=True)):
            raise IntegrityInspectionError("開發完整性檢查遇到非契約儲存型別")
        row = dict(zip((column.name for column in columns), values, strict=True))
        for key, owners in zip(keys, seen, strict=True):
            identity = tuple(row[column] for column in key)
            _require(identity not in owners)
            owners.add(identity)
        rows.append(row)
    return rows


def _decode_reservation(payload):
    data = fixed_object(payload, capacity=MAX_ROW_BYTES)
    _require(set(data) == {field.name for field in fields(DevelopmentReservationRequest)})
    _require(type(data["children"]) is list and 1 <= len(data["children"]) <= 999)
    child_fields = {field.name for field in fields(DevelopmentReservationChild)}
    _require(all(type(child) is dict and set(child) == child_fields for child in data["children"]))
    data["children"] = tuple(DevelopmentReservationChild(**child) for child in data["children"])
    request = DevelopmentReservationRequest(**data)
    _require(_validate(request) == payload)
    return request


def _verify_contents(rows, deadline):
    # 各批只取自己完整的一組；最後尚未消耗的任何子列都是孤立／額外資料。
    grouped = {name: defaultdict(list) for name in (RESERVED_CHILD, CHILD, LINK, COST)}
    for name, groups in grouped.items():
        for row in rows[name]:
            _check_deadline(deadline)
            groups[row["batch_id"]].append(row)

    reservations = {}
    for header in rows[HEADER]:
        _check_deadline(deadline)
        request = _decode_reservation(header["payload"])
        expected = QuoteNumberReservationService._header_row(
            request, header["payload"], header["prefix"], header["reserved_at"])
        _require(header == expected)
        _require(taiwan_prefix(datetime.fromisoformat(header["reserved_at"])) == header["prefix"])
        children = sorted(grouped[RESERVED_CHILD].pop(request.batch_id, []), key=lambda row: row["position"])
        _require(children == _child_rows(request, header["prefix"]))
        reservations[request.batch_id] = _result(header, children)

    for header in rows[BATCH]:
        _check_deadline(deadline)
        document = fixed_object(header["payload"], capacity=MAX_DOCUMENT_BYTES)
        payload = canonical(document["fixed_request"])
        request = decode_request(payload)
        reservation = reservations.get(request.batch_id)
        _require(reservation is not None)
        _require(_validate(build_development_reservation_request(request)) == reservation["reservation"]["payload"])
        expected = DevelopmentAtomicSnapshotService._document(request, payload, reservation, header["created_at"])
        expected_rows = DevelopmentAtomicSnapshotService._rows(request, expected)
        _require(header == expected_rows[0])
        for name, wanted, order in (
                (CHILD, expected_rows[1], ("position",)),
                (LINK, expected_rows[2], ("position",)),
                (COST, expected_rows[3], ("child_quote_id", "position"))):
            key = lambda row: tuple(row[column] for column in order)
            actual = grouped[name].pop(request.batch_id, [])
            _require(sorted(actual, key=key) == sorted(wanted, key=key))
        _check_deadline(deadline)
    _require(not any(grouped.values()))
    return len(rows[BATCH]), len(reservations) - len(rows[BATCH])


def inspect_development_integrity(path: Path, *, timeout: float = 5.0) -> IntegrityInspectionReport:
    """可信 caller 明確指定隔離開發檔；受檢六表只有一次 owned 唯讀交易。

    無掃目錄、外部引擎、修補、快取、配號或授權。全表預檢容量後才載入內容。
    期限於 SQL VM 與各階段檢查，不能硬中斷單次 JSON 處理或作業系統 I/O。
    """
    if not isinstance(path, Path):
        raise TypeError("須明確提供隔離 SQLite 的 Path，不接受字串、URL 或外部連線")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("timeout 須為大於零且至多 30 秒的有限數值")
    if path.is_symlink() or not path.is_file():
        raise ValueError("來源須為既存一般檔案，拒絕缺檔、目錄及末端符號連結")
    try:
        path = path.resolve(strict=True)
        deadline = time.monotonic() + timeout
        expected = _expected_schema()
        metadata, *_ = development_atomic_schema()
        _check_deadline(deadline)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                     timeout=max(0, deadline - time.monotonic()),
                                     isolation_level=None)) as connection:
            connection.set_authorizer(_authorize)
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            connection.execute("BEGIN")
            try:
                for name in TABLES:
                    _check_deadline(deadline)
                    if _table_signature(connection, name) != expected[name]:
                        raise IntegrityInspectionError("開發六表結構不符；請先隔離並檢查結構")
                budget = MAX_TOTAL_BYTES
                for name in TABLES:
                    budget = _preflight(connection, metadata.tables[name], deadline, budget)
                rows = {name: _read_table(connection, metadata.tables[name], deadline) for name in TABLES}
                saved, reserved_only = _verify_contents(rows, deadline)
                _check_deadline(deadline)
                return IntegrityInspectionReport(tuple((name, len(rows[name])) for name in TABLES),
                                                 saved, reserved_only, _digest(expected))
            finally:
                connection.set_progress_handler(None, 0)
                connection.rollback()
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        # 不將原始內容、身份、成本、SQL、路徑或底層錯誤帶到報告／日誌。
        raise IntegrityInspectionError("開發完整性檢查失敗；請隔離來源並由開發／DBA 調查") from None
