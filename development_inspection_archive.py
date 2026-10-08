"""T45 開發檢查原子存檔；只發布本次受控結果，不是正式稽核或來源認證。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

import development_inspection as inspection
import development_inspection_run as runner
from engine.development_report_validation import validate_development_report


ARCHIVE_VERSION = "development-inspection-archive-v1"
WARNING = ("僅隔離開發報告原子發布；不覆寫既有檔案。"
           "摘要及執行識別不是簽章、來源認證或防重放；不保證斷電耐久、備份最新或同一復原點。"
           "不授權正式報價、確認、保存、修補或恢復配號。")
ARGUMENT_ERROR = "參數或存檔目的無效；須明確聲明開發用途，未啟動檢查。"
EXECUTION_ERROR = ("開發報告存檔未確認完成；目的檔可能已完整發布，請人工核對，"
                   "勿覆寫或自動重試。不提供部分成功回條。")
MAX_ARCHIVE_BYTES = 128 * 1024
FLAGS = ("can_quote", "can_confirm", "can_save", "can_resume_numbering")


def _arguments(argv):
    parser = inspection._Parser(prog="development_inspection_archive", allow_abbrev=False,
                                description=WARNING)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in inspection.COMMANDS:
        sub = commands.add_parser(command, allow_abbrev=False, description=WARNING)
        sub.add_argument("--development-only", action=inspection._Once, nargs=0, required=True)
        names = ("database",) if command in ("schema", "integrity") else ("reference", "candidate")
        for name in (*names, "output-directory", "report-name"):
            sub.add_argument("--" + name, action=inspection._Once, required=True)
        sub.add_argument("--timeout", action=inspection._Once,
                         default="2" if command == "schema" else "10")
        sub.add_argument("--process-timeout", action=inspection._Once, default="45")
    raw = parser.parse_args(argv)
    args = [raw.command, "--development-only", "--timeout", raw.timeout,
            "--process-timeout", raw.process_timeout]
    names = ("database",) if raw.command in ("schema", "integrity") else ("reference", "candidate")
    for name in names:
        args.extend(["--" + name, getattr(raw, name)])
    operation, child_args, timeout = runner._arguments(args)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.json", raw.report_name):
        raise inspection._ArgumentError()
    return operation, child_args, timeout, raw.output_directory, raw.report_name


@contextmanager
def _destination(raw, name):
    """釘住既存私有目錄；祖先目錄及同 UID 程序仍必須可信。"""
    if os.name != "posix":
        raise RuntimeError("unsupported platform")
    fd = None
    try:
        try:
            if not raw or "\x00" in raw or "://" in raw or raw.lower().startswith("file:"):
                raise ValueError()
            path = Path(raw)
            if path.is_symlink():
                raise ValueError()
            resolved = path.resolve(strict=True)
            if resolved.is_relative_to(runner.ROOT) or resolved.name.startswith(".env"):
                raise ValueError()
            fd = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError()
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError()
        except (ValueError, OSError, RuntimeError):
            raise inspection._ArgumentError() from None
        # 不支援目錄同步的檔案系統，在檢查前拒絕，沒有降級覆寫策略。
        os.fsync(fd)
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def _encode(document, operation, code):
    """重驗完整外層與內嵌契約；不是接受外部文件的命令入口。"""
    if type(code) is not int or code not in (0, 1) or type(document) is not dict:
        raise ValueError("invalid result")
    keys = {"run_version", "run_id", "operation", "status", "inspection_exit_code",
            "validation_version", "warning", "inspection", *FLAGS}
    if set(document) != keys or any(document[key] is not False for key in FLAGS):
        raise ValueError("invalid envelope")
    run_id = uuid.UUID(document["run_id"])
    if str(run_id) != document["run_id"] or run_id.version != 4:
        raise ValueError("invalid identifier")
    if document["run_version"] != runner.RUN_VERSION or document["warning"] != runner.WARNING:
        raise ValueError("invalid version")
    payload = json.dumps(document["inspection"], ensure_ascii=True, allow_nan=False).encode("utf-8")
    checked = validate_development_report(payload, expected_operation=operation, expected_exit_code=code)
    if (document["operation"] != checked.operation or document["status"] != checked.status
            or type(document["inspection_exit_code"]) is not int
            or document["inspection_exit_code"] != code
            or document["validation_version"] != checked.validation_version):
        raise ValueError("inconsistent envelope")
    encoded = (json.dumps(document, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_ARCHIVE_BYTES:
        raise ValueError("archive limit")
    return encoded


def _publish(directory_fd, name, payload):
    """同目錄暫存、同步、硬連結無覆寫發布；發布後不刪除目的檔。"""
    temporary = ".inspection-" + uuid.uuid4().hex + ".tmp"
    fd = None
    created = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory_fd)
        created = True
        os.fchmod(fd, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("incomplete write")
            remaining = remaining[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        # link 是原子的 no-clobber；既存一般檔、目錄、符號／硬連結均不覆寫。
        os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                follow_symlinks=False)
        os.unlink(temporary, dir_fd=directory_fd)
        created = False
        os.fsync(directory_fd)
    finally:
        try:
            if fd is not None:
                os.close(fd)
        finally:
            if created:
                os.unlink(temporary, dir_fd=directory_fd)


def main(argv=None):
    try:
        try:
            operation, child_args, timeout, directory, name = _arguments(argv)
        except SystemExit as result:
            return result.code
        with _destination(directory, name) as directory_fd:
            document, code = runner._run(operation, child_args, timeout)
            payload = _encode(document, operation, code)
            receipt = {"archive_version": ARCHIVE_VERSION, "run_id": document["run_id"],
                       "operation": operation, "status": document["status"],
                       "inspection_exit_code": code, "publication": "published",
                       "byte_count": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                       "warning": WARNING, **dict.fromkeys(FLAGS, False)}
            encoded = json.dumps(receipt, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n"
            _publish(directory_fd, name, payload)
        sys.stdout.write(encoded)
        sys.stdout.flush()
        return code
    except inspection._ArgumentError:
        return inspection._error(ARGUMENT_ERROR, 2)
    except KeyboardInterrupt:
        return inspection._error(EXECUTION_ERROR, 130)
    except Exception:
        return inspection._error(EXECUTION_ERROR, 3)


if __name__ == "__main__":
    raise SystemExit(main())
