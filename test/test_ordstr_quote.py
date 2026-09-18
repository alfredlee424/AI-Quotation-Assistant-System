"""真實四主檔的現行報價；不使用 seed，不操作全域 engine。"""
from decimal import Decimal, ROUND_HALF_UP

import pytest

from database import repository as repo
from engine.calculator import calculate_from_draft, calculate_from_ordstr, check_required_selections
from engine.configuration import resolve_configuration


@pytest.fixture(autouse=True)
def real_catalog(cmt1_db):
    pass


def test_get_product_categories():
    categories = repo.get_product_categories()
    assert len(categories) == 1
    assert categories[0]["prodkind"] == "CMT1"
    assert categories[0]["ordkind"] == "1"
    assert categories[0]["quo_rate"] == 1.0


def test_get_product_category_single():
    assert repo.get_product_category("CMT1")["codsc"] == "辦公桌"


def test_get_product_category_missing():
    assert repo.get_product_category("NOPE") is None


def test_ordstr_root_children():
    children = repo.get_ordstr_children("CMT1")
    assert [c["optnoc"] for c in children] == ["A001", "B001"]
    assert children[0]["must_chose"] == "Y"
    assert not children[1]["must_chose"]


def test_expand_tree_depth_and_leaf():
    nodes = {n["pathc"]: n for n in repo.expand_ordstr_tree("CMT1")}
    assert len(nodes) == 55
    assert nodes[r"CMT1\A001"]["depth"] == 1
    assert nodes[r"CMT1\A001"]["is_leaf"] is False
    assert nodes[r"CMT1\A001\S004"]["depth"] == 2
    assert nodes[r"CMT1\A001\S004"]["is_leaf"] is True
    assert nodes[r"CMT1\A001\S050\S005\S008"]["depth"] == 4


def test_get_required_nodes():
    required = repo.get_required_nodes("CMT1")
    paths = {n["pathc"] for n in required}
    assert r"CMT1\A001" in paths
    assert r"CMT1\A001\W001" in paths
    assert r"CMT1\B001" not in paths
    assert all(n["must_chose"] == "Y" for n in required)


def manual_current_cost():
    # 獨立人工公式：桌面用料 + 桌面工費 + 木腳用料 + 木腳工費。
    # 數值直接依四主檔驗算，絕不呼叫待測解析器或用結果倒推期望值。
    d = Decimal
    return (d("4.5") / 24 * 700 + d(513) / 2 + d(240) / 2 + 500 + 100
            + 150 + 300 + 100 + 500 + 300
            + d(8) / 120 * 480 + d(4) / 6 * 140 + 50 + 50 + 100 + 50)


def test_calculate_from_ordstr_material_cost(current_draft):
    result = calculate_from_ordstr("CMT1", current_draft["selections"], qty=1)
    expected = manual_current_cost().quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    assert expected == Decimal("2833.08")
    assert result.total_cost == float(expected)
    assert result == calculate_from_draft(current_draft)
    items = {i["path"]: i for i in result.items}
    assert items[r"CMT1\A001"]["spdsc"] == "60*180"
    assert items[r"CMT1\A001\S004"]["part_cost"] == pytest.approx(131.25)
    leg = items[r"CMT1\B001\S004"]
    assert (leg["stdqty"], leg["stdpar"], leg["part_cost"]) == (8, 120, 32)
    paper = items[r"CMT1\B001\S005\S008"]
    assert (paper["stdqty"], paper["stdpar"]) == (4, 6)
    assert paper["part_cost"] == 93.33  # 明細輸出保留兩位小數


def test_calculate_from_ordstr_quo_rate_markup(current_draft):
    result = calculate_from_draft(current_draft)
    assert result.markup_rate == 0
    assert result.subtotal == result.total_cost


def test_calculate_from_ordstr_quo_rate_130(current_draft, cmt1_db):
    from database.models import Invdoc
    with cmt1_db() as db:
        db.get(Invdoc, ("103", "CMT1")).quo_rate = 1.3
        db.commit()
    result = calculate_from_draft(current_draft)
    assert result.markup_rate == pytest.approx(.3)
    expected = (manual_current_cost() * Decimal("1.3")).quantize(Decimal(".01"))
    assert result.subtotal == float(expected)


def test_check_required_missing_when_empty():
    missing = check_required_selections("CMT1", {})
    assert r"CMT1\A001" in {m["pathc"] for m in missing}


def test_check_required_satisfied(current_draft):
    assert resolve_configuration(current_draft)["valid"]
    assert check_required_selections("CMT1", current_draft["selections"]) == []
