"""真實 Streamlit 圖面核對流程及舊內部試算失效。"""
from test_multi_trial import two_cmt
from test_multi_trial_ui import start, widget, run_trial
from test_drawing_review import declare, record


def add_dependency(app, draft):
    widget(app, "multiselect", "圖面適用明細").set_value([draft.lines[0].line_id])
    widget(app, "text_input", "圖面用途／待確認內容").set_value("桌面尺寸核對")
    widget(app, "text_input", "圖面依賴登記理由").set_value("圖面尚未提供")
    return widget(app, "button", "登記圖面依賴（缺圖待補）").click().run()


def fill_record(app):
    widget(app, "text_input", "圖面依據識別（不自動開啟）").set_value("TEST-DRAWING")
    widget(app, "text_input", "圖面版本").set_value("R1")
    widget(app, "text_area", "人工確認內容（部位、尺寸或定位摘要）").set_value("依指定版本核對桌面尺寸")
    widget(app, "text_input", "本次圖面核對理由").set_value("人工查閱圖面")


def test_declare_clears_old_trial_and_preserves_single_workspace(two_cmt):
    app = run_trial(start(two_cmt))
    assert two_cmt.draft_id in app.session_state.multi_internal_trials
    app = add_dependency(app, two_cmt)
    assert not app.exception and not app.error
    draft = app.session_state.multi_quote_drafts[two_cmt.draft_id]
    assert draft.drawings[0].status == "MISSING"
    assert two_cmt.draft_id not in app.session_state.multi_internal_trials
    assert app.session_state.current_quote == {"marker": "existing-single"}
    app = run_trial(app)
    report = app.session_state.multi_internal_trials[two_cmt.draft_id]
    assert report["totals"] is None and report["calculated_count"] == 1


def test_record_requires_acknowledgment_and_version(two_cmt):
    draft = declare(two_cmt)
    app = start(draft)
    fill_record(app)
    widget(app, "button", "保存圖面人工核對（非正式核准）").click().run()
    assert app.error and not app.exception
    assert app.session_state.multi_quote_drafts[draft.draft_id] == draft
    widget(app, "checkbox", "已人工核對指定版本；了解此紀錄不是工程核准").check()
    widget(app, "text_input", "圖面版本").set_value("")
    widget(app, "button", "保存圖面人工核對（非正式核准）").click().run()
    assert app.error and app.session_state.multi_quote_drafts[draft.draft_id] == draft
    widget(app, "text_input", "圖面版本").set_value("R1")
    widget(app, "button", "保存圖面人工核對（非正式核准）").click().run()
    assert not app.error and not app.exception
    updated = app.session_state.multi_quote_drafts[draft.draft_id]
    assert updated.drawings[0].status == "RECORDED" and updated.blockers == draft.blockers
    assert updated.drawings[0].evidence[-1].actor == "測試"
    assert not any("確定建立" in button.label for button in app.button)


def test_mark_missing_preserves_evidence_and_invalidates_trial(two_cmt):
    draft = record(declare(two_cmt))
    app = run_trial(start(draft))
    widget(app, "text_input", "本次圖面核對理由").set_value("圖面作廢，待新版")
    widget(app, "button", "標記缺圖／撤回本次核對").click().run()
    assert not app.exception and not app.error
    updated = app.session_state.multi_quote_drafts[draft.draft_id]
    assert updated.drawings[0].status == "MISSING"
    assert updated.drawings[0].evidence == draft.drawings[0].evidence
    assert draft.draft_id not in app.session_state.multi_internal_trials


def test_missing_actor_does_not_create_dependency(two_cmt):
    app = start(two_cmt)
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("")
    app = add_dependency(app, two_cmt)
    assert app.error and not app.exception
    assert app.session_state.multi_quote_drafts[two_cmt.draft_id] == two_cmt
