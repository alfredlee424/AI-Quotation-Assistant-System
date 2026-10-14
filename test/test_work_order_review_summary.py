"""合成工單的固定分類計數、失效、拒絕與資料最小化。"""
from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from agent.drawing_review import apply_drawing_command, DrawingDependency
from agent.multi_quote import DraftChange, ScopedQuestion, new_multi_quote
from agent.requirement_review import apply_review_command
from engine import work_order_review_summary as core
from quote_batch_fixtures import four_items, condition


def build(batch, draft, actor="測試"):
    return core.build_work_order_review_summary(draft, source_batch=batch, actor=actor)


def drawing(batch, draft, op="declare", **fields):
    return apply_drawing_command(draft, {"op": op, **fields}, draft_id=draft.draft_id,
                                 expected_revision=draft.revision, actor="測試", reason="測試",
                                 source_batch=batch)


def declare(batch, draft):
    return drawing(batch, draft, targets=[draft.lines[0].line_id],
                   requirement_ids=[next(r.requirement_id for r in draft.requirements if r.targets)],
                   purpose="合成定位")


def test_four_items_are_not_eleven_and_no_effects(monkeypatch):
    batch, draft = four_items()
    original = deepcopy((batch, draft))
    from database import repository
    from engine import calculator, configuration
    deny = Mock(side_effect=AssertionError("禁止查價或保存"))
    monkeypatch.setattr(repository, "_session", deny)
    monkeypatch.setattr(configuration, "load_catalog", deny)
    monkeypatch.setattr(calculator, "calculate_quote", deny)
    # 整個摘要只使用目前記憶體來源，無須主檔資料。
    report = build(batch, draft)
    assert report["source"]["item_count"] == 4
    assert report["source"]["ledger_coverage_complete"] is True
    assert report["requirements"]["states"]["RECORDED"] == len(draft.requirements)
    assert report["requirements"]["unmapped_selection_count"] == 4
    assert report["expansion"]["review_complete"] is True
    assert report["recorded_changes"]["categories"]["requirement_review"] == len(draft.requirements)
    assert all(report[key] is False for key in (
        "pricing_checked", "semantic_coverage_certified", "actor_is_authenticated",
        "can_quote", "can_confirm", "can_save"))
    assert set(report["formal_gates"].values()) == {"PENDING"}
    assert (batch, draft) == original
    assert build(batch, draft) == report
    deny.assert_not_called()


def test_pending_and_missing_ledger_not_hidden():
    batch, draft = four_items(quantities=False, reviewed=False)
    report = build(batch, draft)
    assert report["requirements"]["pending_count"] == len(draft.requirements)
    assert report["expansion"]["children_with_pending_quantity"] == 4
    assert report["expansion"]["children_with_issues"] == 4
    draft = replace(draft, requirements=draft.requirements[1:])
    report = build(batch, draft)
    assert report["source"]["uncovered_line_count"] == 1
    assert not report["source"]["ledger_coverage_complete"]
    assert not report["requirements"]["all_records_reviewed"]


def test_configuration_change_recomputes_stale_without_mutating_draft():
    batch, draft = four_items()
    draft = declare(batch, draft)
    draft = drawing(batch, draft, "record", dependency_id=draft.drawings[0].dependency_id,
                    reference="圖面秘密", version="V1", summary="人工摘要")
    old = build(batch, draft)
    assert old["drawings"]["states"]["RECORDED"] == 1
    first = replace(draft.lines[0], configuration={**draft.lines[0].configuration, "qty": 20})
    changed = replace(draft, lines=(first, *draft.lines[1:]))
    report = build(batch, changed)
    assert report["requirements"]["states"]["STALE"] > 0
    assert report["drawings"]["states"]["STALE"] == 1
    assert report["drawings"]["issue_projection_count"] == 1
    assert changed.drawings[0].status == "RECORDED"  # 摘要不回寫失效狀態。
    assert old["drawings"]["states"]["RECORDED"] == 1


def test_engineering_requirement_remains_pending():
    batch, draft = four_items()
    target = next(r for r in draft.requirements if r.targets)
    draft = apply_review_command(draft, {
        "op": "record", "requirement_id": target.requirement_id,
        "targets": list(target.targets), "disposition": "engineering_review", "bindings": [],
        "reference": "未核准", "explanation": "待工程",
    }, draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="測試", source_batch=batch)
    report = build(batch, draft)
    assert report["requirements"]["states"]["NEEDS_ENGINEERING"] == 1
    assert report["requirements"]["pending_count"] == 1
    assert not report["requirements"]["all_records_reviewed"]


def test_condition_and_drawing_projection_counts_are_separate():
    batch, draft = four_items()
    req = next(r for r in draft.requirements if r.targets)
    targets = [line.line_id for line in draft.lines]
    draft = condition(batch, draft, "surface_hole", "FORBIDDEN", targets, req.requirement_id, scope="order")
    draft = condition(batch, draft, "surface_hole", "REQUIRED", targets[:1], req.requirement_id)
    draft = declare(batch, draft)
    report = build(batch, draft)
    assert report["conditions"]["record_count"] == 2
    assert report["conditions"]["issue_kinds"]["conflict"] == 1
    assert report["conditions"]["issue_kinds"]["unbound"] == 5
    assert report["drawings"]["states"]["MISSING"] == 1
    assert report["recorded_changes"]["categories"]["condition_review"] == 2
    assert report["recorded_changes"]["categories"]["drawing_review"] == 1


def test_question_without_valid_resolution_is_reopened():
    batch, draft = four_items()
    question = ScopedQuestion("synthetic-id", draft.lines[0].line_id, "秘密問題", status="RESOLVED")
    draft = replace(draft, questions=(question,))
    report = build(batch, draft)
    assert report["questions"]["states"] == {"OPEN": 1, "RESOLVED": 0}
    assert draft.questions[0].status == "RESOLVED"


def test_no_raw_text_identifiers_prices_or_digests_in_summary():
    batch, draft = four_items()
    marker = "PRIVATE-DO-NOT-EXPORT"
    change = DraftChange(500, marker, (draft.lines[0].line_id,), marker, marker, marker,
                         {marker: "987654.32"})
    draft = replace(draft, blockers=(*draft.blockers, marker), changes=(*draft.changes, change),
                    questions=(ScopedQuestion("question-" + marker, draft.draft_id, marker),))
    report = build(batch, draft, actor=marker)
    payload = json.dumps(report, ensure_ascii=False)
    for secret in (marker, "987654.32", batch.batch_id, batch.source_digest, draft.draft_id,
                   draft.source["source_ref_raw"], *[line.line_id for line in draft.lines],
                   *[r.requirement_id for r in draft.requirements], *[r.text for r in draft.requirements]):
        assert secret not in payload
    assert report["recorded_changes"]["categories"]["other"] == 1
    assert "digest" not in payload
    assert "recorded_at" not in payload


@pytest.mark.parametrize("kind", ["source", "raw", "archive", "missing", "duplicate", "coordinate", "overlap", "manual", "schema"])
def test_bad_source_or_coverage_rejects_entire_summary(kind):
    batch, draft = four_items()
    if kind == "source":
        batch = replace(batch, revision=batch.revision + 1)
    elif kind == "raw":
        batch = replace(batch, raw_text=batch.raw_text + "SECRET")
    elif kind == "archive":
        draft = replace(draft, lines=draft.lines[1:], archived_lines=draft.lines[:1])
    elif kind == "missing":
        draft = replace(draft, lines=draft.lines[1:])
    elif kind == "duplicate":
        draft = replace(draft, lines=(*draft.lines, draft.lines[0]))
    elif kind == "coordinate":
        record = replace(draft.requirements[0], text="SECRET")
        draft = replace(draft, requirements=(record, *draft.requirements[1:]))
    elif kind == "overlap":
        record = replace(draft.requirements[0], requirement_id="other")
        draft = replace(draft, requirements=(*draft.requirements, record))
    elif kind == "manual":
        draft = new_multi_quote()
    else:
        draft = replace(draft, schema_version=999)
    with pytest.raises(ValueError) as error:
        build(batch, draft)
    assert str(error.value) == core.ERROR


@pytest.mark.parametrize("actor", [None, "", " ", 42, "x" * 81])
def test_invalid_actor_uses_fixed_error(actor):
    batch, draft = four_items()
    with pytest.raises(ValueError, match=core.ERROR):
        build(batch, draft, actor)


@pytest.mark.parametrize("kind", ["requirement", "condition", "drawing", "question"])
def test_unknown_status_rejected_not_used_as_output_key(kind):
    batch, draft = four_items()
    if kind == "requirement":
        draft = replace(draft, requirements=(replace(draft.requirements[0], status="SECRET"), *draft.requirements[1:]))
    elif kind == "condition":
        req = next(r for r in draft.requirements if r.targets)
        draft = condition(batch, draft, "surface_hole", "FORBIDDEN", req.targets, req.requirement_id)
        draft = replace(draft, conditions=(replace(draft.conditions[0], status="SECRET"),))
    elif kind == "drawing":
        draft = declare(batch, draft)
        draft = replace(draft, drawings=(replace(draft.drawings[0], status="SECRET"),))
    else:
        draft = replace(draft, questions=(ScopedQuestion("id", draft.draft_id, "text", status="SECRET"),))
    with pytest.raises(ValueError) as error:
        build(batch, draft)
    assert str(error.value) == core.ERROR


@pytest.mark.parametrize("kind", ["drawing_target", "drawing_requirement", "question_target", "no_evidence"])
def test_invalid_evidence_or_scope_rejected(kind):
    batch, draft = four_items()
    if kind.startswith("drawing"):
        draft = replace(draft, drawings=(DrawingDependency("id", ("foreign",) if kind == "drawing_target" else (draft.lines[0].line_id,),
                         ("foreign",) if kind == "drawing_requirement" else (), "purpose"),))
    elif kind == "question_target":
        draft = replace(draft, questions=(ScopedQuestion("id", "foreign", "text"),))
    else:
        draft = replace(draft, requirements=(replace(draft.requirements[0], evidence=()), *draft.requirements[1:]))
    with pytest.raises(ValueError, match=core.ERROR):
        build(batch, draft)


def test_returned_dictionary_not_cached_or_accepted_as_input():
    batch, draft = four_items()
    expected = build(batch, draft)
    altered = build(batch, draft)
    altered["formal_gates"].clear()
    altered["can_quote"] = True
    assert build(batch, draft) == expected
    with pytest.raises(ValueError):
        build(batch, altered)


def test_formal_gate_names_match_existing_review_contract():
    from engine.quote_batch_review import FORMAL_PENDING
    assert core.FORMAL_GATES == tuple(key for key, _ in FORMAL_PENDING)


@pytest.mark.parametrize("operation", ["split", "reopen"])
def test_manual_ledger_changes_are_counted_without_double_counting_archived_records(operation):
    batch, draft = four_items()
    command = {"op": operation, "requirement_id": draft.requirements[-1].requirement_id}
    if operation == "split":
        command["offset"] = 4
    revised = apply_review_command(draft, command, draft_id=draft.draft_id,
                                  expected_revision=draft.revision, actor="測試", reason="人工修正",
                                  source_batch=batch)
    report = build(batch, revised)
    assert report["source"]["ledger_coverage_complete"]
    assert report["requirements"]["pending_count"] == (2 if operation == "split" else 1)
    assert report["requirements"]["record_count"] == len(draft.requirements) + (operation == "split")
    assert report["requirements"]["archived_record_count"] == (operation == "split")
    assert report["recorded_changes"]["draft_event_count"] == len(draft.changes) + 1


def test_real_question_resolution_and_reopening_are_reflected():
    from test_requirement_review import add_question, review
    batch, draft = four_items()
    draft = add_question(batch, draft)
    req = next(r for r in draft.requirements if draft.lines[0].line_id in r.targets)
    resolved = review(batch, draft, {
        "op": "resolve_question", "question_id": draft.questions[0].question_id,
        "requirement_ids": [req.requirement_id], "reference": "合成依據", "answer": "人工紀錄",
    })
    assert build(batch, resolved)["questions"]["states"] == {"OPEN": 0, "RESOLVED": 1}
    reopened = review(batch, resolved, {"op": "reopen", "requirement_id": req.requirement_id})
    assert build(batch, reopened)["questions"]["states"] == {"OPEN": 1, "RESOLVED": 0}


def test_withdrawn_condition_is_counted_but_not_an_active_issue():
    from agent.conditions import apply_condition_command
    batch, draft = four_items()
    req = next(r for r in draft.requirements if r.targets)
    draft = condition(batch, draft, "surface_hole", "FORBIDDEN", req.targets, req.requirement_id)
    revised = apply_condition_command(draft, {"op": "withdraw", "condition_id": draft.conditions[0].condition_id},
        draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="撤回", source_batch=batch)
    report = build(batch, revised)
    assert report["conditions"]["states"]["WITHDRAWN"] == 1
    assert report["conditions"]["issue_count"] == 0
