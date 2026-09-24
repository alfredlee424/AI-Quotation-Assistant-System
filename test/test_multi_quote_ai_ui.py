"""跨明細 AI 核對與人工套用介面，不連外、不寫正式報價。"""
from streamlit.testing.v1 import AppTest

from test_multi_quote_agent import multi, sdk, proposal


def workspace():
    import streamlit as st
    from utils.multi_quote_ui import render_multi_quote_workspace
    render_multi_quote_workspace(st)


def widget(app, category, label):
    return next(element for element in getattr(app, category) if element.label == label)


def start(draft):
    app = AppTest.from_function(workspace)
    app.session_state.multi_quote_drafts = {draft.draft_id: draft}
    app.session_state.multi_selected = draft.draft_id
    app.session_state.current_quote = {"status": "PREVIEW", "marker": "existing-single"}
    app.run()
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("測試")
    widget(app, "multiselect", "允許 AI 修改的明細（不代表全部套同規格）").set_value([line.line_id for line in draft.lines])
    widget(app, "text_area", "本次跨明細需求").set_value("第一筆3件，第二筆4件")
    return app


def test_model_proposal_requires_separate_human_apply(multi, sdk, cmt1_db):
    from database.models import QuoteSnapshotDocument, ordqdt_ai
    sdk.respond(proposal(multi))
    app = start(multi)
    widget(app, "button", "產生 AI 配置提案（不修改草稿）").click().run()
    assert not app.exception and not app.error
    assert app.session_state.multi_quote_drafts[multi.draft_id] == multi
    assert widget(app, "button", "套用已驗證配置提案（非報價）")
    assert not any("987654321" in element.value for element in app.text)
    widget(app, "button", "套用已驗證配置提案（非報價）").click().run()
    assert not app.exception and not app.error
    updated = app.session_state.multi_quote_drafts[multi.draft_id]
    assert [line.configuration["qty"] for line in updated.lines] == [3, 4]
    assert updated.revision == multi.revision + 1
    assert app.session_state.current_quote["marker"] == "existing-single"
    assert not any(element.label == "套用已驗證配置提案（非報價）" for element in app.button)
    with cmt1_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == db.query(ordqdt_ai).count() == 0


def test_editing_request_removes_pending_proposal(multi, sdk):
    sdk.respond(proposal(multi))
    app = start(multi)
    widget(app, "button", "產生 AI 配置提案（不修改草稿）").click().run()
    widget(app, "text_area", "本次跨明細需求").set_value("改成其他需求").run()
    assert not app.exception
    assert not any(element.label == "套用已驗證配置提案（非報價）" for element in app.button)
    assert app.session_state.multi_quote_drafts[multi.draft_id] == multi


def test_failed_regeneration_clears_previous_proposal(multi, sdk):
    sdk.respond(proposal(multi))
    app = start(multi)
    widget(app, "button", "產生 AI 配置提案（不修改草稿）").click().run()
    sdk.create.side_effect = RuntimeError("offline")
    widget(app, "button", "產生 AI 配置提案（不修改草稿）").click().run()
    assert not app.exception and app.error
    assert not any(element.label == "套用已驗證配置提案（非報價）" for element in app.button)
    assert app.session_state.multi_quote_drafts[multi.draft_id] == multi


def test_no_explicit_scope_does_not_call_model(multi, sdk):
    app = start(multi)
    widget(app, "multiselect", "允許 AI 修改的明細（不代表全部套同規格）").set_value([])
    widget(app, "button", "產生 AI 配置提案（不修改草稿）").click().run()
    assert not app.exception and app.error
    sdk.constructor.assert_not_called()


def test_saved_questions_display_targets_without_changing_other_lines(multi, sdk):
    content = proposal(multi)
    content["updates"] = []
    content["questions"] = [{"target_id": multi.lines[1].line_id, "text": "第二筆每件圓孔要幾個？"}]
    sdk.respond(content)
    app = start(multi)
    widget(app, "button", "產生 AI 配置提案（不修改草稿）").click().run()
    widget(app, "button", "套用已驗證配置提案（非報價）").click().run()
    assert not app.exception and not app.error
    updated = app.session_state.multi_quote_drafts[multi.draft_id]
    assert updated.lines[0] == multi.lines[0]
    assert any("第二張測試桌｜第二筆每件圓孔要幾個？" in element.value for element in app.text)
