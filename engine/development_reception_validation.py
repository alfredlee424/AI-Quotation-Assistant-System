"""T45 接收紀錄純離線核驗；不認證記載的歷史程序事實。"""
from dataclasses import asdict, dataclass
import hashlib

from engine import development_archive_validation as archive


VALIDATION_VERSION = "development-reception-validation-v1"
WARNING = ("僅離線報告與接收紀錄內容核驗，未啟動程序或觀察歷史退出。"
           "記載退出碼的一致性不是存檔或接收程序成功退出、實際發布、來源或最新性證明。"
           "共同偽造及重放仍可能通過；不證明停寫、同一復原點或斷電耐久，"
           "不授權正式報價、確認、保存或恢復配號。")
ERROR = "報告或接收紀錄無效、不完整或與預期不符；不提供部分核驗結果。"
# 固定 wire 契約，不匯入可執行或寫檔的接收服務。
RECEPTION_VERSION = "development-inspection-reception-v1"
RECEPTION_WARNING = ("僅隔離開發受控接收；保存本次實際回條及觀察到的存檔程序退出碼。"
                     "紀錄不是簽章、來源認證、防重放或接收程序自身成功退出證明。"
                     "報告與接收紀錄不是跨檔原子交易，不保證斷電耐久、最新性、停寫或同一復原點；"
                     "不授權正式報價、確認、保存或恢復配號。")
MAX_RECEPTION_BYTES = 64 * 1024


class ReceptionValidationError(ValueError):
    """固定錯誤，不攜帶輸入原文。"""


@dataclass(frozen=True)
class ValidatedDevelopmentReception:
    operation: str
    inspection_exit_code: int
    recorded_archive_process_exit_code: int
    status: str
    validation_version: str = VALIDATION_VERSION
    warning: str = WARNING
    can_quote: bool = False
    can_confirm: bool = False
    can_save: bool = False
    can_resume_numbering: bool = False


def validate_development_reception(archive_payload, reception_payload, *, expected_operation, expected_run_id):
    """重驗完整接收紀錄、原回條位元組及原報告；不輸出原文或識別。"""
    try:
        archive.validate_expected(expected_operation, expected_run_id)
        record = archive._decode(reception_payload, MAX_RECEPTION_BYTES)
        archive._object(record, ("reception_version", "run_id", "operation", "status",
                                "archive_process_exit_code", "inspection_exit_code", "receipt_utf8",
                                "receipt_byte_count", "receipt_sha256", "validation", "warning"))
        archive._require(record["reception_version"] == RECEPTION_VERSION)
        archive._require(record["warning"] == RECEPTION_WARNING)
        archive._require(record["run_id"] == expected_run_id and record["operation"] == expected_operation)
        for field in ("archive_process_exit_code", "inspection_exit_code"):
            archive._require(type(record[field]) is int and record[field] in (0, 1))
        archive._require(type(record["receipt_utf8"]) is str)
        receipt = record["receipt_utf8"].encode("utf-8")
        archive._require(0 < len(receipt) <= archive.MAX_RECEIPT_BYTES)
        archive._require(type(record["receipt_byte_count"]) is int and record["receipt_byte_count"] == len(receipt))
        archive._require(record["receipt_sha256"] == hashlib.sha256(receipt).hexdigest())
        checked = archive.validate_development_archive(
            archive_payload, receipt, expected_operation=expected_operation, expected_run_id=expected_run_id)
        # 不信任持久摘要；重驗後逐欄比對，另拒絕 bool 與 int 相等的型別漏洞。
        summary = record["validation"]
        archive._object(summary, ("operation", "inspection_exit_code", "status", "validation_version", "warning"))
        archive._require(type(summary["inspection_exit_code"]) is int)
        archive._require(summary == asdict(checked))
        archive._require(record["archive_process_exit_code"] == record["inspection_exit_code"]
                         == checked.inspection_exit_code)
        archive._require(record["status"] == checked.status)
        return ValidatedDevelopmentReception(checked.operation, checked.inspection_exit_code,
                                             record["archive_process_exit_code"], checked.status)
    except Exception:
        raise ReceptionValidationError(ERROR) from None
