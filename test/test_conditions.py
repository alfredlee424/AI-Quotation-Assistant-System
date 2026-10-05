"""人工条件範圍與拒絕路徑；不把人工紀錄當作正式核准。"""
from copy import deepcopy
from dataclasses import replace

import pytest

from agent.conditions import apply_condition_command, condition_report, refresh_conditions, validate_condition_configuration
from agent.multi_quote import apply_multi_command, apply_multi_proposal, export_multi_draft, from_work_order
from agent.requirement_review import apply_review_command
from agent.work_orders import parse_work_orders
from work_order_fixtures import CASES

HOLE = r"CMT1\A001\S019\S030"


def manage(batch, draft, command):
    return apply_multi_command(draft, command, draft_id=draft.draft_id, expected_revision=draft.revision,
                               actor="測試", reason="測試操作", source_batch=batch)


@pytest.fixture
def condition_case(cmt1_db):
    batch = parse_work_orders("環式會議桌生產工單\n20990101-1測試\n平直60X180-----2\n平彎60X120-----1\n彎角面無孔，桌下走線；腳板不挖孔")
    draft = from_work_order(batch, batch.orders[0].order_id)
    for line in draft.lines:
        draft = manage(batch, draft, {"op": "configure", "line_id": line.line_id,
                                      "proposal": {"prodkind": "CMT1", "qty": 2, "changes": [
                                          {"op": "set", "path": r"CMT1\A001", "code": "C005"}]}})
    assert len(draft.lines) == 2
    return batch, draft


def record_command(draft, **changes):
    command = {"op": "record", "condition_id": "", "subject": "surface_hole", "state": "REQUIRED",
               "scope": "order", "group_label": "", "targets": [line.line_id for line in draft.lines], "value": "",
               "requirement_ids": [draft.requirements[-1].requirement_id], "bindings": [], "overrides": [],
               "reference": "人工核對-v1", "explanation": "僅驗證作用範圍，不代表來源語意或工程核准"}
    command.update(changes)
    return command


def record(batch, draft, command=None, **changes):
    return apply_condition_command(draft, command or record_command(draft, **changes), draft_id=draft.draft_id,
                                   expected_revision=draft.revision, actor="測試", reason="人工核對", source_batch=batch)


def state(draft, index=0, subject="surface_hole"):
    return next(row for row in condition_report(draft)["rows"]
                if row["line_id"] == draft.lines[index].line_id and row["subject"] == subject)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key)
def test_samples_require_explicit_scope_without_inferring_semantics(case):
    batch = parse_work_orders(case.raw_text)
    draft = from_work_order(batch, batch.orders[0].order_id)
    before = export_multi_draft(draft)
    changed = record(batch, draft)
    assert changed.source == draft.source and changed.requirements == draft.requirements
    assert changed.blockers == draft.blockers and changed.lines == draft.lines
    assert changed.conditions[0].targets == tuple(line.line_id for line in draft.lines)
    assert changed.conditions[0].rules_version == "explicit-scope-v1"
    assert not condition_report(changed)["can_quote"]
    assert export_multi_draft(draft) == before


@pytest.mark.parametrize("value", ["UNSPECIFIED", "REQUIRED", "FORBIDDEN", "NOT_APPLICABLE"])
def test_four_states_remain_distinct(condition_case, value):
    batch, draft = condition_case
    assert state(draft)["state"] == "UNSPECIFIED" and not state(draft)["condition_ids"]
    changed = record(batch, draft, state=value)
    assert state(changed)["state"] == value and state(changed)["condition_ids"]
    assert condition_report(changed)["issues"]  # 未指定或未綁定，不能視為完整。


def test_explicit_local_exception_keeps_other_line_and_wiring(condition_case):
    batch, draft = condition_case
    draft = record(batch, draft)
    parent = draft.conditions[0].condition_id
    draft = record(batch, draft, subject="underdesk_wiring")
    draft = record(batch, draft, state="FORBIDDEN", scope="line", targets=[draft.lines[1].line_id], overrides=[parent])
    assert state(draft, 0)["state"] == "REQUIRED"
    assert state(draft, 1)["state"] == "FORBIDDEN"
    assert state(draft, 1)["overridden_ids"] == [parent]
    assert state(draft, 1, "underdesk_wiring")["state"] == "REQUIRED"
    assert state(draft, 1, "leg_panel_hole")["state"] == "UNSPECIFIED"


def test_group_to_line_exception_retains_transitive_inheritance(condition_case):
    batch, draft = condition_case
    draft = record(batch, draft)
    root = draft.conditions[0].condition_id
    draft = record(batch, draft, scope="group", group_label="主席", state="FORBIDDEN", overrides=[root])
    group = draft.conditions[-1].condition_id
    draft = record(batch, draft, scope="line", targets=[draft.lines[1].line_id], overrides=[group])
    assert state(draft, 0)["state"] == "FORBIDDEN"
    assert state(draft, 1)["state"] == "REQUIRED"
    assert set(state(draft, 1)["overridden_ids"]) == {root, group}


@pytest.mark.parametrize("scope", ["order", "line"])
def test_opposite_conditions_without_explicit_override_conflict(condition_case, scope):
    batch, draft = condition_case
    draft = record(batch, draft)
    targets = [line.line_id for line in draft.lines] if scope == "order" else [draft.lines[0].line_id]
    draft = record(batch, draft, scope=scope, targets=targets, state="FORBIDDEN")
    assert state(draft)["state"] == "CONFLICT"
    before = export_multi_draft(draft)
    with pytest.raises(ValueError, match="條件衝突"):
        manage(batch, draft, {"op": "configure", "line_id": draft.lines[0].line_id, "proposal": {"qty": 4}})
    assert export_multi_draft(draft) == before


def test_different_position_values_conflict_and_require_geometry_review(condition_case):
    batch, draft = condition_case
    draft = record(batch, draft, subject="hole_position", value="靠左")
    assert {i["kind"] for i in condition_report(draft)["issues"]} >= {"position_dependency", "position_review"}
    draft = record(batch, draft, subject="hole_position", value="置中")
    assert state(draft, subject="hole_position")["state"] == "CONFLICT"


@pytest.mark.parametrize("change", [
    {"targets": []}, {"targets": ["other-order"]}, {"targets": [True]}, {"targets": "all"},
    {"scope": "line"}, {"scope": "group"}, {"scope": []}, {"state": []}, {"state": "APPROVED"},
    {"subject": "any_hole"}, {"group_label": "不允許"}, {"reference": ""}, {"requirement_ids": []},
    {"requirement_ids": ["other-source"]}, {"bindings": {}}, {"overrides": ["unknown"]},
    {"condition_id": "foreign"}, {"condition_id": []}, {"price": 0},
])
def test_invalid_record_atomic(condition_case, change):
    batch, draft = condition_case
    before = export_multi_draft(draft)
    with pytest.raises(ValueError):
        record(batch, draft, **change)
    assert export_multi_draft(draft) == before


def test_illegal_bindings_rejected(condition_case):
    batch, draft = condition_case
    for bindings in ([{"line_id": "foreign", "path": HOLE}],
                     [{"line_id": draft.lines[0].line_id, "path": "unknown"}],
                     [{"line_id": draft.lines[0].line_id, "path": HOLE}] * 2):
        with pytest.raises(ValueError):
            record(batch, draft, bindings=bindings)


@pytest.mark.parametrize("state_value", ["FORBIDDEN", "NOT_APPLICABLE"])
def test_bound_negative_rejects_config_and_ai_batch_without_partial_changes(condition_case, state_value):
    batch, draft = condition_case
    target = draft.lines[1].line_id
    draft = record(batch, draft, scope="line", targets=[target], state=state_value,
                   bindings=[{"line_id": target, "path": HOLE}])
    before = export_multi_draft(draft)
    proposal = {"changes": [{"op": "set", "path": HOLE, "code": "C001"}]}
    with pytest.raises(ValueError, match="否定條件"):
        manage(batch, draft, {"op": "configure", "line_id": target, "proposal": proposal})
    with pytest.raises(ValueError, match="否定條件"):
        apply_multi_proposal(draft, {"draft_id": draft.draft_id, "revision": draft.revision,
                                    "updates": [{"line_id": draft.lines[0].line_id, "proposal": {"qty": 9}},
                                                {"line_id": target, "proposal": proposal}], "questions": []},
                             allowed_line_ids=tuple(line.line_id for line in draft.lines), actor="測試", reason="整批測試", source_batch=batch)
    assert export_multi_draft(draft) == before
    allowed = manage(batch, draft, {"op": "configure", "line_id": draft.lines[0].line_id, "proposal": proposal})
    assert HOLE in allowed.lines[0].configuration["selections"]
    assert HOLE not in allowed.lines[1].configuration["selections"]


def test_required_auto_branch_cannot_readd_forbidden_node(condition_case):
    batch, draft = condition_case
    path = r"CMT1\A001\W001"
    target = draft.lines[0].line_id
    draft = record(batch, draft, scope="line", targets=[target], state="FORBIDDEN", bindings=[{"line_id": target, "path": path}])
    assert any(i["kind"] == "forbidden_selection" for i in condition_report(draft)["issues"])
    with pytest.raises(ValueError, match="自動必選"):
        manage(batch, draft, {"op": "configure", "line_id": target, "proposal": {"changes": [{"op": "remove", "path": path}]}})


def test_scopes_stale_on_membership_change_but_not_reorder(condition_case):
    batch, draft = condition_case
    draft = record(batch, draft)
    reordered = manage(batch, draft, {"op": "reorder", "line_ids": [line.line_id for line in reversed(draft.lines)]})
    assert reordered.conditions[0].status == "RECORDED"
    added = manage(batch, draft, {"op": "add", "label": "新增明細"})
    assert added.conditions[0].status == "STALE"
    assert state(added, 2)["state"] == "UNSPECIFIED"  # 不擅自擴大共用範圍。
    removed = manage(batch, draft, {"op": "remove", "line_id": draft.lines[0].line_id})
    restored = manage(batch, removed, {"op": "restore", "line_id": draft.lines[0].line_id})
    assert restored.conditions[0].status == "STALE"


def test_configuration_change_stales_evidence_until_explicit_rerecord(condition_case):
    batch, draft = condition_case
    draft = record(batch, draft)
    changed = manage(batch, draft, {"op": "configure", "line_id": draft.lines[0].line_id, "proposal": {"qty": 8}})
    assert changed.conditions[0].status == "STALE"
    restored = manage(batch, changed, {"op": "configure", "line_id": draft.lines[0].line_id, "proposal": {"qty": 2}})
    assert restored.conditions[0].status == "STALE"
    checked = record(batch, restored, condition_id=draft.conditions[0].condition_id)
    assert checked.conditions[0].status == "RECORDED" and len(checked.conditions[0].evidence) == 2
    assert checked.revision == restored.revision + 1


def test_split_source_and_parent_change_stale_dependent_exception(condition_case):
    batch, draft = condition_case
    draft = record(batch, draft)
    parent = draft.conditions[0].condition_id
    draft = record(batch, draft, scope="line", targets=[draft.lines[0].line_id], state="FORBIDDEN", overrides=[parent])
    changed = record(batch, draft, condition_id=parent, reference="新依據-v2")
    assert changed.conditions[0].status == "RECORDED" and changed.conditions[1].status == "STALE"
    assert state(changed)["state"] == "CONFLICT"  # 舊例外不能繼續壓掉新父條件。
    split = apply_review_command(draft, {"op": "split", "requirement_id": draft.requirements[-1].requirement_id, "offset": 4},
                                 draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="拆分", source_batch=batch)
    assert all(c.status == "STALE" for c in split.conditions)


def test_withdraw_retains_history_and_invalidates_children(condition_case):
    batch, draft = condition_case
    draft = record(batch, draft)
    parent = draft.conditions[0].condition_id
    draft = record(batch, draft, scope="line", targets=[draft.lines[0].line_id], state="FORBIDDEN", overrides=[parent])
    changed = record(batch, draft, {"op": "withdraw", "condition_id": parent})
    assert [c.status for c in changed.conditions] == ["WITHDRAWN", "STALE"]
    assert changed.blockers == draft.blockers and changed.requirements == draft.requirements
    assert changed.conditions[0].evidence == draft.conditions[0].evidence


@pytest.mark.parametrize("change", ["same_scope", "other_subject", "outside_parent", "self"])
def test_invalid_override_cannot_create_implicit_priority(condition_case, change):
    batch, draft = condition_case
    draft = record(batch, draft, scope="group", group_label="主席", targets=[draft.lines[0].line_id])
    parent = draft.conditions[0].condition_id
    command = record_command(draft, scope="line", targets=[draft.lines[0].line_id], overrides=[parent])
    if change == "same_scope":
        command.update(scope="group", group_label="列席")
    elif change == "other_subject":
        command["subject"] = "leg_panel_hole"
    elif change == "outside_parent":
        command["targets"] = [draft.lines[1].line_id]
    else:
        command["condition_id"] = parent
    with pytest.raises(ValueError, match="例外"):
        record(batch, draft, command)


def test_stale_source_and_revision_rejected(condition_case):
    batch, draft = condition_case
    changed = replace(batch, revision=batch.revision + 1)
    with pytest.raises(ValueError, match="來源工單"):
        record(changed, draft)
    with pytest.raises(ValueError, match="版本"):
        apply_condition_command(draft, record_command(draft), draft_id=draft.draft_id,
                                expected_revision=True, actor="測試", reason="核對", source_batch=batch)


def test_report_is_readonly_and_export_has_evidence_without_prices(condition_case):
    batch, draft = condition_case
    draft = record(batch, draft)
    before = deepcopy(draft)
    condition_report(draft)
    refresh_conditions(draft)
    assert draft == before
    exported = export_multi_draft(draft)
    assert exported["conditions"][0]["requirement_ids"] == draft.conditions[0].requirement_ids
    assert exported["conditions"][0]["evidence"][0]["reference"] == "人工核對-v1"
    assert exported["status"] == "CONFIGURATION_ONLY"


def test_required_missing_part_is_reported_not_silently_added(condition_case):
    batch, draft = condition_case
    target = draft.lines[0].line_id
    changed = record(batch, draft, scope="line", targets=[target], bindings=[{"line_id": target, "path": HOLE}])
    assert changed.lines == draft.lines
    assert any(i["kind"] == "missing_selection" for i in condition_report(changed)["issues"])


def test_negative_checks_active_missing_node_and_product_binding(condition_case):
    batch, draft = condition_case
    target = draft.lines[0].line_id
    draft = record(batch, draft, scope="line", targets=[target], state="FORBIDDEN", bindings=[{"line_id": target, "path": HOLE}])
    config = deepcopy(draft.lines[0].configuration)
    assert HOLE not in config["selections"]
    with pytest.raises(ValueError, match="自動必選"):
        validate_condition_configuration(draft, target, config, {HOLE})
    config["prodkind"] = "OTHER"
    with pytest.raises(ValueError, match="產品已變更"):
        validate_condition_configuration(draft, target, config, set())
