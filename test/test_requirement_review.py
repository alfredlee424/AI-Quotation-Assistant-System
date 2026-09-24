"""來源需求帳本、證據失效與人工問題結案；不賦予正式報價權限。"""
from copy import deepcopy
from dataclasses import replace

import pytest

from agent.multi_quote import apply_multi_command, apply_multi_proposal, export_multi_draft, from_work_order, new_multi_quote
from agent.requirement_review import apply_review_command, requirement_report
from agent.work_orders import parse_work_orders, reclassify_line
from work_order_fixtures import CASES


@pytest.fixture
def review_case(cmt1_db):
    batch = parse_work_orders("環式會議桌生產工單\n20990101-1測試\n平直60X180-----2\n桌面置中／腳板不挖孔")
    draft = from_work_order(batch, batch.orders[0].order_id)
    draft = apply_multi_command(draft, {"op": "configure", "line_id": draft.lines[0].line_id,
                                        "proposal": {"prodkind": "CMT1", "qty": 2, "changes": [
                                            {"op": "set", "path": r"CMT1\A001", "code": "C005"}]}},
                                draft_id=draft.draft_id, expected_revision=0, actor="測試", reason="設定尺寸", source_batch=batch)
    return batch, draft


def review(batch, draft, command, **extra):
    args = dict(draft_id=draft.draft_id, expected_revision=draft.revision,
                actor="測試人員", reason="核對需求", source_batch=batch)
    args.update(extra)
    return apply_review_command(draft, command, **args)


def record_command(draft, record=None, disposition="specification"):
    record = record or next(r for r in draft.requirements if r.text.startswith("平直"))
    return {"op": "record", "requirement_id": record.requirement_id, "disposition": disposition,
            "targets": [draft.lines[0].line_id],
            "bindings": [{"line_id": draft.lines[0].line_id, "path": r"CMT1\A001"}] if disposition == "specification" else [],
            "reference": "測試核對記錄-v1", "explanation": "依測試規格表核對尺寸；此為人工處置紀錄"}


def add_question(batch, draft, text="此明細尺寸是否正確？", target=None):
    return apply_multi_proposal(draft, {"draft_id": draft.draft_id, "revision": draft.revision, "updates": [],
                                        "questions": [{"target_id": target or draft.lines[0].line_id, "text": text}]},
                                allowed_line_ids=(draft.lines[0].line_id,), actor="測試", reason="追問", source_batch=batch)


def resolve(batch, draft):
    req = next(r for r in draft.requirements if r.status == "RECORDED")
    return review(batch, draft, {"op": "resolve_question", "question_id": draft.questions[0].question_id,
                                "requirement_ids": [req.requirement_id], "reference": "核對依據-v1",
                                "answer": "已依上述需求與所選規格核對；保留工程核准關卡。"})


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key)
def test_all_work_order_lines_start_unreviewed_with_complete_source_spans(case):
    batch = parse_work_orders(case.raw_text)
    draft = from_work_order(batch, batch.orders[0].order_id)
    assert len(draft.requirements) == len(case.lines)
    assert [r.text for r in draft.requirements] == list(case.lines)
    assert all(r.status == "UNREVIEWED" and not r.disposition and not r.bindings for r in draft.requirements)
    report = requirement_report(draft)
    assert report["source_coverage_complete"]
    assert not report["all_records_reviewed"] and not report["can_quote"]


def test_non_source_drafts_are_not_treated_as_covered():
    report = requirement_report(new_multi_quote())
    assert not report["source_available"] and not report["source_coverage_complete"] and not report["can_quote"]


def test_split_preserves_text_coordinates_identity_and_archived_evidence(review_case):
    batch, draft = review_case
    old = draft.requirements[-1]
    split = review(batch, draft, {"op": "split", "requirement_id": old.requirement_id, "offset": 4})
    children = [r for r in split.requirements if r.parent_id == old.requirement_id]
    assert "".join(r.text for r in children) == old.text
    assert [(r.start, r.end) for r in children] == [(0, 4), (4, len(old.text))]
    assert all(r.status == "UNREVIEWED" for r in children)
    assert split.archived_requirements[0].status == "SPLIT"
    assert split.requirements[0] == draft.requirements[0]
    assert requirement_report(split)["source_coverage_complete"]
    assert draft.requirements[-1] == old and not draft.archived_requirements


@pytest.mark.parametrize("offset", [0, -1, 999, True, 1.5, "2"])
def test_invalid_split_is_atomic(review_case, offset):
    batch, draft = review_case
    before = export_multi_draft(draft)
    with pytest.raises(ValueError):
        review(batch, draft, {"op": "split", "requirement_id": draft.requirements[-1].requirement_id, "offset": offset})
    assert export_multi_draft(draft) == before


def test_record_creates_bidirectional_mapping_without_unlocking_quote(review_case):
    batch, draft = review_case
    recorded = review(batch, draft, record_command(draft))
    record = next(r for r in recorded.requirements if r.status == "RECORDED")
    assert record.bindings[0].code == "C005" and record.bindings[0].line_id == draft.lines[0].line_id
    assert record.evidence[0].actor == "測試人員"
    report = requirement_report(recorded)
    assert report["bindings"][0]["requirement_id"] == record.requirement_id
    assert report["bindings"][0]["path"] == r"CMT1\A001"
    assert not report["can_quote"] and recorded.blockers == draft.blockers
    assert recorded.status == "CONFIGURATION_ONLY" and draft.requirements[2].status == "UNREVIEWED"


def test_reverse_report_exposes_unmapped_and_stale_selections(review_case):
    batch, draft = review_case
    assert any(item["path"] == r"CMT1\A001" for item in requirement_report(draft)["unmapped_selections"])
    recorded = review(batch, draft, record_command(draft))
    assert not any(item["path"] == r"CMT1\A001" for item in requirement_report(recorded)["unmapped_selections"])
    key = next(record.requirement_id for record in recorded.requirements if record.status == "RECORDED")
    reopened = review(batch, recorded, {"op": "reopen", "requirement_id": key})
    assert any(item["path"] == r"CMT1\A001" for item in requirement_report(reopened)["unmapped_selections"])


@pytest.mark.parametrize("update", [
    {"targets": []}, {"targets": ["other-line"]}, {"bindings": []},
    {"bindings": [{"line_id": "other-line", "path": r"CMT1\A001"}]},
    {"reference": ""}, {"reference": None}, {"explanation": " "}, {"explanation": "a" * 2001},
    {"disposition": "approved"}, {"cost": 0},
])
def test_invalid_or_out_of_scope_evidence_is_rejected(review_case, update):
    batch, draft = review_case
    command = record_command(draft)
    command.update(update)
    before = export_multi_draft(draft)
    with pytest.raises(ValueError):
        review(batch, draft, command)
    assert export_multi_draft(draft) == before


def test_standard_metadata_and_engineering_dispositions_remain_distinct(review_case):
    batch, draft = review_case
    header = draft.requirements[1]
    command = record_command(draft, header, "metadata")
    command["targets"] = []
    recorded = review(batch, draft, command)
    assert recorded.requirements[1].status == "RECORDED"
    engineering = review(batch, recorded, record_command(recorded, disposition="engineering_review"))
    assert any(r.status == "NEEDS_ENGINEERING" for r in engineering.requirements)
    assert requirement_report(engineering)["pending_requirement_ids"]


def test_recorded_evidence_becomes_stale_after_quantity_change(review_case):
    batch, draft = review_case
    recorded = review(batch, draft, record_command(draft))
    changed = apply_multi_command(recorded, {"op": "configure", "line_id": recorded.lines[0].line_id, "proposal": {"qty": 3}},
                                  draft_id=recorded.draft_id, expected_revision=recorded.revision,
                                  actor="測試", reason="改數量", source_batch=batch)
    stale = next(r for r in changed.requirements if r.disposition == "specification")
    assert stale.status == "STALE" and len(stale.evidence) == 1
    assert not requirement_report(changed)["all_records_reviewed"]


def test_human_question_resolution_needs_recorded_requirement_and_keeps_gate(review_case):
    batch, draft = review_case
    draft = review(batch, draft, record_command(draft))
    asked = add_question(batch, draft)
    closed = resolve(batch, asked)
    question = closed.questions[0]
    assert question.status == "RESOLVED" and question.resolution and len(question.resolution_history) == 1
    assert question.text not in closed.lines[0].configuration["questions"]
    assert closed.lines[0].configuration["questions"]  # 系統關卡仍在。
    assert closed.blockers == draft.blockers
    assert not requirement_report(closed)["can_quote"]


def test_question_same_text_as_preexisting_gate_does_not_remove_gate(review_case):
    batch, draft = review_case
    draft = review(batch, draft, record_command(draft))
    original = draft.lines[0].configuration["questions"][0]
    asked = add_question(batch, draft, text=original)
    assert not asked.questions[0].owns_projection
    closed = resolve(batch, asked)
    assert original in closed.lines[0].configuration["questions"]


def test_configuration_change_reopens_question_and_invalidates_record(review_case):
    batch, draft = review_case
    closed = resolve(batch, add_question(batch, review(batch, draft, record_command(draft))))
    target = closed.lines[0].line_id
    changed = apply_multi_command(closed, {"op": "configure", "line_id": target, "proposal": {"qty": 8}},
                                  draft_id=closed.draft_id, expected_revision=closed.revision,
                                  actor="測試", reason="變更", source_batch=batch)
    assert changed.questions[0].status == "OPEN"
    assert changed.questions[0].text in changed.lines[0].configuration["questions"]
    assert changed.questions[0].resolution_history == closed.questions[0].resolution_history
    assert any(record.status == "STALE" for record in changed.requirements)


def test_splitting_or_reopening_referenced_requirement_reopens_question(review_case):
    batch, draft = review_case
    closed = resolve(batch, add_question(batch, review(batch, draft, record_command(draft))))
    key = closed.questions[0].requirement_ids[0]
    split = review(batch, closed, {"op": "split", "requirement_id": key, "offset": 2})
    assert split.questions[0].status == "OPEN"
    reopened = review(batch, closed, {"op": "reopen", "requirement_id": key})
    assert reopened.questions[0].status == "OPEN"


def test_question_cannot_close_using_unreviewed_engineering_or_wrong_requirement(review_case):
    batch, draft = review_case
    asked = add_question(batch, draft)
    for key in ("other-requirement", draft.requirements[-1].requirement_id):
        with pytest.raises(ValueError):
            review(batch, asked, {"op": "resolve_question", "question_id": asked.questions[0].question_id,
                                  "requirement_ids": [key], "reference": "test-v1", "answer": "已回答"})
    engineering = review(batch, asked, record_command(asked, disposition="engineering_review"))
    key = next(r.requirement_id for r in engineering.requirements if r.status == "NEEDS_ENGINEERING")
    with pytest.raises(ValueError):
        review(batch, engineering, {"op": "resolve_question", "question_id": asked.questions[0].question_id,
                                   "requirement_ids": [key], "reference": "test-v1", "answer": "已回答"})


def test_archiving_target_reopens_evidence_and_preserves_provenance(review_case):
    batch, draft = review_case
    closed = resolve(batch, add_question(batch, review(batch, draft, record_command(draft))))
    archived = apply_multi_command(closed, {"op": "remove", "line_id": closed.lines[0].line_id},
                                   draft_id=closed.draft_id, expected_revision=closed.revision,
                                   actor="測試", reason="移除", source_batch=batch)
    assert archived.questions[0].status == "OPEN" and not archived.lines
    assert archived.requirements and archived.source == closed.source
    restored = apply_multi_command(archived, {"op": "restore", "line_id": closed.lines[0].line_id},
                                   draft_id=archived.draft_id, expected_revision=archived.revision,
                                   actor="測試", reason="恢復", source_batch=batch)
    assert restored.questions[0].text in restored.lines[0].configuration["questions"]


def test_re_recording_evidence_invalidates_old_question_resolution(review_case):
    batch, draft = review_case
    closed = resolve(batch, add_question(batch, review(batch, draft, record_command(draft))))
    command = record_command(closed)
    command["reference"] = "更新的依據-v2"
    changed = review(batch, closed, command)
    assert changed.questions[0].status == "OPEN"
    assert changed.questions[0].resolution_history == closed.questions[0].resolution_history
    closed_again = resolve(batch, changed)
    assert closed_again.questions[0].status == "RESOLVED"
    assert len(closed_again.questions[0].resolution_history) == 2


def test_requirement_reassigned_to_another_line_reopens_old_target_question(review_case):
    batch, draft = review_case
    closed = resolve(batch, add_question(batch, review(batch, draft, record_command(draft))))
    added = apply_multi_command(closed, {"op": "add", "label": "另一筆"}, draft_id=closed.draft_id,
                                expected_revision=closed.revision, actor="測試", reason="新增", source_batch=batch)
    command = record_command(added, disposition="production_note")
    command["targets"] = [added.lines[1].line_id]
    changed = review(batch, added, command)
    assert changed.questions[0].status == "OPEN"


def test_source_change_and_wrong_revision_rejected_before_review(review_case):
    batch, draft = review_case
    command = record_command(draft)
    with pytest.raises(ValueError, match="版本"):
        review(batch, draft, command, expected_revision=-1)
    changed = reclassify_line(batch, line_id=batch.lines[-1].line_id, kind="note",
                              expected_revision=0, actor="測試", reason="分類變更")
    with pytest.raises(ValueError, match="來源工單"):
        review(changed, draft, command)


def test_report_detects_missing_and_corrupted_source_spans(review_case):
    _, draft = review_case
    missing = replace(draft, requirements=draft.requirements[:-1])
    assert not requirement_report(missing)["source_coverage_complete"]
    corrupted = replace(draft, requirements=(replace(draft.requirements[0], text="不符來源"), *draft.requirements[1:]))
    assert requirement_report(corrupted)["invalid_requirement_ids"]


def test_resolved_evidence_not_sent_to_model_context(review_case):
    from agent.multi_quote_agent import build_multi_context
    import json
    batch, draft = review_case
    command = record_command(draft)
    command["reference"] = "PRIVATE-EVIDENCE-123"
    closed = resolve(batch, add_question(batch, review(batch, draft, command)))
    context = build_multi_context(closed, (closed.lines[0].line_id,))
    text = json.dumps(context, ensure_ascii=False)
    assert "PRIVATE-EVIDENCE-123" not in text
    assert not context["questions"]
