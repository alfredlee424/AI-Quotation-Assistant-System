"""真實 Streamlit：明確開啟、即時重算、撤回與失敗無舊下載。"""
from dataclasses import replace
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from quote_batch_fixtures import four_items
from test_multi_trial_ui import widget
from utils import work_order_review_summary_ui as ui


ENABLE = "顯示工單核對去識別化摘要（非正式）"
DOWNLOAD = "下載工單核對去識別化摘要（非正式）"


def workspace():
    import streamlit as st
    from utils.work_order_review_summary_ui import render_work_order_review_summary
    render_work_order_review_summary(st, st.session_state.draft,
                                    st.session_state.batch, st.session_state.actor)


def start():
    batch, draft = four_items()
    app = AppTest.from_function(workspace)
    app.session_state.batch = batch
    app.session_state.draft = draft
    app.session_state.actor = "測試"
    return app.run(), batch, draft


def enabled(app):
    return widget(app, "checkbox", ENABLE).check().run()


def downloads(app):
    return [d for d in app.get("download_button") if d.label == DOWNLOAD]


def test_opt_in_recompute_and_withdraw(monkeypatch):
    spy = Mock(wraps=ui.build_work_order_review_summary)
    monkeypatch.setattr(ui, "build_work_order_review_summary", spy)
    app, batch, draft = start()
    spy.assert_not_called()
    assert not app.exception and not downloads(app)
    enabled(app)
    assert not app.exception and not app.error and len(downloads(app)) == 1
    spy.assert_called_once()
    app.run()
    assert spy.call_count == 2
    widget(app, "checkbox", ENABLE).uncheck().run()
    assert spy.call_count == 2 and not downloads(app)
    assert app.session_state.batch == batch and app.session_state.draft == draft


@pytest.mark.parametrize("error", [ValueError("SECRET-SOURCE"), RuntimeError("SECRET-HOST")])
def test_failure_hides_old_success_and_sanitizes_errors(error, monkeypatch):
    app, _, _ = start()
    enabled(app)
    assert downloads(app)
    monkeypatch.setattr(ui, "build_work_order_review_summary", Mock(side_effect=error))
    app.run()
    assert not app.exception and not downloads(app) and not app.json
    assert [e.value for e in app.error] == [ui.ERROR]


@pytest.mark.parametrize("change", ["actor", "source", "archive", "manual"])
def test_changed_context_never_shows_old_summary(change):
    app, batch, draft = start()
    enabled(app)
    if change == "actor":
        app.session_state.actor = ""
    elif change == "source":
        app.session_state.batch = replace(batch, revision=batch.revision + 1)
    elif change == "archive":
        app.session_state.draft = replace(draft, lines=draft.lines[1:], archived_lines=draft.lines[:1])
    else:
        from agent.multi_quote import new_multi_quote
        app.session_state.draft = new_multi_quote()
    app.run()
    assert not app.exception and not downloads(app) and not app.json


def test_updated_draft_is_recomputed_not_reused(monkeypatch):
    app, _, draft = start()
    enabled(app)
    spy = Mock(wraps=ui.build_work_order_review_summary)
    monkeypatch.setattr(ui, "build_work_order_review_summary", spy)
    first = replace(draft.lines[0], configuration={**draft.lines[0].configuration, "qty": None})
    changed = replace(draft, lines=(first, *draft.lines[1:]))
    app.session_state.draft = changed
    app.run()
    assert not app.exception and downloads(app)
    assert spy.call_args.args[0] == changed
    assert '"children_with_pending_quantity": 1' in app.json[0].value


def test_summary_is_integrated_in_real_multi_quote_workspace(monkeypatch):
    from test_quote_batch_ui import start as start_full
    batch, draft = four_items()
    spy = Mock(wraps=ui.build_work_order_review_summary)
    monkeypatch.setattr(ui, "build_work_order_review_summary", spy)
    app = start_full(batch, draft)
    assert not app.exception and widget(app, "checkbox", ENABLE).disabled
    spy.assert_not_called()
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("測試").run()
    enabled(app)
    assert not app.exception and downloads(app)
    assert app.session_state.multi_quote_drafts[draft.draft_id] == draft
    assert app.session_state.current_quote == {"marker": "untouched"}


def test_serialization_failure_has_no_partial_summary(monkeypatch):
    from types import SimpleNamespace
    app, _, _ = start()
    enabled(app)
    monkeypatch.setattr(ui, "json", SimpleNamespace(dumps=Mock(side_effect=ValueError("SECRET"))))
    app.run()
    assert not app.exception and not app.json and not downloads(app)
    assert [e.value for e in app.error] == [ui.ERROR]


def test_download_contains_only_summary_and_fixed_filename():
    import json
    batch, draft = four_items()
    st = Mock()
    st.checkbox.return_value = True
    ui.render_work_order_review_summary(st, draft, batch, "測試")
    call = st.download_button.call_args
    assert call.kwargs["file_name"] == "work-order-review-summary.json"
    payload = json.loads(call.args[1])
    assert payload["source"]["item_count"] == 4
    assert "source_ref_raw" not in call.args[1] and draft.draft_id not in call.args[1]
