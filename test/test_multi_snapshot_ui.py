"""關聯演練的顯式操作、舊結果清除與非正式界線。"""
from unittest.mock import Mock

from test_multi_trial import two_cmt, command
from test_multi_trial_ui import start, widget, run_trial
from utils import multi_snapshot_ui as ui


def run_layout(app):
    return widget(app, "button", "產生快照關聯演練（不查價、不保存）").click().run()


def test_explicit_operation_uses_frozen_trial_without_querying(two_cmt, monkeypatch):
    spy = Mock(wraps=ui.build_snapshot_layout)
    monkeypatch.setattr(ui, "build_snapshot_layout", spy)
    app = start(two_cmt)
    assert not any(b.label == "產生快照關聯演練（不查價、不保存）" for b in app.button)
    app = run_trial(app)
    spy.assert_not_called()
    from database import repository as repo
    monkeypatch.setattr(repo, "_session", Mock(side_effect=AssertionError("不可再次查價")))
    app = run_layout(app)
    assert not app.exception and not app.error
    assert app.session_state.multi_snapshot_layouts[two_cmt.draft_id]["layout_complete"]
    assert app.session_state.multi_quote_drafts[two_cmt.draft_id] == two_cmt
    assert app.session_state.current_quote == {"marker": "existing-single"}
    app.run()
    assert spy.call_count == 1
    repo._session.assert_not_called()
    assert not any("確定建立" in b.label for b in app.button)


def test_new_trial_and_actor_change_clear_layout(two_cmt):
    app = run_layout(run_trial(start(two_cmt)))
    assert two_cmt.draft_id in app.session_state.multi_snapshot_layouts
    app = run_trial(app)
    assert two_cmt.draft_id not in app.session_state.multi_snapshot_layouts
    app = run_layout(app)
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("另一位").run()
    assert two_cmt.draft_id not in app.session_state.multi_snapshot_layouts
    assert two_cmt.draft_id not in app.session_state.multi_internal_trials


def test_partial_layout_is_explicit(two_cmt):
    draft = command(two_cmt, {"op": "add", "label": "未指定配置"})
    app = run_layout(run_trial(start(draft)))
    assert not app.exception
    result = app.session_state.multi_snapshot_layouts[draft.draft_id]
    assert not result["layout_complete"] and result["excluded_lines"] and result["totals"] is None
    assert any("關聯演練未完整" in w.value for w in app.warning)


def test_failure_removes_prior_layout_and_hides_internal_error(two_cmt, monkeypatch):
    app = run_layout(run_trial(start(two_cmt)))
    monkeypatch.setattr(ui, "build_snapshot_layout", Mock(side_effect=RuntimeError("SECRET-DB-HOST")))
    app = run_layout(app)
    assert not app.exception and app.error
    assert two_cmt.draft_id not in app.session_state.multi_snapshot_layouts
    assert all("SECRET-DB-HOST" not in e.value for e in app.error)


def test_long_actor_is_reported_not_truncated(two_cmt):
    actor = "操作人名稱超過八個字"
    app = start(two_cmt)
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value(actor)
    app = run_layout(run_trial(app))
    assert not app.exception and app.error
    result = app.session_state.multi_snapshot_layouts[two_cmt.draft_id]
    assert result["actor"] == actor and not result["layout_complete"]
    assert any("addusrno" in e.value for e in app.error)
