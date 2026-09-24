"""數值診斷介面只呈現結果，不查主檔、不修改工單或單品報價。"""
from streamlit.testing.v1 import AppTest

from agent.work_orders import parse_work_orders, export_review


def diagnostic_app():
    import streamlit as st
    from utils.work_order_diagnostics_ui import render_work_order_diagnostics
    render_work_order_diagnostics(st, st.session_state.batch)


def test_diagnostic_ui_shows_metric_values_and_does_not_mutate_state(monkeypatch):
    from database import repository as repo
    def forbidden(*args, **kwargs):
        raise AssertionError("唯讀文字診斷不得查主檔")
    monkeypatch.setattr(repo, "get_product_categories", forbidden)
    batch = parse_work_orders("20990101-1測試\n桌面600mmX1800mm，假厚36MM")
    before = export_review(batch)
    app = AppTest.from_function(diagnostic_app)
    app.session_state.batch = batch
    app.session_state.current_quote = {"preview": "unchanged"}
    app.run()
    assert not app.exception
    rows = app.dataframe[0].value
    assert "60 × 180" in rows["正規化公分"].tolist()
    assert "3.6" in rows["正規化公分"].tolist()
    assert export_review(app.session_state.batch) == before
    assert app.session_state.current_quote == {"preview": "unchanged"}


def test_diagnostic_ui_warns_about_size_conflict_without_correction():
    batch = parse_work_orders("20990101-1測試\n20X360(120X180X2)船型-----2")
    app = AppTest.from_function(diagnostic_app)
    app.session_state.batch = batch
    app.run()
    assert not app.exception and app.warning
    assert any("分片無法容納" in value for value in app.dataframe[1].value["提示"].tolist())
    assert "20X360" in app.session_state.batch.raw_text
