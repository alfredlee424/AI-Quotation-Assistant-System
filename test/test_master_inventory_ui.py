"""主檔盤點 UI：明確同意才查詢，輸入變更或失敗清除舊結果。"""
from unittest.mock import Mock

from streamlit.testing.v1 import AppTest

from engine.master_inventory import inventory_products
from utils import master_inventory_ui as ui


def workspace():
    import streamlit as st
    from utils.master_inventory_ui import render_master_inventory
    render_master_inventory(st)


def widget(app, kind, label):
    return next(element for element in getattr(app, kind) if element.label == label)


def start():
    app = AppTest.from_function(workspace)
    app.session_state.current_quote = {"marker": "single"}
    app.session_state.multi_quote_drafts = {"marker": "multi"}
    app.session_state.work_order_batch = "source-marker"
    return app.run()


def run_inventory(app):
    widget(app, "text_input", "資料來源／受控副本版本（人工說明）").set_value("隔離副本-v1")
    widget(app, "checkbox", "已確認允許唯讀查詢目前設定的資料庫").check().run()
    return widget(app, "button", "執行唯讀主檔盤點").click().run()


def test_entry_never_queries_until_explicit_run(cmt1_db, monkeypatch):
    spy = Mock(wraps=inventory_products)
    monkeypatch.setattr(ui, "inventory_products", spy)
    app = start()
    assert widget(app, "button", "執行唯讀主檔盤點").disabled
    assert not app.exception
    spy.assert_not_called()
    app = run_inventory(app)
    assert not app.exception and not app.error
    spy.assert_called_once_with(("CMT1", "MT", "DT1"), source_label="隔離副本-v1")
    assert len(app.session_state.master_inventory_result["document"]["products"]) == 3
    assert app.session_state.current_quote == {"marker": "single"}
    assert app.session_state.multi_quote_drafts == {"marker": "multi"}
    assert app.session_state.work_order_batch == "source-marker"
    app.run()
    assert spy.call_count == 1


def test_failed_retry_clears_previous_success_without_leaking_connection(cmt1_db, monkeypatch):
    app = run_inventory(start())
    monkeypatch.setattr(ui, "inventory_products", Mock(side_effect=RuntimeError("secret-password-host")))
    widget(app, "button", "執行唯讀主檔盤點").click().run()
    assert not app.exception and app.error
    assert "master_inventory_result" not in app.session_state
    assert all("secret-password-host" not in element.value for element in app.error)


def test_input_change_clears_old_document(cmt1_db):
    app = run_inventory(start())
    widget(app, "text_input", "盤點產品代碼（逗號分隔，最多三個）").set_value("MT").run()
    assert not app.exception and "master_inventory_result" not in app.session_state


def test_source_change_and_consent_revocation_clear_old_document(cmt1_db):
    app = run_inventory(start())
    widget(app, "text_input", "資料來源／受控副本版本（人工說明）").set_value("另一副本").run()
    assert "master_inventory_result" not in app.session_state
    app = run_inventory(app)
    widget(app, "checkbox", "已確認允許唯讀查詢目前設定的資料庫").uncheck().run()
    assert "master_inventory_result" not in app.session_state


def test_invalid_scope_cannot_keep_previous_result(cmt1_db):
    app = run_inventory(start())
    widget(app, "text_input", "盤點產品代碼（逗號分隔，最多三個）").set_value("CMT1,CMT1")
    widget(app, "button", "執行唯讀主檔盤點").click().run()
    assert not app.exception and app.error
    assert "master_inventory_result" not in app.session_state


def test_main_workspace_stops_before_seed_or_quote_flow(monkeypatch):
    from database import seed_data
    from database import repository
    seed = Mock(side_effect=AssertionError("禁止初始化"))
    query = Mock(side_effect=AssertionError("禁止自動查詢"))
    monkeypatch.setattr(seed_data, "seed", seed)
    monkeypatch.setattr(repository, "_session", query)
    app = AppTest.from_file("../app.py")
    app.session_state.workspace = "產品線主檔盤點"
    app.session_state.current_quote = {"marker": "single"}
    app.run()
    assert not app.exception and not app.error
    seed.assert_not_called()
    query.assert_not_called()
    assert app.session_state.current_quote == {"marker": "single"}
