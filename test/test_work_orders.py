"""階段 1：實際切分器行為；期望資料不由 production 匯入或反查。"""
from dataclasses import FrozenInstanceError
import hashlib
import json

import pytest

from agent.work_orders import MAX_LINES, MAX_TEXT_LENGTH, export_review, parse_work_orders, reclassify_line
from work_order_fixtures import CASES, combined_input


def change(batch, line, new_kind, **overrides):
    arguments = dict(line_id=line.line_id, kind=new_kind, expected_revision=batch.revision,
                     actor="測試人員", reason="人工核對來源邊界")
    arguments.update(overrides)
    return reclassify_line(batch, **arguments)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key)
def test_single_order_preserves_every_line_and_extracts_only_item_candidates(case):
    batch = parse_work_orders(case.raw_text)
    assert batch.raw_text == case.raw_text
    assert tuple(line.raw for line in batch.lines) == case.lines
    assert len(batch.orders) == 1
    order = batch.orders[0]
    assert order.family_hint == ""  # 不偷讀樣本答案或猜產品代碼。
    assert len(order.items) == len(case.products)
    by_id = {line.line_id: line for line in batch.lines}
    for item, expected in zip(order.items, case.products):
        assert by_id[item.source_line_id].number == expected.line
        assert item.quantity_candidate == expected.quantity
        assert expected.dimension_raw in item.description_raw
        assert item.status == "UNREVIEWED"
    assert set(order.line_ids) == set(by_id)
    assert order.header_line_id in order.context_line_ids
    assert order.status == batch.status == "REVIEW_REQUIRED"
    assert order.warnings
    assert not {"preview", "calc", "prodkind", "selections", "total_price"} & set(export_review(batch))


def test_combined_input_splits_eight_orders_without_losing_or_sharing_lines():
    text = combined_input()
    batch = parse_work_orders(text)
    assert len(batch.orders) == 8
    assert [order.family_hint for order in batch.orders] == [case.family for case in CASES]
    assert [len(order.items) for order in batch.orders] == [4, 4, 4, 1, 1, 1, 3, 3]
    all_ids = [key for order in batch.orders for key in order.line_ids] + list(batch.unassigned_line_ids)
    assert len(all_ids) == len(set(all_ids)) == len(batch.lines)
    assert set(all_ids) == {line.line_id for line in batch.lines}
    assert len({item.item_id for order in batch.orders for item in order.items}) == 21
    assert batch.source_digest == hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("heading,family", [
    ("環式會議桌{生產工單}案例，Trello實際內容", "環式會議桌"),
    ("會議桌生產單案例，Terllo內的資料", "會議桌"),
    ("Trello內餐桌生產單", "餐桌"),
    ("餐桌", "餐桌"),
])
def test_realistic_section_headings_supply_hints_not_database_codes(heading, family):
    batch = parse_work_orders(heading + "\n" + CASES[0].raw_text)
    assert batch.lines[0].kind == "section"
    assert batch.orders[0].family_hint == family


def test_unknown_preamble_and_orphan_product_are_not_applied_to_orders():
    batch = parse_work_orders("單位混寫待確認\n平直70X70------2\n" + CASES[0].raw_text)
    assert batch.unassigned_line_ids == tuple(line.line_id for line in batch.lines[:2])
    assert len(batch.orders[0].items) == 4
    assert batch.lines[0].kind == "unclassified"


def test_new_section_closes_previous_order_and_preserves_orphan_content():
    batch = parse_work_orders(CASES[0].raw_text + "\n餐桌生產單\n需另補資料")
    assert batch.lines[-1].line_id in batch.unassigned_line_ids
    assert batch.lines[-1].line_id not in batch.orders[0].line_ids


def test_header_specifications_negation_typos_and_diagrams_remain_uninterpreted():
    for case in (CASES[0], CASES[2], CASES[-1]):
        batch = parse_work_orders(case.raw_text)
        assert batch.lines[0].line_id in batch.orders[0].context_line_ids
        assert batch.lines[-1].raw == case.lines[-1]
    last = parse_work_orders(CASES[-1].raw_text)
    assert "木心板12尺圓桌" in last.lines[0].raw
    assert "實內尺寸" in last.lines[7].raw
    assert last.lines[7].kind == "unclassified"
    first = parse_work_orders(CASES[0].raw_text)
    assert "面無孔有桌下走線" in first.orders[0].items[0].suffix_raw


def test_invalid_and_duplicate_source_dates_are_not_corrected_or_merged():
    invalid = parse_work_orders(CASES[3].raw_text)
    assert invalid.orders[0].source_ref_raw == "209901321-4"
    assert any("日期格式異常" in warning for warning in invalid.orders[0].warnings)
    duplicate = parse_work_orders(CASES[0].raw_text + "\n" + CASES[0].raw_text)
    assert len(duplicate.orders) == 2
    assert duplicate.orders[0].order_id != duplicate.orders[1].order_id
    assert all(any("重複" in warning for warning in order.warnings) for order in duplicate.orders)
    assert set(item.item_id for item in duplicate.orders[0].items).isdisjoint(
        item.item_id for item in duplicate.orders[1].items)


@pytest.mark.parametrize("quantity", ["0", "1.5", "2e3", "2/3", "1" * 5000])
def test_invalid_piece_counts_never_become_positive_integer_candidates(quantity):
    batch = parse_work_orders("20990101-1測試\n桌面70X70-----" + quantity)
    assert batch.orders[0].items[0].quantity_candidate is None
    assert any("正整數" in warning for warning in batch.orders[0].warnings)


@pytest.mark.parametrize("text", [None, "", " \n\t", "a" * (MAX_TEXT_LENGTH + 1), "a\n" * (MAX_LINES + 1)])
def test_empty_and_oversized_input_is_rejected(text):
    with pytest.raises(ValueError):
        parse_work_orders(text)


def test_crlf_and_blank_lines_preserve_exact_source_and_distinct_batch_identity():
    text = "\r\n20990101-1測試\r\n\r\n桌面70X70-----2\r\n"
    first, second = parse_work_orders(text), parse_work_orders(text)
    assert first.raw_text == text
    assert first.source_digest == second.source_digest
    assert first.batch_id != second.batch_id
    assert [line.number for line in first.lines] == [1, 2, 3, 4]
    assert first.lines[0].kind == first.lines[2].kind == "blank"


@pytest.mark.parametrize("tail", ["", "不是數量"])
def test_long_separator_without_quantity_is_preserved_as_unclassified(tail):
    raw = "桌面" + "-" * 20_000 + tail
    batch = parse_work_orders("20990101-1測試\n" + raw)
    assert batch.lines[1].raw == raw
    assert batch.lines[1].kind == "unclassified"
    assert not batch.orders[0].items


def test_classification_change_is_immutable_audited_and_preserves_unrelated_item_ids():
    batch = parse_work_orders(combined_input())
    line = next(line for line in batch.lines if line.raw == "單面置物版")
    updated = change(batch, line, "item")
    assert batch.revision == 0 and batch.corrections == ()
    assert updated.revision == 1 and updated.raw_text == batch.raw_text
    assert updated.source_digest == batch.source_digest
    assert [item.item_id for item in updated.orders[0].items[:4]] == [item.item_id for item in batch.orders[0].items]
    assert updated.orders[1:] == batch.orders[1:]
    added = updated.orders[0].items[-1]
    assert added.quantity_candidate is None
    audit = updated.corrections[0]
    assert (audit.line_id, audit.before_kind, audit.after_kind) == (line.line_id, "unclassified", "item")
    assert audit.reason and audit.actor and audit.recorded_at
    restored = change(updated, line, "unclassified")
    readded = change(restored, line, "item")
    assert readded.orders[0].items[-1].item_id == added.item_id
    with pytest.raises(FrozenInstanceError):
        updated.raw_text = "changed"


def test_manual_split_then_merge_preserves_item_identity_without_source_loss():
    batch = parse_work_orders("餐桌生產單\n20990101-1測試\n主桌-----1\n轉盤組件\n轉盤-----1")
    line = batch.lines[3]
    original_ids = [item.item_id for item in batch.orders[0].items]
    split = change(batch, line, "order", family_hint="餐桌")
    assert len(split.orders) == 2
    assert [item.item_id for order in split.orders for item in order.items] == original_ids
    assert split.orders[0].order_id == batch.orders[0].order_id
    assert any("人工指定" in warning for warning in split.orders[1].warnings)
    merged = change(split, line, "note")
    assert len(merged.orders) == 1
    assert [item.item_id for item in merged.orders[0].items] == original_ids
    assert line.line_id in merged.orders[0].context_line_ids


def test_manual_header_can_start_an_unrecognized_order_and_set_family():
    batch = parse_work_orders("客製桌需求\n圓桌-----1")
    assert not batch.orders
    fixed = change(batch, batch.lines[0], "order", family_hint="餐桌")
    assert len(fixed.orders) == 1
    assert fixed.orders[0].family_hint == "餐桌"
    assert fixed.orders[0].status == "REVIEW_REQUIRED"


@pytest.mark.parametrize("overrides", [
    {"line_id": "another-batch-line"}, {"kind": "approved"}, {"kind": "blank"},
    {"kind": "section", "family_hint": ""}, {"family_hint": "餐桌"},
    {"actor": " "}, {"actor": None}, {"reason": ""}, {"reason": None},
    {"expected_revision": -1}, {"expected_revision": False},
    {"family_hint": "UNKNOWN"}, {"reason": "a" * 1001},
])
def test_invalid_corrections_leave_original_unchanged(overrides):
    batch = parse_work_orders(CASES[0].raw_text)
    before = export_review(batch)
    with pytest.raises(ValueError):
        change(batch, batch.lines[-1], "note", **overrides)
    assert export_review(batch) == before


def test_stale_noop_and_blank_line_corrections_are_rejected():
    batch = parse_work_orders(CASES[0].raw_text + "\n\n")
    with pytest.raises(ValueError, match="未變更"):
        change(batch, batch.lines[1], "item")
    with pytest.raises(ValueError, match="空白"):
        change(batch, batch.lines[-1], "order")
    updated = change(batch, batch.lines[-2], "note")
    with pytest.raises(ValueError, match="版本"):
        change(updated, batch.lines[1], "note", expected_revision=0)


def test_review_export_is_json_serializable_and_cannot_mutate_live_batch():
    batch = parse_work_orders(combined_input())
    document = export_review(batch)
    assert document["document_type"] == "work_order_review_only"
    assert json.loads(json.dumps(document, ensure_ascii=False))["raw_text"] == combined_input()
    document["orders"][0]["status"] = "PREVIEW"
    assert batch.orders[0].status == "REVIEW_REQUIRED"
