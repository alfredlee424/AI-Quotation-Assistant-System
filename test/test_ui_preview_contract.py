"""抽取 UI 真實確認區塊執行；不 import app，避免啟動 Streamlit／seed。"""
import ast
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.state import QuoteStatus
from agent.tools import create_quote
from database.models import QuoteSnapshotDocument, ordqdt_ai
from engine.preview import freeze_preview


APP = Path(__file__).resolve().parents[1] / "app.py"


class UIHalt(BaseException):
    pass


class FakeStreamlit:
    def __init__(self, draft, clicked=""):
        self.session_state = SimpleNamespace(current_quote=draft, quote_confirmed=False, messages=[])
        self.clicked = clicked
        self.buttons = []
        self.notices = []

    def button(self, label, **kwargs):
        self.buttons.append(label)
        return label == self.clicked

    def columns(self, count):
        return [nullcontext() for _ in range(count)]

    def spinner(self, *args):
        return nullcontext()

    def divider(self):
        pass

    def notice(self, message):
        self.notices.append(message)

    warning = error = info = caption = notice

    def rerun(self):
        raise UIHalt("rerun")

    def stop(self):
        raise UIHalt("stop")


def run_confirmation_block(st, create):
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    nodes = [node for node in ast.walk(tree) if isinstance(node, ast.If)
             and "status == QuoteStatus.PREVIEW" in ast.unparse(node.test)]
    assert len(nodes) == 1, "必須存在唯一的固定預覽確認區塊"
    block = ast.Module(body=[nodes[0]], type_ignores=[])
    namespace = {"st": st, "quote_data": st.session_state.current_quote,
                 "status": st.session_state.current_quote["status"], "QuoteStatus": QuoteStatus,
                 "create_quote": create, "log_quote_created": Mock()}
    exec(compile(ast.fix_missing_locations(block), str(APP), "exec"), namespace)


def test_ui_confirmation_is_two_steps_and_uses_exact_preview_token(current_draft, cmt1_db):
    preview = freeze_preview(current_draft)
    st = FakeStreamlit(current_draft, clicked="✅ 確認建立正式報價單（寫入 ordqdt_ai）")
    create = Mock(wraps=create_quote)
    with pytest.raises(UIHalt, match="rerun"):
        run_confirmation_block(st, create)
    assert st.session_state.quote_confirmed == preview["preview_id"]
    create.assert_not_called()
    with cmt1_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == 0
    st.clicked = "✅ 確定建立"
    with pytest.raises(UIHalt, match="rerun"):
        run_confirmation_block(st, create)
    create.assert_called_once_with(current_draft, user="SYS", preview_id=preview["preview_id"])
    assert current_draft["calc_result"] == preview["calc"]
    assert current_draft["status"] == QuoteStatus.SNAPSHOT_CREATED
    assert st.session_state.quote_confirmed is False
    with cmt1_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == 1
        assert db.query(ordqdt_ai).count() == len(preview["calc"]["items"])


def test_ui_invalid_preview_stops_before_rendering_confirmation(current_draft):
    freeze_preview(current_draft)
    st = FakeStreamlit(current_draft, clicked="✅ 確定建立")
    st.session_state.quote_confirmed = current_draft["preview"]["preview_id"]
    current_draft["qty"] = 99
    create = Mock()
    with pytest.raises(UIHalt, match="stop"):
        run_confirmation_block(st, create)
    create.assert_not_called()
    assert not st.buttons
    assert st.session_state.quote_confirmed is False


def test_ui_new_preview_requires_new_first_confirmation(current_draft):
    old = freeze_preview(current_draft)
    st = FakeStreamlit(current_draft)
    st.session_state.quote_confirmed = old["preview_id"]
    freeze_preview(current_draft)
    create = Mock()
    run_confirmation_block(st, create)
    create.assert_not_called()
    assert "✅ 確定建立" not in st.buttons
    assert len(st.buttons) == 1


def test_ui_render_never_recalculates_and_import_passes_explicit_product_qty():
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    named = [node for node in calls if isinstance(node.func, ast.Name)]
    assert not {node.func.id for node in named} & {
        "calculate_from_draft", "calculate_from_ordstr", "calculate_configuration", "calculate_quote"}
    imports = [node for node in named if node.func.id == "import_pasted_quote"]
    assert len(imports) == 1
    assert "product_qty" in {keyword.arg for keyword in imports[0].keywords}
    saves = [node for node in named if node.func.id == "create_quote"]
    assert len(saves) == 1
    assert "preview_id" in {keyword.arg for keyword in saves[0].keywords}
