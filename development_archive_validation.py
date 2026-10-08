"""明確讀取工作區外的私有報告／回條；不啟動檢查、不寫檔。"""
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import sys

from development_inspection import COMMANDS, _ArgumentError, _Once, _Parser, _error
from engine.development_archive_validation import (
    MAX_ARCHIVE_BYTES, MAX_RECEIPT_BYTES, WARNING,
    validate_development_archive, validate_expected,
)


ROOT = Path(__file__).resolve().parent
ARGUMENT_ERROR = "參數無效或未聲明開發用途；未讀取文件，請使用 --help。"
VALIDATION_ERROR = "存檔離線核驗未完成；文件無效、與預期不符、平台不支援或 I/O 失敗。"


def _arguments(argv):
    parser = _Parser(prog="development_archive_validation", allow_abbrev=False, description=WARNING)
    parser.add_argument("--development-only", action=_Once, nargs=0, required=True)
    parser.add_argument("--operation", action=_Once, required=True, choices=COMMANDS)
    for name in ("run-id", "archive", "receipt"):
        parser.add_argument("--" + name, action=_Once, required=True)
    args = parser.parse_args(argv)
    try:
        validate_expected(args.operation, args.run_id)
        # 全部參數先檢查，避免另一個參數無效卻已讀取第一份內容。
        for value in (args.archive, args.receipt):
            if not value or "\x00" in value or ":" in value:
                raise ValueError()
            path = Path(value)
            if path.suffix != ".json" or any(p.startswith(".env") for p in path.parts):
                raise ValueError()
    except Exception:
        raise _ArgumentError() from None
    return args


@contextmanager
def _open_input(raw, maximum):
    """不跟隨末端連結；先以 nonblocking 開啟再拒絕非一般檔案。"""
    path = Path(raw)
    if path.is_symlink():
        raise ValueError()
    resolved = path.resolve(strict=True)
    if resolved.is_relative_to(ROOT) or any(p.startswith(".env") for p in resolved.parts):
        raise ValueError()
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or not 0 < info.st_size <= maximum):
            raise ValueError()
        yield fd, info
    finally:
        os.close(fd)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, info.st_uid, info.st_nlink)


def _read_input(fd, info, maximum):
    result = bytearray()
    while len(result) <= maximum:
        chunk = os.read(fd, min(16384, maximum + 1 - len(result)))
        if not chunk:
            break
        result.extend(chunk)
    if len(result) != info.st_size or _identity(os.fstat(fd)) != _identity(info):
        raise ValueError()
    return bytes(result)


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
            receipt_fd, receipt_info = stack.enter_context(_open_input(args.receipt, MAX_RECEIPT_BYTES))
            if (archive_info.st_dev, archive_info.st_ino) == (receipt_info.st_dev, receipt_info.st_ino):
                raise ValueError()
            archive = _read_input(archive_fd, archive_info, MAX_ARCHIVE_BYTES)
            receipt = _read_input(receipt_fd, receipt_info, MAX_RECEIPT_BYTES)
            # 兩端讀完再查一次；不是跨檔鎖或抵抗同 UID 惡意寫者的保證。
            if (_identity(os.fstat(archive_fd)) != _identity(archive_info)
                    or _identity(os.fstat(receipt_fd)) != _identity(receipt_info)):
                raise ValueError()
            result = validate_development_archive(archive, receipt, expected_operation=args.operation,
                                                  expected_run_id=args.run_id)
        encoded = json.dumps(asdict(result), ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n"
        if sys.stdout.write(encoded) != len(encoded):
            raise OSError()
        sys.stdout.flush()
        return result.inspection_exit_code
    except KeyboardInterrupt:
        return _error("存檔離線核驗已中止；未提供完整結果。", 130)
    except Exception:
        return _error(VALIDATION_ERROR, 3)


if __name__ == "__main__":
    raise SystemExit(main())
