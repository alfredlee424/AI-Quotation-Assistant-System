"""多明細配置核心：不計價，來源／配置互相隔離，錯誤操作不留下半套修改。"""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from agent.multi_quote import (
    apply_multi_command, check_source_current, export_multi_draft,
    from_single_quote, from_work_order, new_multi_quote,
)
from agent.work_orders import parse_work_orders, reclassify_line
from work_order_fixtures import CASES, combined_input


def command(draft, operation, **kwargs):
    return apply_multi_command(draft, operation, draft_id=draft.draft_id,
                               expected_revision=draft.revision, actor="測試人員", reason="驗證明細操作", **kwargs)


def two_lines():
    draft = command(new_multi_quote(), {"op": "add", "label": "桌型一"})
    return command(draft, {"op": "add", "label": "桌型二"})


def test_new_multi_draft_has_no_default_product_quantity_or_prices():
    draft = two_lines()
    assert draft.schema_version == 1 and draft.revision == 2
    assert draft.status == "CONFIGURATION_ONLY" and draft.blockers
    assert draft.lines[0].line_id != draft.lines[1].line_id
    for line in draft.lines:
        assert line.configuration["prodkind"] is None
        assert line.configuration["qty"] is None
        assert line.configuration["selections"] == {}
        assert line.configuration["questions"]
        assert line.configuration["preview"] is None
    draft.lines[0].configuration["selections"]["example"] = {}
    assert draft.lines[1].configuration["selections"] == {}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key)
def test_work_order_adapter_preserves_source_without_confirming_candidate_numbers(case):
    batch = parse_work_orders(case.raw_text)
    draft = from_work_order(batch, batch.orders[0].order_id)
    assert len(draft.lines) == len(case.products)
    assert [line.line_id for line in draft.lines] == [item.item_id for item in batch.orders[0].items]
    assert all(line.configuration["qty"] is None and line.configuration["prodkind"] is None for line in draft.lines)
    assert [line["raw"] for line in draft.source["lines"]] == list(case.lines)
    assert draft.source["candidates"]
    assert draft.source["batch_revision"] == batch.revision
    assert draft.blockers
    check_source_current(draft, batch)


def test_work_order_adapter_keeps_unassigned_preamble_without_copying_other_orders():
    batch = parse_work_orders("單位與共用條件待核對\n" + combined_input())
    draft = from_work_order(batch, batch.orders[0].order_id)
    assert any(line["raw"] == "單位與共用條件待核對" for line in draft.source["unassigned_lines"])
    assert not any("測試客戶B" in line["raw"] for line in draft.source["lines"])
    other = from_work_order(batch, batch.orders[1].order_id)
    assert other.draft_id != draft.draft_id
    assert {line.line_id for line in other.lines}.isdisjoint(line.line_id for line in draft.lines)


def test_transfer_does_not_include_other_orders_classification_history():
    batch = parse_work_orders(combined_input())
    other_line_id = next(line.line_id for line in batch.lines if line.raw == "撕保護膜腳粒鎖好")
    batch = reclassify_line(batch, line_id=other_line_id, kind="note", expected_revision=0,
                            actor="測試", reason="另一張工單的分類修正")
    draft = from_work_order(batch, batch.orders[0].order_id)
    assert draft.source["classification_history"] == []
    assert "另一張工單的分類修正" not in json.dumps(export_multi_draft(draft), ensure_ascii=False)


def test_work_order_adapter_rejects_unknown_order_and_missing_candidates():
    batch = parse_work_orders("20990101-1測試\n只有備註")
    with pytest.raises(ValueError, match="找不到"):
        from_work_order(batch, "another-order")
    with pytest.raises(ValueError, match="尚無明細"):
        from_work_order(batch, batch.orders[0].order_id)


def test_work_order_configuration_rejects_stale_source_before_any_query(monkeypatch):
    from database import repository as repo
    batch = parse_work_orders(CASES[0].raw_text)
    draft = from_work_order(batch, batch.orders[0].order_id)
    changed = reclassify_line(batch, line_id=batch.lines[-1].line_id, kind="note",
                              expected_revision=0, actor="測試", reason="分類修正")
    def forbidden(**kwargs):
        pytest.fail("來源過期時不應查詢主檔")
    monkeypatch.setattr(repo, "get_product_categories", forbidden)
    for source in (None, changed, parse_work_orders(CASES[0].raw_text)):
        with pytest.raises(ValueError, match="來源工單"):
            command(draft, {"op": "add", "label": "新明細"}, source_batch=source)
    valid = command(draft, {"op": "add", "label": "新明細"}, source_batch=batch)
    assert len(valid.lines) == 5


def test_remove_restore_reorder_keep_identity_provenance_and_independent_revisions():
    batch = parse_work_orders(CASES[0].raw_text)
    draft = from_work_order(batch, batch.orders[0].order_id)
    original = export_multi_draft(draft)
    ids = [line.line_id for line in draft.lines]
    moved = command(draft, {"op": "reorder", "line_ids": ids[::-1]}, source_batch=batch)
    assert [line.line_id for line in moved.lines] == ids[::-1]
    assert all(line.revision == 0 for line in moved.lines)
    removed = command(moved, {"op": "remove", "line_id": ids[0]}, source_batch=batch)
    assert removed.archived_lines[0].line_id == ids[0]
    assert removed.archived_lines[0].revision == 1
    assert removed.archived_lines[0].source_line_ids == draft.lines[0].source_line_ids
    assert removed.source == draft.source and removed.blockers == draft.blockers
    restored = command(removed, {"op": "restore", "line_id": ids[0]}, source_batch=batch)
    assert restored.lines[-1].line_id == ids[0] and restored.lines[-1].revision == 2
    assert not restored.archived_lines and restored.revision == 3
    assert export_multi_draft(draft) == original
    assert [event.operation for event in restored.changes] == ["reorder", "remove", "restore"]


def test_same_product_paths_are_scoped_by_line_and_changes_are_validated(cmt1_db):
    draft = two_lines()
    first, second = (line.line_id for line in draft.lines)
    left = command(draft, {"op": "configure", "line_id": first,
                           "proposal": {"prodkind": "CMT1", "qty": 2,
                                        "changes": [{"op": "set", "path": r"CMT1\A001", "code": "C003"}]}})
    both = command(left, {"op": "configure", "line_id": second,
                          "proposal": {"prodkind": "CMT1", "qty": 6,
                                       "changes": [{"op": "set", "path": r"CMT1\A001", "code": "C005"}]}})
    assert both.lines[0].configuration["selections"][r"CMT1\A001"]["code"] == "C003"
    assert both.lines[1].configuration["selections"][r"CMT1\A001"]["code"] == "C005"
    assert [line.configuration["qty"] for line in both.lines] == [2, 6]
    assert [line.revision for line in both.lines] == [1, 1]
    changed = command(both, {"op": "configure", "line_id": first, "proposal": {"qty": 3}})
    assert changed.lines[1] == both.lines[1]
    assert changed.lines[0].configuration["questions"] == both.lines[0].configuration["questions"]
    assert changed.lines[0].configuration["preview"] is None
    assert draft.lines[0].configuration["selections"] == {}


@pytest.mark.parametrize("proposal", [
    {"qty": True}, {"qty": float("nan")}, {"qty": 0}, {"qty": "3"},
    {"prodkind": "MISSING"}, {"questions": []}, {"discount_rate": .05}, {"total_price": 1},
    {"changes": [{"op": "set", "path": r"CMT1\A001", "code": "C003", "compri": 1}]},
    {"changes": [{"op": "set", "path": r"OTHER\A001", "code": "C003"}]},
    {"changes": [{"op": "set", "path": r"CMT1\A001", "code": "NOT_REAL"}]},
    {"changes": [{"op": "set", "path": r"CMT1\A001", "code": "C003", "line_qty": 0}]},
    {}, {"changes": []},
])
def test_invalid_proposal_cannot_mutate_lines_or_drop_blockers(current_draft, proposal):
    draft = from_single_quote(current_draft)
    before = export_multi_draft(draft)
    with pytest.raises(ValueError):
        command(draft, {"op": "configure", "line_id": draft.lines[0].line_id, "proposal": proposal})
    assert export_multi_draft(draft) == before


def test_partially_valid_proposal_rolls_back_all_changes(current_draft):
    draft = from_single_quote(current_draft)
    before = export_multi_draft(draft)
    with pytest.raises(ValueError):
        command(draft, {"op": "configure", "line_id": draft.lines[0].line_id, "proposal": {
            "qty": 99, "changes": [
                {"op": "set", "path": r"CMT1\A001", "code": "C003"},
                {"op": "set", "path": r"CMT1\UNKNOWN", "code": "C001"},
            ],
        }})
    assert export_multi_draft(draft) == before


def test_product_change_resets_only_target_line_configuration(current_draft, monkeypatch):
    from database import repository as repo
    from engine import configuration
    draft = from_single_quote(current_draft)
    draft = command(draft, {"op": "add", "label": "其他產品"})
    first, second = (line.line_id for line in draft.lines)
    categories = repo.get_product_categories(workgroup=draft.workgroup)
    monkeypatch.setattr(repo, "get_product_categories", lambda **kwargs: [
        *categories, {"prodkind": "OTHER", "codsc": "另一類產品"},
    ])
    original_load = configuration.load_catalog
    def load(product, workgroup):
        if product != "OTHER":
            return original_load(product, workgroup)
        return {"prodkind": "OTHER", "nodes": {r"OTHER\A001": {"pathf": "OTHER", "must_chose": "Y"}},
                "options": {r"OTHER\A001": [{"code": "SIZE", "codsc": "測試尺寸", "compri": 0}]},
                "labels": {"A001": "尺寸"}}
    monkeypatch.setattr(configuration, "load_catalog", load)
    changed = command(draft, {"op": "configure", "line_id": first, "proposal": {"prodkind": "OTHER"}})
    assert changed.lines[0].line_id == first
    assert set(changed.lines[0].configuration["selections"]) == {r"OTHER\A001"}
    assert changed.lines[1] == draft.lines[1] and changed.lines[1].line_id == second
    assert changed.source == draft.source and changed.blockers == draft.blockers


@pytest.mark.parametrize("operation", [
    {"op": "remove", "line_id": "not-in-draft"},
    {"op": "restore", "line_id": "not-in-draft"},
    {"op": "add", "label": ""}, {"op": "add", "label": "x" * 201},
    {"op": "add", "label": "桌", "cost": 0},
    {"op": "reorder", "line_ids": []}, {"op": "reorder", "line_ids": ["bad", "bad"]},
    {"op": "reorder", "line_ids": "bad"}, {"op": "reorder", "line_ids": [True]},
    {"op": "approve"}, {"op": "create_quote"}, None, {},
])
def test_invalid_management_commands_are_atomic(operation):
    draft = two_lines()
    before = export_multi_draft(draft)
    with pytest.raises(ValueError):
        command(draft, operation)
    assert export_multi_draft(draft) == before


@pytest.mark.parametrize("extra", [
    {"draft_id": "wrong"}, {"expected_revision": -1}, {"expected_revision": False},
    {"actor": ""}, {"reason": None}, {"actor": "x" * 81}, {"reason": "x" * 1001},
])
def test_wrong_identity_revision_or_audit_is_rejected(extra):
    draft = two_lines()
    arguments = dict(draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="測試")
    arguments.update(extra)
    with pytest.raises(ValueError):
        apply_multi_command(draft, {"op": "add", "label": "桌"}, **arguments)


def test_other_line_ownership_removed_line_and_noop_reordering_are_rejected():
    first, second = two_lines(), two_lines()
    with pytest.raises(ValueError, match="找不到"):
        command(first, {"op": "remove", "line_id": second.lines[0].line_id})
    with pytest.raises(ValueError, match="未改變"):
        command(first, {"op": "reorder", "line_ids": [line.line_id for line in first.lines]})
    removed = command(first, {"op": "remove", "line_id": first.lines[0].line_id})
    with pytest.raises(ValueError, match="找不到"):
        command(removed, {"op": "configure", "line_id": first.lines[0].line_id, "proposal": {"qty": 1}})
    empty = command(removed, {"op": "remove", "line_id": removed.lines[0].line_id})
    assert not empty.lines and len(empty.archived_lines) == 2 and empty.blockers


def test_single_adapter_strips_preview_and_live_prices_but_keeps_provenance(current_draft):
    from engine.preview import freeze_preview
    freeze_preview(current_draft)
    current_draft.update(status="SNAPSHOT_CREATED", ref_no="Q-SOURCE", discount_rate=.05)
    current_draft["questions"] = ["配件數量尚待確認"]
    current_draft["pending_options"] = [{"code": "候選"}]
    current_draft["imported_quote"] = {"source_rows": [{"source_compri": 700}], "warnings": ["舊名待核對"]}
    before = deepcopy(current_draft)
    draft = from_single_quote(current_draft)
    config = draft.lines[0].configuration
    assert draft.discount_rate == .05 and config["discount_rate"] == 0
    assert config["ref_no"] is config["preview"] is config["calc_result"] is None
    assert config["status"] == "WAITING_FOR_INPUT"
    assert "配件數量尚待確認" in config["questions"]
    assert all("compri" not in item for item in config["selections"].values())
    assert draft.source["snapshot"]["ref_no"] == "Q-SOURCE"
    assert draft.source["snapshot"]["imported_quote"] == before["imported_quote"]
    assert draft.source["snapshot"]["pending_options"] == before["pending_options"]
    assert any("匯入警告" in blocker for blocker in draft.blockers)
    updated = command(draft, {"op": "configure", "line_id": draft.lines[0].line_id, "proposal": {"qty": 2}})
    assert updated.blockers == draft.blockers
    assert updated.source == draft.source
    assert current_draft == before


def test_complete_internal_configuration_cannot_use_old_single_preview(current_draft):
    from agent.tools import preview_quote
    draft = from_single_quote(current_draft)
    with pytest.raises(ValueError, match="未確認"):
        preview_quote(draft.lines[0].configuration)


def test_legacy_13_column_adapter_preserves_qty_meaning_and_source_warnings(cmt1_db):
    from agent.quotation_importer import import_pasted_quote
    text = "103 QTEST CMT1\\A001\\S019\\S030 2 C001 6公分圓孔 1 1 350 2099.01.01 00:00:00 test source"
    imported = import_pasted_quote(text, product_qty=5)
    draft = from_single_quote(imported.draft)
    assert draft.lines[0].configuration["qty"] == 5
    assert draft.lines[0].configuration["selections"][r"CMT1\A001\S019\S030"]["line_qty"] == 2
    assert draft.source["snapshot"]["imported_quote"]["source_rows"] == imported.rows
    assert "source_compri" not in draft.lines[0].configuration["selections"][r"CMT1\A001\S019\S030"]


@pytest.mark.parametrize("mutations", [
    {"workgroup": "999"}, {"selections": []}, {"qty": float("inf")},
    {"discount_rate": .99}, {"questions": [1]}, {"imported_quote": "invalid"},
])
def test_invalid_single_adapter_input_is_rejected(current_draft, mutations):
    source = deepcopy(current_draft)
    source.update(mutations)
    with pytest.raises(ValueError):
        from_single_quote(source)


def test_duplicate_conflicting_paths_are_not_flattened_by_adapter(current_draft):
    current_draft["selections"]["alias"] = {"path": r"CMT1\A001", "code": "conflict", "line_qty": 1}
    with pytest.raises(ValueError, match="衝突"):
        from_single_quote(current_draft)


def test_export_deep_copy_and_no_database_writes(current_draft, cmt1_db):
    from database.models import QuoteSnapshotDocument, ordqdt_ai
    draft = from_single_quote(current_draft)
    document = export_multi_draft(draft)
    assert document["document_type"] == "multi_quote_configuration_only"
    json.dumps(document, ensure_ascii=False, allow_nan=False)
    document["lines"][0]["configuration"]["qty"] = 99
    assert draft.lines[0].configuration["qty"] == current_draft["qty"]
    with cmt1_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == db.query(ordqdt_ai).count() == 0


@pytest.mark.parametrize("updates", [{"status": "PREVIEW"}, {"workgroup": "999"}, {"schema_version": 999}])
def test_unsupported_draft_state_cannot_be_modified(updates):
    draft = replace(two_lines(), **updates)
    with pytest.raises(ValueError):
        command(draft, {"op": "add", "label": "桌"})
