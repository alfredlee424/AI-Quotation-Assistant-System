"""只驗證階段 0 驗收資料品質；不代表工單解析／多品項報價已實作。"""
from collections import Counter
from dataclasses import FrozenInstanceError
import re

import pytest

from work_order_fixtures import CASES, combined_input


def test_eight_deidentified_cases_cover_three_product_families():
    assert len(CASES) == 8
    assert len({case.key for case in CASES}) == 8
    assert Counter(case.family for case in CASES) == {"環式會議桌": 3, "會議桌": 3, "餐桌": 2}
    for index, case in enumerate(CASES):
        assert f"測試客戶{chr(ord('A') + index)}" in case.lines[0]
        assert re.search(r"2099\d{4,5}-\d", case.lines[0])
        assert "2026" not in case.raw_text
    assert "/8樓" in CASES[1].lines[0]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key)
def test_annotations_are_unapproved_and_do_not_supply_prices_or_converted_units(case):
    assert case.schema_version == 1
    assert case.annotation_status == "pending_business_engineering_review"
    assert case.approval_evidence is None
    assert case.clarifications, "每张樣本都仍有待釐清條件，不能假裝是可直接報價的正例"
    assert not {"price", "cost", "normalized_size", "prodkind", "selections"} & set(vars(case))
    with pytest.raises(FrozenInstanceError):
        case.annotation_status = "approved"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key)
def test_every_source_line_has_traceable_requirements_and_valid_targets(case):
    keys = {requirement.key for requirement in case.requirements}
    assert len(keys) == len(case.requirements)
    targets = {product.key for product in case.products} | {"order"}
    covered = set()
    for requirement in case.requirements:
        assert 1 <= requirement.line <= len(case.lines)
        assert requirement.excerpt and requirement.excerpt in case.lines[requirement.line - 1]
        assert requirement.targets and set(requirement.targets) <= targets
        assert requirement.expectation
        assert requirement.certainty in {"literal", "candidate", "unresolved"}
        assert requirement.kind in {
            "metadata", "product", "negative", "process", "accessory", "component",
            "drawing", "material", "finish", "position", "dimension", "shape",
            "quantity", "assembly", "inventory", "measurement",
        }
        covered.add(requirement.line)
    assert covered == {i for i, line in enumerate(case.lines, 1) if line.strip()}
    # 來源行有標註只是結構完整性，不聲稱已用程式证明語意沒有漏接。


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key)
def test_candidate_products_and_clarifications_refer_to_existing_evidence(case):
    products = {product.key for product in case.products}
    assert len(products) == len(case.products)
    for product in case.products:
        assert product.quantity > 0 and type(product.quantity) is int
        assert product.description and product.dimension_raw
        assert product.dimension_raw in case.lines[product.line - 1]
        assert any(product.key in requirement.targets and requirement.line == product.line
                   for requirement in case.requirements)
    requirement_keys = {requirement.key for requirement in case.requirements}
    assert len({question.key for question in case.clarifications}) == len(case.clarifications)
    for question in case.clarifications:
        assert question.requirements and set(question.requirements) <= requirement_keys
        assert question.owner and question.question and question.release_evidence
        assert question.category in {"needs_confirmation", "engineering_review", "conflict"}


@pytest.mark.parametrize("case,expected", [
    (CASES[0], {"products": 11, "boxes": 9}),
    (CASES[1], {"products": 7, "boxes": 7, "holes": 7}),
    (CASES[2], {"products": 9, "holes": 15}),
], ids=["ring_01", "ring_02", "ring_03"])
def test_candidate_arithmetic_has_independent_expected_totals(case, expected):
    assert len(case.products) == 4
    assert dict(case.candidate_totals) == expected
    assert sum(product.quantity for product in case.products) == expected["products"]
    for metric, attribute in (("holes", "holes_per_item"), ("boxes", "boxes_per_item")):
        if metric not in expected:
            continue
        values = [getattr(product, attribute) for product in case.products]
        assert all(type(value) is int and value >= 0 for value in values)
        assert sum(product.quantity * value for product, value in zip(case.products, values)) == expected[metric]


def test_corner_exception_and_chair_guest_scopes_are_not_flattened():
    first = {requirement.key: requirement for requirement in CASES[0].requirements}
    assert first["no_holes"].targets == first["under_wire"].targets == ("p1",)
    assert CASES[0].products[0].holes_per_item == 0
    second = {requirement.key: requirement for requirement in CASES[1].requirements}
    assert second["drawing_position"].targets == ("p1", "p2")
    assert second["center_position"].targets == ("p3", "p4")
    assert set(second["drawing_position"].targets).isdisjoint(second["center_position"].targets)
    assert second["no_leg_holes"].kind == "negative"


@pytest.mark.parametrize("case_key,fragments", [
    ("ring_01", ("面無孔有桌下走線", "單面置物版", "孔靠單/格靠單")),
    ("ring_02", ("3X3.25", "每桌1個", "腳2片不挖走線孔")),
    ("ring_03", ("2孔6公分圓", "格在走線下方", "不挖走縣孔")),
    ("meeting_01", ("209901321", "3X6長方斜刀單鋁13.5X40", "噴黑")),
    ("meeting_02", ("20X360(120X180X2)", "120-20=100", "如圖面")),
    ("meeting_03", ("5X14(5X7X2)", "9842美", "中桶挖走線孔")),
    ("dining_01", ("假厚36MM", "庫存6尺胡面鎖2.5鋁", "再鎖鋁合金")),
    ("dining_02", ("1/4圓", "1/2圓", "CNC", "實內尺寸")),
])
def test_important_ambiguities_are_preserved_instead_of_corrected(case_key, fragments):
    case = next(case for case in CASES if case.key == case_key)
    assert all(fragment in case.raw_text for fragment in fragments)


def test_dining_source_rows_are_not_asserted_to_be_finished_table_counts():
    for case in CASES[-2:]:
        assert case.candidate_totals == ()
        assert any(question.category == "engineering_review" for question in case.clarifications)
    last = {requirement.key: requirement for requirement in CASES[-1].requirements}
    assert last["title_spec"].line == 1  # 標題也有需求，不能全當 metadata。
    assert last["quarter"].certainty == last["half"].certainty == "unresolved"
    assert last["measured_inside"].kind == "measurement"


def test_combined_input_preserves_all_orders_and_is_deterministic():
    text = combined_input()
    assert text == combined_input()
    assert text.count("生產工單") == 3
    assert text.count("測試客戶") == 8
    for case in CASES:
        assert text.count(case.raw_text) == 1
