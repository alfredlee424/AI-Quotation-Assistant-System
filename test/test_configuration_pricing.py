"""解析／計價契約：完整路徑、互斥方案、明確用量規則及數值驗證。"""
from copy import deepcopy

import pytest

from database import repository as repo
from database.models import Ordspe, Ordqty
from engine.configuration import apply_proposal, load_catalog, resolve_configuration
from engine.pricing import calculate_configuration


def set_change(path, code="C001", **extra):
    return dict(op="set", path=path, code=code, **extra)


def test_full_paths_same_optno_and_zero_structural_nodes(current_draft):
    result, resolved = calculate_configuration(current_draft)
    items = {item["path"]: item for item in result.items}
    assert items[r"CMT1\A001\S004"]["compri"] == 700
    assert items[r"CMT1\B001\S004"]["compri"] == 480
    assert items[r"CMT1\A001\S004"]["driver_code"] == "C005"
    assert items[r"CMT1\B001\S004"]["driver_code"] == "C001"
    assert items[r"CMT1\A001"]["part_cost"] == 0
    assert items[r"CMT1\A001\S005"]["part_cost"] == 0
    assert set(items) == set(resolved["selections"])
    assert all(path.startswith("CMT1\\") for path in resolved["selections"])


def test_paid_parent_is_not_discarded_as_non_leaf(current_draft, cmt1_db):
    parent = r"CMT1\A001\S050"
    child = parent + r"\S005\S008"
    with cmt1_db() as db:
        row = db.query(Ordspe).filter_by(path=parent, code="C001").one()
        row.compri = 120  # 人工付費父節點，不改任何真實 fixture
        db.commit()
    apply_proposal(current_draft, {"changes": [set_change(parent), set_change(child, "C003")]})
    result, _ = calculate_configuration(current_draft)
    items = {item["path"]: item for item in result.items}
    assert load_catalog("CMT1")["nodes"][parent]["is_leaf"] is False
    assert items[parent]["part_cost"] == 120
    assert items[child]["part_cost"] == 93.33
    assert result.total_cost == 3046.42  # 未逐項四捨五入的總成本最後才取兩位


def test_branch_switch_requires_explicit_removal_and_keeps_bottom(current_draft):
    old = r"CMT1\A001\S005\S001"
    new = r"CMT1\A001\S005\S000"
    changes = [set_change(new + r"\S006"), set_change(new + r"\S052", "C002")]
    before = deepcopy(current_draft)
    with pytest.raises(ValueError, match="互斥"):
        apply_proposal(current_draft, {"changes": changes})
    assert current_draft == before
    apply_proposal(current_draft, {"changes": [dict(op="remove", path=old), *changes]})
    result, resolved = calculate_configuration(current_draft)
    items = {item["path"]: item for item in result.items}
    assert old not in resolved["active_paths"]
    assert r"CMT1\A001\S005\S007" in items  # 下板不是互斥方案
    assert items[r"CMT1\A001\W001\W010"]["spc_code"] == "C002"
    assert items[r"CMT1\A001\W001\W020"]["spc_code"] == "C002"
    apply_proposal(current_draft, {"changes": [dict(op="remove", path=new), set_change(old)]})
    items = {item["path"]: item for item in calculate_configuration(current_draft)[0].items}
    assert items[r"CMT1\A001\W001\W010"]["spc_code"] == "C001"
    assert items[r"CMT1\A001\W001\W020"]["spc_code"] == "C001"
    assert not any(path.startswith(new) for path in items)


@pytest.mark.parametrize("parent", [r"CMT1\B001\S005", r"CMT1\A001\S049\S005", r"CMT1\A001\S050\S005"])
def test_each_material_container_has_exclusive_policy(current_draft, parent):
    draft = deepcopy(current_draft)
    for suffix in ("S001", "S008"):
        path = parent + "\\" + suffix
        draft["selections"][path] = {"path": path, "code": "C001"}
    resolved = resolve_configuration(draft)
    assert any("互斥" in error and parent in error for error in resolved["errors"])


def test_no_leg_has_no_leg_labor_and_optional_leg_paint_is_excluded(current_draft):
    items = calculate_configuration(current_draft)[0].items
    assert r"CMT1\B001\W001\W040" not in {item["path"] for item in items}
    apply_proposal(current_draft, {"changes": [dict(op="remove", path=r"CMT1\B001")]})
    result, resolved = calculate_configuration(current_draft)
    assert not any(path.startswith(r"CMT1\B001") for path in resolved["active_paths"])
    assert not any(item["path"].startswith(r"CMT1\B001") for item in result.items)
    assert result.total_cost == 2457.75


def test_two_holes_per_product_is_not_two_products(current_draft):
    path = r"CMT1\A001\S019\S030"
    apply_proposal(current_draft, {"qty": 3, "changes": [set_change(path, line_qty=2)]})
    result, _ = calculate_configuration(current_draft)
    hole = next(item for item in result.items if item["path"] == path)
    assert current_draft["qty"] == 3
    assert (hole["product_qty"], hole["line_qty"], hole["qty"]) == (3, 2, 6)
    assert (hole["stdqty"], hole["stdpar"], hole["part_cost"]) == (1, 1, 2100)
    assert hole["source"] == "per_item_policy"


@pytest.mark.parametrize("mode", ["absent", "missing_driver", "empty_driver", "conflicting_driver", "duplicate"])
def test_missing_or_ambiguous_rules_rejected(current_draft, monkeypatch, mode):
    target = r"CMT1\A001\S004"
    original = repo.get_quantity_rules
    rows = deepcopy(original(target))
    if mode == "absent":
        rows = []
    elif mode == "missing_driver":
        rows = [r for r in rows if r["code"] != "C005"]
    elif mode == "empty_driver":
        for row in rows:
            row["part_path"] = ""
    elif mode == "conflicting_driver":
        rows[0]["part_path"] = r"CMT1\B001"
    else:
        rows.append(deepcopy(next(r for r in rows if r["code"] == "C005")))
    monkeypatch.setattr(repo, "get_quantity_rules", lambda path, workgroup="103": rows if path == target else original(path, workgroup))
    with pytest.raises(ValueError, match="用量規則|驅動部位"):
        calculate_configuration(current_draft)


def test_per_item_policy_does_not_hide_missing_driver_rule(current_draft, cmt1_db):
    path = r"CMT1\A001\S019\S030"
    apply_proposal(current_draft, {"changes": [set_change(path)]})
    with cmt1_db() as db:
        db.add(Ordqty(workgroup="103", path=path, code="C003", part_path=r"CMT1\A001", stdqty=1, stdpar=1))
        db.commit()
    with pytest.raises(ValueError, match="尺寸用量規則"):
        calculate_configuration(current_draft)


@pytest.mark.parametrize("field", ["qty", "line_qty", "compri", "stdqty", "stdpar", "quo_rate"])
@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), float("-inf"), None, True])
def test_invalid_numeric_values_rejected(current_draft, monkeypatch, field, value):
    path = r"CMT1\A001\S004"
    if field == "qty":
        current_draft[field] = value
    elif field == "line_qty":
        current_draft["selections"][path][field] = value
    elif field in ("stdqty", "stdpar"):
        original = repo.get_quantity_rules
        def rules(path, workgroup="103"):
            rows = deepcopy(original(path, workgroup))
            for row in rows:
                row[field] = value
            return rows
        monkeypatch.setattr(repo, "get_quantity_rules", rules)
    elif field == "compri":
        original = repo.get_options_by_path
        def options(path, workgroup="103"):
            rows = deepcopy(original(path, workgroup))
            for row in rows:
                row[field] = value
            return rows
        monkeypatch.setattr(repo, "get_options_by_path", options)
    else:
        monkeypatch.setattr(repo, "get_product_category", lambda *a, **kw: {"quo_rate": value})
    with pytest.raises(ValueError):
        calculate_configuration(current_draft)


@pytest.mark.parametrize("field", ["qty", "line_qty", "stdpar", "quo_rate"])
def test_zero_positive_only_fields_rejected(current_draft, monkeypatch, field):
    test_invalid_numeric_values_rejected(current_draft, monkeypatch, field, 0)


def test_zero_usage_is_valid_without_defaulting_to_one(current_draft, cmt1_db):
    with cmt1_db() as db:
        db.get(Ordqty, ("103", r"CMT1\A001\S004", "C005")).stdqty = 0
        db.commit()
    result, _ = calculate_configuration(current_draft)
    item = next(item for item in result.items if item["path"] == r"CMT1\A001\S004")
    assert item["stdqty"] == 0
    assert item["part_cost"] == 0


def test_invalid_current_path_is_not_silently_dropped(current_draft):
    path = r"CMT1\A001\LEGACY"
    current_draft["selections"][path] = dict(path=path, code="C001")
    resolved = resolve_configuration(current_draft)
    assert not resolved["valid"]
    assert any(path in error for error in resolved["errors"])
    with pytest.raises(ValueError, match="路徑不在"):
        calculate_configuration(current_draft)


def test_null_database_price_is_not_free(current_draft, cmt1_db):
    path = r"CMT1\A001\S004"
    with cmt1_db() as db:
        db.query(Ordspe).filter_by(path=path, code="C001").one().compri = None
        db.commit()
    assert repo.get_options_by_path(path)[0]["compri"] is None
    with pytest.raises(ValueError, match="採購單價"):
        calculate_configuration(current_draft)


def test_retired_imported_path_can_only_be_removed(current_draft):
    path = r"CMT1\B001\LEGACY"
    current_draft["selections"][path] = {"path": path, "code": "C001"}
    with pytest.raises(ValueError, match="路徑不在"):
        apply_proposal(current_draft, {"changes": [set_change(path)]})
    apply_proposal(current_draft, {"changes": [{"op": "remove", "path": path}]})
    assert path not in current_draft["selections"]
    assert calculate_configuration(current_draft)[0].total_cost == 2833.08
