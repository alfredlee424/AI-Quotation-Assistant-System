"""離線接收 stdin 報告；不開啟來源檔、不啟動檢查或正式操作。"""
import json
import sys

from development_inspection import COMMANDS, _ArgumentError, _Once, _Parser, _error
from engine.development_report_validation import WARNING, MAX_REPORT_BYTES, validate_development_report


ARGUMENT_ERROR = "參數無效或未聲明開發用途；未讀取報告，請使用 --help。"
VALIDATION_ERROR = "離線報告驗證未完成；輸入無效、不完整、與預期不符或 I/O 失敗。"


def main(argv=None):
    try:
        parser = _Parser(prog="development_report_validation", allow_abbrev=False, description=WARNING)
        parser.add_argument("--development-only", action=_Once, nargs=0, required=True)
        parser.add_argument("--operation", action=_Once, required=True, choices=COMMANDS)
        parser.add_argument("--inspection-exit-code", action=_Once, required=True, choices=("0", "1"),
                            help="呼叫端獨立取得的原檢查程序退出碼；不得以本驗證器退出碼代替")
        try:
            args = parser.parse_args(argv)
        except _ArgumentError:
            return _error(ARGUMENT_ERROR, 2)
        except SystemExit as result:
            return result.code
        # 有界讀取；仍須等待 EOF，無管線截止時間或 OS I/O 硬中斷保證。
        payload = sys.stdin.buffer.read(MAX_REPORT_BYTES + 1)
        result = validate_development_report(payload, expected_operation=args.operation,
                                             expected_exit_code=int(args.inspection_exit_code))
        document = {"validation_version": result.validation_version, "operation": result.operation,
                    "inspection_exit_code": result.inspection_exit_code, "status": result.status,
                    "warning": result.warning, "can_quote": False, "can_confirm": False,
                    "can_save": False, "can_resume_numbering": False}
        sys.stdout.write(json.dumps(document, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n")
        sys.stdout.flush()
        return result.inspection_exit_code
    except KeyboardInterrupt:
        return _error("離線報告驗證已中止；未提供完整結果。", 130)
    except Exception:
        return _error(VALIDATION_ERROR, 3)


if __name__ == "__main__":
    raise SystemExit(main())
