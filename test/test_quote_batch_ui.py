"""Streamlit 真實工作區的顯式展開、完整文字與失效清除。"""
from dataclasses import replace
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from agent import quote_batch as core
from quote_batch_fixtures import SHARED, four_items
from test_multi_trial_ui import widget, workspace
from utils import quote_batch_ui as ui


def start(batch, draft):
    app = AppTest.from_function(workspace)
    app.session_state.multi_quote_drafts = {draft.draft_id: draft}
    app.session_state.multi_selected = draft.draft_id
    app.session_state.current_quote = {"marker": "untouched"}
    app.session_state.work_order_batch = batch
    return app.run()


def run(app):
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("測試").run()
    return widget(app, "button", "產生批次逐張展開核對（不配號、不計價）").click().run()


def test_explicit_four_child_review_and_full_downloads(monkeypatch, isolated_db):
    from database import repository as repo
    from database.models import QuoteSnapshotDocument, ordqdt_ai
    batch, draft = four_items()
    spy = Mock(wraps=ui.build_quote_batch)
    monkeypatch.setattr(ui, "build_quote_batch", spy)
    app = start(batch, draft)
    assert not app.exception
    spy.assert_not_called()
    assert widget(app, "button", "產生批次逐張展開核對（不配號、不計價）").disabled
    monkeypatch.setattr(repo, "_session", Mock(side_effect=AssertionError("禁止主檔查詢")))
    app = run(app)
    assert not app.exception and not app.error
    spy.assert_called_once()
    assert len(app.session_state.quote_batch_expansions[draft.draft_id]["children"]) == 4
    for shared in SHARED:
        assert sum(t.value == shared for t in app.text) == 4
    downloads = [d.label for d in app.get("download_button")]
    assert "下載整批展開核對（非正式、無金額）" in downloads
    assert sum(label.startswith("下載第 ") for label in downloads) == 4
    assert app.session_state.multi_quote_drafts[draft.draft_id] == draft
    assert app.session_state.work_order_batch == batch
    assert app.session_state.current_quote == {"marker": "untouched"}
    assert not any("確認建立" in b.label or "配發" in b.label for b in app.button)
    app.run()
    assert spy.call_count == 1
    repo._session.assert_not_called()
    with isolated_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == db.query(ordqdt_ai).count() == 0


@pytest.mark.parametrize("kind", ["actor", "revision", "reorder", "source", "version", "tamper", "qty"])
def test_stale_result_removed_and_download_hidden(kind, monkeypatch):
    batch, draft = four_items()
    app = run(start(batch, draft))
    assert draft.draft_id in app.session_state.quote_batch_expansions
    if kind == "actor":
        widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("另一人")
    elif kind == "revision":
        app.session_state.multi_quote_drafts = {draft.draft_id: replace(draft, revision=draft.revision + 1)}
    elif kind == "reorder":
        app.session_state.multi_quote_drafts = {draft.draft_id: replace(draft, lines=tuple(reversed(draft.lines)))}
    elif kind == "source":
        app.session_state.work_order_batch = replace(batch, revision=batch.revision + 1)
    elif kind == "version":
        monkeypatch.setattr(core, "DOCUMENT_VERSION", "changed")
    elif kind == "qty":
        first = replace(draft.lines[0], configuration={**draft.lines[0].configuration, "qty": 10})
        app.session_state.multi_quote_drafts = {draft.draft_id: replace(draft, lines=(first, *draft.lines[1:]))}
    else:
        stored = dict(app.session_state.quote_batch_expansions)
        stored[draft.draft_id]["children"].pop()
        app.session_state.quote_batch_expansions = stored
    app.run()
    assert not app.exception
    assert draft.draft_id not in app.session_state.quote_batch_expansions
    assert not any(d.label.startswith("下載第 ") or "下載整批展開" in d.label for d in app.get("download_button"))


@pytest.mark.parametrize("error", [ValueError("核對失敗"), RuntimeError("SECRET-INTERNAL-HOST")])
def test_failed_operation_clears_previous_result(error, monkeypatch):
    batch, draft = four_items()
    app = run(start(batch, draft))
    monkeypatch.setattr(ui, "build_quote_batch", Mock(side_effect=error))
    widget(app, "button", "產生批次逐張展開核對（不配號、不計價）").click().run()
    assert not app.exception and app.error
    assert draft.draft_id not in app.session_state.quote_batch_expansions
    assert all("SECRET" not in e.value for e in app.error)
    assert not any("下載整批展開" in d.label for d in app.get("download_button"))


def test_unknown_quantities_shown_without_candidate_approval():
    batch, draft = four_items(quantities=False, reviewed=False)
    app = run(start(batch, draft))
    assert not app.exception
    assert sum("成品數 待確認" in t.value for t in app.text) == 4
    assert any("成品數未知" in w.value for w in app.warning)
    assert not app.session_state.quote_batch_expansions[draft.draft_id]["expansion_review_complete"]


def test_archived_item_blocks_all_children():
    batch, draft = four_items()
    draft = replace(draft, lines=draft.lines[1:], archived_lines=(draft.lines[0],))
    app = run(start(batch, draft))
    assert not app.exception
    assert any("封存" in e.value for e in app.error)
    assert draft.draft_id not in app.session_state.quote_batch_expansions


def test_separate_drafts_from_same_source_do_not_share_ids():
    batch, draft = four_items()
    from agent.multi_quote import from_work_order
    other = from_work_order(batch, batch.orders[0].order_id)
    app = run(start(batch, draft))
    old = app.session_state.quote_batch_expansions[draft.draft_id]
    app.session_state.multi_quote_drafts = {draft.draft_id: draft, other.draft_id: other}
    app.run()
    widget(app, "selectbox", "選擇配置草稿").set_value(other.draft_id).run()
    assert not any("下載整批展開" in d.label for d in app.get("download_button"))
    app = run(app)
    new = app.session_state.quote_batch_expansions[other.draft_id]
    assert old["quote_batch_id"] != new["quote_batch_id"]
    assert not {c["child_quote_id"] for c in old["children"]} & {c["child_quote_id"] for c in new["children"]}
