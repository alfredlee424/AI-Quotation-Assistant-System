"""真實數值核對 UI：雙步驟、主檔失效、操作人隔離。"""
from unittest.mock import Mock

from streamlit.testing.v1 import AppTest

from test_multi_trial_ui import workspace, widget
from test_numeric_application import numeric_case
from utils import numeric_application_ui as ui


def start(batch, draft):
    app = AppTest.from_function(workspace)
    app.session_state.multi_quote_drafts = {draft.draft_id: draft}
    app.session_state.multi_selected = draft.draft_id
    app.session_state.work_order_batch = batch
    app.session_state.current_quote = {"marker": "existing-single"}
    app.run()
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("測試")
    return app


def consent(app):
    widget(app, "text_input", "數值語意／單位確認依據識別與版本").set_value("人工確認-TEST-R1")
    widget(app, "text_area", "成品數或桌面用途、單位及軸順序的確認內容").set_value("確認為此明細的成品數／桌面尺寸")
    return widget(app, "checkbox", "已人工確認上述數值屬於此明細的成品數或桌面尺寸，不是配件／分片用量").check().run()


def prepare(app):
    return widget(app, "button", "產生數值套用差異（不修改草稿）").click().run()


def test_quantity_requires_two_steps_and_does_not_change_source_or_single(numeric_case):
    batch, draft = numeric_case
    app = start(batch, draft)
    assert widget(app, "button", "產生數值套用差異（不修改草稿）").disabled
    app = prepare(consent(app))
    assert not app.exception and not app.error
    assert app.session_state.multi_quote_drafts[draft.draft_id] == draft
    widget(app, "button", "套用已核對數值差異（非工程核准）").click().run()
    assert not app.exception and not app.error
    updated = app.session_state.multi_quote_drafts[draft.draft_id]
    assert updated.lines[0].configuration["qty"] == 2 and updated.lines[1] == draft.lines[1]
    assert app.session_state.work_order_batch == batch
    assert app.session_state.current_quote == {"marker": "existing-single"}
    assert draft.draft_id not in app.session_state.numeric_application_proposals


def test_dimensions_require_explicit_catalog_query_unit_and_axis(numeric_case, monkeypatch):
    batch, draft = numeric_case
    spy = Mock(wraps=ui.dimension_options)
    monkeypatch.setattr(ui, "dimension_options", spy)
    app = start(batch, draft)
    widget(app, "radio", "數值套用類型").set_value("table_dimensions").run()
    spy.assert_not_called()
    app = consent(app)
    assert widget(app, "button", "產生數值套用差異（不修改草稿）").disabled
    widget(app, "checkbox", "查詢目標明細合法桌面尺寸（唯讀主檔）").check().run()
    widget(app, "selectbox", "要核對的合法桌面尺寸").set_value("C003")
    widget(app, "selectbox", "來源兩軸對應主檔順序").set_value("as_written")
    app = prepare(app)
    assert app.error and not app.exception
    assert draft.draft_id not in app.session_state.numeric_application_proposals
    widget(app, "selectbox", "主檔尺寸單位確認").set_value("cm")
    app = prepare(app)
    assert not app.error and not app.exception
    widget(app, "button", "套用已核對數值差異（非工程核准）").click().run()
    assert not app.error and not app.exception
    updated = app.session_state.multi_quote_drafts[draft.draft_id]
    assert updated.lines[0].configuration["selections"][r"CMT1\A001"]["code"] == "C003"


def test_changed_actor_clears_pending_and_render_does_not_load_catalog(numeric_case, monkeypatch):
    batch, draft = numeric_case
    app = prepare(consent(start(batch, draft)))
    from database import repository as repo
    monkeypatch.setattr(repo, "_session", Mock(side_effect=AssertionError("不得在畫面核對重查主檔")))
    app.run()
    assert not app.exception and not app.error
    assert draft.draft_id in app.session_state.numeric_application_proposals
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("另一位").run()
    assert draft.draft_id not in app.session_state.numeric_application_proposals
    repo._session.assert_not_called()


def test_failure_discards_pending_without_mutation_or_secret(numeric_case, monkeypatch):
    batch, draft = numeric_case
    app = prepare(consent(start(batch, draft)))
    monkeypatch.setattr(ui, "commit_numeric_application", Mock(side_effect=RuntimeError("SECRET-HOST")))
    widget(app, "button", "套用已核對數值差異（非工程核准）").click().run()
    assert not app.exception and app.error
    assert app.session_state.multi_quote_drafts[draft.draft_id] == draft
    assert draft.draft_id not in app.session_state.numeric_application_proposals
    assert all("SECRET-HOST" not in e.value for e in app.error)
