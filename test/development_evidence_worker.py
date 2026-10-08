"""測試專用命令鏈 worker；不提供維運入口或接受外部程式／報告注入。"""
import importlib
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
mode, phase = sys.argv[1:3]
ready_fd, resume_fd = map(int, sys.argv[3:5])
if mode not in ("archive", "validate") or phase not in (
        "normal", "race", "before_link", "after_link", "before_receipt",
        "partial_receipt", "after_receipt"):
    raise SystemExit(90)
if mode == "validate" and phase != "normal":
    raise SystemExit(90)
if Path.cwd().resolve().is_relative_to(ROOT):
    raise SystemExit(90)


def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(part.startswith(".env") for part in path.parts) or path.is_relative_to(ROOT / "logs"):
            raise RuntimeError("private input denied")
    if event.startswith(("socket.", "sqlite3.")) or event in (
            "os.system", "os.fork", "os.posix_spawn"):
        raise RuntimeError("external access denied")
    if event == "import":
        name = args[0]
        if name.split(".")[0] in {
                "sqlalchemy", "sqlite3", "database", "agent", "streamlit", "openai", "dotenv", "config"}:
            raise RuntimeError("dependency denied")
        if mode == "validate" and name in {
                "development_inspection_archive", "development_inspection_run",
                "engine.development_schema_inspection", "engine.development_integrity_inspection",
                "engine.development_registry_comparison", "engine.development_snapshot_comparison"}:
            raise RuntimeError("offline dependency denied")
    if event == "subprocess.Popen":
        runner = sys.modules.get("development_inspection_run")
        if (mode != "archive" or runner is None or args[0] != sys.executable
                or args[1][:6] != [sys.executable, "-I", "-B", "-c", runner._WORKER, str(ROOT)]
                or Path(args[2]) != ROOT or args[3] != {}):
            raise RuntimeError("only fixed inspection child allowed")


sys.addaudithook(audit)
sys.path.insert(0, str(ROOT))
module = importlib.import_module(
    "development_inspection_archive" if mode == "archive" else "development_archive_validation")


def barrier():
    # 控制通道與真實回條 stdout 分開；父程序有界等待，不以 sleep 猜故障點。
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
    original_publish = module._publish

    def publish(*args):
        barrier()
        return original_publish(*args)

    module._publish = publish
elif phase in ("before_receipt", "partial_receipt", "after_receipt"):
    original_stdout = sys.stdout

    class Output:
        stopped = False

        def write(self, text):
            if phase == "before_receipt":
                barrier()
            if phase == "partial_receipt":
                original_stdout.write(text[:19])
                original_stdout.flush()
                barrier()
                original_stdout.write(text[19:])
                return len(text)
            return original_stdout.write(text)

        def flush(self):
            original_stdout.flush()
            if phase == "after_receipt" and not self.stopped:
                self.stopped = True
                barrier()

    sys.stdout = Output()

raise SystemExit(module.main(sys.argv[5:]))
