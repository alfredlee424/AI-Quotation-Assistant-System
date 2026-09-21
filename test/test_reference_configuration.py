"""診斷：期望配置、實際配置與歷史用量差異，不改動產品政策或主檔。"""
from copy import deepcopy
from decimal import Decimal
import json

import pytest

from engine.configuration import apply_proposal, resolve_configuration
from engine.pricing import calculate_configuration


def change(path, code):
    return {"op": "set", "path": path, "code": code}


def test_actual_vs_reference_configuration_cost_difference(current_draft, caplog):
    actual = deepcopy(current_draft)
    apply_proposal(actual, {"changes": [
        {"op": "remove", "path": r"CMT1\A001\S005\S001"},
        change(r"CMT1\A001\S005\S000\S052", "C002"),
        change(r"CMT1\A001\S005\S000\S006", "C001"),
        change(r"CMT1\A001\S019\S020", "C001"),
    ], "questions": []})
    actual_result, actual_resolved = calculate_configuration(actual)
    assert actual_result.total_cost == 3836.58

    expected = deepcopy(current_draft)
    apply_proposal(expected, {"changes": [
        change(r"CMT1\A001\S019\S020", "C001"),
        change(r"CMT1\A001\S050", "C001"),
        change(r"CMT1\A001\S050\S005\S008", "C003"),
    ], "questions": []})
    expected_result, expected_resolved = calculate_configuration(expected)
    assert expected_result.total_cost == 3726.42
    assert actual_resolved["valid"] and expected_resolved["valid"]
    actual_paths = set(actual_resolved["selections"])
    expected_paths = set(expected_resolved["selections"])
    assert len(actual_paths) == len(expected_paths) == 28
    assert actual_paths - expected_paths == {
        r"CMT1\A001\S005\S000", r"CMT1\A001\S005\S000\S052",
        r"CMT1\A001\S005\S000\S006", r"CMT1\A001\S005\S000\S045",
    }
    assert expected_paths - actual_paths == {
        r"CMT1\A001\S005\S001", r"CMT1\A001\S050",
        r"CMT1\A001\S050\S005", r"CMT1\A001\S050\S005\S008",
    }
    # 展開符合性以完整路徑及規格代碼核對，不以筆數、價格或歷史名称判斷。
    different_codes = {
        p: (actual_resolved["selections"][p]["code"], expected_resolved["selections"][p]["code"])
        for p in actual_paths & expected_paths
        if actual_resolved["selections"][p]["code"] != expected_resolved["selections"][p]["code"]
    }
    assert different_codes == {
        r"CMT1\A001\W001\W010": ("C002", "C001"),
        r"CMT1\A001\W001\W020": ("C002", "C001"),
    }
    assert len((actual_paths & expected_paths) - different_codes.keys()) == 22
    items = {i["path"]: i for i in expected_result.items}
    leg = items[r"CMT1\B001\S004"]
    assert (leg["driver_path"], leg["driver_code"]) == (r"CMT1\B001", "C001")
    assert (leg["stdqty"], leg["stdpar"], leg["compri"]) == (8, 120, 480)
    historical_leg_cost = Decimal(2) * Decimal(480) / Decimal(120)
    assert historical_leg_cost == 8
    assert leg["part_cost"] - float(historical_leg_cost) == 24
    assert expected_result.total_cost - 24 == pytest.approx(3702.42)
    events = [json.loads(r.message) for r in caplog.records if r.name == "ai_quote"]
    evidence = [e for e in events if e["action"] == "quote_pricing_sources"]
    assert len(evidence) == 2
    logged_leg = next(i for i in evidence[-1]["result"]["items"] if i["path"] == leg["path"])
    assert logged_leg["source"] == "ordqty"
    assert logged_leg["stdqty"] == 8


def test_optional_partition_is_not_added_without_selection(current_draft):
    resolved = resolve_configuration(current_draft)
    path = r"CMT1\A001\S050"
    assert resolved["catalog"]["nodes"][path]["must_chose"] != "Y"
    assert path not in resolved["active_paths"]
    assert path not in resolved["selections"]


def test_selecting_mdf_top_board_implicitly_activates_laminate_branch(current_draft):
    current_draft["selections"].pop(r"CMT1\A001\S005\S001")
    apply_proposal(current_draft, {"changes": [
        change(r"CMT1\A001\S005\S000\S006", "C001"),
    ], "questions": []})
    assert r"CMT1\A001\S005\S000" in current_draft["selections"]
    assert r"CMT1\A001\S005\S000\S052" in current_draft["allowed_options"]
    assert r"CMT1\A001\S005" not in current_draft["allowed_options"]
