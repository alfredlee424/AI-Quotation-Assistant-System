"""明確讀取私有報告與接收紀錄；純離線、不啟動程序或寫檔。"""
from contextlib import ExitStack
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from development_archive_validation import _identity, _open_input, _read_input
from development_inspection import COMMANDS, _ArgumentError, _Once, _Parser, _error
from engine.development_archive_validation import MAX_ARCHIVE_BYTES, validate_expected
from engine.development_reception_validation import MAX_RECEPTION_BYTES, WARNING, validate_development_reception


ARGUMENT_ERROR = "參數無效或未聲明開發用途；未讀取文件，請使用 --help。"
VALIDATION_ERROR = "接收紀錄離線核驗未完成；文件無效、與預期不符、平台不支援或 I/O 失敗。"


def _arguments(argv):
    parser = _Parser(prog="development_reception_validation", allow_abbrev=False, description=WARNING)
    parser.add_argument("--development-only", action=_Once, nargs=0, required=True)
    parser.add_argument("--operation", action=_Once, required=True, choices=COMMANDS)
    for name in ("run-id", "archive", "reception"):
        parser.add_argument("--" + name, action=_Once, required=True)
    args = parser.parse_args(argv)
    try:
        validate_expected(args.operation, args.run_id)
        for value in (args.archive, args.reception):
            if not value or "\x00" in value or ":" in value:
                raise ValueError()
            path = Path(value)
            if path.suffix != ".json" or any(p.startswith(".env") for p in path.parts):
                raise ValueError()
    except Exception:
        raise _ArgumentError() from None
    return args


def main(argv=None):
    try:
        try:
            args = _arguments(argv)
        except _ArgumentError:
            return _error(ARGUMENT_ERROR, 2)
        except SystemExit as result:
            return result.code
        if os.name != "posix":
            raise RuntimeError()
        with ExitStack() as stack:
            archive_fd, archive_info = stack.enter_context(_open_input(args.archive, MAX_ARCHIVE_BYTES))
            record_fd, record_info = stack.enter_context(_open_input(args.reception, MAX_RECEPTION_BYTES))
            if (archive_info.st_dev, archive_info.st_ino) == (record_info.st_dev, record_info.st_ino):
                raise ValueError()
            payload = _read_input(archive_fd, archive_info, MAX_ARCHIVE_BYTES)
            record = _read_input(record_fd, record_info, MAX_RECEPTION_BYTES)
            if (_identity(os.fstat(archive_fd)) != _identity(archive_info)
                    or _identity(os.fstat(record_fd)) != _identity(record_info)):
                raise ValueError()
            result = validate_development_reception(payload, record, expected_operation=args.operation,
                                                     expected_run_id=args.run_id)
        encoded = json.dumps(asdict(result), ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n"
        if sys.stdout.write(encoded) != len(encoded):
            raise OSError()
        sys.stdout.flush()
        return result.inspection_exit_code
    except KeyboardInterrupt:
        return _error("接收紀錄離線核驗已中止；未提供完整結果。", 130)
    except Exception:
        return _error(VALIDATION_ERROR, 3)


if __name__ == "__main__":
    raise SystemExit(main())
