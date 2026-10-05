"""非正式固定核對；正例保留全部正式阻擋，不假裝工程／身份核准。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
import json
from unittest.mock import Mock

import pytest

from agent import quote_batch
from agent.drawing_review import DrawingDependency
from database import repository as repo
from engine import quote_batch_review as core, quote_batch_trial as trial_core
from quote_batch_fixtures import SHARED, condition
from quote_batch_trial_fixtures import priced_four, review_all


@pytest.fixture
def fixed_case(priced_four):
    batch, draft = priced_four
    trial = trial_core.build_batch_trial(draft, actor="測試", source_batch=batch)
    session = core.BatchReviewSession()
    document = session.freeze(draft, trial, actor="測試", source_batch=batch)
    return batch, draft, trial, session, document


def check(case, document=None, **kwargs):
    batch, draft, trial, session, original = case
    return session.checked(original if document is None else document, draft, trial,
                           actor="測試", source_batch=batch, **kwargs)


def record(case, document, step, token=None):
    batch, draft, trial, session, _ = case
    return session.record(document, draft, trial, actor="測試", source_batch=batch,
                          step=step, token=document["review_state"]["next_token"] if token is None else token)


def assert_nonformal(document):
    assert all(document[key] is False for key in ("can_quote", "can_confirm", "can_save"))
    trial = document["content"]["trial"]
    for part in (trial, *trial["children"]):
        assert all(part[key] is False for key in ("can_quote", "can_confirm", "can_save"))
        assert part["ref_no"] is None and part["formal_suffix"] is None
        assert part["formal_blockers"]


def test_complete_four_eleven_fixed_evidence_and_pending_report(fixed_case):
    batch, draft, trial, session, doc = fixed_case
    content = doc["content"]
    assert quote_batch._json(content["trial"]) == quote_batch._json(trial)
    assert quote_batch._json(content["source_batch"]) == quote_batch._json(asdict(batch))
    assert trial["child_count"] == 4 and trial["totals"]["product_qty"] == 11
    assert [c["product_qty"] for c in trial["children"]] == [2, 1, 6, 2]
    assert len({c["child_quote_id"] for c in trial["children"]}) == 4
    assert draft.blockers  # 不移除來源／正式阻擋來湊正例。
    for child in content["trial"]["children"]:
        assert set(SHARED) <= {d["text"] for d in child["descriptions"]}
        assert child["configuration"] and child["calculation"]["cost_inputs"]
        assert child["calculation"]["items"] and child["calculation"]["rounding_differences"]
        assert all(isinstance(child["calculation"][key], str) for key in trial_core.MONEY_FIELDS)
    report = content["gate_report"]
    assert len(report["formal"]) == 8 and all(g["status"] == "PENDING" for g in report["formal"])
    assert report["formal_blockers"] == trial["formal_blockers"]
    assert report["internal"][1]["evidence"]["unmapped_selections"]  # 語意映射待辦不隱藏。
    assert report["report_digest"] == quote_batch._digest({k: v for k, v in report.items() if k != "report_digest"})
    assert check(fixed_case) == doc and check(fixed_case) is not doc
    assert_nonformal(doc)


def test_two_steps_are_separate_immutable_and_same_content(fixed_case):
    original = fixed_case[-1]
    before = deepcopy(original)
    with pytest.raises(ValueError): record(fixed_case, original, 2)
    assert original == before and check(fixed_case) == before
    first = record(fixed_case, original, 1)
    assert original == before and len(first["review_state"]["history"]) == 1
    with pytest.raises(ValueError, match="重試"): record(fixed_case, first, 1)
    assert check(fixed_case, first) == first
    with pytest.raises(ValueError): record(fixed_case, first, 2, original["review_state"]["next_token"])
    second = record(fixed_case, first, 2)
    assert len(first["review_state"]["history"]) == 1 and len(second["review_state"]["history"]) == 2
    assert second["review_state"]["next_token"] is None
    for doc in (first, second):
        assert doc["content"] == original["content"] and doc["content_digest"] == original["content_digest"]
        assert doc["state_digest"] != original["state_digest"]
        assert_nonformal(doc)
    assert len({e["event_id"] for e in second["review_state"]["history"]}) == 2
    for event in second["review_state"]["history"]:
        assert event["actor"] == "測試" and event["recorded_at"]
        assert event["trial_id"] == fixed_case[2]["trial_id"]
        assert event["trial_digest"] == fixed_case[2]["content_digest"]
    with pytest.raises(ValueError): record(fixed_case, first, 2)
    with pytest.raises(ValueError): record(fixed_case, second, 2)
    assert check(fixed_case, second) == second


@pytest.mark.parametrize("step", [0, 3, True, "1", None])
def test_bad_step_does_not_change_state(fixed_case, step):
    doc = fixed_case[-1]
    with pytest.raises(ValueError): record(fixed_case, doc, step)
    assert check(fixed_case) == doc and not doc["review_state"]["history"]


def test_parallel_same_state_has_only_one_event(fixed_case):
    def attempt(_):
        try: return record(fixed_case, fixed_case[-1], 1)
        except ValueError: return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    success = [r for r in results if r is not None]
    assert len(success) == 1 and len(success[0]["review_state"]["history"]) == 1
    assert check(fixed_case, success[0]) == success[0]


@pytest.mark.parametrize("kind", ["blocked", "unreviewed", "missing_ledger", "engineering", "no_qty"])
def test_incomplete_authentic_trial_cannot_freeze(priced_four, kind):
    batch, draft = deepcopy(priced_four)
    if kind == "blocked": draft.lines[0].configuration["questions"] = ["仍缺規格"]
    elif kind == "unreviewed": draft = replace(draft, requirements=tuple(replace(r, status="UNREVIEWED") for r in draft.requirements))
    elif kind == "missing_ledger": draft = replace(draft, requirements=draft.requirements[1:])
    elif kind == "engineering": draft = replace(draft, requirements=tuple(replace(r, status="NEEDS_ENGINEERING") for r in draft.requirements))
    else: draft.lines[0].configuration["qty"] = None
    trial = trial_core.build_batch_trial(draft, actor="測試", source_batch=batch)
    assert trial["totals"] is None
    with pytest.raises(ValueError): core.BatchReviewSession().freeze(draft, trial, actor="測試", source_batch=batch)


@pytest.mark.parametrize("kind", ["missing", "archived", "source_forged", "source_missing", "raw", "classification"])
def test_current_source_incomplete_rejects_creation(fixed_case, kind):
    batch, draft, trial, session, doc = fixed_case
    draft = deepcopy(draft)
    if kind == "missing": draft = replace(draft, lines=draft.lines[1:])
    elif kind == "archived": draft = replace(draft, lines=draft.lines[1:], archived_lines=(draft.lines[0],))
    elif kind == "source_forged": draft.source["source_ref_raw"] = "FORGED"
    elif kind == "source_missing": batch = None
    elif kind == "raw": batch = replace(batch, raw_text=batch.raw_text + "偽造")
    else: batch = replace(batch, lines=(replace(batch.lines[0], kind="unknown"), *batch.lines[1:]))
    with pytest.raises(ValueError): session.freeze(draft, trial, actor="測試", source_batch=batch)
    with pytest.raises(ValueError): check(fixed_case)  # 失敗不留舊成功。


@pytest.mark.parametrize("kind", ["revision", "qty", "selection", "order", "discount", "requirements", "evidence",
    "shared", "drawing", "source", "source_classification", "source_raw", "draft_source", "line_qty",
    "actor", "trial", "policy", "review_policy", "review_rules"])
@pytest.mark.parametrize("stage", [0, 1, 2])
def test_context_changes_invalidate_all_stages_and_cannot_revive(fixed_case, monkeypatch, kind, stage):
    batch, draft, trial, session, doc = fixed_case
    for step in range(1, stage + 1): doc = record(fixed_case, doc, step)
    changed, source, current, actor = deepcopy(draft), batch, trial, "測試"
    if kind == "revision": changed = replace(changed, revision=changed.revision + 1)
    elif kind == "qty": changed.lines[0].configuration["qty"] = 3
    elif kind == "line_qty": next(iter(changed.lines[0].configuration["selections"].values()))["line_qty"] = 3
    elif kind == "selection": next(iter(changed.lines[0].configuration["selections"].values()))["code"] = "CHANGED"
    elif kind == "order": changed = replace(changed, lines=tuple(reversed(changed.lines)))
    elif kind == "discount": changed = replace(changed, discount_rate=.1)
    elif kind == "requirements": changed = replace(changed, requirements=changed.requirements[1:])
    elif kind == "evidence":
        r = changed.requirements[0]
        changed = replace(changed, requirements=(replace(r, evidence=(replace(r.evidence[0], reference="changed"),)), *changed.requirements[1:]))
    elif kind == "shared": changed = condition(batch, changed, "hole_position", "REQUIRED", [changed.lines[0].line_id], changed.requirements[-1].requirement_id)
    elif kind == "drawing": changed = replace(changed, drawings=(DrawingDependency("d" * 32, (changed.lines[0].line_id,), (changed.requirements[-1].requirement_id,), "合成圖"),))
    elif kind == "source": source = replace(batch, revision=batch.revision + 1)
    elif kind == "source_classification": source = replace(batch, lines=(replace(batch.lines[0], kind="unknown"), *batch.lines[1:]))
    elif kind == "source_raw": source = replace(batch, raw_text=batch.raw_text + "改變")
    elif kind == "draft_source": changed.source["source_ref_raw"] = "改變"
    elif kind == "actor": actor = "另一人"
    elif kind == "trial": current = trial_core.build_batch_trial(draft, actor="測試", source_batch=batch)
    elif kind == "policy": monkeypatch.setattr(trial_core, "POLICY_VERSION", "changed")
    elif kind == "review_rules": monkeypatch.setattr(core, "FORMAL_PENDING", (("business", "changed"),))
    else: monkeypatch.setattr(core, "REVIEW_VERSION", "changed")
    with pytest.raises(ValueError): session.checked(doc, changed, current, actor=actor, source_batch=source)
    monkeypatch.undo()
    with pytest.raises(ValueError): session.checked(doc, draft, trial, actor="測試", source_batch=batch)


@pytest.mark.parametrize("kind", ["amount", "delete_child", "child_amount", "flag", "child_flag", "formal_blockers",
    "pending", "state", "history", "token", "type", "actor", "review_id", "order"])
def test_tamper_rehash_all_digests_still_rejected(fixed_case, kind):
    doc = deepcopy(fixed_case[-1])
    content = doc["content"]
    trial = content["trial"]
    if kind == "amount": trial["totals"]["total_price"] = "0.01"
    elif kind == "delete_child": trial["children"].pop()
    elif kind == "child_amount": trial["children"][0]["calculation"]["total_price"] = "0.01"
    elif kind == "flag": doc["can_confirm"] = True
    elif kind == "child_flag": trial["children"][0]["can_quote"] = True
    elif kind == "formal_blockers": content["gate_report"]["formal_blockers"] = []
    elif kind == "pending": content["gate_report"]["formal"] = []
    elif kind == "state": doc["review_state"]["stage"] = 2
    elif kind == "history": doc["review_state"]["history"].append({"step": 1})
    elif kind == "token": doc["review_state"]["next_token"] = "forged"
    elif kind == "type": doc["document_type"] = trial_core.DOCUMENT_TYPE
    elif kind == "actor": content["actor"] = "另一人"
    elif kind == "review_id": content["review_id"] = "f" * 32
    else: trial["children"].reverse()
    doc["content_digest"] = quote_batch._digest(content)
    doc["state_digest"] = quote_batch._digest({k: v for k, v in doc.items() if k not in ("state_digest", "process_seal")})
    with pytest.raises(ValueError): check(fixed_case, doc)


def test_cross_review_token_new_session_and_new_trial_replay(fixed_case):
    batch, draft, trial, session, doc = fixed_case
    first = record(fixed_case, doc, 1)
    new_doc = session.freeze(draft, trial, actor="測試", source_batch=batch)
    with pytest.raises(ValueError): record(fixed_case, new_doc, 1, first["review_state"]["next_token"])
    with pytest.raises(ValueError): record(fixed_case, first, 2)
    assert check(fixed_case, new_doc) == new_doc
    with pytest.raises(ValueError): core.BatchReviewSession().checked(new_doc, draft, trial, actor="測試", source_batch=batch)


def test_cross_batch_token_rejected(fixed_case):
    batch, draft, trial, session, doc = fixed_case
    from agent.multi_quote import from_work_order
    other = from_work_order(batch, batch.orders[0].order_id)
    other = replace(other, lines=deepcopy(draft.lines))
    other = review_all(batch, other)
    other_trial = trial_core.build_batch_trial(other, actor="測試", source_batch=batch)
    other_session = core.BatchReviewSession()
    other_doc = other_session.freeze(other, other_trial, actor="測試", source_batch=batch)
    first = record(fixed_case, doc, 1)
    other_first = other_session.record(other_doc, other, other_trial, step=1,
        token=other_doc["review_state"]["next_token"], actor="測試", source_batch=batch)
    with pytest.raises(ValueError): record(fixed_case, first, 2, other_first["review_state"]["next_token"])
    assert check(fixed_case, first) == first


def forbid_side_effects(monkeypatch):
    from engine import configuration, calculator, pricing, snapshot, preview, quote_number_reservation as numbering
    spies = []
    for owner, name in ((repo, "_session"), (repo, "save_quote_snapshot"),
        (configuration, "load_catalog"), (configuration, "resolve_configuration"),
        (trial_core, "resolve_configuration"), (trial_core, "build_batch_trial"),
        (trial_core, "calculate_quote"), (trial_core, "_configuration_cost_inputs"),
        (calculator, "calculate_quote"), (pricing, "calculate_configuration"),
        (snapshot, "build_snapshot_items"), (preview, "freeze_preview"),
        (numbering.QuoteNumberReservationService, "reserve"), (numbering, "reservation_schema")):
        spy = Mock(side_effect=AssertionError("禁止主檔／計算／快照／registry 呼叫"))
        monkeypatch.setattr(owner, name, spy)
        spies.append(spy)
    return spies


def test_freeze_display_record_download_no_master_calculator_registry_snapshot(fixed_case, monkeypatch):
    batch, draft, trial, session, old = fixed_case
    before = deepcopy((batch, draft, trial))
    spies = forbid_side_effects(monkeypatch)
    doc = session.freeze(draft, trial, actor="測試", source_batch=batch)
    for stage in (0, 1, 2):
        if stage: doc = record(fixed_case, doc, stage)
        assert check(fixed_case, doc) == doc
        downloaded = json.loads(session.download(doc, draft, trial, actor="測試", source_batch=batch))
        assert downloaded == doc and len(downloaded["review_state"]["history"]) == stage
    assert (batch, draft, trial) == before
    for spy in spies: spy.assert_not_called()


@pytest.mark.parametrize("target", ["preview", "confirm", "save", "snapshot", "trial", "description", "old_trial", "old_snapshot", "reservation"])
def test_new_document_rejected_by_all_other_entries(fixed_case, monkeypatch, target):
    from engine.preview import freeze_preview, checked_preview
    from engine.snapshot import build_snapshot_items
    from engine.multi_trial import checked_internal_trial
    from engine.multi_snapshot import build_snapshot_layout
    from engine.quote_number_reservation import QuoteNumberReservationService
    batch, draft, trial, session, doc = fixed_case
    spy = Mock(side_effect=AssertionError("不可開 DB"))
    monkeypatch.setattr(repo, "_session", spy)
    with pytest.raises(ValueError):
        if target == "preview": freeze_preview(doc)
        elif target == "confirm": checked_preview({"preview": doc, "status": "PREVIEW"}, "fake")
        elif target == "save": repo.save_quote_snapshot([], preview=doc)
        elif target == "snapshot": build_snapshot_items({}, doc, "fake")
        elif target == "trial": trial_core.checked_batch_trial(draft, doc, actor="測試", source_batch=batch)
        elif target == "description": quote_batch.checked_quote_batch(draft, doc, actor="測試", source_batch=batch)
        elif target == "old_trial": checked_internal_trial(draft, doc, actor="測試", source_batch=batch)
        elif target == "old_snapshot": build_snapshot_layout(draft, doc, actor="測試", source_batch=batch)
        else: QuoteNumberReservationService.reserve(None, doc)  # typed validation 先於引擎存取。
    spy.assert_not_called()


@pytest.mark.parametrize("kind", ["trial", "description", "single", "old_trial", "reservation"])
def test_other_document_types_and_renaming_cannot_enter_review(fixed_case, kind):
    batch, draft, trial, session, doc = fixed_case
    wrong = deepcopy(trial if kind == "trial" else trial["expansion"] if kind == "description" else {"document_type": kind})
    with pytest.raises(ValueError): session.checked(wrong, draft, trial, actor="測試", source_batch=batch)
    wrong["document_type"] = core.DOCUMENT_TYPE
    with pytest.raises(ValueError): session.checked(wrong, draft, trial, actor="測試", source_batch=batch)


@pytest.mark.parametrize("kind", ["no_total", "missing_child", "blocked_forged", "source_forged", "flags", "child_cost"])
def test_freeze_rejects_rehashed_trial_without_authentic_seal(fixed_case, kind):
    batch, draft, original, session, doc = fixed_case
    trial = deepcopy(original)
    if kind == "no_total": trial["totals"] = None
    elif kind == "missing_child": trial["children"].pop()
    elif kind == "blocked_forged":
        trial["children"][0]["blockers"] = []
        trial["children"][0]["status"] = "CALCULATED"
        trial["calculated_count"] = 100
    elif kind == "source_forged": trial["expansion"]["source"]["source_ref_raw"] = "fake"
    elif kind == "flags": trial["can_confirm"] = True
    else: trial["children"][0]["calculation"]["cost_inputs"] = []
    trial["content_digest"] = quote_batch._digest({k: v for k, v in trial.items() if k not in ("content_digest", "process_seal")})
    with pytest.raises(ValueError): session.freeze(draft, trial, actor="測試", source_batch=batch)
    with pytest.raises(ValueError): check(fixed_case)


def test_second_step_event_failure_preserves_first_state(fixed_case, monkeypatch):
    first = record(fixed_case, fixed_case[-1], 1)
    before = deepcopy(first)
    monkeypatch.setattr(core, "uuid4", Mock(side_effect=RuntimeError("合成事件失敗")))
    with pytest.raises(RuntimeError): record(fixed_case, first, 2)
    assert first == before and check(fixed_case, first) == before


def test_explicit_invalidation_and_absent_current_trial_reject_download(fixed_case):
    batch, draft, trial, session, doc = fixed_case
    first = record(fixed_case, doc, 1)
    with pytest.raises(ValueError): session.download(first, draft, None, actor="測試", source_batch=batch)
    with pytest.raises(ValueError): check(fixed_case, first)
    new = session.freeze(draft, trial, actor="測試", source_batch=batch)
    session.invalidate()
    with pytest.raises(ValueError): check(fixed_case, new)
