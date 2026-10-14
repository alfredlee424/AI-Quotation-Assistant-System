"""只供合成驗收測試的隔離子程序；不啟動正式 app／UI／模型。"""
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0]))
        if any(part.startswith(".env") for part in path.parts) or path.is_relative_to(ROOT / "logs"):
            raise AssertionError("測試禁止讀取私人設定／真工單")
    if event.startswith("socket."):
        raise AssertionError("測試禁止所有網路")
    if event == "import" and args[0] in {"app", "streamlit", "openai"}:
        raise AssertionError("測試禁止正式 app／UI／模型")


sys.addaudithook(audit)
from acceptance.bootstrap import initialize_directory, isolate_process, verify_directory

if sys.argv[1] == "initialize":
    directory = initialize_directory(sys.argv[2])
else:
    directory = verify_directory(isolate_process(sys.argv[2]))

from acceptance import business as b
from acceptance.workflow import Workflow, WorkflowError, WorkflowOperationError, InputContext

if sys.argv[1] == "initialize":
    b.initialize_master(directory)
    w = Workflow.initialize(directory)
else:
    w = Workflow(directory)


def options(**changes):
    value = dict(actor="合成操作人", accepted_assumptions=[a["id"] for a in b.ASSUMPTIONS],
                 source_confirmed=True, discount_rate="0.05")
    value.update(changes)
    return value


def create(**changes):
    raw = changes.pop("raw", b.SAMPLE)
    return w.create(raw, **options(**changes))


def confirmed(view):
    view = w.confirm(view["id"], 1, view["token"], view["context"])
    return w.confirm(view["id"], 2, view["token"], view["context"])


def emit(value):
    from dataclasses import asdict, is_dataclass
    print(json.dumps(value, ensure_ascii=False, default=lambda item: asdict(item) if is_dataclass(item) else item))


def rejected(operation, error=ValueError):
    try:
        operation()
    except error:
        return
    raise AssertionError("操作應被拒絕")


exec(compile(sys.stdin.read(), "<synthetic-acceptance-test>", "exec"))
w.close()
