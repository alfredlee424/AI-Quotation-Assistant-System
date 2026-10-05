"""主檔盤點：資料問題與核准缺口分離，不計價、不寫入、不回退預設值。"""
from copy import deepcopy
import json

import pytest
from sqlalchemy import event

from database import master_inventory as reader
from database.models import Invdoc, Ordstr, Ordspe, Ordqty
from engine import master_inventory as inventory


def snapshot(product="CMT1"):
    root, material, labor = (product + path for path in (r"\A001", r"\A001\S004", r"\A001\W001\W010"))
    paths = [root, material, product + r"\A001\W001", labor]
    return {"prodkind": product, "workgroup": "103",
            "categories": [{"prodkind": product, "codsc": "測試產品", "ordkind": "1", "quo_rate": 1.3}],
            "edges": [{"pathf": path.rsplit("\\", 1)[0], "pathc": path, "optnof": path.rsplit("\\", 1)[0].split("\\")[-1],
                       "optnoc": path.split("\\")[-1], "must_chose": "Y", "seq": i} for i, path in enumerate(paths)],
            "options": [{"path": root, "code": code, "codsc": code, "compri": 0} for code in ("C001", "C002")]
                       + [{"path": material, "code": "MAT", "codsc": "板材", "compri": 123456.75},
                          {"path": labor, "code": "LAB", "codsc": "工費", "compri": 7}],
            "quantities": [{"path": material, "code": code, "part_path": root, "stdqty": qty, "stdpar": 2}
                           for code, qty in (("C001", 0), ("C002", 4))],
            "definitions": [{"optno": path.split("\\")[-1], "optdesc": path.split("\\")[-1], "kind": "3"} for path in paths]}


def kinds(report):
    return {issue["kind"] for issue in report["issues"]}


def material_path(data):
    return data["prodkind"] + r"\A001\S004"


def path_report(report, path):
    return next(row for row in report["paths"] if row["path"] == path)


def test_analysis_preserves_zero_and_sensitive_costs_not_exported():
    data = snapshot()
    before = deepcopy(data)
    report = inventory.analyze_product_master(data)
    assert data == before
    assert report["data_error_count"] == 0 and not report["can_quote"]
    assert report["review_count"] > 0
    assert "123456.75" not in json.dumps(report, ensure_ascii=False)
    assert path_report(report, material_path(data))["quantity_mode"] == "quantity_rules"
    assert "invalid_numerator" not in kinds(report)
    assert path_report(report, r"CMT1\A001\W001")["structural_container"]


@pytest.mark.parametrize("product,mode", [("CMT1", "existing_cmt1_per_item_policy"), ("MT", "missing_quantity_rules"), ("DT1", "missing_quantity_rules")])
def test_three_products_do_not_inherit_cmt1_fallback(product, mode):
    report = inventory.analyze_product_master(snapshot(product))
    assert path_report(report, product + r"\A001\W001\W010")["quantity_mode"] == mode
    assert "material_policy_review" in kinds(report) and "compatibility_review" in kinds(report)
    assert "included_process_review" in kinds(report) and not report["can_quote"]


@pytest.mark.parametrize("value", [None, -1, float("inf"), float("nan"), True, "invalid"])
def test_invalid_cost_not_zero(value):
    data = snapshot()
    data["options"][2]["compri"] = value
    report = inventory.analyze_product_master(data)
    assert "invalid_cost" in kinds(report)
    assert path_report(report, material_path(data))["invalid_cost_count"] == 1
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("value", [None, 0, -1, float("inf"), float("nan"), True])
def test_quote_coefficient_must_be_positive_finite(value):
    data = snapshot()
    data["categories"][0]["quo_rate"] = value
    assert "invalid_quote_rate" in kinds(inventory.analyze_product_master(data))


@pytest.mark.parametrize("field,value,kind", [("stdqty", None, "invalid_numerator"), ("stdqty", -1, "invalid_numerator"),
    ("stdqty", float("inf"), "invalid_numerator"), ("stdpar", None, "invalid_denominator"), ("stdpar", 0, "invalid_denominator"),
    ("stdpar", -1, "invalid_denominator"), ("stdpar", float("nan"), "invalid_denominator")])
def test_invalid_quantity_is_not_defaulted(field, value, kind):
    data = snapshot()
    data["quantities"][0][field] = value
    assert kind in kinds(inventory.analyze_product_master(data))


def test_quantity_coverage_uses_driver_codes_not_material_codes():
    data = snapshot()
    data["quantities"].pop()
    report = inventory.analyze_product_master(data)
    assert path_report(report, material_path(data))["missing_driver_codes"] == ["C002"]
    assert "MAT" not in path_report(report, material_path(data))["missing_driver_codes"]


def test_existing_partial_rules_override_per_item_policy():
    data = snapshot()
    data["quantities"].append({"path": r"CMT1\A001\W001\W010", "code": "C001", "part_path": r"CMT1\A001", "stdqty": 1, "stdpar": 1})
    row = path_report(inventory.analyze_product_master(data), r"CMT1\A001\W001\W010")
    assert row["quantity_mode"] == "quantity_rules" and row["missing_driver_codes"] == ["C002"]


@pytest.mark.parametrize("mutation,expected", [("empty", "ambiguous_driver"), ("multiple", "ambiguous_driver"),
    ("foreign", "invalid_driver"), ("unknown_code", "unknown_quantity_code"), ("duplicate", "duplicate_quantity")])
def test_invalid_driver_relationships(mutation, expected):
    data = snapshot()
    if mutation == "empty":
        for row in data["quantities"]:
            row["part_path"] = ""
    elif mutation == "multiple":
        data["quantities"][0]["part_path"] = material_path(data)
    elif mutation == "foreign":
        for row in data["quantities"]:
            row["part_path"] = r"MT\A001"
    elif mutation == "unknown_code":
        data["quantities"][0]["code"] = "RETIRED"
    else:
        data["quantities"].append(deepcopy(data["quantities"][0]))
    assert expected in kinds(inventory.analyze_product_master(data))


def test_orphan_disconnected_duplicate_and_cross_product_structure():
    data = snapshot()
    data["edges"] += [dict(data["edges"][1]),
                      {"pathf": r"CMT1\MISSING", "pathc": r"CMT1\MISSING\S999", "optnoc": "S999"},
                      {"pathf": r"CMT1\A001", "pathc": r"MT\A001\S001", "optnoc": "S001"}]
    data["options"].append({"path": r"CMT1\ORPHAN", "code": "X", "codsc": "旧項目", "compri": 0})
    data["quantities"].append({"path": r"CMT1\ORPHAN", "code": "X", "part_path": r"CMT1\A001", "stdqty": 1, "stdpar": 1})
    assert kinds(inventory.analyze_product_master(data)) >= {"duplicate_node", "unreachable_node", "missing_parent", "invalid_edge", "orphan_option", "orphan_quantity"}


def test_duplicate_options_are_not_collapsed_and_missing_leaf_is_not_free():
    data = snapshot()
    data["options"].append({**data["options"][2], "codsc": "同代碼不同描述"})
    data["options"] = [row for row in data["options"] if row["code"] != "LAB"]
    report = inventory.analyze_product_master(data)
    assert {"duplicate_option", "missing_leaf_options"} <= kinds(report)
    assert path_report(report, material_path(data))["option_count"] == 2


def test_zero_cost_does_not_demand_quantity_but_does_not_approve_included_work():
    data = snapshot("MT")
    data["options"][-1]["compri"] = 0
    report = inventory.analyze_product_master(data)
    assert "missing_quantity_rules" not in kinds(report)
    assert "included_process_review" in kinds(report) and "labor_inclusion_review" in kinds(report)


def test_digest_stable_with_row_order_but_sensitive_to_cost_and_quantity():
    data = snapshot()
    report = inventory.analyze_product_master(data)
    for name in inventory.TABLES:
        data[name].reverse()
    reordered = inventory.analyze_product_master(data)
    assert reordered == report
    data["quantities"][0]["stdqty"] = 17
    changed = inventory.analyze_product_master(data)
    assert changed["source_digest"] != report["source_digest"]
    data["options"][0]["compri"] = 987
    assert inventory.analyze_product_master(data)["source_digest"] != changed["source_digest"]


def test_empty_and_wrong_category_are_never_ready():
    data = snapshot()
    data["categories"][0]["ordkind"] = "2"
    assert "not_quotation_category" in kinds(inventory.analyze_product_master(data))
    for name in inventory.TABLES:
        data[name] = []
    report = inventory.analyze_product_master(data)
    assert {"category_count", "missing_structure", "invalid_quote_rate"} <= kinds(report)
    assert not report["can_quote"]


@pytest.mark.parametrize("products", [[], (), ("CMT1",) * 2, ("CMT1", "MT", "DT1", "OTHER"), (True,), ("CMT1\\A001",), ("%",), ("",)])
def test_invalid_scope_rejected_before_query(monkeypatch, products):
    monkeypatch.setattr(inventory, "read_product_master", lambda *args: pytest.fail("不得查詢"))
    with pytest.raises(ValueError):
        inventory.inventory_products(products, source_label="測試")


def test_missing_source_rejected_before_query(monkeypatch):
    monkeypatch.setattr(inventory, "read_product_master", lambda *args: pytest.fail("不得查詢"))
    with pytest.raises(ValueError):
        inventory.inventory_products(("CMT1",), source_label="")


def test_batch_read_failure_does_not_return_partial_report(monkeypatch):
    def read(product, group):
        if product == "MT":
            raise RuntimeError("讀取失敗")
        return snapshot(product)
    monkeypatch.setattr(inventory, "read_product_master", read)
    with pytest.raises(RuntimeError):
        inventory.inventory_products(("CMT1", "MT"), source_label="測試")


def test_real_fixture_readonly_report(cmt1_db):
    statements = []
    with cmt1_db() as db:
        engine = db.get_bind()
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        document = inventory.inventory_products(("CMT1", "MT", "DT1"), source_label="隔離測試副本-v1")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
    assert len(statements) == 15
    assert document["products"][0]["source_counts"]["edges"] > 0
    assert "missing_structure" in kinds(document["products"][1])
    assert document["schema_version"] == 1 and not document["can_quote"]
    assert not document["cross_query_snapshot_guaranteed"]
    assert not document["policy_context"]["other_product_policy_inherited"]
    json.dumps(document, allow_nan=False)


def test_reader_prefix_workgroup_and_zero_null_preservation(isolated_db):
    with isolated_db() as db:
        for product, group in (("MT", "103"), ("MT1", "103"), ("MT", "999"), ("P_1", "103"), ("PX1", "103")):
            db.add(Invdoc(workgroup=group, prodkind=product, codsc=product, ordkind="1", quo_rate=1.0))
            db.add(Ordspe(workgroup=group, path=product + r"\A001", code="C1", codsc=product, compri=0))
            db.add(Ordqty(workgroup=group, path=product + r"\A001", code="C1", part_path=product + r"\A001", stdqty=0, stdpar=None))
        db.commit()
    for product in ("MT", "P_1"):
        data = reader.read_product_master(product)
        assert len(data["options"]) == 1 and data["options"][0]["path"] == product + r"\A001"
        assert data["quantities"][0]["stdqty"] == 0 and data["quantities"][0]["stdpar"] is None


def test_reader_keeps_disconnected_edges_and_duplicate_option_codes(isolated_db):
    with isolated_db() as db:
        db.add(Ordstr(workgroup="103", pathf=r"MT\MISSING", pathc=r"MT\MISSING\S004", optnof="MISSING", optnoc="S004"))
        for description in ("一", "二"):
            db.add(Ordspe(workgroup="103", path=r"MT\A001", code="C1", codsc=description, compri=0))
        db.commit()
    data = reader.read_product_master("MT")
    assert len(data["options"]) == 2 and len(data["edges"]) == 1
    assert "unreachable_node" in kinds(inventory.analyze_product_master(data))


def test_reader_detects_padded_duplicate_categories(isolated_db):
    with isolated_db() as db:
        for product in ("MT", "MT   "):
            db.add(Invdoc(workgroup="103", prodkind=product, codsc="測試", ordkind="1", quo_rate=1.0))
        db.commit()
    data = reader.read_product_master("MT")
    assert len(data["categories"]) == 2
    assert "category_count" in kinds(inventory.analyze_product_master(data))


def test_limit_overflow_is_rejected_not_silently_truncated(cmt1_db, monkeypatch):
    monkeypatch.setattr(reader, "MAX_TABLE_ROWS", 1)
    with pytest.raises(ValueError, match="上限"):
        reader.read_product_master("CMT1")
