"""真實內部試算 UI：唯讀、顯式操作、失效保護與無正式確認。"""
from unittest.mock import Mock

from streamlit.testing.v1 import AppTest

from test_multi_trial import two_cmt, command
from utils import multi_trial_ui as ui


def workspace():
    import streamlit as st
    from utils.multi_quote_ui import render_multi_quote_workspace
    render_multi_quote_workspace(st)


def widget(app, kind, label):
    return next(element for element in getattr(app, kind) if element.label == label)


def start(draft):
    app = AppTest.from_function(workspace)
    app.session_state.multi_quote_drafts = {draft.draft_id: draft}
    app.session_state.multi_selected = draft.draft_id
    app.session_state.current_quote = {"marker": "existing-single"}
    app.run()
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("測試")
    return app


def run_trial(app):
    widget(app, "checkbox", "了解僅供內部核對，不可作為客戶報價").check().run()
    return widget(app, "button", "產生多明細內部試算（唯讀主檔）").click().run()


def test_explicit_trial_does_not_change_drafts_or_offer_confirmation(two_cmt, monkeypatch):
    spy = Mock(wraps=ui.build_internal_trial)
    monkeypatch.setattr(ui, "build_internal_trial", spy)
    app = start(two_cmt)
    spy.assert_not_called()
    assert widget(app, "button", "產生多明細內部試算（唯讀主檔）").disabled
    app = run_trial(app)
    assert not app.exception and not app.error
    assert app.session_state.multi_quote_drafts[two_cmt.draft_id] == two_cmt
    assert app.session_state.current_quote == {"marker": "existing-single"}
    assert app.session_state.multi_internal_trials[two_cmt.draft_id]["totals"] is not None
    assert not any("確定建立" in button.label or "確認建立正式" in button.label for button in app.button)
    app.run()
    assert spy.call_count == 1


def test_partial_result_explicitly_omits_order_total(two_cmt):
    draft = command(two_cmt, {"op": "add", "label": "尚未指定的明細"})
    app = run_trial(start(draft))
    assert not app.exception
    assert any("不提供整單合計" in element.value for element in app.error)
    document = app.session_state.multi_internal_trials[draft.draft_id]
    assert document["calculated_count"] == 2 and document["totals"] is None


def test_discount_edit_clears_old_trial_without_auto_recalculation(two_cmt):
    app = run_trial(start(two_cmt))
    widget(app, "number_input", "整單折扣率（0.1 表示九折）").set_value(0.1)
    widget(app, "text_input", "整單折扣調整理由").set_value("核對整單折扣")
    widget(app, "button", "保存整單折扣（不試算）").click().run()
    assert not app.exception and not app.error
    assert app.session_state.multi_quote_drafts[two_cmt.draft_id].discount_rate == 0.1
    assert two_cmt.draft_id not in app.session_state.multi_internal_trials


def test_actor_change_and_failed_retry_clear_old_trial(two_cmt, monkeypatch):
    app = run_trial(start(two_cmt))
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("另一位").run()
    assert two_cmt.draft_id not in app.session_state.multi_internal_trials
    app = run_trial(app)
    monkeypatch.setattr(ui, "build_internal_trial", Mock(side_effect=RuntimeError("secret-host")))
    widget(app, "button", "產生多明細內部試算（唯讀主檔）").click().run()
    assert app.error and not app.exception
    assert two_cmt.draft_id not in app.session_state.multi_internal_trials
    assert all("secret-host" not in element.value for element in app.error)
