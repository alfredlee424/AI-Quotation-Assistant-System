"""多明細快照組裝演練：同路徑隔離、容量、版本與舊入口阻擋。"""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from database import repository as repo
from engine import multi_snapshot as layout
from engine.multi_trial import _digest
from test_multi_trial import two_cmt, mixed_products, build, command


def assemble(draft, trial=None, actor="測試"):
    return layout.build_snapshot_layout(draft, trial or build(draft), actor=actor)


def resign(trial):
    trial["content_digest"] = _digest({k: v for k, v in trial.items() if k != "content_digest"})
    return trial


def test_same_paths_stay_in_separate_products_and_have_global_sequences(two_cmt):
    trial = build(two_cmt)
    before = deepcopy(trial)
    result = assemble(two_cmt, trial)
    assert result["row_count"] == sum(len(r["calculation"]["items"]) for r in trial["lines"])
    assert [r["seq_no"] for r in result["rows"]] == [f"{n:05d}" for n in range(1, result["row_count"] + 1)]
    assert len({(r["line_id"], r["path"]) for r in result["rows"]}) == result["row_count"]
    first, second = result["products"]
    same_path = [r for r in result["rows"] if r["path"] == r"CMT1\A001"]
    assert [r["line_id"] for r in same_path] == [first["line_id"], second["line_id"]]
    assert [r["snapshot_values"]["qty"] for r in same_path] == [1, 3]
    assert all(r["snapshot_values"]["compri"] == 0 for r in same_path)
    assert result["row_links"] == [{"workgroup": "103", "seq_no": r["seq_no"], "line_id": r["line_id"], "path": r["path"]}
                                   for r in result["rows"]]
    assert result["layout_complete"] and not result["storage_issues"]
    assert trial == before and result["totals"] == trial["totals"]
    assert not result["can_save"] and not result["can_confirm"] and not result["can_quote"]
    assert "ref_no" not in result and "preview_id" not in result
    for row, cost in zip(result["rows"], [i for r in trial["lines"] for i in r["calculation"]["items"]]):
        assert row["cost_evidence"] == cost and "ref_no" not in row["snapshot_values"]


def test_different_specs_at_same_path_do_not_mix(two_cmt):
    changed = command(two_cmt, {"op": "configure", "line_id": two_cmt.lines[1].line_id,
                               "proposal": {"changes": [{"op": "set", "path": r"CMT1\A001", "code": "C003"}]}})
    trial = build(changed)
    result = assemble(changed, trial)
    for source in trial["lines"]:
        by_path = {r["path"]: r for r in result["rows"] if r["line_id"] == source["line_id"]}
        for item in source["calculation"]["items"]:
            assert by_path[item["path"]]["cost_evidence"] == item
    dimensions = [r for r in result["rows"] if r["path"] == r"CMT1\A001"]
    assert dimensions[0]["snapshot_values"]["spc_code"] != dimensions[1]["snapshot_values"]["spc_code"]


def test_preserves_per_item_quantity_and_structure_nodes(two_cmt):
    draft = command(two_cmt, {"op": "configure", "line_id": two_cmt.lines[1].line_id,
                            "proposal": {"changes": [{"op": "set", "path": r"CMT1\A001\S010", "code": "C001", "line_qty": 2}]}})
    trial = build(draft)
    result = assemble(draft, trial)
    for frozen in trial["lines"]:
        rows = [r for r in result["rows"] if r["line_id"] == frozen["line_id"]]
        assert {r["path"] for r in rows} == set(frozen["calculation"]["selections"])
    row = next(r for r in result["rows"] if r["line_id"] == draft.lines[1].line_id and r["path"] == r"CMT1\A001\S010")
    assert row["cost_evidence"]["line_qty"] == 2 and row["snapshot_values"]["qty"] == 6


def test_build_and_check_never_query_master_or_write(two_cmt, monkeypatch):
    trial = build(two_cmt)
    monkeypatch.setattr(repo, "_session", Mock(side_effect=AssertionError("不可讀寫資料庫")))
    result = assemble(two_cmt, trial)
    checked = layout.checked_snapshot_layout(two_cmt, trial, result, actor="測試")
    assert checked == result and checked is not result
    repo._session.assert_not_called()


@pytest.mark.parametrize("index", [0, -1, 100000, True, 1.0, "1"])
def test_sequence_rejects_overflow_and_non_integer(index):
    with pytest.raises(ValueError):
        layout.snapshot_sequence(index)


def test_sequence_boundary():
    assert layout.snapshot_sequence(1) == "00001" and layout.snapshot_sequence(99999) == "99999"


def test_entire_layout_rejected_if_capacity_exceeded(two_cmt, monkeypatch):
    trial = build(two_cmt)
    monkeypatch.setattr(layout, "MAX_SNAPSHOT_ROWS", 1)
    with pytest.raises(ValueError, match="序號"):
        assemble(two_cmt, trial)


@pytest.mark.parametrize("change", ["duplicate", "foreign_line", "missing_item", "unknown_path", "code", "qty",
                                   "line_qty", "product_qty", "nan", "zero_divisor", "source", "driver", "selection_price"])
def test_inconsistent_frozen_row_is_rejected_even_with_matching_digest(two_cmt, change):
    trial = build(two_cmt)
    calc = trial["lines"][0]["calculation"]
    item = calc["items"][0]
    if change == "duplicate":
        calc["items"].append(deepcopy(item))
    elif change == "foreign_line":
        item["line_id"] = two_cmt.lines[1].line_id
    elif change == "missing_item":
        calc["items"].pop()
    elif change == "unknown_path":
        item["path"] = "OTHER\\A001"
    elif change == "code":
        item["spc_code"] = "WRONG"
    elif change in ("qty", "line_qty", "product_qty"):
        item[change] = 99
    elif change == "nan":
        item["compri"] = "NaN"
    elif change == "zero_divisor":
        item["stdpar"] = 0
    elif change == "source":
        item["source"] = "AI_PRICE"
    elif change == "driver":
        item.update(source="ordqty", driver_path="OTHER\\A001")
    else:
        calc["selections"][item["path"]]["compri"] = 99
    with pytest.raises(ValueError):
        assemble(two_cmt, resign(trial))


@pytest.mark.parametrize("change", ["order", "line_revision", "label", "status", "selection_path"])
def test_document_identity_and_shape_are_validated(two_cmt, change):
    trial = build(two_cmt)
    if change == "order":
        trial["lines"].reverse()
    elif change == "selection_path":
        selections = trial["lines"][0]["calculation"]["selections"]
        selections[next(iter(selections))]["path"] = "foreign"
    else:
        trial["lines"][0][change] = "invalid"
    with pytest.raises(ValueError):
        assemble(two_cmt, resign(trial))


def test_capacity_report_does_not_truncate_text(two_cmt):
    trial = build(two_cmt)
    item = trial["lines"][0]["calculation"]["items"][0]
    item["spdsc"] = "說明" * 30
    trial = resign(trial)
    result = assemble(two_cmt, trial)
    assert result["rows"][0]["snapshot_values"]["spdsc"] == "說明" * 30
    assert any(i["field"] == "spdsc" and i["limit"] == 40 for i in result["storage_issues"])
    assert not result["layout_complete"] and not result["can_save"]


def test_actor_capacity_and_no_silent_truncation(two_cmt):
    from engine.multi_trial import build_internal_trial
    actor = "操作人名稱超過八個字"
    trial = build_internal_trial(two_cmt, actor=actor)
    result = assemble(two_cmt, trial, actor=actor)
    assert result["actor"] == actor
    assert any(i["field"] == "addusrno" for i in result["storage_issues"])


@pytest.mark.parametrize("kind", ["blocked", "archived"])
def test_partial_layout_exposes_omissions_without_order_total(two_cmt, kind):
    if kind == "blocked":
        draft = command(two_cmt, {"op": "add", "label": "尚未指定規格"})
    else:
        draft = command(two_cmt, {"op": "remove", "line_id": two_cmt.lines[1].line_id})
    result = assemble(draft)
    assert result["rows"] and not result["layout_complete"] and result["totals"] is None
    assert result["excluded_lines"] if kind == "blocked" else result["archived_line_ids"]


def test_rounding_difference_does_not_reprice_or_allocate_discount(mixed_products):
    draft = command(mixed_products, {"op": "set_discount", "discount_rate": .1})
    trial = build(draft)
    result = assemble(draft, trial)
    assert result["totals"] == trial["totals"]
    assert [p["pricing"]["quote_rate"] for p in result["products"]] == [1.5, 2.0]
    assert [r["snapshot_values"]["amount"] for r in result["rows"]] == [.5, .67]
    assert result["display_row_amount_difference"] == 0
    assert [r["cost_evidence"]["compri"] for r in result["rows"]] == [.335, .335]


@pytest.mark.parametrize("mutation", ["reorder", "actor", "retry", "row", "relation", "totals", "version", "can_save"])
def test_layout_invalidates_on_context_or_content_change(two_cmt, mutation):
    trial = build(two_cmt)
    document = assemble(two_cmt, trial)
    actor = "測試"
    draft = two_cmt
    if mutation == "reorder":
        draft = command(draft, {"op": "reorder", "line_ids": [r.line_id for r in reversed(draft.lines)]})
    elif mutation == "actor":
        actor = "另一位"
    elif mutation == "retry":
        trial = build(draft)
    elif mutation == "row":
        document["rows"][0]["snapshot_values"]["amount"] += 1
    elif mutation == "relation":
        document["row_links"][0]["line_id"] = draft.lines[1].line_id
    elif mutation == "totals":
        document["totals"]["total_price"] += 1
    elif mutation == "version":
        document["layout_version"] = "future"
    else:
        document["can_save"] = True
    with pytest.raises(ValueError):
        layout.checked_snapshot_layout(draft, trial, document, actor=actor)


def test_old_save_entry_rejects_layout_before_connecting(two_cmt, monkeypatch):
    trial = build(two_cmt)
    document = assemble(two_cmt, trial)
    monkeypatch.setattr(repo, "_session", Mock(side_effect=AssertionError("不可開啟資料庫")))
    for preview in (trial, document):
        with pytest.raises(ValueError, match="既有單品"):
            repo.save_quote_snapshot(document["rows"], preview=preview)
    with pytest.raises(ValueError, match="既有單品"):
        repo.save_quote_snapshot(document["rows"])
    repo._session.assert_not_called()


def test_nonzero_display_rounding_difference_is_preserved(mixed_products, isolated_db):
    from database.models import Invdoc
    with isolated_db() as db:
        db.query(Invdoc).update({Invdoc.quo_rate: 1})
        db.commit()
    trial = build(mixed_products)
    result = assemble(mixed_products, trial)
    assert [r["snapshot_values"]["amount"] for r in result["rows"]] == [.34, .34]
    assert result["totals"]["subtotal"] == .67
    assert result["display_row_amount_difference"] == .01
    assert [p["pricing"]["raw_subtotal"] for p in result["products"]] == ["0.335", "0.335"]


def test_drawing_evidence_and_blockers_are_retained(two_cmt):
    from test_drawing_review import declare, record
    draft = record(declare(two_cmt))
    trial = build(draft)
    result = assemble(draft, trial)
    assert result["drawings"] == trial["drawings"]
    assert result["order_blockers"] == trial["order_blockers"]
    changed = record(draft, version="R2")
    with pytest.raises(ValueError):
        layout.checked_snapshot_layout(changed, trial, result, actor="測試")
