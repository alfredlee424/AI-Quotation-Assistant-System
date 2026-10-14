"""獨立 unittest；合成主檔、禁止網路，不載入 pytest conftest 或真 DB。

python -B -m unittest discover -s test -p test_local_dynamic.py -v
"""
from copy import deepcopy
from decimal import Decimal, localcontext
import json
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

from local_quote.catalog import build_catalog
from local_quote.dynamic import analyze, freeze, reconcile_frozen, validate, validate_document


ROOT = r"CMT1\A001"
BOARD = ROOT + r"\S004"
BOX = ROOT + r"\S019\S020"
WIRE = ROOT + r"\S019\S022"
HOLE = ROOT + r"\S019\S035"
STAMP = {"actor": "test-operator", "reason": "合成資料人工核對", "time": "2026-10-14T08:00:00+08:00"}
SOURCE = "平直70X120-----2"


def fixture(material=False, code="CMT1"):
    """本檔自有 fixture，不 import 舊 business/backend/storage/agent。"""
    tables = {k: [] for k in ("ordstr", "ordspd", "ordspe", "ordqty")}
    labels = {}

    def node(tail, name, opts=(), required="Y", rules=()):
        path = code + "\\" + tail
        parent, optno = path.rsplit("\\", 1)
        tables["ordstr"].append(dict(workgroup="103", pathf=parent, pathc=path, optnoc=optno,
                                      optnof=parent.split("\\")[-1], must_chose=required, seq=len(labels)))
        labels[optno] = name
        for c, description, price in opts:
            tables["ordspe"].append(dict(workgroup="103", path=path, code=c, codsc=description, compri=price))
        for driver, c, usage, divisor in rules:
            tables["ordqty"].append(dict(workgroup="103", path=path, part_path=code + "\\" + driver,
                                          code=c, stdqty=usage, stdpar=divisor))

    node("A001", "桌面尺寸", [("D120", "70*120", "0"), ("D150", "70*150", "0")])
    node(r"A001\S010", "外型", [("FLAT", "平直", "10")])
    node(r"A001\S004", "板材", [("BOARD", "25mm MDF", "700")],
         rules=[("A001", "D120", "4.5", "24"), ("A001", "D150", "6", "24")])
    node(r"A001\S019", "選配容器", required="N")
    node(r"A001\S019\S020", "線盒", [("SILVER", "銀線盒", "120"), ("BLACK", "黑線盒", "140")], required="N")
    node(r"A001\S019\S022", "桌下槽走線", [("WIRE", "桌下走線", "100")], required="N")
    node(r"A001\S019\S035", "方孔", [("SMALL", "小孔", "50"), ("LARGE", "大孔", "70")], required="N")
    node("B001", "木腳", [("LEG", "45寬木腳", "0")], required="N")
    node(r"B001\S004", "板材", [("WOOD", "木腳板", "300")], rules=[("B001", "LEG", "2", "3")])
    if material:
        node(r"A001\S005", "材料容器")
        node(r"A001\S005\S000", "美耐板", [("WHITE", "白美耐板", "80")], required="N",
             rules=[("A001", "D120", "1", "2"), ("A001", "D150", "1", "2")])
        node(r"A001\S005\S001", "美耐皿", [("WALNUT", "817胡桃木", "100")], required="N",
             rules=[("A001", "D120", "1", "2"), ("A001", "D150", "1", "2")])
        node(r"A001\S005\S007", "下板", [("BOTTOM", "底板", "10")],
             rules=[("A001", "D120", "1", "2"), ("A001", "D150", "1", "2")])
    tables["ordspd"] = [dict(workgroup="103", optno=k, optdesc=v) for k, v in labels.items()]
    return build_catalog("103", dict(prodkind=code, codsc="合成測試產品", quo_rate="1.30"), tables)


def operator_choices(source, catalog, initial=None):
    """測試模擬使用者逐輪選候選；runtime 絕不呼叫這個 helper。"""
    choices = deepcopy(initial or {})
    for _ in range(8):
        result = analyze(source, catalog, choices=choices)
        additions = [g for g in result["gaps"] if g["candidates"] and g["id"] not in choices.get(g["child_id"], {})]
        if not additions:
            return choices, result
        for gap in additions:
            candidate = gap["candidates"][0]
            choices.setdefault(gap["child_id"], {})[gap["id"]] = {k: candidate[k] for k in ("path", "code")}
    raise AssertionError("選擇未收斂")


def child(result, index=0):
    return result["children"][index]


def item(result, path, index=0):
    return next(i for i in child(result, index)["items"] if i["path"] == path)


class DynamicTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("禁止網路"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.cat = fixture()

    def assertReady(self, result, index=0):
        self.assertTrue(child(result, index)["ready"], child(result, index)["issues"])

    def test_arbitrary_count_ids_ordinals_lines_and_raw_audit(self):
        source = "來源：合成\n\n" + "\n".join(f"平直70X120-----{i}" for i in range(1, 10))
        result = analyze(source, self.cat)
        self.assertEqual(len(result["children"]), 9)
        self.assertEqual(len({c["id"] for c in result["children"]}), 9)
        self.assertEqual([c["product_qty"] for c in result["children"]], list(range(1, 10)))
        self.assertEqual(child(result)["source_line"], 3)
        self.assertEqual([l["raw"] for l in result["source_ledger"]], source.splitlines())
        self.assertTrue(all(c["ready"] for c in result["children"]))

    def test_empty_and_invalid_order_quantity_not_silently_discarded(self):
        for source in ("", "桌面---0", "桌面---1.5", "桌面---NaN", "桌面---"):
            with self.subTest(source=source):
                result = analyze(source, self.cat)
                self.assertTrue(result["global_issues"])
                self.assertFalse(result["children"])

    def test_source_groups_shared_local_and_explicit_exceptions(self):
        source = ("工單：甲\n" + SOURCE + "\n平直70X150-----3\n共用[除2]：無木腳\n"
                  "第2項：45寬木腳\n工單：乙\n" + SOURCE + "\n共用：面無孔")
        result = analyze(source, self.cat)
        self.assertEqual(len(result["children"]), 3)
        self.assertTrue(all(c["ready"] for c in result["children"]), result["gaps"])
        self.assertNotIn(r"CMT1\B001", child(result)["selections"])
        self.assertIn(r"CMT1\B001", child(result, 1)["selections"])
        self.assertNotIn(r"CMT1\B001", child(result, 2)["selections"])
        self.assertEqual(child(result, 2)["group_ordinal"], 1)
        self.assertEqual(result["source_ledger"][3]["excludes"], [2])

    def test_unknown_unscoped_line_blocks_all_but_retains_draft(self):
        result = analyze(SOURCE + "\n腳粒鎖好／撕保護膜", self.cat)
        self.assertTrue(result["global_issues"])
        self.assertFalse(child(result)["ready"])
        self.assertIn("known_cost", child(result)["calc"])
        self.assertNotIn("total_cost", child(result)["calc"])

    def test_trello_headers_keep_unequal_quantities_and_group_boundaries(self):
        source = ("(1) 20260921-3甲公司\n平直70X120-----2\n平直70X150-----6\n"
                  "孔靠單/格靠單\n(2)20260819-2乙公司/13樓\n平直70X120-----1\n"
                  "平直70X150-----3\n共用：面無孔")
        result = analyze(source, self.cat)
        self.assertEqual([c["product_qty"] for c in result["children"]], [2, 6, 1, 3])
        self.assertEqual([c["group_ordinal"] for c in result["children"]], [1, 2, 1, 2])
        self.assertEqual([c["ready"] for c in result["children"]], [False, False, True, True])
        self.assertEqual(child(result, 2)["group"], "20260819-2乙公司/13樓")
        self.assertEqual(result["source_ledger"][4]["scope"], "header")
        validate_document(freeze(result, child(result, 2)["id"], **STAMP), current=result)

    def test_trello_reference_typo_preserved_not_treated_as_quantity_or_date(self):
        result = analyze("(1)202609521-1甲公司\n" + SOURCE, self.cat)
        self.assertReady(result)
        self.assertEqual(child(result)["group"], "202609521-1甲公司")
        self.assertEqual(child(result)["product_qty"], 2)

    def test_unknown_before_any_order_is_not_silently_unassigned(self):
        result = analyze("未確認說明\n工單：甲\n" + SOURCE, self.cat)
        self.assertFalse(child(result)["ready"])

    def test_chinese_slash_conditions_split_without_losing_requirements(self):
        result = analyze(SOURCE + "\n共用：面無孔/無木腳", self.cat)
        self.assertReady(result)
        self.assertEqual([r["text"] for r in child(result)["requirements"] if r["kind"] == "constraint"],
                         ["面無孔", "無木腳"])
        result = analyze(SOURCE + "\n共用：火星加工/金星塗裝", self.cat)
        self.assertFalse(child(result)["ready"])
        self.assertEqual([r["text"] for r in child(result)["requirements"] if r["kind"] == "semantic"],
                         ["火星加工", "金星塗裝"])

    def test_unknown_inline_is_requirement_never_free_note(self):
        result = analyze(SOURCE + " 火星加工", self.cat)
        self.assertTrue(any("需要建立客製化新件" in g["message"] for g in result["gaps"]))
        self.assertFalse(child(result)["ready"])

    def test_decimal_cost_quantity_driver_and_coefficient_tax(self):
        result = analyze(SOURCE, self.cat)
        self.assertReady(result)
        board = item(result, BOARD)
        self.assertEqual(board["driver_path"], ROOT)
        self.assertEqual(board["driver_code"], "D120")
        self.assertEqual(board["part_cost"], "262.50")
        self.assertEqual(child(result)["calc"]["total_cost"], "282.50")
        self.assertEqual(child(result)["calc"]["subtotal"], "367.25")
        self.assertEqual(child(result)["calc"]["tax_amount"], "18.36")
        self.assertEqual(child(result)["calc"]["total_price"], "385.61")
        self.assertEqual(item(analyze("平直70X150-----2", self.cat), BOARD)["part_cost"], "350.00")

    def test_decimal_context_external_precision_does_not_change_result(self):
        expected = analyze(SOURCE, self.cat)
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(analyze(SOURCE, self.cat), expected)

    def test_accessories_independent_quantities_belong_to_product(self):
        source = SOURCE + "\n配件：每桌2個銀線盒\n配件：每桌3個桌下走線\n平直70X150-----5"
        result = analyze(source, self.cat)
        self.assertReady(result)
        self.assertReady(result, 1)
        self.assertEqual(item(result, BOX)["qty"], "4")
        self.assertEqual(item(result, BOX)["part_cost"], "480.00")
        self.assertEqual(item(result, WIRE)["qty"], "6")
        self.assertNotIn(BOX, child(result, 1)["selections"])
        self.assertNotIn(HOLE, child(result)["selections"])
        self.assertIn(ROOT + r"\S019", [i["path"] for i in child(result)["items"]])

    def test_unknown_accessory_quantity_is_none_not_default_one(self):
        result = analyze(SOURCE + "\n配件：銀線盒", self.cat)
        self.assertIsNone(item(result, BOX)["line_qty"])
        self.assertIsNone(item(result, BOX)["qty"])
        self.assertIsNone(item(result, BOX)["part_cost"])
        self.assertFalse(child(result)["ready"])

    def test_total_accessory_count_not_multiplied_or_averaged(self):
        result = analyze(SOURCE + "\n配件：銀線盒共3個", self.cat)
        self.assertReady(result)
        self.assertEqual(item(result, BOX)["qty"], "3")
        self.assertEqual(item(result, BOX)["quantity_basis"], "total")
        source = SOURCE + "\n平直70X120-----3\n共用：銀線盒共5個"
        result = analyze(source, self.cat)
        self.assertTrue(all(not c["ready"] for c in result["children"]))
        self.assertTrue(all(item(result, BOX, i)["line_qty"] is None for i in range(2)))

    def test_accessory_required_children_inherit_quantity_and_manual_repair(self):
        path = BOX + r"\W020"
        self.cat["nodes"][path] = dict(pathf=BOX, pathc=path, optnoc="W020", optnof="S020", must_chose="Y")
        self.cat["labels"]["W020"] = "組裝工"
        self.cat["options"][path] = [dict(path=path, code="LABOR", codsc="組裝工", compri="10")]
        self.cat["rules"][path] = []
        for description, quantity in (("每桌2個銀線盒", "4"), ("銀線盒共3個", "3")):
            result = analyze(SOURCE + "\n配件：" + description, self.cat)
            self.assertReady(result)
            self.assertEqual(item(result, path)["qty"], quantity)
            self.assertEqual(item(result, path)["sources"]["line_qty"], "parent_accessory:" + BOX)
        source = SOURCE + "\n配件：銀線盒"
        result = analyze(source, self.cat)
        self.assertIsNone(item(result, path)["line_qty"])
        cid = child(result)["id"]
        result = analyze(source, self.cat, overrides={cid: {BOX: dict(STAMP, values={"line_qty": "3"})}})
        self.assertReady(result)
        self.assertEqual(item(result, path)["qty"], "6")

    def test_ambiguity_requires_legal_manual_identity(self):
        source = SOURCE + "\n配件：每桌2個線盒"
        result = analyze(source, self.cat)
        gap = next(g for g in result["gaps"] if len(g["candidates"]) == 2)
        cid, key = gap["child_id"], gap["id"]
        result = analyze(source, self.cat, choices={cid: {key: {"path": BOX, "code": "BLACK"}}})
        self.assertReady(result)
        self.assertEqual(item(result, BOX)["part_cost"], "560.00")
        result = analyze(source, self.cat, choices={cid: {key: {"path": BOARD, "code": "BOARD"}}})
        self.assertTrue(any(g["kind"] == "invalid_choice" for g in result["gaps"]))
        result = analyze(source, self.cat, choices={cid: {key: {"path": BOX, "code": "BLACK", "compri": "0"}}})
        self.assertFalse(child(result)["ready"])

    def test_all_inputs_bound_to_child_and_identity(self):
        source = SOURCE + "\n" + SOURCE
        result = analyze(source, self.cat)
        cid = child(result)["id"]
        result = analyze(source, self.cat, overrides={cid: {BOARD: dict(STAMP, values={"compri": "800"})}})
        self.assertEqual(item(result, BOARD)["compri"], "800")
        self.assertEqual(item(result, BOARD, 1)["compri"], "700")
        with self.assertRaises(ValueError):
            analyze(source, self.cat, overrides={"1": {BOARD: dict(STAMP, values={"compri": "800"})}})
        result = analyze(source, self.cat, choices={cid: {"obsolete": {"path": BOX, "code": "BLACK"}}})
        self.assertTrue(any(g["kind"] == "stale_input" for g in child(result)["issues"]))
        self.assertReady(result, 1)

    def test_inactive_optional_branch_does_not_open_required_descendants(self):
        result = analyze(SOURCE, self.cat)
        self.assertReady(result)
        self.assertFalse(any(i["path"].startswith(r"CMT1\B001") for i in child(result)["items"]))
        cid = child(result)["id"]
        result = analyze(SOURCE, self.cat, choices={cid: {r"node:CMT1\B001": {"path": r"CMT1\B001", "code": "LEG"}}})
        self.assertReady(result)
        self.assertEqual(item(result, r"CMT1\B001\S004")["driver_code"], "LEG")
        self.assertEqual(item(result, r"CMT1\B001\S004")["part_cost"], "400.00")
        self.assertNotIn(BOX, child(result)["selections"])

    def test_material_exactly_one_not_all_siblings(self):
        cat = fixture(material=True)
        choices, result = operator_choices(SOURCE, cat)
        self.assertReady(result)
        selected = child(result)["selections"]
        self.assertIn(ROOT + r"\S005", selected)
        self.assertIn(ROOT + r"\S005\S007", selected)
        branches = {ROOT + r"\S005\S000", ROOT + r"\S005\S001"}
        self.assertEqual(len(branches & selected.keys()), 1)
        cid = child(result)["id"]
        both = {cid: {"node:" + p: {"path": p, "code": cat["options"][p][0]["code"]} for p in branches}}
        result = analyze(SOURCE, cat, choices=both)
        self.assertTrue(any(g["kind"] == "conflict" for g in result["gaps"]))

    def test_missing_required_parent_spec_is_never_ready(self):
        self.cat["options"][ROOT] = []
        result = analyze(SOURCE, self.cat)
        self.assertFalse(child(result)["ready"])
        cat = fixture(material=True)
        choices, result = operator_choices(SOURCE, cat)
        self.assertReady(result)
        self.assertTrue(all(i["selected"] for i in child(result)["items"]))

    def test_custom_material_cannot_bypass_required_descendant(self):
        cat = fixture(material=True)
        parent = ROOT + r"\S005\S000"
        nested = parent + r"\W010"
        cat["nodes"][nested] = dict(pathf=parent, pathc=nested, optnoc="W010", optnof="S000", must_chose="Y")
        cat["labels"]["W010"] = "材料加工"
        cat["options"][nested] = []
        cat["rules"][nested] = []
        result = analyze(SOURCE, cat)
        cid = child(result)["id"]
        result = analyze(SOURCE, cat, customs={cid: {"material:" + ROOT + r"\S005":
                         dict(STAMP, path=parent, code="TMP:MAT", codsc="客製白板", compri="3")}})
        self.assertFalse(child(result)["ready"])
        self.assertIn(nested, child(result)["active_paths"])
        self.assertTrue(any(g["id"] == "node:" + nested for g in result["gaps"]))

    def test_required_material_flags_cannot_bypass_exclusion(self):
        cat = fixture(material=True)
        for p in (ROOT + r"\S005\S000", ROOT + r"\S005\S001"):
            cat["nodes"][p]["must_chose"] = "Y"
        result = analyze(SOURCE, cat)
        self.assertFalse(child(result)["ready"])
        self.assertTrue(any("互斥" in g["message"] for g in result["gaps"]))

    def test_forbidden_holes_and_legs_even_with_explicit_choice(self):
        for prohibition, path, code in (("面無孔", HOLE, "SMALL"), ("無木腳", r"CMT1\B001", "LEG")):
            source = SOURCE + " " + prohibition
            cid = child(analyze(source, self.cat))["id"]
            result = analyze(source, self.cat, choices={cid: {"node:" + path: {"path": path, "code": code}}})
            self.assertFalse(child(result)["ready"])
            self.assertNotIn(path, child(result)["selections"])
        cat = deepcopy(self.cat)
        cat["nodes"][r"CMT1\B001"]["must_chose"] = "Y"
        result = analyze(SOURCE + " 無木腳", cat)
        self.assertTrue(any(g["kind"] == "conflict" for g in result["gaps"]))

    def test_missing_invalid_cost_still_draft_and_none_not_zero(self):
        for value in (None, "", "NaN", float("inf"), "-1"):
            cat = deepcopy(self.cat)
            cat["options"][BOARD][0]["compri"] = value
            result = analyze(SOURCE, cat)
            self.assertIsNone(item(result, BOARD)["compri"])
            self.assertIsNone(item(result, BOARD)["part_cost"])
            self.assertEqual(child(result)["calc"]["known_cost"], "20.00")
            self.assertNotIn("total_cost", child(result)["calc"])
            self.assertTrue(any("需要建立部件成本" in g["message"] for g in result["gaps"]))

    def test_ordqty_missing_driver_duplicate_or_zero_divisor_no_fallback(self):
        for mode in ("missing", "wrong_code", "wrong_driver", "duplicate", "zero", "null"):
            cat = deepcopy(self.cat)
            if mode == "missing":
                cat["rules"][BOARD] = []
            elif mode == "wrong_code":
                cat["rules"][BOARD] = [cat["rules"][BOARD][1]]
            elif mode == "wrong_driver":
                cat["rules"][BOARD][0]["part_path"] = "OTHER\\A001"
            elif mode == "duplicate":
                cat["rules"][BOARD].append(deepcopy(cat["rules"][BOARD][0]))
            else:
                cat["rules"][BOARD][0]["stdpar"] = "0" if mode == "zero" else None
            with self.subTest(mode=mode):
                result = analyze(SOURCE, cat)
                self.assertFalse(child(result)["ready"])
                self.assertIsNone(item(result, BOARD)["part_cost"])

    def test_per_item_policy_only_without_any_ordqty_records(self):
        result = analyze(SOURCE, self.cat)
        shape = ROOT + r"\S010"
        self.assertEqual(item(result, shape)["sources"]["stdqty"], "CMT1_existing_per_item_policy")
        self.cat["rules"][shape] = [dict(path=shape, part_path=ROOT, code="D150", stdqty="1", stdpar="1")]
        result = analyze(SOURCE, self.cat)
        self.assertIsNone(item(result, shape)["stdqty"])
        self.assertFalse(child(result)["ready"])

    def test_structural_zero_is_legal_without_fabricated_usage(self):
        result = analyze(SOURCE + "\n配件：每桌1個銀線盒", self.cat)
        self.assertReady(result)
        container = item(result, ROOT + r"\S019")
        self.assertEqual(container["compri"], "0")
        self.assertEqual(container["part_cost"], "0.00")
        self.assertIsNone(container["stdqty"])
        self.assertIsNone(container["stdpar"])

    def test_positive_parent_is_costed_not_just_leaves(self):
        self.cat["options"][ROOT][0]["compri"] = "5"
        self.cat["rules"][ROOT] = [dict(path=ROOT, part_path=ROOT, code="D120", stdqty="2", stdpar="1")]
        result = analyze(SOURCE, self.cat)
        self.assertReady(result)
        self.assertEqual(item(result, ROOT)["part_cost"], "20.00")
        self.assertEqual(child(result)["calc"]["total_cost"], "302.50")

    def test_manual_cost_repair_all_fields_and_audit(self):
        self.cat["options"][BOARD][0]["compri"] = None
        self.cat["rules"][BOARD] = []
        result = analyze(SOURCE, self.cat)
        cid = child(result)["id"]
        override = dict(STAMP, values={"compri": "123.45", "stdqty": "3", "stdpar": "2", "line_qty": "2"})
        result = analyze(SOURCE, self.cat, overrides={cid: {BOARD: override}})
        self.assertReady(result)
        board = item(result, BOARD)
        self.assertEqual(board["part_cost"], "740.70")
        self.assertEqual(board["audit"][0]["actor"], STAMP["actor"])
        self.assertEqual(board["audit"][0]["before"]["compri"], None)
        self.assertTrue(all(s == "operator_override" for s in board["sources"].values()))

    def test_invalid_override_values_and_audits_fail_closed(self):
        cid = child(analyze(SOURCE, self.cat))["id"]
        for field, value in (("compri", "NaN"), ("stdqty", "Infinity"), ("stdpar", "0"),
                             ("line_qty", "0"), ("compri", "-1"), ("stdpar", None), ("stdqty", True)):
            with self.subTest(field=field, value=value):
                result = analyze(SOURCE, self.cat, overrides={cid: {BOARD: dict(STAMP, values={field: value})}})
                self.assertFalse(child(result)["ready"])
                self.assertTrue(any(g["kind"] == "invalid_override" for g in result["gaps"]))
        for audit in ({}, dict(STAMP, actor=""), dict(STAMP, time="2026-10-14")):
            result = analyze(SOURCE, self.cat, overrides={cid: {BOARD: dict(audit, values={"compri": "9"})}})
            self.assertFalse(child(result)["ready"])

    def test_cost_overflow_is_a_gap_not_exception_or_infinite_amount(self):
        self.cat["options"][BOARD][0]["compri"] = "3e38"
        self.cat["rules"][BOARD][0].update(stdqty="3e38", stdpar="1e-38")
        result = analyze(SOURCE, self.cat)
        self.assertFalse(child(result)["ready"])
        self.assertIsNone(item(result, BOARD)["part_cost"])
        json.dumps(result, allow_nan=False)

    def test_zero_usage_allowed_with_positive_divisor(self):
        self.cat["rules"][BOARD][0]["stdqty"] = "0"
        result = analyze(SOURCE, self.cat)
        self.assertReady(result)
        self.assertEqual(item(result, BOARD)["part_cost"], "0.00")

    def test_mixed_and_taiwan_units_block_and_audited_resolution(self):
        for text in ("平直2台尺X120公分-----2", "平直2X4台尺-----2", "平直700mmX120cm-----2"):
            result = analyze(text, self.cat)
            self.assertFalse(child(result)["ready"])
            gap = next(g for g in result["gaps"] if g["kind"] == "units")
            resolution = dict(STAMP, text="平直70X120公分")
            fixed = analyze(text, self.cat, resolutions={gap["child_id"]: {gap["id"]: resolution}})
            self.assertReady(fixed)
            source = child(fixed)["requirements"][0]["source"]
            self.assertIn("台尺" if "台尺" in text else "mm", source["original"])
            self.assertEqual(source["resolution"]["actor"], STAMP["actor"])

    def test_explicit_metric_dimensions_exact_no_nearest_match(self):
        self.assertReady(analyze("平直700X1200mm-----2", self.cat))
        result = analyze("平直70X140-----2", self.cat)
        self.assertFalse(child(result)["ready"])
        self.assertTrue(any(g["kind"] == "custom_required" for g in result["gaps"]))

    def test_temporary_dimension_attached_existing_node_and_cost_override(self):
        source = "平直70X140-----2"
        result = analyze(source, self.cat)
        gap = next(g for g in result["gaps"] if g["kind"] == "custom_required")
        cid, key = gap["child_id"], gap["id"]
        custom = dict(STAMP, path=ROOT, code="TMP:D140", codsc="客製70*140", compri="0")
        customs = {cid: {key: custom}}
        # 臨時 driver 不能偷偷拿 D120 的用量，連父節點自己的用量也需補齊。
        result = analyze(source, self.cat, customs=customs)
        self.assertFalse(child(result)["ready"])
        self.assertIsNone(item(result, BOARD)["stdqty"])
        overrides = {cid: {ROOT: dict(STAMP, values={"stdqty": "1", "stdpar": "1"}),
                           BOARD: dict(STAMP, values={"stdqty": "5.5", "stdpar": "24"})}}
        result = analyze(source, self.cat, customs=customs, overrides=overrides)
        self.assertReady(result)
        self.assertTrue(item(result, ROOT)["custom"])
        self.assertEqual(len(self.cat["options"][ROOT]), 2)
        self.assertEqual(item(result, BOARD)["driver_code"], "TMP:D140")

    def test_custom_invalid_path_and_cannot_bypass_forbidden_branch(self):
        source = SOURCE + "\n配件：每桌1個火星件"
        result = analyze(source, self.cat)
        gap = next(g for g in result["gaps"] if g["kind"] == "custom_required")
        custom = dict(STAMP, path="CMT1\\NEW", code="TMP:X", codsc="火星件", compri="10")
        result = analyze(source, self.cat, customs={gap["child_id"]: {gap["id"]: custom}})
        self.assertTrue(any(g["kind"] == "invalid_custom" for g in result["gaps"]))
        source += "\n共用：面無孔"
        result = analyze(source, self.cat)
        gap = next(g for g in result["gaps"] if g["kind"] == "custom_required")
        custom["path"] = HOLE
        result = analyze(source, self.cat, customs={gap["child_id"]: {gap["id"]: custom}})
        self.assertFalse(child(result)["ready"])
        self.assertNotIn(HOLE, child(result)["selections"])

    def test_resolution_cannot_be_blank_or_free_unparsed_note(self):
        source = SOURCE + " 未知工序"
        result = analyze(source, self.cat)
        gap = next(g for g in result["gaps"] if g["kind"] == "custom_required")
        for text in ("", "這只是一段備註"):
            result = analyze(source, self.cat, resolutions={gap["child_id"]: {gap["id"]: dict(STAMP, text=text)}})
            self.assertFalse(child(result)["ready"])

    def test_generated_dimension_gap_exposes_original_resolution_identity(self):
        source = "平直70X140-----2"
        result = analyze(source, self.cat)
        gap = next(g for g in result["gaps"] if g["kind"] == "custom_required")
        self.assertNotEqual(gap["id"], gap["resolution_id"])
        self.assertEqual(gap["source"]["line"], 1)
        result = analyze(source, self.cat, resolutions={gap["child_id"]: {
            gap["resolution_id"]: dict(STAMP, text="平直70X120")}})
        self.assertReady(result)

    def test_full_hierarchy_parent_depth_and_json_serializable(self):
        cat = fixture(material=True)
        _, result = operator_choices(SOURCE + "\n配件：每桌1個銀線盒", cat)
        self.assertReady(result)
        rows = child(result)["items"]
        paths = [row["path"] for row in rows]
        for row in rows:
            if row["parent_path"] != cat["prodkind"]:
                self.assertLess(paths.index(row["parent_path"]), paths.index(row["path"]))
            self.assertEqual(row["depth"], row["path"].count("\\"))
        json.dumps(result, allow_nan=False)

    def test_snapshot_structural_corruption_rejected(self):
        for mode in ("orphan", "identity", "foreign", "missing_label"):
            cat = deepcopy(self.cat)
            if mode == "orphan":
                del cat["nodes"][ROOT]
                del cat["options"][ROOT]
                del cat["rules"][ROOT]
            elif mode == "identity":
                cat["nodes"][BOARD]["pathf"] = BOARD
            elif mode == "foreign":
                cat["options"][BOARD][0]["workgroup"] = "999"
            else:
                del cat["labels"]["S004"]
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                analyze(SOURCE, cat)

    def test_freeze_validate_tamper_and_no_trusting_ready_summary(self):
        result = analyze(SOURCE, self.cat)
        document = freeze(result, child(result)["id"], **STAMP)
        self.assertEqual(validate_document(document, current=result), child(result))
        self.assertEqual(validate(child(result), result["context"]), child(result))
        for mutate in (
            lambda c: c["calc"].update(total_price="0.00"),
            lambda c: c["items"][1].update(compri="0"),
            lambda c: c["items"].pop(),
            lambda c: c["selections"].pop(BOARD),
            lambda c: c.update(source_line=99),
        ):
            bad = deepcopy(document)
            mutate(bad["child"])
            with self.assertRaises(ValueError):
                validate_document(bad)
        bad_result = analyze(SOURCE + " 未知", self.cat)
        child(bad_result)["ready"] = True
        child(bad_result)["issues"] = []
        with self.assertRaises(ValueError):
            freeze(bad_result, child(bad_result)["id"], **STAMP)

    def test_revision_isolation_and_source_change_revokes_all(self):
        source = {"document_id": "work-001", "text": SOURCE + "\n" + SOURCE}
        result = analyze(source, self.cat)
        documents = [freeze(result, c["id"], **STAMP) for c in result["children"]]
        first_id = child(result)["id"]
        updated = analyze(source, self.cat, overrides={first_id: {BOARD: dict(STAMP, values={"compri": "800"})}})
        self.assertNotEqual(child(result)["revision"], child(updated)["revision"])
        self.assertEqual(child(result, 1)["revision"], child(updated, 1)["revision"])
        with self.assertRaises(ValueError):
            validate_document(documents[0], current=updated)
        validate_document(documents[1], current=updated)
        source["text"] += "\n"
        changed = analyze(source, self.cat)
        self.assertEqual([c["id"] for c in result["children"]], [c["id"] for c in changed["children"]])
        for doc in documents:
            with self.assertRaises(ValueError):
                validate_document(doc, current=changed)

    def test_edit_reconciliation_does_not_resurrect_reverted_freeze(self):
        source = SOURCE + "\n" + SOURCE
        initial = analyze(source, self.cat)
        documents = {c["id"]: freeze(initial, c["id"], **STAMP) for c in initial["children"]}
        cid = child(initial)["id"]
        changed = analyze(source, self.cat, overrides={cid: {BOARD: dict(STAMP, values={"compri": "800"})}})
        reconciled = reconcile_frozen(changed, documents)
        self.assertEqual(set(reconciled["revoked"]), {cid})
        retained = reconcile_frozen(initial, reconciled["retained"])["retained"]
        self.assertNotIn(cid, retained)
        self.assertEqual(len(retained), 1)
        self.assertFalse(reconcile_frozen(analyze(source + "\n", self.cat), retained)["retained"])

    def test_frozen_detached_single_child_even_other_child_incomplete(self):
        source = SOURCE + "\n平直70X140-----3"
        result = analyze(source, self.cat)
        document = freeze(result, child(result)["id"], **STAMP)
        self.cat["options"][BOARD][0]["compri"] = "999"
        child(result)["items"][0]["codsc"] = "改過"
        validated = validate_document(json.loads(json.dumps(document)), trusted_catalog_digest=document["child"]["catalog_digest"])
        self.assertEqual(validated["items"][0]["codsc"], "70*120")
        with self.assertRaises(ValueError):
            validate_document(document, trusted_catalog_digest="untrusted")

    def test_non_cmt1_does_not_inherit_per_item_policy(self):
        cat = fixture(code="ZZ")
        result = analyze("主檔配置-----1", cat)
        self.assertTrue(result["gaps"])
        choices, result = operator_choices("主檔配置-----1", cat)
        self.assertFalse(child(result)["ready"])
        self.assertIsNone(item(result, r"ZZ\A001\S010")["stdqty"])

    def test_import_has_no_agent_database_storage_or_config_side_effects(self):
        code = (
            "import sys, socket; "
            "socket.socket.connect=lambda *a, **k: (_ for _ in ()).throw(AssertionError('network')); "
            "from local_quote.dynamic import analyze; "
            "assert not any(n == 'config' or n.split('.')[0] in ('agent', 'database', 'sqlalchemy') "
            "or n in ('local_quote.business', 'local_quote.storage', 'local_quote.backend') for n in sys.modules)"
        )
        completed = subprocess.run([sys.executable, "-B", "-c", code], cwd=Path(__file__).resolve().parents[1],
                                   text=True, capture_output=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
