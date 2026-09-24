"""測試真實 Streamlit 核對介面；不啟動主應用、不呼叫 seed 或外部服務。"""
import ast
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from work_order_fixtures import CASES, combined_input


def review_app():
    import streamlit as st
    from utils.work_order_ui import render_work_order_review
    if "current_quote" not in st.session_state:
        st.session_state.current_quote = {"status": "PREVIEW", "preview": {"preview_id": "existing"}}
        st.session_state.quote_confirmed = "existing"
    render_work_order_review(st)


def import_text(text):
    app = AppTest.from_function(review_app).run()
    app.text_area(key="work_order_input").set_value(text)
    app.button(key="work_order_parse").click().run()
    assert not app.exception
    return app


def test_review_ui_displays_eight_orders_without_changing_existing_quote(isolated_db):
    from database.models import QuoteSnapshotDocument, ordqdt_ai
    app = import_text(combined_input())
    assert len(app.session_state.work_order_batch.orders) == 8
    assert app.session_state.current_quote == {"status": "PREVIEW", "preview": {"preview_id": "existing"}}
    assert app.session_state.quote_confirmed == "existing"
    assert not any("建立正式報價" in button.label or "確定建立" in button.label for button in app.button)
    assert any("8 張工單候選" in caption.value for caption in app.caption)
    with isolated_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == db.query(ordqdt_ai).count() == 0


def test_review_ui_errors_do_not_replace_previous_batch():
    app = import_text(CASES[0].raw_text)
    batch_id = app.session_state.work_order_batch.batch_id
    app.text_area(key="work_order_input").set_value(" ").run()
    assert any("輸入已變更" in warning.value for warning in app.warning)
    assert not app.selectbox  # 舊原文未重新套用前，不可編輯舊草稿。
    app.button(key="work_order_parse").click().run()
    assert app.error
    assert app.session_state.work_order_batch.batch_id == batch_id


def test_review_ui_manual_classification_retains_ids_and_records_reason():
    app = import_text(CASES[0].raw_text)
    before = app.session_state.work_order_batch
    line = next(line for line in before.lines if line.raw == "單面置物版")
    next(box for box in app.selectbox if box.label == "待修正來源行").select(line.line_id).run()
    next(box for box in app.selectbox if box.label == "修正分類").select("item")
    next(field for field in app.text_input if field.label.startswith("修正人")).set_value("測試人員")
    next(field for field in app.text_input if field.label.startswith("修正理由")).set_value("置物板先獨立列為候選")
    next(button for button in app.button if button.label == "套用分類修正").click().run()
    assert not app.exception
    updated = app.session_state.work_order_batch
    assert updated.revision == 1
    assert len(updated.orders[0].items) == 5
    assert updated.orders[0].items[:4] == before.orders[0].items
    assert updated.corrections[0].reason == "置物板先獨立列為候選"
    # 重按同原文不丟棄人工修正。
    app.button(key="work_order_parse").click().run()
    assert app.session_state.work_order_batch == updated


def test_review_ui_unknown_header_can_be_manually_promoted():
    app = import_text("客製需求\n桌面-----1")
    assert not app.session_state.work_order_batch.orders
    next(box for box in app.selectbox if box.label == "修正分類").select("order")
    next(box for box in app.selectbox if box.label.startswith("類別線索")).select("餐桌")
    next(field for field in app.text_input if field.label.startswith("修正人")).set_value("測試人員")
    next(field for field in app.text_input if field.label.startswith("修正理由")).set_value("確認此處為工單起始")
    next(button for button in app.button if button.label == "套用分類修正").click().run()
    assert not app.exception
    assert app.session_state.work_order_batch.orders[0].family_hint == "餐桌"


def test_main_app_stops_review_workspace_before_initialization_or_quote_ui():
    source = Path(__file__).resolve().parents[1] / "app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    guards = [node for node in tree.body if isinstance(node, ast.If)
              and ast.unparse(node.test) == "workspace == '生產工單核對'"]
    assert len(guards) == 1
    guard = guards[0]
    assert isinstance(guard.body[-1], ast.Expr) and ast.unparse(guard.body[-1].value) == "st.stop()"
    init_calls = [node for node in tree.body if isinstance(node, ast.Expr)
                  and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "_init_app"]
    assert len(init_calls) == 1 and init_calls[0].lineno > guard.end_lineno
    # 直接執行工作區分支，證明 stop 中斷，不繼續執行後面的初始化。
    class Halt(BaseException):
        pass
    class UI:
        def stop(self):
            raise Halt
    calls = []
    block = ast.Module(body=[guard, init_calls[0]], type_ignores=[])
    with pytest.raises(Halt):
        exec(compile(block, str(source), "exec"), {
            "workspace": "生產工單核對", "st": UI(),
            "render_work_order_review": lambda st: calls.append("review"),
            "_init_app": lambda: calls.append("seed"),
        })
    assert calls == ["review"]
