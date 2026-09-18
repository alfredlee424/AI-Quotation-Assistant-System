from engine.calculator import calculate_from_ordstr
import pytest


def test_accessory_size_input_does_not_override_table_size():
    from agent.rule_parser import extract_size

    assert extract_size("改 40*13.5 銀鋁單掀") is None


def test_w001_cost_nodes_are_included_in_ordstr_calculation(current_draft):
    selections = current_draft["selections"]

    result = calculate_from_ordstr("CMT1", selections, qty=1)
    paths = {item["path"] for item in result.items}

    assert "CMT1\\A001\\W001\\W010" in paths
    assert "CMT1\\A001\\W001\\W020" in paths
    assert "CMT1\\A001\\W001\\W030" in paths
    assert "CMT1\\A001\\W001\\W040" in paths
    assert "CMT1\\A001\\W001\\W050" in paths
    assert "CMT1\\B001\\W001\\W010" in paths
    assert "CMT1\\B001\\W001\\W020" in paths
    assert "CMT1\\B001\\W001\\W030" in paths
    assert "CMT1\\B001\\W001\\W050" in paths


def test_latest_s020_selection_is_used_in_calculation(current_draft):
    from engine.configuration import apply_proposal
    path = r"CMT1\A001\S019\S020"
    apply_proposal(current_draft, {"changes": [{"op": "set", "path": path, "code": "C003"}]})
    apply_proposal(current_draft, {"changes": [{"op": "set", "path": path, "code": "C001"}]})
    selections = current_draft["selections"]

    result = calculate_from_ordstr("CMT1", selections, qty=1)
    s020 = next(item for item in result.items if item["path"] == "CMT1\\A001\\S019\\S020")
    assert s020["spc_code"] == "C001"
    assert s020["compri"] == 800.0
