"""真實工作區：明確試算、折扣稽核、下載與失敗清除，不增加自動查價。"""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from database import repository as repo
from engine import quote_batch_trial as core
from utils import quote_batch_trial_ui as ui
from quote_batch_trial_fixtures import priced_pair, priced_four, mixed_products
from quote_batch_fixtures import SHARED
from test_quote_batch_ui import start
from test_multi_trial_ui import widget

RUN = "產生批次逐張獨立金額試算（唯讀主檔）"
CONSENT = "了解批次逐張試算僅供非正式內部核對，不可向客戶確認"


def run(app):
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("測試").run()
    widget(app, "checkbox", CONSENT).check().run()
    return widget(app, "button", RUN).click().run()


def downloads(app):
    return [d for d in app.get("download_button") if "非正式金額核對" in d.label]


def test_no_consent_no_read_and_repaint_no_lookup(priced_pair, monkeypatch):
    batch, draft = priced_pair
    spy = Mock(wraps=ui.build_batch_trial)
    monkeypatch.setattr(ui, "build_batch_trial", spy)
    queries = Mock(side_effect=AssertionError("未按試算不應查價"))
    monkeypatch.setattr(repo, "_session", queries)
    app = start(batch, draft)
    assert not app.exception and widget(app, "button", RUN).disabled
    app.run()
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("測試").run()
    assert widget(app, "button", RUN).disabled
    widget(app, "checkbox", CONSENT).check().run()
    assert not widget(app, "button", RUN).disabled
    spy.assert_not_called()
    queries.assert_not_called()
    assert not downloads(app)


def test_four_full_child_documents_and_amounts(priced_four, monkeypatch):
    batch, draft = priced_four
    spy = Mock(wraps=ui.build_batch_trial)
    monkeypatch.setattr(ui, "build_batch_trial", spy)
    app = run(start(batch, draft))
    assert not app.exception and not app.error
    doc = app.session_state.quote_batch_trials[draft.draft_id]
    assert doc["calculated_count"] == 4 and doc["totals"]["product_qty"] == 11
    assert len(downloads(app)) == 5
    for shared in SHARED:
        assert sum(t.value.endswith("｜" + shared) and "RECORDED_SCOPE" in t.value for t in app.text) == 4
    assert app.session_state.multi_quote_drafts[draft.draft_id] == draft
    assert app.session_state.current_quote == {"marker": "untouched"}
    assert app.session_state.work_order_batch == batch
    assert not any("確認建立" in b.label or "配發" in b.label for b in app.button)
    queries = Mock(side_effect=AssertionError("展示不可重算"))
    monkeypatch.setattr(repo, "_session", queries)
    app.run()
    assert not app.exception and len(downloads(app)) == 5
    assert app.session_state.quote_batch_trials[draft.draft_id] == doc
    spy.assert_called_once()
    queries.assert_not_called()


@pytest.mark.parametrize("error", [ValueError("合成拒絕"), RuntimeError("SECRET-HOST")])
def test_retry_failure_clears_previous_success(priced_pair, monkeypatch, error):
    batch, draft = priced_pair
    app = run(start(batch, draft))
    assert draft.draft_id in app.session_state.quote_batch_trials
    monkeypatch.setattr(ui, "build_batch_trial", Mock(side_effect=error))
    widget(app, "button", RUN).click().run()
    assert not app.exception and app.error
    assert draft.draft_id not in app.session_state.quote_batch_trials
    assert not downloads(app)
    assert all("SECRET" not in e.value for e in app.error)


def test_failed_child_replaces_old_full_totals(priced_pair, monkeypatch):
    batch, draft = priced_pair
    app = run(start(batch, draft))
    previous = app.session_state.quote_batch_trials[draft.draft_id]
    monkeypatch.setattr(core, "_configuration_cost_inputs", Mock(side_effect=RuntimeError("SECRET")))
    widget(app, "button", RUN).click().run()
    doc = app.session_state.quote_batch_trials[draft.draft_id]
    assert not app.exception and app.error
    assert doc["trial_id"] != previous["trial_id"]
    assert doc["totals"] is None and len(doc["children"]) == 2
    assert all(c["calculation"] is None for c in doc["children"])
    assert len(downloads(app)) == 3  # 可下載失敗原因，但不能殘留舊成功值。


@pytest.mark.parametrize("kind", ["actor", "revision", "reorder", "qty", "source", "policy", "tamper"])
def test_stale_result_hidden_and_cleared(priced_pair, monkeypatch, kind):
    batch, draft = deepcopy(priced_pair)
    app = run(start(batch, draft))
    if kind == "actor": widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("另一人")
    elif kind == "source": app.session_state.work_order_batch = replace(batch, revision=batch.revision + 1)
    elif kind == "policy": monkeypatch.setattr(core, "POLICY_VERSION", "changed")
    elif kind == "tamper":
        store = dict(app.session_state.quote_batch_trials)
        store[draft.draft_id]["totals"]["total_price"] = "0.01"
        app.session_state.quote_batch_trials = store
    else:
        if kind == "revision": draft = replace(draft, revision=draft.revision + 1)
        elif kind == "reorder": draft = replace(draft, lines=tuple(reversed(draft.lines)))
        else: draft.lines[0].configuration["qty"] = 4
        app.session_state.multi_quote_drafts = {draft.draft_id: draft}
    app.run()
    assert not app.exception
    assert draft.draft_id not in app.session_state.quote_batch_trials
    assert not downloads(app)


def test_discount_uses_command_audit_clears_results_without_query(priced_pair, monkeypatch):
    batch, draft = priced_pair
    app = run(start(batch, draft))
    # 同時產生原描述核對，設定折扣後兩種舊結果均應清除。
    widget(app, "button", "產生批次逐張展開核對（不配號、不計價）").click().run()
    queries = Mock(side_effect=AssertionError("設定折扣不得自動試算"))
    monkeypatch.setattr(repo, "_session", queries)
    widget(app, "number_input", "批次共同折扣率（0.1 表示每張九折）").set_value(.1)
    widget(app, "text_input", "批次共同折扣調整理由").set_value("合成共同折扣核對")
    widget(app, "button", "設定批次共同折扣（不試算）").click().run()
    assert not app.exception and not app.error
    updated = app.session_state.multi_quote_drafts[draft.draft_id]
    assert updated.discount_rate == .1 and updated.revision == draft.revision + 1
    assert updated.lines == draft.lines and updated.changes[-1].operation == "set_discount"
    assert draft.draft_id not in app.session_state.quote_batch_trials
    assert draft.draft_id not in app.session_state.quote_batch_expansions
    assert not downloads(app)
    queries.assert_not_called()


def test_bad_discount_reason_keeps_draft_unchanged(priced_pair):
    batch, draft = priced_pair
    app = run(start(batch, draft))
    widget(app, "number_input", "批次共同折扣率（0.1 表示每張九折）").set_value(.1)
    widget(app, "button", "設定批次共同折扣（不試算）").click().run()
    assert not app.exception and app.error
    assert app.session_state.multi_quote_drafts[draft.draft_id] == draft


def test_switch_draft_hides_other_result(priced_pair):
    from agent.multi_quote import from_work_order
    batch, draft = priced_pair
    app = run(start(batch, draft))
    other = from_work_order(batch, batch.orders[0].order_id)
    app.session_state.multi_quote_drafts = {draft.draft_id: draft, other.draft_id: other}
    app.run()
    widget(app, "selectbox", "選擇配置草稿").set_value(other.draft_id).run()
    assert not app.exception and not downloads(app)


def test_preserves_description_only_ui_no_amount_queries(priced_pair, monkeypatch):
    batch, draft = priced_pair
    queries = Mock(side_effect=AssertionError("描述展開不可查價"))
    monkeypatch.setattr(repo, "_session", queries)
    app = start(batch, draft)
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("測試").run()
    widget(app, "button", "產生批次逐張展開核對（不配號、不計價）").click().run()
    assert not app.exception and not app.error
    assert draft.draft_id in app.session_state.quote_batch_expansions
    assert not downloads(app)
    queries.assert_not_called()
