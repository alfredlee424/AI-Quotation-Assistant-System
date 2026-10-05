"""圖面依賴、人工引用、失效傳播及內部試算的安全界線。"""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from agent.drawing_review import apply_drawing_command, drawing_report
from agent.multi_quote import apply_multi_proposal, export_multi_draft
from agent.requirement_review import apply_review_command, refresh_review_evidence
from engine.multi_trial import checked_internal_trial
from test_multi_trial import two_cmt, reviewed_order, command, build


def draw(draft, content, batch=None, **kwargs):
    options = dict(draft_id=draft.draft_id, expected_revision=draft.revision,
                   actor="工程核對人", reason="人工圖面核對", source_batch=batch)
    options.update(kwargs)
    return apply_drawing_command(draft, content, **options)


def declare(draft, batch=None, **kwargs):
    content = {"op": "declare", "targets": [draft.lines[0].line_id],
               "requirement_ids": [draft.requirements[-1].requirement_id] if draft.requirements else [],
               "purpose": "核對桌面尺寸與定位，未提供圖面"}
    content.update(kwargs)
    return draw(draft, content, batch)


def record(draft, batch=None, **kwargs):
    content = {"op": "record", "dependency_id": draft.drawings[-1].dependency_id,
               "reference": "DRAW-TEST-001", "version": "R1", "summary": "人工確認指定版桌面尺寸；不代表工程核准"}
    content.update(kwargs)
    return draw(draft, content, batch)


def test_missing_blocks_only_target_and_never_offers_order_total(two_cmt):
    before = export_multi_draft(two_cmt)
    draft = declare(two_cmt)
    assert export_multi_draft(two_cmt) == before
    assert draft.revision == two_cmt.revision + 1 and draft.drawings[0].status == "MISSING"
    report = build(draft)
    assert [r["status"] for r in report["lines"]] == ["BLOCKED", "CALCULATED"]
    assert report["totals"] is None and not report["can_confirm"]
    assert report["drawings"]["issues"][0]["line_id"] == draft.lines[0].line_id


def test_record_preserves_history_identity_and_all_formal_gates(two_cmt, isolated_db):
    missing = declare(two_cmt)
    draft = record(missing)
    evidence = draft.drawings[0].evidence[0]
    assert evidence.actor == "工程核對人" and evidence.version == "R1" and evidence.recorded_at
    assert evidence.context_digest and draft.drawings[0].dependency_id == missing.drawings[0].dependency_id
    assert draft.blockers == two_cmt.blockers and draft.lines == two_cmt.lines
    report = build(draft)
    assert report["calculated_count"] == 2 and not report["can_quote"]
    assert drawing_report(draft)["issues"] == []
    from agent.tools import preview_quote
    with pytest.raises(ValueError):
        preview_quote(draft.lines[0].configuration)
    from database.models import QuoteSnapshotDocument, ordqdt_ai
    with isolated_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == db.query(ordqdt_ai).count() == 0
    revised = record(draft, version="R2", summary="新版核對內容")
    assert revised.drawings[0].evidence[:-1] == draft.drawings[0].evidence
    assert revised.drawings[0].evidence[-1].version == "R2"
    exported = export_multi_draft(revised)
    json.dumps(exported, ensure_ascii=False, allow_nan=False)
    exported["source"]["kind"] = "tampered"
    assert revised.source["kind"] == "single_quote"


@pytest.mark.parametrize("change", [
    {"targets": []}, {"targets": ["foreign"]}, {"targets": "not-a-list"},
    {"targets": [True]}, {"requirement_ids": ["foreign"]}, {"requirement_ids": [None]},
    {"purpose": " "}, {"purpose": 1}, {"purpose": "x" * 1001},
    {"price": 0}, {"status": "RECORDED"}, {"op": "delete"},
])
def test_invalid_declaration_is_atomic(two_cmt, change):
    before = export_multi_draft(two_cmt)
    with pytest.raises(ValueError):
        declare(two_cmt, **change)
    assert export_multi_draft(two_cmt) == before


@pytest.mark.parametrize("change", [
    {"reference": ""}, {"reference": "x" * 501}, {"version": " "}, {"version": "x" * 101},
    {"summary": ""}, {"summary": "x" * 2001}, {"summary": None},
    {"dependency_id": "foreign"}, {"dependency_id": []}, {"actor": "AI"},
    {"approved": True}, {"cost": 0},
])
def test_invalid_record_is_atomic(two_cmt, change):
    draft = declare(two_cmt)
    before = export_multi_draft(draft)
    with pytest.raises(ValueError):
        record(draft, **change)
    assert export_multi_draft(draft) == before


@pytest.mark.parametrize("options", [
    {"draft_id": "foreign"}, {"expected_revision": -1}, {"expected_revision": True},
    {"actor": ""}, {"reason": ""},
])
def test_identity_is_required(two_cmt, options):
    with pytest.raises(ValueError):
        draw(two_cmt, {"op": "declare", "targets": [two_cmt.lines[0].line_id],
                       "requirement_ids": [], "purpose": "圖面"}, **options)


def test_duplicate_and_archived_declarations_rejected(two_cmt):
    draft = declare(two_cmt)
    with pytest.raises(ValueError, match="已存在"):
        declare(draft)
    target = draft.lines[0].line_id
    archived = command(draft, {"op": "remove", "line_id": target})
    with pytest.raises(ValueError, match="封存"):
        declare(archived, targets=[target])
    with pytest.raises(ValueError, match="封存"):
        record(archived)
    with pytest.raises(ValueError):
        declare(two_cmt, targets=[target, target])


def test_target_configuration_change_is_sticky_but_unrelated_change_is_not(two_cmt):
    draft = record(declare(two_cmt))
    other = command(draft, {"op": "configure", "line_id": draft.lines[1].line_id, "proposal": {"qty": 5}})
    assert other.drawings[0].status == "RECORDED"
    target = draft.lines[0].line_id
    changed = command(other, {"op": "configure", "line_id": target, "proposal": {"qty": 9}})
    restored = command(changed, {"op": "configure", "line_id": target, "proposal": {"qty": 1}})
    assert changed.drawings[0].status == restored.drawings[0].status == "STALE"
    assert build(restored)["totals"] is None
    assert record(restored).drawings[0].status == "RECORDED"


def test_archive_restore_and_ai_configuration_invalidate(two_cmt):
    draft = record(declare(two_cmt))
    target = draft.lines[0].line_id
    archived = command(draft, {"op": "remove", "line_id": target})
    restored = command(archived, {"op": "restore", "line_id": target})
    assert restored.drawings[0].status == "STALE"
    assert archived.drawings[0].evidence == draft.drawings[0].evidence
    changed = apply_multi_proposal(draft, {"draft_id": draft.draft_id, "revision": draft.revision,
        "updates": [{"line_id": target, "proposal": {"qty": 8}}], "questions": []},
        allowed_line_ids=(target,), actor="測試", reason="AI 配置核對")
    assert changed.drawings[0].status == "STALE"


def test_reordering_does_not_invalidate_drawing_but_invalidates_trial(two_cmt):
    draft = record(declare(two_cmt))
    report = build(draft)
    moved = command(draft, {"op": "reorder", "line_ids": [r.line_id for r in reversed(draft.lines)]})
    assert moved.drawings[0].status == "RECORDED"
    with pytest.raises(ValueError):
        checked_internal_trial(moved, report, actor="測試")


@pytest.mark.parametrize("operation", ["record", "mark_missing", "declare", "tamper"])
def test_drawing_change_invalidates_old_trial(two_cmt, operation):
    draft = record(declare(two_cmt))
    report = build(draft)
    if operation == "record":
        changed = record(draft, version="R2")
    elif operation == "mark_missing":
        changed = draw(draft, {"op": operation, "dependency_id": draft.drawings[0].dependency_id})
        assert changed.drawings[0].evidence == draft.drawings[0].evidence
    elif operation == "declare":
        changed = declare(draft, purpose="另一部位定位")
    else:
        changed = deepcopy(draft)
        changed = replace(changed, drawings=(replace(changed.drawings[0], purpose="異動用途"),))
    with pytest.raises(ValueError):
        checked_internal_trial(changed, report, actor="測試")


def test_shared_reference_new_version_requires_other_targets_to_review(two_cmt):
    draft = record(declare(two_cmt))
    draft = record(declare(draft, targets=[draft.lines[1].line_id]))
    assert all(r.status == "RECORDED" for r in draft.drawings)
    revised = record(draft, version="R2")
    assert [r.status for r in revised.drawings] == ["STALE", "RECORDED"]
    assert build(revised)["totals"] is None
    revised = record(revised, dependency_id=revised.drawings[0].dependency_id, version="R2")
    assert not drawing_report(revised)["issues"]


def test_work_order_requires_source_and_rejects_stale_batch(reviewed_order):
    batch, draft = reviewed_order
    with pytest.raises(ValueError):
        declare(draft, batch, requirement_ids=[])
    with pytest.raises(ValueError, match="來源工單"):
        declare(draft, replace(batch, revision=batch.revision + 1))
    draft = record(declare(draft, batch), batch)
    requirement = draft.requirements[-1]
    updated = apply_review_command(draft, {"op": "reopen", "requirement_id": requirement.requirement_id},
        draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="來源重核", source_batch=batch)
    assert updated.drawings[0].status == "STALE"
    assert build(record(updated, batch), batch)["calculated_count"] == 0  # 不解除未核對來源


def test_source_split_keeps_history_and_blocks_old_dependency(reviewed_order):
    batch, draft = reviewed_order
    draft = record(declare(draft, batch), batch)
    updated = apply_review_command(draft, {"op": "split", "requirement_id": draft.requirements[-1].requirement_id, "offset": 2},
        draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="拆分原文", source_batch=batch)
    assert updated.drawings[0].status == "STALE"
    with pytest.raises(ValueError, match="已拆分"):
        record(updated, batch)


def test_condition_change_invalidates_drawing(reviewed_order):
    from agent.conditions import apply_condition_command
    batch, draft = reviewed_order
    draft = record(declare(draft, batch), batch)
    revised = apply_condition_command(draft, {"op": "record", "condition_id": "", "subject": "hole_position",
        "state": "REQUIRED", "scope": "line", "group_label": "", "targets": [draft.lines[0].line_id],
        "value": "距離邊緣 10 公分", "requirement_ids": [draft.requirements[-1].requirement_id],
        "bindings": [], "overrides": [], "reference": "規則-v1", "explanation": "人工定位"},
        draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="改定位", source_batch=batch)
    assert revised.drawings[0].status == "STALE"
    assert build(record(revised, batch), batch)["totals"] is None  # 圖面紀錄不能解鎖幾何問題


def test_context_not_sent_to_model_and_no_mutation_on_report(two_cmt):
    from agent.multi_quote_agent import build_multi_context
    draft = record(declare(two_cmt), reference="SECRET-DRAW-REF", summary="SECRET-DRAW-SUMMARY")
    before = export_multi_draft(draft)
    context = json.dumps(build_multi_context(draft, (draft.lines[0].line_id,)), ensure_ascii=False)
    assert "SECRET-DRAW" not in context and "工程核對人" not in context
    drawing_report(draft)
    assert export_multi_draft(draft) == before
    changed = deepcopy(draft)
    changed.source["note"] = "來源已改"
    assert refresh_review_evidence(changed).drawings[0].status == "STALE"
