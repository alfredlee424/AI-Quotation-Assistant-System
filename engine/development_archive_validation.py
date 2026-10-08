"""T45 存檔與回條的純離線核驗；不證明發布、來源或最新性。"""
from dataclasses import dataclass
import hashlib
import json
import uuid

from engine.development_report_validation import COMMANDS, FLAGS, validate_development_report


VALIDATION_VERSION = "development-archive-validation-v1"
WARNING = ("僅離線存檔與回條內容核驗，未重新執行檢查或驗證實際發布。"
           "回條、執行識別及摘要可能共同偽造或重放，不是簽章、來源認證或最新性證明。"
           "不證明停寫、同一復原點或斷電耐久，不授權正式報價、確認、保存或恢復配號。")
ERROR = "存檔或回條無效、不完整或與預期不符；不提供部分核驗結果。"
MAX_ARCHIVE_BYTES = 128 * 1024
MAX_RECEIPT_BYTES = 8 * 1024
# 固定 v1 wire 契約：刻意不匯入可執行／寫檔的 producer；升版須另行審查。
RUN_VERSION = "development-inspection-run-v1"
RUN_WARNING = ("僅隔離開發受控執行；結果來自本次啟動的檢查子程序並通過報告格式驗證。"
               "執行識別不是簽章、身份或來源認證，路徑對應依賴可信本機與檔案未遭替換。"
               "不證明來源權威、備份最新、停寫或跨庫同一復原點；"
               "不授權正式報價、確認、保存、修補或恢復配號。")
ARCHIVE_VERSION = "development-inspection-archive-v1"
ARCHIVE_WARNING = ("僅隔離開發報告原子發布；不覆寫既有檔案。"
                   "摘要及執行識別不是簽章、來源認證或防重放；不保證斷電耐久、備份最新或同一復原點。"
                   "不授權正式報價、確認、保存、修補或恢復配號。")


class ArchiveValidationError(ValueError):
    """不攜帶輸入原文的固定錯誤。"""


@dataclass(frozen=True)
class ValidatedDevelopmentArchive:
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
        raise ArchiveValidationError(ERROR)


def validate_expected(operation, run_id):
    """參數核對不讀輸入；識別只是 caller 預期，不是認證。"""
    try:
        _require(type(operation) is str and operation in COMMANDS)
        _require(type(run_id) is str and len(run_id) == 36)
        parsed = uuid.UUID(run_id)
        _require(parsed.version == 4 and str(parsed) == run_id)
    except Exception:
        raise ArchiveValidationError(ERROR) from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _invalid_number(value):
    raise ArchiveValidationError(ERROR)


def _decode(payload, maximum):
    _require(type(payload) is bytes and 0 < len(payload) <= maximum)
    return json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs,
                      parse_float=_invalid_number, parse_constant=_invalid_number)


def _object(value, fields):
    _require(type(value) is dict and set(value) == {*fields, *FLAGS})
    _require(all(value[key] is False for key in FLAGS))


def validate_development_archive(archive_payload, receipt_payload, *, expected_operation, expected_run_id):
    """完整 bytes 雙文件核驗；摘要涵蓋原位元組（含空白及尾端換行）。"""
    try:
        validate_expected(expected_operation, expected_run_id)
        document = _decode(archive_payload, MAX_ARCHIVE_BYTES)
        receipt = _decode(receipt_payload, MAX_RECEIPT_BYTES)
        _object(document, ("run_version", "run_id", "operation", "status", "inspection_exit_code",
                           "validation_version", "warning", "inspection"))
        _object(receipt, ("archive_version", "run_id", "operation", "status", "inspection_exit_code",
                          "publication", "byte_count", "sha256", "warning"))
        _require(document["run_version"] == RUN_VERSION and document["warning"] == RUN_WARNING)
        _require(receipt["archive_version"] == ARCHIVE_VERSION and receipt["warning"] == ARCHIVE_WARNING)
        _require(receipt["publication"] == "published")
        _require(type(receipt["byte_count"]) is int and receipt["byte_count"] == len(archive_payload))
        _require(receipt["sha256"] == hashlib.sha256(archive_payload).hexdigest())
        for value in (document, receipt):
            _require(value["run_id"] == expected_run_id and value["operation"] == expected_operation)
            _require(type(value["inspection_exit_code"]) is int and value["inspection_exit_code"] in (0, 1))
        code = receipt["inspection_exit_code"]
        _require(document["inspection_exit_code"] == code)
        payload = json.dumps(document["inspection"], ensure_ascii=True, allow_nan=False).encode("utf-8")
        checked = validate_development_report(payload, expected_operation=expected_operation, expected_exit_code=code)
        _require(document["validation_version"] == checked.validation_version)
        _require(document["status"] == receipt["status"] == checked.status)
        return ValidatedDevelopmentArchive(checked.operation, code, checked.status)
    except Exception:
        raise ArchiveValidationError(ERROR) from None
