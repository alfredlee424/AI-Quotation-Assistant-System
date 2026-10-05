"""真實 Streamlit 多明細工作區：固定建立、兩次獨立操作、失效及下載。"""
from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from engine import quote_batch_review as core, quote_batch_trial as trial_core
from utils import quote_batch_review_ui as ui, quote_batch_trial_ui as trial_ui
from quote_batch_trial_fixtures import priced_four
from quote_batch_fixtures import SHARED
from test_quote_batch_ui import start
from test_quote_batch_trial_ui import run as run_trial, RUN
from test_multi_trial_ui import widget
from test_quote_batch_review import forbid_side_effects, assert_nonformal


def ready(case):
    return run_trial(start(*case))


def create(app):
    return widget(app, "button", ui.CREATE).click().run()


def stored(app, draft):
    return app.session_state.quote_batch_reviews[draft.draft_id]


def downloads(app):
    return [d for d in app.get("download_button") if d.label == ui.DOWNLOAD]


def test_no_operation_no_freeze_or_external_calls(priced_four, monkeypatch):
    app = ready(priced_four)
    freeze = Mock(side_effect=AssertionError("不可自動固定"))
    monkeypatch.setattr(core.BatchReviewSession, "freeze", freeze)
    spies = forbid_side_effects(monkeypatch)
    app.run()
    assert not app.exception and not app.error and not downloads(app)
    assert not app.session_state.quote_batch_reviews
    freeze.assert_not_called()
    for spy in spies: spy.assert_not_called()


def test_missing_trial_does_not_generate_one(priced_four, monkeypatch):
    app = start(*priced_four)
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("測試").run()
    spies = forbid_side_effects(monkeypatch)
    create(app)
    assert not app.exception and app.error and not downloads(app)
    assert not app.session_state.quote_batch_reviews
    for spy in spies: spy.assert_not_called()


def test_four_complete_content_two_separate_steps_and_download_history(priced_four, monkeypatch):
    import streamlit as st
    batch, draft = priced_four
    app = ready(priced_four)
    spies = forbid_side_effects(monkeypatch)
    captured = []
    real_download = st.download_button
    def capture(label, data, *args, **kwargs):
        if label == ui.DOWNLOAD: captured.append(json.loads(data))
        return real_download(label, data, *args, **kwargs)
    monkeypatch.setattr(st, "download_button", capture)
    create(app)
    assert not app.exception and not app.error and len(downloads(app)) == 1
    original = deepcopy(stored(app, draft))
    assert original["review_state"]["stage"] == 0
    assert widget(app, "button", ui.SECOND).disabled and not widget(app, "button", ui.FIRST).disabled
    assert captured[-1] == original
    for shared in SHARED:
        assert sum(t.value.startswith("固定描述｜") and t.value.endswith("｜" + shared) for t in app.text) == 4
    assert sum(w.value.startswith("PENDING｜") for w in app.warning) == 8
    assert original["content"]["trial"]["totals"]["product_qty"] == 11
    app.run()
    assert stored(app, draft) == original  # 重繪不改 UUID、時間或 token。
    widget(app, "button", ui.FIRST).click().run()
    assert not app.exception and not app.error
    first = deepcopy(stored(app, draft))
    assert first["review_state"]["stage"] == 1 and len(first["review_state"]["history"]) == 1
    assert widget(app, "button", ui.FIRST).disabled and not widget(app, "button", ui.SECOND).disabled
    assert captured[-1] == first
    app.run()
    assert stored(app, draft) == first
    widget(app, "button", ui.SECOND).click().run()
    second = deepcopy(stored(app, draft))
    assert not app.exception and not app.error
    assert second["review_state"]["stage"] == 2 and len(second["review_state"]["history"]) == 2
    assert widget(app, "button", ui.FIRST).disabled and widget(app, "button", ui.SECOND).disabled
    assert captured[-1] == second
    assert second["content"] == first["content"] == original["content"]
    assert app.session_state.current_quote == {"marker": "untouched"}
    assert app.session_state.multi_quote_drafts[draft.draft_id] == draft
    assert app.session_state.work_order_batch == batch
    assert not any("正式保存" in b.label or "配號保留" in b.label for b in app.button)
    assert_nonformal(second)
    for spy in spies: spy.assert_not_called()


@pytest.mark.parametrize("stage", [1, 2])
@pytest.mark.parametrize("failure", ["record", "create", "download"])
def test_failure_removes_old_success_and_confirmation(priced_four, monkeypatch, stage, failure):
    batch, draft = priced_four
    app = create(ready(priced_four))
    widget(app, "button", ui.FIRST).click().run()
    if stage == 2: widget(app, "button", ui.SECOND).click().run()
    original = deepcopy(stored(app, draft))
    target = {"record": "record", "create": "freeze", "download": "download"}[failure]
    monkeypatch.setattr(core.BatchReviewSession, target, Mock(side_effect=RuntimeError("SECRET-HOST")))
    if failure == "record":
        if stage == 2:
            # 完成狀態沒有可操作核對鈕；先建立新的第一階段，不繞過 disabled。
            create(app)
            widget(app, "button", ui.FIRST).click().run()
        else: widget(app, "button", ui.SECOND).click().run()
    elif failure == "create": create(app)
    else: app.run()
    assert not app.exception and app.error
    assert draft.draft_id not in app.session_state.quote_batch_reviews
    assert draft.draft_id not in app.session_state.quote_batch_review_sessions
    assert not downloads(app) and not any(b.label in (ui.FIRST, ui.SECOND) for b in app.button)
    assert not any("SECRET" in e.value for e in app.error)
    assert original["review_state"]["stage"] == stage


@pytest.mark.parametrize("kind", ["new_trial", "trial_failure", "actor", "revision", "order", "source", "discount", "review_policy", "tamper"])
def test_stale_ui_clears_two_tokens_and_old_download(priced_four, monkeypatch, kind):
    batch, draft = priced_four
    app = create(ready(priced_four))
    widget(app, "button", ui.FIRST).click().run()
    original = deepcopy(stored(app, draft))
    controller = app.session_state.quote_batch_review_sessions[draft.draft_id]
    old_trial = deepcopy(app.session_state.quote_batch_trials[draft.draft_id])
    if kind == "new_trial": widget(app, "button", RUN).click().run()
    elif kind == "trial_failure":
        monkeypatch.setattr(trial_ui, "build_batch_trial", Mock(side_effect=ValueError("試算拒絕")))
        widget(app, "button", RUN).click().run()
    elif kind == "actor": widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("另一人").run()
    elif kind == "source":
        app.session_state.work_order_batch = replace(batch, revision=batch.revision + 1)
        app.run()
    elif kind == "review_policy":
        monkeypatch.setattr(core, "REVIEW_VERSION", "changed")
        app.run()
    elif kind == "tamper":
        changed = deepcopy(original)
        changed["content"]["trial"]["children"].pop()
        app.session_state.quote_batch_reviews = {draft.draft_id: changed}
        app.run()
    elif kind == "discount":
        widget(app, "number_input", "批次共同折扣率（0.1 表示每張九折）").set_value(.1)
        widget(app, "text_input", "批次共同折扣調整理由").set_value("合成測試折扣")
        widget(app, "button", "設定批次共同折扣（不試算）").click().run()
    else:
        changed = replace(draft, revision=draft.revision + 1) if kind == "revision" else replace(draft, lines=tuple(reversed(draft.lines)))
        app.session_state.multi_quote_drafts = {draft.draft_id: changed}
        app.run()
    assert not app.exception and not downloads(app)
    assert draft.draft_id not in app.session_state.quote_batch_reviews
    assert draft.draft_id not in app.session_state.quote_batch_review_sessions
    monkeypatch.undo()
    with pytest.raises(ValueError): controller.checked(original, draft, old_trial, actor="測試", source_batch=batch)


def test_incomplete_trial_no_review_or_two_step_buttons(priced_four):
    batch, draft = deepcopy(priced_four)
    draft.lines[0].configuration["questions"] = ["尚缺規格"]
    app = create(ready((batch, draft)))
    assert not app.exception and app.error and not downloads(app)
    assert draft.draft_id not in app.session_state.quote_batch_reviews
    assert not any(b.label in (ui.FIRST, ui.SECOND) for b in app.button)


def test_switch_draft_isolated_and_original_not_recreated(priced_four):
    from agent.multi_quote import from_work_order
    batch, draft = priced_four
    app = create(ready(priced_four))
    widget(app, "button", ui.FIRST).click().run()
    original = deepcopy(stored(app, draft))
    other = from_work_order(batch, batch.orders[0].order_id)
    app.session_state.multi_quote_drafts = {draft.draft_id: draft, other.draft_id: other}
    app.run()
    widget(app, "selectbox", "選擇配置草稿").set_value(other.draft_id).run()
    assert not app.exception and not downloads(app)
    assert not any(b.label in (ui.FIRST, ui.SECOND) for b in app.button)
    widget(app, "selectbox", "選擇配置草稿").set_value(draft.draft_id).run()
    # Streamlit 可能清理離開畫面的 actor widget；無論是否保留，都不能帶入他批核對。
    if draft.draft_id in app.session_state.quote_batch_reviews:
        assert stored(app, draft) == original
    else:
        assert not downloads(app)
