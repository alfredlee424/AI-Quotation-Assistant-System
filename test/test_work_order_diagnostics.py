"""唯讀數值正規化與工單歧義診斷；不從測試候選答案反推解析結果。"""
from copy import deepcopy
import json

import pytest

from agent.work_order_diagnostics import diagnose_work_order
from agent.work_orders import export_review, parse_work_orders, reclassify_line
from work_order_fixtures import CASES, combined_input


def diagnose(text):
    return diagnose_work_order(parse_work_orders("20990101-1測試\n" + text))


def findings(result, kind):
    return [item for item in result["findings"] if item["kind"] == kind]


@pytest.mark.parametrize("text,expected", [
    ("桌面60cmX180cm", ("60", "180")),
    ("桌面600X1800mm", ("60", "180")),
    ("桌面600毫米X180公分", ("60", "180")),
    ("桌面60公分×180", ("60", "180")),
    ("桌面６００ｍｍＸ１８００ｍｍ", ("60", "180")),
    ("桌面60.125cm*180.5cm", ("60.125", "180.5")),
])
def test_explicit_metric_dimensions_normalize_exactly(text, expected):
    result = diagnose(text)
    item = findings(result, "table_dimensions_candidate")[0]
    assert item["centimeters"] == expected
    assert item["status"] == "NORMALIZED"
    assert not result["can_apply_configuration"] and not result["can_quote"]


@pytest.mark.parametrize("text", ["60X180", "1200X600", "3X3.25", "5X14"])
def test_unitless_dimensions_never_assume_centimeters_or_millimeters(text):
    result = diagnose(text)
    item = findings(result, "table_dimensions_candidate")[0]
    assert item["centimeters"] is None and item["units"] == ("unknown", "unknown")
    assert any(issue["code"] == "unit_required" for issue in result["issues"])


@pytest.mark.parametrize("text", ["9尺圓", "12台尺圓", "3尺X6尺", "3台尺X6台尺"])
def test_foot_policy_is_required_and_no_conversion_is_invented(text):
    result = diagnose(text)
    assert result["findings"]
    assert all(item["centimeters"] is None for item in result["findings"])
    assert any(issue["code"] == "foot_policy_required" for issue in result["issues"])


def test_tabletop_and_wire_box_dimensions_have_separate_roles():
    result = diagnose("桌面60cmX180cm，線盒13.5cmX40cm，數量3張")
    assert findings(result, "table_dimensions_candidate")[0]["centimeters"] == ("60", "180")
    assert findings(result, "accessory_dimensions_candidate")[0]["centimeters"] == ("13.5", "40")
    assert findings(result, "item_count")[0]["values"] == ("3",)


def test_false_thickness_is_not_board_thickness_and_decimal_conversion_is_exact():
    result = diagnose("基板厚18MM，桌緣假厚36MM，轉盤假厚3.6公分")
    assert findings(result, "board_thickness")[0]["centimeters"] == ("1.8",)
    assert [item["centimeters"] for item in findings(result, "false_thickness")] == [("3.6",), ("3.6",)]
    assert len([issue for issue in result["issues"] if issue["code"] == "false_thickness_not_board"]) == 2


def test_hole_count_diameter_box_and_row_count_are_not_multiplied():
    result = diagnose("平直70X140------4 面2孔6公分圓孔，單鋁13.5X40")
    assert findings(result, "row_quantity_candidate")[0]["values"] == ("4",)
    holes = findings(result, "hole_count")[0]
    assert holes["values"] == ("2",) and holes["scope"] == "per_product_candidate"
    assert findings(result, "hole_diameter_candidate")[0]["centimeters"] == ("6",)
    assert "total_holes" not in result


def test_each_table_scope_applies_to_both_counts_without_inferring_table_total():
    result = diagnose("每桌1個銀色單掀鋁毛刷13.5X40及1孔6公分圓孔")
    counts = [item for item in result["findings"] if item["kind"] in ("item_count", "hole_count")]
    assert len(counts) == 2
    assert all(item["scope"] == "per_product_explicit" for item in counts)
    assert not findings(result, "row_quantity_candidate")


def test_panel_count_is_not_thickness_or_product_count():
    result = diagnose("5X14(5X7X2)船形單鋁13.5X60---------2")
    assert findings(result, "panel_dimensions_candidate")[0]["values"] == ("5", "7")
    assert findings(result, "panel_count_candidate")[0]["values"] == ("2",)
    assert findings(result, "row_quantity_candidate")[0]["values"] == ("2",)
    assert not findings(result, "three_axis_dimensions")
    assert any(issue["code"] == "assembly_interpretation_required" for issue in result["issues"])


def test_explicit_third_axis_remains_length_not_panel_count():
    result = diagnose("尺寸(120cmX180cmX2cm)")
    assert findings(result, "three_axis_dimensions")[0]["centimeters"] == ("120", "180", "2")
    assert not findings(result, "panel_count_candidate")


def test_conflicting_overall_and_panel_sizes_are_not_autocorrected():
    text = "20X360(120X180X2)船型-----------2"
    result = diagnose(text)
    assert findings(result, "table_dimensions_candidate")[0]["values"] == ("20", "360")
    assert any(issue["code"] == "overall_panel_size_conflict" for issue in result["issues"])
    assert all(item["raw"] in text for item in result["findings"])


def test_metric_panel_comparison_uses_same_unit_and_does_not_infer_assembly():
    result = diagnose("120cmX360cm(1200mmX1800mmX2)")
    assert not any(issue["code"] == "overall_panel_size_conflict" for issue in result["issues"])
    assert any(issue["code"] == "assembly_interpretation_required" for issue in result["issues"])
    unresolved = diagnose("120cmX360cm(120X180X2)")
    assert any(issue["code"] == "assembly_units_unresolved" for issue in unresolved["issues"])


def test_circle_fractions_remain_shapes_not_decimal_quantities():
    result = diagnose("1/4圓合併，1/2圓，12尺圓桌")
    assert [item["values"] for item in findings(result, "circle_segment")] == [("1", "4"), ("1", "2")]
    assert all(item["centimeters"] is None for item in findings(result, "circle_segment"))
    assert not any(item["values"] in (("0.25",), ("0.5",)) for item in result["findings"])


def test_aluminum_spec_formulas_aliases_and_drawing_dependencies_are_only_warnings():
    text = "庫存6尺胡面鎖2.5鋁，船形弧度120-20=100，腳2片不挖走縣孔，置物版靠單／照圖／合鐵架實內尺寸"
    batch = parse_work_orders(text)
    before = export_review(batch)
    result = diagnose_work_order(batch)
    codes = {issue["code"] for issue in result["issues"]}
    assert {"aluminum_spec_required", "geometry_definition_required", "alias_confirmation_required", "external_evidence_required"} <= codes
    assert export_review(batch) == before
    assert "不挖走縣孔" in batch.raw_text


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key)
def test_eight_samples_preserve_offsets_and_do_not_change_review_state(case):
    batch = parse_work_orders(case.raw_text)
    before = deepcopy(export_review(batch))
    result = diagnose_work_order(batch)
    assert result == diagnose_work_order(batch)
    by_id = {line.line_id: line for line in batch.lines}
    for item in [*result["findings"], *result["issues"]]:
        raw = by_id[item["source_line_id"]].raw
        assert raw[item["start"]:item["end"]] == item["raw"]
        assert item["line_number"] == by_id[item["source_line_id"]].number
    assert export_review(batch) == before
    json.dumps(result, ensure_ascii=False, allow_nan=False)


def test_fullwidth_numbers_keep_original_coordinates():
    raw = "桌面６００ｍｍＸ１８００ｍｍ"
    result = diagnose(raw)
    item = findings(result, "table_dimensions_candidate")[0]
    assert item["raw"] == "６００ｍｍＸ１８００ｍｍ"
    assert raw[item["start"]:item["end"]] == item["raw"]


@pytest.mark.parametrize("raw", ["桌面0cmX180cm", "每桌0個圓孔", "每桌1.5個圓孔", "桌面120X180(120X90X0)"])
def test_invalid_positive_dimensions_or_counts_are_not_approved(raw):
    result = diagnose(raw)
    assert any(issue["code"] in ("nonpositive_dimension", "invalid_item_count", "invalid_panel_count") for issue in result["issues"])
    assert not result["can_quote"]


def test_four_axis_chain_is_not_silently_truncated():
    result = diagnose("10X20X30X40mm")
    assert any(issue["code"] == "unsupported_dimension_chain" for issue in result["issues"])
    assert not any(item["centimeters"] is not None for item in result["findings"])


def test_unsupported_chain_does_not_hide_following_accessory_dimensions():
    result = diagnose("10X20X30X40mm，線盒13.5cmX40cm")
    assert any(issue["raw"] == "10X20X30X40mm" for issue in result["issues"])
    assert findings(result, "accessory_dimensions_candidate")[0]["centimeters"] == ("13.5", "40")


@pytest.mark.parametrize("text", ["60..5cmX180cm", "60e2cmX180cm", "1234567890cmX180cm", "-60cmX180cm"])
def test_invalid_numeric_tokens_are_not_partially_normalized(text):
    result = diagnose(text)
    assert not any(item["centimeters"] is not None for item in result["findings"])


def test_long_whitespace_is_handled_without_quadratic_dimension_backtracking():
    result = diagnose("桌面1" + " " * 20_000 + "不是尺寸")
    assert not result["findings"]


def test_combined_input_keeps_order_ownership_and_source_version():
    batch = parse_work_orders(combined_input())
    result = diagnose_work_order(batch)
    ownership = {key: order.order_id for order in batch.orders for key in order.line_ids}
    assert all(item["order_id"] == ownership.get(item["source_line_id"]) for item in result["findings"])
    line = next(line for line in batch.lines if line.raw == "單面置物版")
    changed = reclassify_line(batch, line_id=line.line_id, kind="note", expected_revision=0, actor="測試", reason="分類")
    updated = diagnose_work_order(changed)
    assert updated["batch_revision"] == 1 and updated["source_digest"] == result["source_digest"]
