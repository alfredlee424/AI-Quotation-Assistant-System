"""T45 受控開發檢查：本次子程序輸出／退出碼直接驗證，不是正式授權。"""
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
import uuid

import development_inspection as inspection
from engine.development_report_validation import MAX_REPORT_BYTES, validate_development_report


RUN_VERSION = "development-inspection-run-v1"
WARNING = ("僅隔離開發受控執行；結果來自本次啟動的檢查子程序並通過報告格式驗證。"
           "執行識別不是簽章、身份或來源認證，路徑對應依賴可信本機與檔案未遭替換。"
           "不證明來源權威、備份最新、停寫或跨庫同一復原點；"
           "不授權正式報價、確認、保存、修補或恢復配號。")
ARGUMENT_ERROR = "參數無效或未聲明隔離開發用途；請使用 --help，未啟動子程序。"
EXECUTION_ERROR = "受控開發檢查未完成或報告無效；不提供部分結果，請由開發／DBA 調查。"
MAX_STDERR_BYTES = 4096
ROOT = Path(__file__).resolve().parent

# -I 忽略 PYTHON* 與使用者 site；僅插入固定專案根目錄，不接受 caller 腳本。
# 防護在專案／第三方匯入前安裝；不能取代可信解譯器、套件及檔案系統權限。
_WORKER = r'''
from pathlib import Path
import sys
root = Path(sys.argv[1])
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).resolve()
        if path.name.startswith(".env") or path.is_relative_to(root / "logs"):
            raise RuntimeError("private source denied")
    if event.startswith("socket.") or event in ("subprocess.Popen", "os.system", "os.fork", "os.posix_spawn"):
        raise RuntimeError("external operation denied")
    if event == "import" and args[0] in {
        "config", "database.connection", "database.models", "database.repository",
        "engine.pricing", "engine.snapshot", "streamlit", "openai", "dotenv",
    }:
        raise RuntimeError("production dependency denied")
sys.addaudithook(audit)
sys.path.insert(0, str(root))
import development_inspection
raise SystemExit(development_inspection.main(sys.argv[2:]))
'''


def _arguments(argv):
    parser = inspection._Parser(prog="development_inspection_run", allow_abbrev=False, description=WARNING)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in inspection.COMMANDS:
        sub = commands.add_parser(command, allow_abbrev=False, description=WARNING)
        sub.add_argument("--development-only", action=inspection._Once, nargs=0, required=True)
        names = ("database",) if command in ("schema", "integrity") else ("reference", "candidate")
        for name in names:
            sub.add_argument("--" + name, action=inspection._Once, required=True)
        sub.add_argument("--timeout", action=inspection._Once, default="2" if command == "schema" else "10",
                         help="原檢查服務期限，大於 0 且至多 30 秒")
        sub.add_argument("--process-timeout", action=inspection._Once, default="45",
                         help="子程序收集及等待總期限，大於 0 且至多 60 秒；僅 POSIX")
    raw = parser.parse_args(argv)
    try:
        process_timeout = float(raw.process_timeout)
        if not math.isfinite(process_timeout) or not 0 < process_timeout <= 60:
            raise ValueError()
    except (ValueError, OverflowError):
        raise inspection._ArgumentError() from None
    names = ("database",) if raw.command in ("schema", "integrity") else ("reference", "candidate")
    original = [raw.command, "--development-only", "--timeout", raw.timeout]
    for name in names:
        original.extend(["--" + name, getattr(raw, name)])
    # 重用原入口全部路徑、同檔、私人檔案與期限拒絕規則，不讀取內容。
    args = inspection._arguments(original)
    child_args = [args.command, "--development-only", "--timeout", str(args.timeout)]
    for name in names:
        child_args.extend(["--" + name, str(getattr(args, name).absolute())])
    return args.command, child_args, process_timeout


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return remaining


def _collect(command, timeout):
    """私有 POSIX 有界管線收集；command 僅由固定入口組成，不是公開執行 API。"""
    if os.name != "posix":
        raise RuntimeError("unsupported platform")
    deadline = time.monotonic() + timeout
    process = None
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": MAX_REPORT_BYTES, "stderr": MAX_STDERR_BYTES}
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=ROOT, env={}, close_fds=True,
                                   shell=False)
        for name in buffers:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            for key, _ in selector.select(_remaining(deadline)):
                name = key.data
                # 最多讀取上限加一，不能先無界 communicate 再檢查長度。
                chunk = os.read(key.fd, min(8192, limits[name] - len(buffers[name]) + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[name].extend(chunk)
                if len(buffers[name]) > limits[name]:
                    raise ValueError("output limit")
        code = process.wait(timeout=_remaining(deadline))
        _remaining(deadline)
        if code not in (0, 1) or buffers["stderr"]:
            raise ValueError("incomplete inspection")
        return bytes(buffers["stdout"]), code
    finally:
        selector.close()
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
            finally:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


def _run(operation, child_args, timeout):
    run_id = str(uuid.uuid4())
    command = [sys.executable, "-I", "-B", "-c", _WORKER, str(ROOT), *child_args]
    payload, code = _collect(command, timeout)
    validated = validate_development_report(payload, expected_operation=operation, expected_exit_code=code)
    # 僅已嚴格驗證、未知欄位已拒絕的原命令文件；不轉出子程序 stderr 或來源路徑。
    return {"run_version": RUN_VERSION, "run_id": run_id, "operation": validated.operation,
            "status": validated.status, "inspection_exit_code": validated.inspection_exit_code,
            "validation_version": validated.validation_version, "warning": WARNING,
            "can_quote": False, "can_confirm": False, "can_save": False,
            "can_resume_numbering": False, "inspection": json.loads(payload)}, code


def main(argv=None):
    try:
        try:
            operation, child_args, timeout = _arguments(argv)
        except inspection._ArgumentError:
            return inspection._error(ARGUMENT_ERROR, 2)
        except SystemExit as result:
            return result.code
        document, code = _run(operation, child_args, timeout)
        encoded = json.dumps(document, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n"
        sys.stdout.write(encoded)
        sys.stdout.flush()
        return code
    except KeyboardInterrupt:
        return inspection._error("受控開發檢查已中止；不提供完整結果。", 130)
    except Exception:
        return inspection._error(EXECUTION_ERROR, 3)


if __name__ == "__main__":
    raise SystemExit(main())
