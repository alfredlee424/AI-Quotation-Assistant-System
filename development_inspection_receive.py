"""T45 受控接收：觀察存檔程序退出後，核驗並原子保存原始回條與紀錄。"""
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

import development_archive_validation as reader
import development_inspection as inspection
import development_inspection_archive as archive
import development_inspection_run as runner
from engine import development_archive_validation as validation


RECEPTION_VERSION = "development-inspection-reception-v1"
WARNING = ("僅隔離開發受控接收；保存本次實際回條及觀察到的存檔程序退出碼。"
           "紀錄不是簽章、來源認證、防重放或接收程序自身成功退出證明。"
           "報告與接收紀錄不是跨檔原子交易，不保證斷電耐久、最新性、停寫或同一復原點；"
           "不授權正式報價、確認、保存或恢復配號。")
ARGUMENT_ERROR = "參數或接收目的無效；須明確聲明開發用途，未啟動存檔程序。"
EXECUTION_ERROR = ("開發受控接收未確認完成；報告或接收紀錄可能已發布，"
                   "請人工核對，勿覆寫、補回條或自動重試。")
MAX_RECEPTION_BYTES = 64 * 1024

# 只有固定受控檢查子程序可啟動一次；本層不連資料庫。
_WORKER = r'''
from pathlib import Path
import os
import sys
root = Path(sys.argv[1])
allowed = None
spawned = False
def audit(event, args):
    global spawned
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(p.startswith(".env") for p in path.parts) or path.is_relative_to(root / "logs"):
            raise RuntimeError("private input denied")
    if event.startswith(("socket.", "sqlite3.")) or event in {"os.system", "os.fork", "os.posix_spawn"}:
        raise RuntimeError("external operation denied")
    if event == "subprocess.Popen":
        if spawned or allowed is None or args[0] != sys.executable or args[1] != allowed or args[3] != {}:
            raise RuntimeError("unexpected child")
        spawned = True
    if event == "import" and (args[0].split(".")[0] in {
        "database", "sqlalchemy", "sqlite3", "agent", "config", "dotenv", "openai", "streamlit"
    } or args[0] in {"engine.pricing", "engine.snapshot"}):
        raise RuntimeError("production dependency denied")
sys.addaudithook(audit)
sys.path.insert(0, str(root))
import development_inspection_archive as archive
operation, child_args, timeout, directory, name = archive._arguments(sys.argv[2:])
allowed = [sys.executable, "-I", "-B", "-c", archive.runner._WORKER, str(root), *child_args]
raise SystemExit(archive.main(sys.argv[2:]))
'''


def _arguments(argv):
    parser = inspection._Parser(prog="development_inspection_receive", allow_abbrev=False,
                                description=WARNING)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in inspection.COMMANDS:
        sub = commands.add_parser(command, allow_abbrev=False, description=WARNING)
        sub.add_argument("--development-only", action=inspection._Once, nargs=0, required=True)
        names = ("database",) if command in ("schema", "integrity") else ("reference", "candidate")
        for name in (*names, "output-directory", "report-name", "reception-name"):
            sub.add_argument("--" + name, action=inspection._Once, required=True)
        sub.add_argument("--timeout", action=inspection._Once, default="2" if command == "schema" else "10")
        sub.add_argument("--process-timeout", action=inspection._Once, default="45")
        sub.add_argument("--archive-timeout", action=inspection._Once, default="90",
                         help="存檔子程序收集與退出期限，大於 0 且至多 120 秒；僅 POSIX")
    raw = parser.parse_args(argv)
    try:
        timeout = float(raw.archive_timeout)
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError()
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.json", raw.reception_name)
                or raw.reception_name.casefold() == raw.report_name.casefold()):
            raise ValueError()
        directory = Path(raw.output_directory)
        if any(p.startswith(".env") for p in directory.parts):
            raise ValueError()
        if any(p.startswith(".env") for p in directory.resolve().parts):
            raise ValueError()
    except (ValueError, OSError, RuntimeError, OverflowError):
        raise inspection._ArgumentError() from None
    args = [raw.command, "--development-only", "--timeout", raw.timeout,
            "--process-timeout", raw.process_timeout,
            "--output-directory", str(directory.absolute()), "--report-name", raw.report_name]
    names = ("database",) if raw.command in ("schema", "integrity") else ("reference", "candidate")
    for name in names:
        args.extend(["--" + name, getattr(raw, name)])
    operation, child_args, _, _, _ = archive._arguments(args)
    # 原入口已完整核對來源；子程序工作目錄不同，必須傳遞正規化絕對路徑。
    for name in names:
        args[args.index("--" + name) + 1] = child_args[child_args.index("--" + name) + 1]
    return operation, args, timeout, str(directory.absolute()), raw.report_name, raw.reception_name


def _collect(command, timeout):
    """有界收集自有程序群組；完整 EOF 不等於成功退出。"""
    if os.name != "posix":
        raise RuntimeError("unsupported platform")
    deadline = time.monotonic() + timeout
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": validation.MAX_RECEIPT_BYTES, "stderr": runner.MAX_STDERR_BYTES}
    process = None
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=runner.ROOT, env={}, close_fds=True,
                                   shell=False, start_new_session=True)
        for name in buffers:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            for key, _ in selector.select(runner._remaining(deadline)):
                name = key.data
                chunk = os.read(key.fd, min(8192, limits[name] - len(buffers[name]) + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[name].extend(chunk)
                if len(buffers[name]) > limits[name]:
                    raise ValueError("output limit")
        code = process.wait(timeout=runner._remaining(deadline))
        runner._remaining(deadline)
        if code not in (0, 1) or buffers["stderr"]:
            raise ValueError("archive did not complete")
        return bytes(buffers["stdout"]), code
    finally:
        selector.close()
        if process is not None:
            try:
                # 即使 leader 已退出，也終止仍持有管線的自有檢查後代。
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
            finally:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


def _absent(directory_fd, name):
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise inspection._ArgumentError()


def _read_report(directory_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                 dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or not 0 < info.st_size <= validation.MAX_ARCHIVE_BYTES):
            raise ValueError("invalid report file")
        payload = reader._read_input(fd, info, validation.MAX_ARCHIVE_BYTES)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if reader._identity(named) != reader._identity(info):
            raise ValueError("report replaced")
        return payload
    finally:
        os.close(fd)


def _encode(payload, receipt, operation, code):
    if type(code) is not int or code not in (0, 1):
        raise ValueError("invalid observed exit")
    # 識別取自本次直接收集的管線，不接受外部 caller 文件或自填退出碼。
    received = validation._decode(receipt, validation.MAX_RECEIPT_BYTES)
    checked = validation.validate_development_archive(
        payload, receipt, expected_operation=operation, expected_run_id=received["run_id"])
    if checked.inspection_exit_code != code:
        raise ValueError("observed exit mismatch")
    record = {"reception_version": RECEPTION_VERSION, "run_id": received["run_id"],
              "operation": operation, "status": checked.status, "archive_process_exit_code": code,
              "inspection_exit_code": checked.inspection_exit_code,
              "receipt_utf8": receipt.decode("utf-8"), "receipt_byte_count": len(receipt),
              "receipt_sha256": hashlib.sha256(receipt).hexdigest(),
              "validation": asdict(checked), "warning": WARNING, **dict.fromkeys(archive.FLAGS, False)}
    encoded = (json.dumps(record, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_RECEPTION_BYTES:
        raise ValueError("reception limit")
    summary = {key: record[key] for key in ("reception_version", "run_id", "operation", "status",
                                          "archive_process_exit_code", "inspection_exit_code", "warning",
                                          *archive.FLAGS)}
    summary.update(publication="published", byte_count=len(encoded), sha256=hashlib.sha256(encoded).hexdigest())
    return encoded, json.dumps(summary, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n"


def main(argv=None):
    try:
        try:
            operation, args, timeout, directory, report_name, reception_name = _arguments(argv)
        except SystemExit as result:
            return result.code
        with archive._destination(directory, reception_name) as directory_fd:
            _absent(directory_fd, report_name)
            original = os.fstat(directory_fd)
            command = [sys.executable, "-I", "-B", "-c", _WORKER, str(runner.ROOT), *args]
            receipt, code = _collect(command, timeout)
            current = os.stat(directory, follow_symlinks=False)
            if ((current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)
                    or not stat.S_ISDIR(current.st_mode) or current.st_uid != os.geteuid()
                    or current.st_mode & 0o077):
                raise ValueError("destination changed")
            payload = _read_report(directory_fd, report_name)
            encoded, summary = _encode(payload, receipt, operation, code)
            archive._publish(directory_fd, reception_name, encoded)
        if sys.stdout.write(summary) != len(summary):
            raise OSError("incomplete output")
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
