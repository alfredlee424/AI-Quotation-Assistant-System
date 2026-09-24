"""多明細配置真實介面操作測試；不執行主應用初始化或正式報價。"""
import ast
from copy import deepcopy
from pathlib import Path

from streamlit.testing.v1 import AppTest

from agent.state import new_quote_draft
from agent.work_orders import parse_work_orders, reclassify_line
from work_order_fixtures import CASES


def multi_app():
    import streamlit as st
    from utils.multi_quote_ui import render_multi_quote_workspace
    render_multi_quote_workspace(st)


def element(app, category, label):
    return next(item for item in getattr(app, category) if item.label == label)


def active(app):
    return app.session_state.multi_quote_drafts[app.session_state.multi_selected]


def actor_and_reason(app):
    element(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("測試人員")
    element(app, "text_input", "本次操作理由").set_value("核對明細")


def add_line(app, name):
    actor_and_reason(app)
    element(app, "text_input", "新增明細名稱").set_value(name)
    element(app, "button", "新增明細").click().run()
    assert not app.exception


def test_blank_workspace_add_reorder_archive_restore_without_master_queries(monkeypatch):
    from database import repository as repo
    def forbidden(*args, **kwargs):
        raise AssertionError("未開啟合法選配，不應讀主檔")
    monkeypatch.setattr(repo, "get_product_categories", forbidden)
    app = AppTest.from_function(multi_app).run()
    app.button(key="multi_new").click().run()
    add_line(app, "桌型A")
    add_line(app, "桌型B")
    draft = active(app)
    ids = [line.line_id for line in draft.lines]
    assert len(draft.lines) == 2 and draft.revision == 2
    element(app, "selectbox", "編輯明細").select(ids[1]).run()
    element(app, "button", "明細上移").click().run()
    assert [line.line_id for line in active(app).lines] == ids[::-1]
    assert element(app, "selectbox", "編輯明細").value == ids[1]
    element(app, "button", "移除並封存此明細").click().run()
    assert active(app).archived_lines[0].line_id == ids[1]
    element(app, "button", "恢復封存明細").click().run()
    assert [line.line_id for line in active(app).lines] == ids
    assert not app.exception and not app.error


def test_copy_single_keeps_existing_quote_and_removes_confirmation_state(current_draft):
    single = deepcopy(current_draft)
    single["preview"] = {"preview_id": "old-preview"}
    app = AppTest.from_function(multi_app)
    app.session_state.current_quote = single
    app.session_state.quote_confirmed = "old-preview"
    app.run().button(key="multi_from_single").click().run()
    assert not app.exception
    assert active(app).lines[0].configuration["preview"] is None
    assert app.session_state.current_quote == single
    assert app.session_state.quote_confirmed == "old-preview"
    assert not any("確定建立" in item.label for item in app.button)


def test_work_order_transfer_keeps_candidate_numbers_unconfirmed_and_stops_on_source_change():
    batch = parse_work_orders(CASES[0].raw_text)
    app = AppTest.from_function(multi_app)
    app.session_state.work_order_batch = batch
    app.session_state.current_quote = new_quote_draft()
    app.run().button(key="multi_from_order").click().run()
    assert len(active(app).lines) == 4
    assert all(line.configuration["qty"] is None for line in active(app).lines)
    old = active(app)
    app.session_state.work_order_batch = reclassify_line(
        batch, line_id=batch.lines[-1].line_id, kind="note", expected_revision=0, actor="測試", reason="改分類",
    )
    app.run()
    assert any("來源工單" in error.value for error in app.error)
    assert not any(item.label == "新增明細" for item in app.button)
    assert active(app) == old


def test_manual_product_quantity_and_option_changes_are_scoped_to_selected_line(cmt1_db):
    app = AppTest.from_function(multi_app).run()
    app.button(key="multi_new").click().run()
    add_line(app, "桌型A")
    add_line(app, "桌型B")
    first, second = (line.line_id for line in active(app).lines)
    element(app, "selectbox", "編輯明細").select(first).run()
    element(app, "checkbox", "顯示合法選配（唯讀查詢目前產品主檔）").check().run()
    element(app, "selectbox", "產品類別").select("CMT1")
    element(app, "number_input", "產品數量（須人工核對，不沿用工單候選）").set_value(3.0)
    element(app, "button", "套用產品及數量").click().run()
    assert not app.exception
    assert active(app).lines[0].configuration["qty"] == 3
    assert active(app).lines[1].configuration["qty"] is None
    element(app, "selectbox", "配置部位（完整路徑）").select(r"CMT1\A001").run()
    # 選擇由資料庫提供的第一個尺寸，使用者仍須明確按下套用。
    element(app, "button", "套用此部位規格").click().run()
    assert not app.exception
    assert r"CMT1\A001" in active(app).lines[0].configuration["selections"]
    assert active(app).lines[1].configuration["selections"] == {}
    assert active(app).lines[1].line_id == second


def test_missing_actor_rejects_change_and_keeps_draft():
    app = AppTest.from_function(multi_app).run()
    app.button(key="multi_new").click().run()
    before = active(app)
    element(app, "text_input", "新增明細名稱").set_value("無簽名操作")
    element(app, "button", "新增明細").click().run()
    assert app.error
    assert active(app) == before


def test_configuring_second_line_retains_focus_across_reruns(cmt1_db):
    app = AppTest.from_function(multi_app).run()
    app.button(key="multi_new").click().run()
    add_line(app, "第一筆")
    add_line(app, "第二筆")
    target = active(app).lines[1].line_id
    element(app, "selectbox", "編輯明細").select(target).run()
    element(app, "checkbox", "顯示合法選配（唯讀查詢目前產品主檔）").check().run()
    element(app, "selectbox", "產品類別").select("CMT1")
    element(app, "number_input", "產品數量（須人工核對，不沿用工單候選）").set_value(2.0)
    element(app, "button", "套用產品及數量").click().run()
    assert not app.exception
    assert element(app, "selectbox", "編輯明細").value == target
    assert active(app).lines[0].configuration["qty"] is None
    assert active(app).lines[1].configuration["qty"] == 2


def test_main_multi_workspace_stops_before_single_initialization():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
    guard = next(node for node in tree.body if isinstance(node, ast.If)
                 and ast.unparse(node.test) == "workspace == '多明細配置草稿'")
    assert ast.unparse(guard.body[-1]) == "st.stop()"
    init = next(node for node in tree.body if isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "_init_app")
    assert guard.end_lineno < init.lineno
