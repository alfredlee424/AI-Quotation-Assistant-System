from pathlib import Path

import pytest

from agent.order_logic import (
    load_cmt1_order_data,
    parse_ord,
    parse_ordqty,
    parse_ordspe,
)
from agent.core import LLMAgent
from agent.rule_parser import extract_quantity


TEST_DIR = Path(__file__).parent


def test_load_cmt1_files_and_known_order_refs():
    data = load_cmt1_order_data(TEST_DIR)

    assert len(data.orders) == 292
    assert data.order_refs() == [
        "QU26810002-01001",
        "QU26821001-01001",
        "QU26821001-01002",
        "QU26821002-01001",
        "QU26823002-01001",
        "QU26823003-01001",
        "QU26823004-01001",
        "QU26823004-01002",
        "QU26823004-01003",
        "QU26824002-01001",
        "QU26824005-01001",
    ]
    assert len(data.structure) == 55
    assert len(data.categories) == 51
    assert data.orders[0]["path"] == "CMT1\\A001"
    assert data.orders[0]["spc_code"] == "C033"


def test_ordqty_uses_empty_codsc_and_preserves_stdpar():
    rows = parse_ordqty((TEST_DIR / "ordqty.txt").read_text(encoding="utf-8"))
    row = next(
        item for item in rows
        if item["path"] == "CMT1\\A001\\S004" and item["code"] == "C005"
    )
    assert row["codsc"] is None
    assert row["part_path"] == "CMT1\\A001"
    assert row["stdqty"] == 4.5
    assert row["stdpar"] == 24.0


def test_order_validation_does_not_replace_historical_values():
    data = load_cmt1_order_data(TEST_DIR)
    order_ref = "QU26821001-01001"
    original = next(row for row in data.orders if row["ref_no"] == order_ref)
    original_values = (original["stdqty"], original["stdpar"], original["compri"])

    warnings = data.validate(order_ref)

    assert (
        original["stdqty"], original["stdpar"], original["compri"]
    ) == original_values
    assert isinstance(warnings, list)


def test_parser_rejects_wrong_column_count():
    with pytest.raises(ValueError, match="應有 13 欄"):
        parse_ord("103\tREF\tCMT1\\A001")

    with pytest.raises(ValueError, match="應有 11 欄"):
        parse_ordspe("103\tCMT1\\A001\tC001")


def test_dimensions_do_not_become_product_quantity():
    assert extract_quantity("我要 1 張 60*180 的桌子") == 1
    assert extract_quantity("40*13.5 銀鋁單掀") is None
    assert extract_quantity("60*180") is None


def test_confirmation_does_not_choose_first_ambiguous_option():
    candidates = [{"code": "C001"}, {"code": "C003"}]
    assert LLMAgent._select_pending_option("確認", candidates) is None


def test_historical_snapshot_cost_4009_08_is_not_repriced_from_current_masters(monkeypatch):
    from copy import deepcopy
    from decimal import Decimal, ROUND_HALF_UP
    from database import repository as repo
    from engine.calculator import QuoteItem, calculate_quote

    data = load_cmt1_order_data(TEST_DIR)
    rows = [row for row in data.orders if row["ref_no"] == "QU26821001-01001"]
    before = deepcopy(rows)
    # 歷史計算只吃已保存值。故意禁止所有新報價主檔查詢。
    def forbidden(*args, **kwargs):
        raise AssertionError("歷史訂單不得讀取現行主檔重算")
    for method in ("get_options_by_path", "get_quantity_rules", "get_product_category"):
        monkeypatch.setattr(repo, method, forbidden)
    d = Decimal
    manual = sum(d(str(r["qty"])) * d(str(r["stdqty"])) / d(str(r["stdpar"])) * d(str(r["compri"])) for r in rows)
    assert manual.quantize(d(".01"), rounding=ROUND_HALF_UP) == d("4009.08")
    result = calculate_quote([
        QuoteItem(part_code=r["spc_code"], part_desc=r["spdsc"], path=r["path"],
                  spc_code=r["spc_code"], spdsc=r["spdsc"], qty=r["qty"],
                  stdqty=r["stdqty"], stdpar=r["stdpar"], compri=r["compri"])
        for r in rows
    ], markup_rate=0, tax_rate=0)
    assert result.total_cost == result.total_price == 4009.08
    leg = next(row for row in rows if row["path"] == r"CMT1\B001\S004")
    assert leg["stdqty"] == 2  # 現行主檔是8；快照維持2，不能覆寫。
    paid_parent = next(item for item in result.items if item["path"] == r"CMT1\B001\S050")
    assert paid_parent["part_cost"] == 200
    assert rows == before
