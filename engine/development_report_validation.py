"""T45 離線報告接收契約；只驗證收到的 bytes，不驗證資料庫或授權。"""
from dataclasses import dataclass
import json
import re

from development_inspection import COMMANDS, OUTPUT_VERSION, WARNING as CLI_WARNING


VALIDATION_VERSION = "development-report-validation-v1"
WARNING = ("僅離線報告格式與內部一致性驗證，未重新檢查資料庫。"
           "輸入及退出碼由呼叫端提供，可能過期或偽造；摘要不是簽章。"
           "不認證來源、最新性、停寫或同一復原點，不授權正式報價、確認、保存或恢復配號。")
ERROR = "開發報告無效、不完整或與預期不符；不提供部分驗證結果。"
MAX_REPORT_BYTES = 64 * 1024
FLAGS = ("can_quote", "can_confirm", "can_save", "can_resume_numbering")
HEADER = "quote_number_reservation"
RESERVED_CHILD = "quote_number_reserved_child"
BATCH = "development_atomic_batch"
CHILD = "development_atomic_child"
LINK = "development_atomic_source_link"
COST = "development_atomic_cost"
TABLES = (HEADER, RESERVED_CHILD, BATCH, CHILD, LINK, COST)
MAX_ROWS = {name: 10000 if name in (HEADER, BATCH) else 100000 for name in TABLES}
# 固定 v1 wire 契約，不匯入服務、SQLAlchemy 或動態建表取得允許值。
# 服務升版必須明確重審此契約；整合測試核對版本、警語、順序與唯一鍵。
VERSIONS = {
    "schema": "development-schema-inspection-v1",
    "integrity": "development-integrity-inspection-v1",
    "registry": "development-registry-comparison-v1",
    "snapshots": "development-snapshot-comparison-v1",
}
WARNINGS = {
    "schema": ("僅開發結構比對，不是正式部署、報價或恢復配號授權；"
               "未驗證資料完整性、持久文件版本、備份最新性或權威 registry。"),
    "integrity": ("僅當次單一讀交易的六張開發表內部一致性；不是業務、工程或身份核准。"
                  "保留未保存不等於遺失或可回收；全部刪除、共同竄改或健康舊備份仍可能通過。"
                  "未認證權威來源、最新性或同一復原點，不授權正式報價、保存或恢復配號。"),
    "registry": ("僅兩份開發 registry 的內容對帳；參考端未經權威認證。"
                 "兩庫各自讀取快照，不是跨庫同一復原點；相等不代表最新、有效或未遭共同竄改。"
                 "未驗證完整快照／業務內容，不授權正式報價、保存或恢復配號。"),
    "snapshots": ("僅兩份隔離開發六表在各自讀交易內通過完整性核驗後的內容對帳；"
                  "參考端未經權威認證，兩端不是跨庫同時快照或同一復原點。"
                  "共同過期、整組刪除或一致重寫仍可能相等；不是來源真實性、最新性或正式核准。"
                  "保留未保存不代表可回收；不授權正式報價、確認、保存或恢復配號。"),
}
REGISTRY_KEYS = {HEADER: ("request_id", "preview_identity", "prefix"),
                 RESERVED_CHILD: ("child_quote_id", "reserved_ref_no")}
UNIQUE_KEYS = {
    HEADER: (("batch_id", "workgroup", "prefix"), ("request_id",), ("preview_identity",), ("prefix",)),
    RESERVED_CHILD: (("child_quote_id",), ("reserved_ref_no",), ("batch_id", "source_item_id"),
                     ("batch_id", "configuration_line_id"), ("batch_id", "position"), ("batch_id", "suffix")),
    BATCH: (("batch_id", "workgroup", "prefix"), ("request_id",), ("preview_identity",), ("save_id",)),
    CHILD: (("ref_no",), ("batch_id", "position")),
    LINK: (("batch_id", "source_item_id"), ("batch_id", "configuration_line_id"), ("batch_id", "position")),
    COST: (("batch_id", "child_quote_id", "path"), ("batch_id", "child_quote_id", "position")),
}
ISSUE_CODES = ("missing", "not_table", "definition_mismatch", "columns_mismatch",
               "foreign_keys_mismatch", "indexes_mismatch", "extra_objects_mismatch")


class ReportValidationError(ValueError):
    """固定訊息，不攜帶來源字串、解碼錯誤或部分報告。"""


@dataclass(frozen=True)
class ValidatedDevelopmentReport:
    operation: str
    inspection_exit_code: int
    status: str
    validation_version: str = VALIDATION_VERSION
    warning: str = WARNING
    can_quote: bool = False
    can_confirm: bool = False
    can_save: bool = False
    can_resume_numbering: bool = False


def _require(condition):
    if not condition:
        raise ReportValidationError(ERROR)


def _object(value, keys):
    _require(type(value) is dict and set(value) == set(keys))


def _count(value, maximum):
    _require(type(value) is int and 0 <= value <= maximum)


def _digest(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def _metadata(report, operation, keys):
    _object(report, (*FLAGS, "report_version", "warning", *keys))
    _require(report["report_version"] == VERSIONS[operation] and report["warning"] == WARNINGS[operation])
    _require(all(report[key] is False for key in FLAGS))


def _schema(report):
    _metadata(report, "schema", ("schema_matches", "expected_digest", "observed_digest", "checked_tables", "issues"))
    _digest(report["expected_digest"])
    _digest(report["observed_digest"])
    _require(report["checked_tables"] == sorted(TABLES))
    issues = report["issues"]
    _require(type(issues) is list and len(issues) <= len(TABLES) * len(ISSUE_CODES))
    seen = []
    for issue in issues:
        _object(issue, ("table", "code"))
        _require(issue["table"] in TABLES and issue["code"] in ISSUE_CODES)
        item = (issue["table"], ISSUE_CODES.index(issue["code"]))
        _require(item not in seen)
        seen.append(item)
    _require(seen == sorted(seen))
    for name in TABLES:
        codes = [code for table, code in seen if table == name]
        _require(not codes or codes[0] >= 2 or len(codes) == 1)
    matched = not issues
    _require(report["schema_matches"] is matched)
    _require((report["expected_digest"] == report["observed_digest"]) == matched)
    return matched


def _integrity(report):
    _metadata(report, "integrity", ("table_counts", "verified_saved_batches", "reservation_only_batches", "schema_digest"))
    _digest(report["schema_digest"])
    counts = report["table_counts"]
    _object(counts, TABLES)
    for name in TABLES:
        _count(counts[name], MAX_ROWS[name])
    saved, reserved = report["verified_saved_batches"], report["reservation_only_batches"]
    _count(saved, MAX_ROWS[BATCH])
    _count(reserved, MAX_ROWS[HEADER])
    _require(saved == counts[BATCH] and saved + reserved == counts[HEADER])
    _require(counts[CHILD] == counts[LINK])
    _require(saved <= counts[CHILD] <= 999 * saved)
    _require(counts[CHILD] <= counts[COST] <= 99999 * counts[CHILD])
    unsaved_children = counts[RESERVED_CHILD] - counts[CHILD]
    _require(reserved <= unsaved_children <= 999 * reserved)
    return True


def _comparison(report, operation):
    keys = ("contents_match", "tables", "identity_conflicts")
    _metadata(report, operation, (*keys, "schema_digest") if operation == "registry" else
              (*keys, "reference_integrity", "candidate_integrity"))
    names = TABLES[:2] if operation == "registry" else TABLES
    if operation == "registry":
        _digest(report["schema_digest"])
    else:
        for side in ("reference_integrity", "candidate_integrity"):
            _integrity(report[side])
        _require(report["reference_integrity"]["schema_digest"] == report["candidate_integrity"]["schema_digest"])
    tables = report["tables"]
    _require(type(tables) is list and len(tables) == len(names))
    differences = {}
    for name, row in zip(names, tables):
        _object(row, ("table", "reference_rows", "candidate_rows", "missing_rows", "extra_rows", "changed_rows",
                      "reference_digest", "candidate_digest"))
        _require(row["table"] == name)
        for key in ("reference_rows", "candidate_rows", "missing_rows", "extra_rows", "changed_rows"):
            _count(row[key], MAX_ROWS[name])
        a, b, missing, extra, changed = (row[key] for key in (
            "reference_rows", "candidate_rows", "missing_rows", "extra_rows", "changed_rows"))
        _require(missing <= a and extra <= b and a - missing == b - extra and changed <= a - missing)
        for key in ("reference_digest", "candidate_digest"):
            _digest(row[key])
        different = bool(missing or extra or changed)
        _require((row["reference_digest"] != row["candidate_digest"]) == different)
        differences[name] = (different, min(a, b), missing + changed, extra + changed)
        if operation == "snapshots":
            _require(a == report["reference_integrity"]["table_counts"][name])
            _require(b == report["candidate_integrity"]["table_counts"][name])
    conflicts = report["identity_conflicts"]
    allowed = [(name, (key,)) for name in names for key in REGISTRY_KEYS[name]] if operation == "registry" else [
        (name, columns) for name in names for columns in sorted(UNIQUE_KEYS[name])]
    _require(type(conflicts) is list and len(conflicts) <= len(allowed))
    seen = []
    for conflict in conflicts:
        key = "column" if operation == "registry" else "columns"
        _object(conflict, ("table", key, "count"))
        columns = conflict[key]
        if operation == "registry":
            _require(type(columns) is str)
            columns = (columns,)
        else:
            _require(type(columns) is list and all(type(column) is str for column in columns))
            columns = tuple(columns)
        identity = (conflict["table"], columns)
        _require(identity in allowed and identity not in seen)
        seen.append(identity)
        different, shared_max, left_changed, right_changed = differences[conflict["table"]]
        _count(conflict["count"], min(shared_max, left_changed, right_changed))
        _require(different and conflict["count"] > 0)
    _require(seen == sorted(seen, key=allowed.index))
    matched = not conflicts and not any(value[0] for value in differences.values())
    _require(report["contents_match"] is matched)
    return matched


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _reject_number(value):
    raise ReportValidationError(ERROR)


def validate_development_report(payload: bytes, *, expected_operation: str,
                                expected_exit_code: int) -> ValidatedDevelopmentReport:
    """只接受完整 UTF-8 JSON bytes 與外部明確預期；不載入服務、不返回原報告。

    caller 必須自行取得真實 producer 退出碼；任意傳入 0 不是來源認證。
    固定 v1 嚴格拒絕未知欄位／版本，不自行修補、升版或放寬。
    """
    try:
        _require(type(expected_operation) is str and expected_operation in COMMANDS)
        _require(type(expected_exit_code) is int and expected_exit_code in (0, 1))
        _require(type(payload) is bytes and 0 < len(payload) <= MAX_REPORT_BYTES)
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_float=_reject_number, parse_constant=_reject_number)
        _object(document, (*FLAGS, "output_version", "operation", "status", "exit_code", "warning", "report"))
        _require(document["output_version"] == OUTPUT_VERSION and document["operation"] == expected_operation)
        _require(document["warning"] == CLI_WARNING and all(document[key] is False for key in FLAGS))
        _require(type(document["exit_code"]) is int and document["exit_code"] == expected_exit_code)
        status = "completed" if expected_exit_code == 0 else "differences"
        _require(document["status"] == status)
        report = document["report"]
        matched = (_schema(report) if expected_operation == "schema" else
                   _integrity(report) if expected_operation == "integrity" else
                   _comparison(report, expected_operation))
        _require(matched == (expected_exit_code == 0))
        return ValidatedDevelopmentReport(expected_operation, expected_exit_code, status)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        # 不讓 JSONDecodeError.doc、原始例外或 traceback 訊息成為輸出欄位。
        pass
    raise ReportValidationError(ERROR) from None
