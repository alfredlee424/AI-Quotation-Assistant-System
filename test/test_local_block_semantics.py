"""六份獨立來源、獨立合成主檔；unittest 執行，不載入 DB/conftest。"""
from copy import deepcopy
import socket
import unittest
from unittest.mock import patch

from local_quote import dynamic
from local_quote.catalog import build_catalog
from local_quote.semantic_normalization import semantic_equal
from local_block_semantics_fixtures import CASES, V1_FROZEN


AUDIT = dict(actor="semantic-test", reason="獨立合成設定核對", time="2026-10-15T10:00:00+08:00")
TOP = r"SYN\TOP"


def master(missing_price=False):
    """非 CMT1、無既定 path code；全部單價／用量由測試自有四表給定。"""
    tables = {name: [] for name in ("ordstr", "ordspd", "ordspe", "ordqty")}

    def node(path, label, options=(), required="N", driver=None):
        parent, code = path.rsplit("\\", 1)
        tables["ordstr"].append(dict(workgroup="T", pathf=parent, pathc=path, optnoc=code,
                                     optnof=parent.split("\\")[-1], must_chose=required, seq=len(tables["ordstr"])))
        tables["ordspd"].append(dict(workgroup="T", optno=code, optdesc=label))
        for identity, text, price in options:
            tables["ordspe"].append(dict(workgroup="T", path=path, code=identity, codsc=text, compri=price))
            if driver is None:
                tables["ordqty"].append(dict(workgroup="T", path=path, part_path=path,
                                             code=identity, stdqty="1", stdpar="1"))
        if driver:
            for identity in ("D70", "D120", "D140"):
                tables["ordqty"].append(dict(workgroup="T", path=path, part_path=driver,
                                             code=identity, stdqty="2", stdpar="1"))

    node(TOP, "桌面尺寸", [("D70", "70*70", "0"), ("D120", "70*120", "0"),
                              ("D140", "70*140", "0"), ("DAMB", "3*3.25", "0")], "Y")
    node(TOP + r"\FORM", "外型", [("CORNER", "彎角", "2"), ("FLAT", "平直", "2"), ("CURVE", "平彎", "2")], "Y")
    node(TOP + r"\EDGE", "邊加工", [("CUT", "削角", "3")])
    node(TOP + r"\BOARD", "板材", [("BASE", "基板", "100")], "Y", TOP)
    node(TOP + r"\FACE", "面材", [("817", "817胡桃", None if missing_price else "11"),
                                     ("340", "紅胡340", "13"), ("808", "808白橡", "17")])
    node(TOP + r"\PAINT", "塗裝", [("WAL", "胡桃漆", "5"), ("RED", "噴紅木", "7"), ("WHITE", "噴白橡", "9")])
    node(TOP + r"\SHELF", "置物板", [("SINGLE", "單面置物板", "19")])
    node(TOP + r"\WIRE", "桌下槽走線", [("WIRE", "桌下走線", "23")])
    node(TOP + r"\BOX", "線盒", [("SILVER", "銀色單掀鋁毛刷13.5X40", "29")])
    node(TOP + r"\RECT", "方孔", [("RECT", "13.5X40方孔", "31")])
    node(TOP + r"\ROUND", "圓孔", [("R6", "6公分圓孔", "7")])
    node(TOP + r"\HOLEPOS", "孔位置", [("CENTER", "孔置中", "3")])
    node(TOP + r"\FILM", "保護膜", [("REMOVE", "撕保護膜", "2")])
    node(r"SYN\LEGS", "木腳")
    node(r"SYN\LEGS\LEGFACE", "腳材", [("OAK", "白橡紙", "11")])
    node(r"SYN\LEGS\LOCK", "腳粒", [("LOCK", "腳粒鎖好", "3")])
    node(r"SYN\LEGS\LEGHOLE", "走線孔", [("HOLE", "6公分圓孔", "13")])
    node(r"SYN\PANEL", "隔板")
    node(r"SYN\PANEL\PANELHOLE", "隔板孔位置", [("CENTER", "隔板孔位置中", "5")])
    node(r"SYN\PANEL\PANELPOS", "隔板水平位置", [("CENTER", "格置中", "5")])
    node(r"SYN\PANEL\PANELVERT", "隔板垂直位置", [("BELOW", "格在走線下方", "7")])
    return build_catalog("T", dict(prodkind="SYN", codsc="独立語意測試主檔", quo_rate="1.25"), tables)


def case(name):
    return CASES[name]


def requirements(child, kind=None, part=None, active=True):
    return [r for r in child["normalized_requirements"] if (not active or r["effective"])
            and (kind is None or r["kind"] == kind) and (part is None or r["part"] == part)]


def costs(analysis):
    return [c["calc"] for c in analysis["children"]]


def missing(analysis):
    return [[(g["kind"], g.get("fields"), g.get("custom_paths")) for g in c["issues"]] for c in analysis["children"]]


class BlockSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("不允許網路"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.cat = master()

    def analyze(self, text, **inputs):
        return dynamic.analyze(text, self.cat, **inputs)

    def test_six_literal_sources_three_semantic_pairs_not_twenty_four_orders(self):
        for group, qty in ((1, [2, 1, 6, 2]), (2, [2, 1, 2, 2]), (3, [2, 1, 4, 2])):
            pair = [self.analyze(case(f"{group}-{style}")) for style in (1, 2)]
            for result in pair:
                self.assertEqual([c["product_qty"] for c in result["children"]], qty)
                self.assertFalse(result["global_issues"])
                self.assertEqual(result["source_ledger"][0]["scope"], "header")
                for c in result["children"]:
                    self.assertFalse(requirements(c, "unknown"), c["normalized_requirements"])
                    self.assertFalse([r for r in requirements(c) if r["status"] == "pending_master"])
                    self.assertFalse(c["ready"])  # 原句的圖面或單位未核對，不是假完成。
                    self.assertNotIn("total_cost", c["calc"])
                    self.assertTrue(set(c["active_paths"]) <= self.cat["nodes"].keys())
                    for r in requirements(c):
                        self.assertTrue({"kind", "part", "targets", "value", "quantity", "basis", "origin",
                                         "override", "conflict", "status", "effective", "requirement_ids"} <= r.keys())
                        self.assertIn(r["origin"]["raw"], result["context"]["source"]["text"])
                        self.assertIsInstance(r["origin"]["line"], int)
            self.assertTrue(semantic_equal(pair[0]["children"], pair[1]["children"]))
            self.assertEqual(costs(pair[0]), costs(pair[1]))
            self.assertEqual(missing(pair[0]), missing(pair[1]))

    def test_pair_cost_equal_with_same_manual_input_and_missing_price(self):
        for group in (1, 2, 3):
            for no_price in (False, True):
                self.cat = master(missing_price=no_price)
                pair = []
                for style in (1, 2):
                    source = case(f"{group}-{style}")
                    a = self.analyze(source)
                    changes = {c["id"]: {TOP + r"\FORM": dict(AUDIT, values={"compri": "41"})} for c in a["children"]}
                    pair.append(self.analyze(source, overrides=changes))
                self.assertTrue(semantic_equal(pair[0]["children"], pair[1]["children"]))
                self.assertEqual(costs(pair[0]), costs(pair[1]))
                self.assertEqual(missing(pair[0]), missing(pair[1]))
                if no_price and group == 1:
                    for c in pair[0]["children"]:
                        row = next(i for i in c["items"] if i["path"] == TOP + r"\FACE")
                        self.assertIsNone(row["compri"])
                        self.assertIsNone(row["part_cost"])

    def test_group_one_quantities_cost_corner_edge_and_no_hole(self):
        a = self.analyze(case("1-1"))
        # 獨立手算：板200+形2+邊3+走線23+面11+漆5+架19+鎖3+膜2 = 268/件。
        self.assertEqual(a["children"][0]["calc"]["known_cost"], "536.00")
        self.assertEqual([c["calc"]["known_cost"] for c in a["children"][1:]], ["302.00", "1812.00", "604.00"])
        c = a["children"][0]
        self.assertEqual(c["shape"], "corner")
        self.assertEqual(requirements(c, "edge")[0]["value"], "cut")
        self.assertFalse(requirements(c, "hole"))
        self.assertNotIn(TOP + r"\RECT", c["selections"])
        self.assertNotIn(TOP + r"\ROUND", c["selections"])
        self.assertNotIn(TOP + r"\HOLEPOS", c["selections"])
        # 局部無孔使共用孔靠單不適用；格靠單仍待核對。
        inactive = [r for r in requirements(c, active=False) if r["status"] == "not_applicable"]
        self.assertEqual([r["part"] for r in inactive], ["tabletop_hole"])
        for c in a["children"][1:]:
            hole = requirements(c, "hole")[0]
            self.assertEqual(hole["value"]["shape"], "rectangle")
            self.assertEqual(hole["quantity"], "1")
            self.assertEqual(c["selections"][TOP + r"\BOX"]["line_qty"], "1")

    def test_role_targets_resolve_even_between_products_or_before_products(self):
        a = self.analyze(case("2-1"))
        self.assertEqual([c["role"] for c in a["children"]], [None, "chair", "attendee", "attendee"])
        self.assertEqual([c["shape"] for c in a["children"]], ["corner", "straight", "straight", "curved"])
        for c in a["children"]:
            r = requirements(c, part="divider_hole")[0]
            self.assertEqual(r["targets"], [1, 2] if c["group_ordinal"] < 3 else [3, 4])
            self.assertEqual(r["kind"], "external_confirmation" if c["group_ordinal"] < 3 else "position")
        source = case("2-1")
        lines = source.splitlines()
        conditions = [s for s in lines if s.startswith(("主席及", "列席桌"))]
        moved = "\n".join([lines[0]] + conditions + [s for s in lines[1:] if s not in conditions])
        self.assertTrue(semantic_equal(a["children"], self.analyze(moved)["children"]))

    def test_parts_box_and_round_quantity_no_diameter_as_table_dimension(self):
        for c in self.analyze(case("2-2"))["children"]:
            self.assertEqual(len(requirements(c, "dimension")), 1)
            self.assertTrue(requirements(c, "dimension")[0]["value"]["confirmation_required"])
            self.assertEqual(requirements(c, "hole")[0]["value"], dict(shape="round", diameter="6", unit="cm"))
            self.assertEqual(requirements(c, "box")[0]["quantity"], "1")
            self.assertEqual(requirements(c, "hole")[0]["quantity"], "1")
            self.assertEqual({r["part"] for r in requirements(c, "material")}, {"tabletop", "legs"})
            self.assertEqual(requirements(c, "coating")[0]["part"], "tabletop")
            self.assertEqual(requirements(c, "constraint")[0]["part"], "leg_hole")
            self.assertIn(TOP + r"\ROUND", c["selections"])
            self.assertNotIn(TOP + r"\RECT", c["selections"])
            self.assertNotIn(TOP, c["selections"])  # 3x3.25 即使主檔有同數字也不偷選。
            self.assertNotIn(r"SYN\LEGS\LEGHOLE", c["selections"])
            self.assertEqual(len(requirements(c, "assembly")), 2)

    def test_box_required_cutout_inherits_quantity_without_duplicate_round_holes(self):
        box = TOP + r"\BOX"
        path = box + r"\BOXCUT"
        self.cat["nodes"][path] = dict(pathf=box, pathc=path, optnoc="BOXCUT", optnof="BOX", must_chose="Y")
        self.cat["labels"]["BOXCUT"] = "線盒方孔"
        self.cat["options"][path] = [dict(path=path, code="CUT", codsc="13.5X40方孔", compri="4")]
        self.cat["rules"][path] = [dict(path=path, part_path=path, code="CUT", stdqty="1", stdpar="1")]
        a = self.analyze("平直70X120-----2\n共用：2個銀色單掀鋁毛刷13.5X40及1孔6公分圓孔")
        c = a["children"][0]
        self.assertTrue(c["ready"], c["issues"])
        quantities = {i["path"]: i["qty"] for i in c["items"]}
        self.assertEqual(quantities[box], "4")
        self.assertEqual(quantities[path], "4")
        self.assertEqual(quantities[TOP + r"\ROUND"], "2")
        self.assertEqual(len(requirements(c, "hole")), 1)
        self.assertNotIn(TOP + r"\RECT", c["active_paths"])

    def test_required_holes_and_manual_node_cannot_bypass_part_constraints(self):
        text = "平直70X120-----1 面無孔\n共用：腳2片不挖走線孔"
        c = self.analyze(text)["children"][0]
        for path, code in ((TOP + r"\ROUND", "R6"), (r"SYN\LEGS\LEGHOLE", "HOLE")):
            fixed = self.analyze(text, choices={c["id"]: {"node:" + path: dict(path=path, code=code)}})
            self.assertFalse(fixed["children"][0]["ready"])
            self.assertNotIn(path, fixed["children"][0]["selections"])
        self.cat["nodes"][TOP + r"\ROUND"]["must_chose"] = "Y"
        a = self.analyze(text)
        self.assertTrue(any(g["kind"] == "conflict" for g in a["gaps"]))

    def test_only_explicit_controlled_typo_and_fullwidth_colon_pairs(self):
        for group in (1, 2, 3):
            a = self.analyze(case(f"{group}-1"))
            b = self.analyze(case(f"{group}-2").replace("共用:", "共用："))
            self.assertTrue(semantic_equal(a["children"], b["children"]))
        a = self.analyze(case("3-1"))
        b = self.analyze(case("3-1").replace("走縣孔", "走線孔"))
        self.assertTrue(semantic_equal(a["children"], b["children"]))

    def test_semantic_comparison_detects_changed_qty_targets_parts_and_holes(self):
        children = self.analyze(case("2-1"))["children"]
        for field, value in (("quantity", "3"), ("targets", [4]), ("part", "leg_hole"),
                             ("value", dict(shape="round", diameter="9", unit="cm"))):
            changed = deepcopy(children)
            requirements(changed[0], "hole")[0][field] = value
            self.assertFalse(semantic_equal(children, changed))

    def test_group_three_per_product_holes_and_controlled_typo_provenance(self):
        children = self.analyze(case("3-1"))["children"]
        self.assertEqual([requirements(c, "hole")[0]["quantity"] for c in children], ["1", "1", "2", "2"])
        self.assertEqual([next(i for i in c["items"] if i["path"] == TOP + r"\ROUND")["qty"] for c in children],
                         ["2", "1", "8", "4"])
        for c in children:
            r = requirements(c, "constraint", "leg_hole")[0]
            self.assertIn("走縣孔", r["origin"]["raw"])
            self.assertEqual(r["value"], "forbidden")
            self.assertEqual(r["quantity"], "2")
            self.assertEqual(requirements(c, "material")[0]["value"], "340_red_walnut")
            self.assertEqual(requirements(c, "coating")[0]["value"], "red_wood")
        unknown = self.analyze("平直70X120-----1\n共用：腳2片不挖走弦孔")
        self.assertTrue(requirements(unknown["children"][0], "unknown"))

    def test_block_empty_lines_new_product_scope_and_order_boundaries(self):
        source = ("工單：甲\n平直70X120-----1\n共用：\n817胡\n\n噴胡\n平彎70X140-----2\n"
                  "共用：單面置物版\n局部：桌下走線\n未指派文字\n工單：乙\n平直70X120-----3")
        a = self.analyze(source)
        first, second, third = a["children"]
        for c in (first, second):
            self.assertEqual(requirements(c, "material")[0]["targets"], [1, 2])
            self.assertTrue(requirements(c, "coating"))
        self.assertFalse(requirements(first, "routing"))
        self.assertTrue(requirements(second, "routing"))
        self.assertFalse(requirements(third, "material"))
        self.assertTrue(third["ready"])
        self.assertEqual(a["source_ledger"][4]["scope"], "blank")
        self.assertTrue(a["global_issues"])
        no_block = self.analyze("平直70X120-----1\n共用：817胡\n噴胡")
        self.assertTrue(no_block["global_issues"])
        product_boundary = self.analyze("平直70X120-----1\n共用：\n817胡\n平直70X120-----1\n噴胡")
        self.assertTrue(product_boundary["global_issues"])

    def test_empty_header_only_invalid_targets(self):
        for text in ("", "共用：", "1-1\n共用：\n\n", "平直70X120-----1\n共用:",
                     "共用：\n工單：甲\n平直70X120-----1", "平直70X120-----1\n列席桌隔板孔位置中"):
            with self.subTest(text=text):
                a = self.analyze(text)
                self.assertTrue(a["global_issues"])
                self.assertFalse(any(c["ready"] for c in a["children"]))

    def test_example_headers_are_separate_scopes_not_shared_across_examples(self):
        source = ("1-1\n平直70X120-----1\n共用：\n817胡\n"
                  "1-2\n平直70X120-----2\n共用：紅胡340")
        a = self.analyze(source)
        self.assertFalse(a["global_issues"])
        self.assertEqual([c["group"] for c in a["children"]], ["1-1", "1-2"])
        self.assertEqual([c["group_ordinal"] for c in a["children"]], [1, 1])
        self.assertEqual([requirements(c, "material")[0]["value"] for c in a["children"]],
                         ["817_walnut", "340_red_walnut"])
        self.assertTrue(all(c["ready"] for c in a["children"]))

    def test_leg_routing_hole_ban_does_not_ban_other_leg_holes(self):
        path = r"SYN\LEGS\FIXHOLE"
        self.cat["nodes"][path] = dict(pathf=r"SYN\LEGS", pathc=path, optnoc="FIXHOLE", optnof="LEGS",
                                      must_chose="Y")
        self.cat["labels"]["FIXHOLE"] = "腳部固定圓孔"
        self.cat["options"][path] = [dict(path=path, code="FIX", codsc="6公分圓孔", compri="4")]
        self.cat["rules"][path] = [dict(path=path, part_path=path, code="FIX", stdqty="1", stdpar="1")]
        source = "平直70X120-----2\n共用：\n腳白橡紙\n腳2片不挖走線孔"
        a = self.analyze(source)
        c = a["children"][0]
        # 固定孔仍依結構必選啟用；其數量尚須人工確認，不能被走線孔禁令省略。
        self.assertIn(path, c["selections"])
        self.assertNotIn(r"SYN\LEGS\LEGHOLE", c["selections"])
        fixed = self.analyze(source, overrides={c["id"]: {path: dict(AUDIT, values={"line_qty": "2"})}})
        self.assertTrue(fixed["children"][0]["ready"], fixed["gaps"])

    def test_local_override_and_equal_priority_conflicts(self):
        a = self.analyze("平直70X120-----1 面808白橡\n共用：817胡")
        c = a["children"][0]
        self.assertTrue(c["ready"], c["issues"])
        material = requirements(c, "material")[0]
        self.assertEqual(material["value"], "808_white_oak")
        self.assertEqual(len(material["override"]), 1)
        self.assertEqual(c["selections"][TOP + r"\FACE"]["code"], "808")
        for text in ("平直70X120-----1 面808白橡/817胡", "平直70X120-----1\n共用：817胡\n共用：紅胡340",
                     "平直70X120-----1 面無孔/1孔6公分圓"):
            c = self.analyze(text)["children"][0]
            self.assertFalse(c["ready"])
            self.assertTrue(any(g["kind"] == "conflict" for g in c["issues"]))
            self.assertTrue(any(r["conflict"] for r in requirements(c)))

    def test_missing_custom_paths_strict_candidates_not_wrong_parts(self):
        self.cat["options"][TOP + r"\PAINT"] = []
        a = self.analyze("平直70X120-----1 噴白橡")
        c = a["children"][0]
        r = requirements(c, "coating")[0]
        self.assertEqual(r["status"], "pending_master")
        self.assertEqual(r["custom_paths"], [TOP + r"\PAINT"])
        self.assertFalse(r["candidates"])
        for wrong in (TOP + r"\FACE", r"SYN\NEW"):
            fixed = self.analyze(a["context"]["source"], customs={c["id"]: {r["id"]:
                dict(AUDIT, path=wrong, code="TMP:PAINT", codsc="白橡漆", compri="8")}})
            self.assertTrue(any(g["kind"] == "invalid_custom" for g in fixed["gaps"]))
        fixed = self.analyze(a["context"]["source"], customs={c["id"]: {r["id"]:
            dict(AUDIT, path=TOP + r"\PAINT", code="TMP:PAINT", codsc="白橡漆", compri="8")}},
            overrides={c["id"]: {TOP + r"\PAINT": dict(AUDIT, values={"stdqty": "1", "stdpar": "1"})}})
        self.assertTrue(fixed["children"][0]["ready"], fixed["gaps"])
        self.assertEqual(set(self.cat["nodes"]), set(fixed["context"]["catalog_snapshot"]["nodes"]))

    def test_unknown_retained_and_resolution_cannot_swallow_typed_demand(self):
        a = self.analyze("平直70X120-----1 火星加工/金星塗裝")
        c = a["children"][0]
        self.assertEqual([r["value"] for r in requirements(c, "unknown")], ["火星加工", "金星塗裝"])
        self.assertFalse(c["ready"])
        a = self.analyze("平直70X120-----1\n共用：孔靠單")
        c = a["children"][0]
        r = requirements(c, "external_confirmation")[0]
        fixed = self.analyze(a["context"]["source"], resolutions={c["id"]: {
            r["origin"]["fragment_id"]: dict(AUDIT, text="主檔配置")}})
        self.assertTrue(any(g["kind"] == "invalid_resolution" for g in fixed["gaps"]))
        self.assertFalse(fixed["children"][0]["ready"])

    def test_unconfirmed_dimensions_no_fake_cost_and_audited_metric_resolution(self):
        a = self.analyze("彎角3X3.25-----2")
        c = a["children"][0]
        r = requirements(c, "dimension")[0]
        self.assertEqual(r["status"], "pending_units")
        self.assertNotIn(TOP, c["selections"])
        board = next(i for i in c["items"] if i["path"] == TOP + r"\BOARD")
        self.assertIsNone(board["part_cost"])
        fixed = self.analyze(a["context"]["source"], resolutions={c["id"]: {
            r["origin"]["fragment_id"]: dict(AUDIT, text="彎角70X70公分")}})
        self.assertTrue(fixed["children"][0]["ready"], fixed["gaps"])
        self.assertIn("3X3.25", requirements(fixed["children"][0], "dimension")[0]["origin"]["original"])

    def test_v1_historical_frozen_validates_unchanged_and_dispatch_not_new_parser(self):
        document = deepcopy(V1_FROZEN)
        original = deepcopy(document)
        with patch.object(dynamic.normalization, "parse", side_effect=AssertionError("舊文件不可重跑新 parser")):
            self.assertEqual(dynamic.validate_document(document), document["child"])
            rebuilt = dynamic._rebuild(document["context"])
            self.assertEqual(dynamic.freeze(rebuilt, document["child"]["id"], **document["frozen_audit"]), document)
            retained = dynamic.reconcile_frozen(rebuilt, {document["child"]["id"]: document})
            self.assertEqual(len(retained["retained"]), 1)
        self.assertEqual(document, original)
        for field, value in (("product_qty", 999), ("ready", False)):
            bad = deepcopy(document)
            bad["child"][field] = value
            with self.assertRaises(ValueError):
                dynamic.validate_document(bad)
        bad = deepcopy(document)
        bad["version"] = "dynamic-2"
        with self.assertRaises(ValueError):
            dynamic.validate_document(bad)

    def test_v2_normalized_fields_tampering_and_revision_revoke(self):
        a = self.analyze("平直70X120-----1")
        c = a["children"][0]
        document = dynamic.freeze(a, c["id"], **AUDIT)
        self.assertEqual(dynamic.validate_document(document), c)
        for field, value in (("targets", [99]), ("quantity", "999"), ("part", "legs"), ("conflict", ["fake"])):
            bad = deepcopy(document)
            bad["child"]["normalized_requirements"][0][field] = value
            with self.assertRaises(ValueError):
                dynamic.validate_document(bad)
        changed = self.analyze("平直70X120-----1\n共用：噴胡")
        self.assertTrue(dynamic.reconcile_frozen(changed, {c["id"]: document})["revoked"])


if __name__ == "__main__":
    unittest.main()
