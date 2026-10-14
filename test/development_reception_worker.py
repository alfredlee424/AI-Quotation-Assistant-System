"""測試專用接收鏈 worker；active 階段另在仍執行的存檔程序同步。"""
import importlib
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
mode, phase = sys.argv[1:3]
ready_fd, resume_fd, child_fd = map(int, sys.argv[3:6])
if mode not in ("receive", "validate") or phase not in (
        "normal", "after_collect", "before_link", "after_link", "race",
        "before_summary", "partial_summary", "after_summary",
        "active_before_run", "active_before_link", "active_after_link", "active_after_receipt",
        "active_inspection_read"):
    raise SystemExit(90)
if (mode == "validate" and phase != "normal") or Path.cwd().resolve().is_relative_to(ROOT):
    raise SystemExit(90)
allowed = None
spawned = False


def audit(event, args):
    global spawned
    if event == "open":
        if isinstance(args[0], (str, bytes)):
            path = Path(os.fsdecode(args[0])).resolve()
            if any(p.startswith(".env") for p in path.parts) or path.is_relative_to(ROOT / "logs"):
                raise RuntimeError("private input denied")
        if mode == "validate" and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            raise RuntimeError("offline write denied")
    if event.startswith(("socket.", "sqlite3.")) or event in {
            "os.system", "os.fork", "os.posix_spawn"}:
        raise RuntimeError("external operation denied")
    if mode == "validate" and event in {"os.remove", "os.rename", "os.link", "os.mkdir", "os.chmod"}:
        raise RuntimeError("offline mutation denied")
    if event == "import":
        name = args[0]
        if name.split(".")[0] in {
                "database", "sqlalchemy", "sqlite3", "agent", "config", "dotenv", "openai", "streamlit"}:
            raise RuntimeError("production dependency denied")
        if mode == "validate" and name in {
                "development_inspection_receive", "development_inspection_archive", "development_inspection_run",
                "engine.development_schema_inspection", "engine.development_integrity_inspection",
                "engine.development_registry_comparison", "engine.development_snapshot_comparison"}:
            raise RuntimeError("offline dependency denied")
    if event == "subprocess.Popen":
        if (mode != "receive" or spawned or allowed is None or args[0] != sys.executable
                or args[1] != allowed or Path(args[2]) != ROOT or args[3] != {}):
            raise RuntimeError("unexpected child")
        spawned = True


sys.addaudithook(audit)
sys.path.insert(0, str(ROOT))
module = importlib.import_module(
    "development_inspection_receive" if mode == "receive" else "development_reception_validation")
if mode == "receive":
    if phase.startswith("active_"):
        # 僅測試注入同步點；保留原 worker 的匯入前防護與固定真實檢查命令。
        marker = "operation, child_args, timeout, directory, name = archive._arguments(sys.argv[2:])"
        assert module._WORKER.count(marker) == 1
        hooks = (ROOT / "test" / "development_active_reception_hooks.py").read_text(encoding="utf-8")
        setup = f"phase = {phase!r}\nready_fd = {ready_fd}\nresume_fd = {resume_fd}\n"
        if phase == "active_inspection_read":
            inspection_hooks = (ROOT / "test" / "development_live_inspection_hooks.py").read_text(encoding="utf-8")
            setup += f"inspection_hooks = {inspection_hooks!r}\n"
        module._WORKER = module._WORKER.replace(marker, setup + hooks + "\n" + marker)
    # 實際參數仍由原 main 驗證；求助與拒絕不預先解析或啟動子程序。
    original_collect = module._collect
    original_popen = subprocess.Popen

    def popen(*args, **kwargs):
        if phase.startswith("active_"):
            kwargs["pass_fds"] = (ready_fd, resume_fd)
        process = original_popen(*args, **kwargs)
        # 獨立群組不屬於外層 worker；通知父測試以便逾時也能清理。
        os.write(child_fd, process.pid.to_bytes(8, "big"))
        return process

    subprocess.Popen = popen

    def collect(command, timeout):
        global allowed
        allowed = command
        result = original_collect(command, timeout)
        if phase == "after_collect":
            barrier()
        return result

    module._collect = collect


def barrier():
    os.write(ready_fd, b"R")
    if os.read(resume_fd, 1) != b"G":
        raise RuntimeError("test controller disconnected")


if phase in ("before_link", "after_link"):
    original_link = os.link

    def link(*args, **kwargs):
        if phase == "before_link":
            barrier()
        original_link(*args, **kwargs)
        if phase == "after_link":
            barrier()

    os.link = link
elif phase == "race":
    original_publish = module.archive._publish

    def publish(*args):
        barrier()
        return original_publish(*args)

    module.archive._publish = publish
elif phase in ("before_summary", "partial_summary", "after_summary"):
    original_stdout = sys.stdout

    class Output:
        stopped = False

        def write(self, text):
            if phase == "before_summary":
                barrier()
            if phase == "partial_summary":
                original_stdout.write(text[:19])
                original_stdout.flush()
                barrier()
                original_stdout.write(text[19:])
                return len(text)
            return original_stdout.write(text)

        def flush(self):
            original_stdout.flush()
            if phase == "after_summary" and not self.stopped:
                self.stopped = True
                barrier()

    sys.stdout = Output()

raise SystemExit(module.main(sys.argv[6:]))
