"""Standalone unittest：純資料與隔離記憶體 SQLite，不載入 repo/conftest。

./venv/bin/python -B -m unittest discover -s test -p test_local_business.py -v
合成主檔只供本檔測試；另外以現有唯讀 CMT1 四檔驗證完整合法配置。
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import json
import socket
import sys
import unittest
from unittest.mock import patch
from typing import Optional

from sqlalchemy import create_engine, event, text

from local_quote import business
from local_quote.business import MasterSource, analyze, generate, input_digest
from local_quote.catalog import build_catalog, number
from local_quote.backend import LocalDatabase, Settings
from local_quote.storage import BatchStore, FrozenPreview, _thaw
from test_local_storage import fixture_schema


SOURCE = "\n".join(("平直70X120-----2 面無孔", "平直70X150-----1", "平直70X120-----6", "平彎70X150-----2"))
ORIGINAL = ("(1)20990101-1 測試客戶\n彎角70X70削角-----2 面無孔有桌下走線\n"
            "平直70X120-----1 面1孔13.5X40單掀鋁\n平直70X140-----6 面1孔13.5X40單掀鋁\n"
            "平彎70X140-----2 面1孔13.5X40單掀鋁\n單面置物版\n孔靠單/格靠單\n817胡/噴胡\n腳粒鎖好/撕保護膜")


def fixture_tables(code="CMT1", wg="103"):
    """明示合成測試資料，價格僅為公式驗證用，不代表真庫。"""
    rows = {k: [] for k in ("ordstr", "ordspd", "ordspe", "ordqty")}
    labels = {}

    def node(tail, name, options=(), required="Y", usage=False):
        path = code + "\\" + tail
        parent, optno = path.rsplit("\\", 1)
        rows["ordstr"].append(dict(workgroup=wg, pathf=parent, pathc=path,
                                   optnof=parent.rsplit("\\", 1)[-1], optnoc=optno, must_chose=required, seq=1))
        labels[optno] = name
        for c, label, price in options:
            rows["ordspe"].append(dict(workgroup=wg, path=path, code=c, codsc=label, compri=price))
        if usage:
            for driver_code, amount in (("C010", 4.5), ("C011", 6.0)):
                rows["ordqty"].append(dict(workgroup=wg, path=path, code=driver_code,
                                           part_path=code + "\\A001", stdqty=amount, stdpar=24.0))

    node("A001", "測試桌面尺寸", [("C010", "70*120", 0), ("C011", "70*150", 0)])
    node("A001\\S010", "測試外型", [("C001", "平直", 10), ("C002", "平彎", 20)])
    node("A001\\S004", "測試立水", [("M1", "25mm", 700), ("M2", "30mm", 900)], usage=True)
    node("A001\\S005", "測試材料容器")
    node("A001\\S005\\S001", "測試美耐皿", [("M1", "817胡桃木", 300)], required="N", usage=True)
    node("A001\\S005\\S000", "測試美耐板", [("M1", "白色", 400)], required="N", usage=True)
    node("A001\\S005\\S007", "測試下板", [("M1", "下板25mm", 200)], usage=True)
    node("A001\\S015", "測試噴漆", [("C001", "胡桃漆", 30)])
    node("A001\\W001", "測試工費容器")
    node("A001\\W001\\W010", "測試裁切", [("W1", "美耐皿裁切", 100), ("W2", "美耐板裁切", 130)])
    node("A001\\S019", "測試選配", required="N")
    node("A001\\S019\\S020", "測試線盒", [("C001", "40*13.5銀鋁單掀", 120), ("C002", "40*13.5黑鋁單掀", 140)], required="N")
    node("A001\\S019\\S022", "測試桌下槽", [("C001", "L型", 100), ("C002", "雙層", 160)], required="N")
    node("A001\\S019\\S035", "測試方孔", [("C001", "小孔", 50), ("C002", "大孔", 70)], required="N")
    node("A001\\S019\\S040", "測試置物板", [("C001", "單邊置物板", 80), ("C002", "雙邊置物板", 120)], required="N")
    node("B001", "測試木腳", [("B1", "45寬", 0), ("B2", "55寬", 0)], required="N")
    node("B001\\S005", "測試材料容器")
    node("B001\\S005\\S001", "測試美耐皿", [("M1", "817胡桃木", 300)], required="N")
    node("B001\\S005\\S000", "測試美耐板", [("M1", "白色", 400)], required="N")
    node("B001\\S015", "測試噴漆", [("C001", "胡桃漆", 30)], required="N")
    rows["ordspd"] = [dict(workgroup=wg, optno=k, optdesc=v, kind="1", code="") for k, v in labels.items()]
    return rows


def catalog(code="CMT1", wg="103"):
    return build_catalog(wg, {"prodkind": code, "codsc": "合成測試類別", "quo_rate": 1.3}, fixture_tables(code, wg))


def choose_required(source, cat, initial=None):
    """測試模擬人逐輪選白名單；不是 runtime 自動挑第一筆。"""
    choices = dict(initial or {})
    for _ in range(12):
        result = analyze(source, cat, choices)
        eligible = [g for g in result["gaps"] if g["candidates"] and g["key"] not in choices]
        if not eligible:
            return choices, result
        for gap in eligible:
            candidates = gap["candidates"]
            preferred = [c for c in candidates if c["path"].endswith("\\S001")]
            choices[gap["key"]] = (preferred or candidates)[0]["value"]
    raise AssertionError("候選無法在有限輪內完成")


def existing_math():
    """只執行既有純核心 AST；剝除 config/repo imports，不複製計算公式。"""
    tree = ast.parse(Path("engine/calculator.py").read_text(encoding="utf-8"))
    wanted = {"QuoteItem", "CalcResult", "calculate_quote"}
    tree.body = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in wanted]

    class Pure(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return None

    tree = ast.fix_missing_locations(Pure().visit(tree))
    env = dict(__name__=__name__, dataclass=dataclass, field=field, Decimal=Decimal,
               ROUND_HALF_UP=ROUND_HALF_UP, Optional=Optional, number=number,
               MARKUP_RATE=0.3, TAX_RATE=0.05, MAX_DISCOUNT_RATE=0.1)
    exec(compile(tree, "engine/calculator.py", "exec"), env)
    return env


class PureBusinessTests(unittest.TestCase):
    def setUp(self):
        self.no_network = patch.object(socket.socket, "connect", side_effect=AssertionError("禁止網路"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.cat = catalog()

    def test_no_category_no_analysis(self):
        for value in (None, {}, "CMT1"):
            with self.assertRaises(ValueError):
                analyze(SOURCE, value)

    def test_full_four_success_freezes_detached_preview(self):
        choices, analysis = choose_required(SOURCE, self.cat)
        self.assertFalse(analysis["gaps"])
        result = generate(SOURCE, self.cat, choices)
        self.assertIsInstance(result["frozen"], FrozenPreview)
        frozen = _thaw(result["frozen"])
        self.assertEqual(len(frozen["children"]), 4)
        self.assertEqual(frozen["input_digest"], input_digest(SOURCE, self.cat, choices))
        self.assertEqual(Decimal(frozen["batch_total"]), sum(Decimal(str(c["calc"]["total_price"])) for c in result["children"]))
        old_payload = result["frozen"].payload
        result["children"][0]["calc"]["items"][0]["compri"] = 123456
        self.cat["options"][r"CMT1\A001\S004"][0]["compri"] = 9999
        self.assertEqual(old_payload, result["frozen"].payload)

    def test_analyze_never_calculates_or_freezes(self):
        with patch.object(business, "_calculate", side_effect=AssertionError), patch.object(business, "freeze_preview", side_effect=AssertionError):
            choices, result = choose_required(SOURCE, self.cat)
            self.assertFalse(result["gaps"])
            self.assertTrue(choices)

    def test_different_dimensions_use_own_driver_rule(self):
        choices, _ = choose_required(SOURCE, self.cat)
        result = generate(SOURCE, self.cat, choices)
        for child, driver_code, usage in zip(result["children"], ("C010", "C011", "C010", "C011"), (4.5, 6, 4.5, 6)):
            row = next(i for i in child["calc"]["items"] if i["path"] == r"CMT1\A001\S004")
            self.assertEqual((row["driver_path"], row["driver_code"], row["stdqty"]), (r"CMT1\A001", driver_code, usage))
            self.assertEqual(row["part_cost"], child["product_qty"] * usage / 24 * 700)

    def test_existing_calculator_exact_match(self):
        core = existing_math()
        choices, _ = choose_required(SOURCE, self.cat)
        result = generate(SOURCE, self.cat, choices)
        for child in result["children"]:
            fields = core["QuoteItem"].__dataclass_fields__
            items = [core["QuoteItem"](**{k: row[k] for k in fields}) for row in child["calc"]["items"]]
            expected = asdict(core["calculate_quote"](items, discount_rate=0,
                                                      markup_rate=self.cat["category"]["quo_rate"] - 1, tax_rate=0.05))
            for k, v in expected.items():
                if k == "items":
                    for actual, old in zip(child["calc"]["items"], v):
                        self.assertEqual({key: actual[key] for key in old}, old)
                else:
                    self.assertEqual(child["calc"][k], v, k)

    def test_existing_policy_exact_match_without_import(self):
        tree = ast.parse(Path("engine/pricing.py").read_text(encoding="utf-8"))
        values = [ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "PER_ITEM_OPTIONS" for t in n.targets)]
        self.assertEqual(business.PER_ITEM_OPTIONS, values[0])

    def test_original_is_not_reportable_and_no_holes_first(self):
        choices, a = choose_required(ORIGINAL, self.cat)
        self.assertTrue(a["gaps"])
        first = a["children"][0]
        self.assertFalse(any(p.endswith(("\\S025", "\\S030", "\\S035")) for p in first["selections"]))
        missing_sizes = [g for g in a["gaps"] if "桌面尺寸" in g["label"] and not g["candidates"]]
        self.assertEqual(len(missing_sizes), 3)
        self.assertTrue(any("削角" in g["label"] for g in a["gaps"]))
        self.assertTrue(any("孔靠單" in g["label"] for g in a["gaps"]))
        result = generate(ORIGINAL, self.cat, choices)
        self.assertIsNone(result["frozen"])
        self.assertEqual(len(result["children"]), 4)
        self.assertTrue(all("error" in c for c in result["children"]))

    def test_missing_size_not_replaceable_with_other_candidate(self):
        source = SOURCE.replace("70X120", "70X140", 1)
        a = analyze(source, self.cat)
        gap = next(g for g in a["gaps"] if "桌面尺寸：70X140" in g["label"])
        self.assertEqual(gap["candidates"], [])
        a = analyze(source, self.cat, {gap["key"]: {"path": r"CMT1\A001", "code": "C010"}})
        self.assertTrue(any("白名單" in g["label"] for g in a["gaps"]))

    def test_choices_identity_no_price_quantity_code_or_path_tampering(self):
        gap = next(g for g in analyze(SOURCE, self.cat)["gaps"] if g["candidates"])
        identity = json.loads(gap["candidates"][0]["value"])
        for bad in (dict(identity, compri=1), dict(identity, line_qty=2), dict(identity, stdqty=1),
                    dict(identity, path=r"CMT1\B001"), dict(identity, code="NOPE"), "M1"):
            a = analyze(SOURCE, self.cat, {gap["key"]: bad})
            self.assertTrue(any(g["key"] == gap["key"] and "白名單" in g["label"] for g in a["gaps"]))
        a = analyze(SOURCE, self.cat, {gap["key"]: identity})
        self.assertFalse(any(g["key"] == gap["key"] for g in a["gaps"]))

    def test_stale_extra_and_inactive_choices_block(self):
        choices, _ = choose_required(SOURCE, self.cat)
        for source, selected in ((SOURCE + "\n", choices), (SOURCE, dict(choices, extra="bad"))):
            result = generate(source, self.cat, selected)
            self.assertIsNone(result["frozen"])
            self.assertTrue(result["analysis"]["global_gaps"])

    def test_digest_full_source_workgroup_category_and_choices(self):
        before = input_digest(SOURCE, self.cat)
        other_wg = dict(self.cat, workgroup="104")
        other_rate = deepcopy(self.cat)
        other_rate["category"]["quo_rate"] = 1.4
        for source, cat, choices in ((SOURCE + "\n", self.cat, {}), (SOURCE, other_wg, {}),
                                     (SOURCE, catalog("DT1"), {}), (SOURCE, other_rate, {}),
                                     (SOURCE, self.cat, {"x": "y"})):
            self.assertNotEqual(before, input_digest(source, cat, choices))
        self.assertEqual(input_digest(SOURCE, "CMT1"), input_digest(SOURCE, "CMT1", {}))

    def test_unknown_common_and_item_fragments_block_all_coverage(self):
        for text_value in (SOURCE + "\n特殊未知工序", SOURCE.replace("面無孔", "面無孔任意文字")):
            choices, result = choose_required(text_value, self.cat)
            self.assertTrue(any("未理解原文" in g["label"] for g in result["gaps"]))
            self.assertIsNone(generate(text_value, self.cat, choices)["frozen"])

    def test_hole_box_separate_and_color_requires_choice(self):
        source = SOURCE.replace("面無孔", "面2孔13.5X40單掀鋁")
        a = analyze(source, self.cat)
        child = a["children"][0]
        hole = next(g for g in child["gaps"] if "面孔數" in g["label"])
        box = next(g for g in child["gaps"] if "線盒" in g["label"])
        self.assertFalse(hole["candidates"])
        self.assertEqual(len(box["candidates"]), 2)
        chosen = {box["key"]: box["candidates"][0]["value"]}
        a = analyze(source, self.cat, chosen)
        self.assertEqual(a["children"][0]["selections"][r"CMT1\A001\S019\S020"]["line_qty"], 2)
        self.assertEqual(a["children"][0]["product_qty"], 2)

    def test_no_hole_blocks_explicit_hole_and_positive_request(self):
        for suffix in (r" 規格[CMT1\A001\S019\S035]=C001", " 面1孔"):
            source = SOURCE.replace("面無孔", "面無孔" + suffix)
            a = analyze(source, self.cat)
            self.assertTrue(a["children"][0]["gaps"])
            self.assertNotIn(r"CMT1\A001\S019\S035", a["children"][0]["selections"])

    def test_branch_color_needs_path_and_legs_not_defaulted(self):
        a = analyze(SOURCE + "\n817胡/噴胡", self.cat)
        first = a["children"][0]
        gap = next(g for g in first["gaps"] if "817胡：" in g["label"])
        self.assertEqual({c["path"].split("\\")[1] for c in gap["candidates"]}, {"A001", "B001"})
        self.assertNotIn(r"CMT1\B001", first["selections"])
        selected = next(c for c in gap["candidates"] if "\\B001\\" in c["path"])
        a = analyze(SOURCE + "\n817胡/噴胡", self.cat, {gap["key"]: selected["value"]})
        self.assertTrue(any("木腳" in g["label"] for g in a["children"][0]["gaps"]))

    def test_material_exclusive_and_optional_tree_activation(self):
        source = SOURCE + "\n" + r"規格[CMT1\A001\S005\S001]=M1 規格[CMT1\A001\S005\S000]=M1"
        a = analyze(source, self.cat)
        self.assertTrue(any("互斥" in g["label"] for g in a["gaps"]))
        self.assertEqual(len(a["gaps"]), len({g["key"] for g in a["gaps"]}))
        self.assertEqual(len(a["questions"]), len({g["key"] for g in a["questions"]}))
        a = analyze(SOURCE, self.cat)
        self.assertFalse(any("\\B001" in p for p in a["children"][0]["active_paths"]))

    def test_only_four_or_reasons_never_partial_freeze(self):
        for source in ("\n".join(SOURCE.splitlines()[:3]), SOURCE + "\n平直70X120-----1", ""):
            r = generate(source, self.cat)
            self.assertEqual(len(r["children"]), 4)
            self.assertTrue(all("error" in c for c in r["children"]))
            self.assertIsNone(r["frozen"])

    def test_ambiguous_shared_scope_or_empty_description_cannot_pass(self):
        for source in (SOURCE.replace("\n", "\n木腳\n", 1), SOURCE.replace("平彎70X150", "")):
            choices, _ = choose_required(source, self.cat)
            result = generate(source, self.cat, choices)
            self.assertTrue(result["analysis"]["global_gaps"])
            self.assertIsNone(result["frozen"])

    def test_empty_prices_nonfinite_and_bad_rules_never_zero(self):
        for value in (None, "", float("nan"), float("inf"), -1, True):
            cat = deepcopy(self.cat)
            for option in cat["options"][r"CMT1\A001\S004"]:
                option["compri"] = value
            choices, _ = choose_required(SOURCE, cat)
            self.assertIsNone(generate(SOURCE, cat, choices)["frozen"])
        choices, _ = choose_required(SOURCE, self.cat)
        for value in (None, "", 0, float("nan"), -1, "1e-400"):
            cat = deepcopy(self.cat)
            cat["rules"][r"CMT1\A001\S004"][0]["stdpar"] = value
            result = generate(SOURCE, cat, choices)
            self.assertIn("error", result["children"][0])
            self.assertIn("calc", result["children"][1])
            self.assertIsNone(result["frozen"])

    def test_missing_rule_no_one_fallback_even_for_policy_paths(self):
        choices, _ = choose_required(SOURCE, self.cat)
        cat = deepcopy(self.cat)
        cat["rules"][r"CMT1\A001\S010"] = [dict(part_path=r"CMT1\A001", code="C011", stdqty=1, stdpar=1)]
        result = generate(SOURCE, cat, choices)
        self.assertIn("唯一用量規則", result["children"][0]["error"])
        self.assertIn("calc", result["children"][1])
        cat = deepcopy(self.cat)
        cat["rules"][r"CMT1\A001\S004"] = []
        self.assertIn("不能默認", generate(SOURCE, cat, choices)["children"][0]["error"])

    def test_zero_container_name_from_master_and_zero_usage_valid(self):
        choices, _ = choose_required(SOURCE, self.cat)
        self.cat["rules"][r"CMT1\A001\S004"][0]["stdqty"] = 0
        result = generate(SOURCE, self.cat, choices)
        self.assertIsNotNone(result["frozen"])
        items = {i["path"]: i for i in result["children"][0]["calc"]["items"]}
        self.assertEqual(items[r"CMT1\A001\S005"]["spdsc"], "測試材料容器")
        self.assertEqual(items[r"CMT1\A001\S005"]["compri"], 0)
        self.assertEqual(items[r"CMT1\A001\S004"]["part_cost"], 0)

    def test_other_category_not_cmt_semantics_or_per_item(self):
        cat = catalog("DT1")
        self.assertTrue(any("此類別" in g["label"] for g in analyze(SOURCE, cat)["gaps"]))
        source = "\n".join(["主檔配置-----1"] * 4)
        choices, a = choose_required(source, cat)
        self.assertFalse(a["gaps"])
        r = generate(source, cat, choices)
        self.assertIsNone(r["frozen"])
        self.assertTrue(all("正價缺少用量規則" in c["error"] for c in r["children"]))
        for path, options in cat["options"].items():
            if any(o["compri"] > 0 for o in options) and not cat["rules"][path]:
                cat["rules"][path] = [dict(part_path=r"DT1\A001", code=c, stdqty=1, stdpar=1) for c in ("C010", "C011")]
        self.assertIsInstance(generate(source, cat, choices)["frozen"], FrozenPreview)

    def test_imports_no_config_repository_or_model(self):
        import subprocess
        code = ("import local_quote.business,sys; "
                "forbidden=('config','database','acceptance','app','openai','agent.core'); "
                "assert not any(n==f or n.startswith(f+'.') for n in sys.modules for f in forbidden)")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rate_and_numerator_validation_and_driver_mismatch(self):
        for value in (None, "", float("nan"), float("inf"), 0, -1):
            cat = deepcopy(self.cat)
            cat["category"]["quo_rate"] = value
            # nonfinite input metadata itself must not acquire a misleading digest.
            if isinstance(value, float):
                with self.assertRaises(ValueError):
                    generate(SOURCE, cat)
            else:
                choices, _ = choose_required(SOURCE, cat)
                self.assertIsNone(generate(SOURCE, cat, choices)["frozen"])
        choices, _ = choose_required(SOURCE, self.cat)
        for bad in (dict(part_path=r"DT1\A001"), dict(part_path=None), dict(stdqty=None),
                    dict(stdqty=-1), dict(stdqty=float("inf"))):
            cat = deepcopy(self.cat)
            cat["rules"][r"CMT1\A001\S004"][0].update(bad)
            self.assertIsNone(generate(SOURCE, cat, choices)["frozen"])

    def test_unspecified_holes_quantities_calculate_without_changing_product_qty(self):
        source = SOURCE.replace("面無孔", "面2孔")
        choices, a = choose_required(source, self.cat)
        self.assertFalse(a["gaps"])
        result = generate(source, self.cat, choices)
        self.assertIsInstance(result["frozen"], FrozenPreview)
        row = next(i for i in result["children"][0]["calc"]["items"] if i["optno"] == "S035")
        self.assertEqual((row["product_qty"], row["line_qty"], row["qty"]), (2, 2, 4))
        self.assertEqual(row["part_cost"], 200)

    def test_stale_descendant_choice_after_material_switch_blocks(self):
        cat = deepcopy(self.cat)
        parent = r"CMT1\A001\S005\S000"
        path = parent + r"\S006"
        cat["nodes"][path] = dict(pathf=parent, pathc=path, optnoc="S006", must_chose="Y")
        cat["labels"]["S006"] = "上板測試"
        cat["options"][path] = [dict(path=path, code=c, codsc=c, compri=0) for c in ("T1", "T2")]
        cat["rules"][path] = []
        a = analyze(SOURCE, cat)
        gap = next(g for g in a["children"][0]["gaps"] if "請選材料方案" in g["label"])
        material = next(c for c in gap["candidates"] if c["path"] == parent)
        choices, _ = choose_required(SOURCE, cat, {gap["key"]: material["value"]})
        choices[gap["key"]] = next(c["value"] for c in gap["candidates"] if c["path"].endswith("\\S001"))
        result = generate(SOURCE, cat, choices)
        self.assertIsNone(result["frozen"])
        self.assertTrue(result["analysis"]["global_gaps"])


class MasterSourceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        self.metadata = fixture_schema(self.engine)
        self.db = LocalDatabase(Settings("mysql+pymysql://fixture:fixture@localhost/isolated", "103"),
                                engine=self.engine, testing=True)
        with self.engine.begin() as conn:
            for code, wg in (("CMT1", "103"), ("DT1", "103"), ("MT", "104")):
                conn.execute(self.metadata.tables["invdoc"].insert(), dict(workgroup=wg, prodkind=code,
                             codsc="測試 " + code, ordkind="1", quo_rate=1))
                for name, rows in fixture_tables(code, wg).items():
                    for row in rows:
                        # 共用選項定義在同工作群只插入一次。
                        if name == "ordspd" and code == "DT1":
                            continue
                        conn.execute(self.metadata.tables[name].insert(), row)
            conn.execute(self.metadata.tables["invdoc"].insert(), dict(workgroup="103", prodkind="NOT_PRODUCT", codsc="非產品", ordkind="2", quo_rate=1))

    def test_categories_database_values_no_hardcode(self):
        cats = MasterSource(self.db).categories()
        self.assertEqual(cats, [{"prodkind": "CMT1", "codsc": "測試 CMT1", "quo_rate": 1.0},
                                {"prodkind": "DT1", "codsc": "測試 DT1", "quo_rate": 1.0}])

    def test_single_read_transaction_five_selects_and_prefix_safe(self):
        queries, reads = [], []
        original_read = self.db.read

        @contextmanager
        def tracked():
            reads.append(True)
            with original_read() as conn:
                def capture(c, cursor, statement, parameters, context, many):
                    queries.append(statement)
                event.listen(conn, "before_cursor_execute", capture)
                try:
                    yield conn
                finally:
                    event.remove(conn, "before_cursor_execute", capture)

        with patch.object(self.db, "read", tracked):
            cat = MasterSource(self.db).catalog("DT1")
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(queries), 5)
        self.assertTrue(all(q.startswith("SELECT ") and "workgroup=" in q and "LIKE" not in q for q in queries))
        self.assertTrue(all(p.startswith("DT1\\") for p in cat["nodes"]))
        self.assertEqual(cat["category"]["codsc"], "測試 DT1")

    def test_invalid_category_or_orphan_tree_fails_closed(self):
        for code in ("MT", "NOT_PRODUCT", "CMT1%' OR 1=1", None):
            with self.assertRaises(ValueError):
                MasterSource(self.db).catalog(code)
        rows = fixture_tables()
        rows["ordstr"][0]["pathf"] = "OTHER"
        with self.assertRaises(ValueError):
            build_catalog("103", catalog()["category"], rows)

    def test_fixed_preview_is_saveable_only_in_isolated_sqlite_no_repricing(self):
        cat = MasterSource(self.db).catalog("CMT1")
        choices, _ = choose_required(SOURCE, cat)
        result = generate(SOURCE, cat, choices)
        self.assertIsInstance(result["frozen"], FrozenPreview)
        with patch.object(business, "_calculate", side_effect=AssertionError("不可重算")), \
                patch.object(MasterSource, "catalog", side_effect=AssertionError("不可查價")):
            saved = BatchStore(self.db).save(result["frozen"], confirmed=True,
                                           input_digest=result["analysis"]["input_digest"])
        self.assertEqual(len(saved["ref_nos"]), 4)
        with self.db.read() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM quote_snapshot_document")).scalar_one(), 4)


class ReadonlyFixtureTests(unittest.TestCase):
    def test_original_against_readonly_fixture_still_blocked(self):
        from cmt1_fixture import load_cmt1_fixture
        cat = build_catalog("103", {"prodkind": "CMT1", "codsc": "唯讀測試類別", "quo_rate": 1}, load_cmt1_fixture())
        choices, a = choose_required(ORIGINAL, cat)
        self.assertEqual(sum("桌面尺寸" in g["label"] and not g["candidates"] for g in a["gaps"]), 3)
        first = a["children"][0]
        self.assertFalse(any("孔" in s["optdesc"] for s in first["selections"].values()))
        second = a["children"][1]
        self.assertEqual(second["selections"][r"CMT1\A001"]["code"], "C010")
        self.assertIsNone(generate(ORIGINAL, cat, choices)["frozen"])

    def test_existing_readonly_master_four_legal_sources(self):
        from cmt1_fixture import load_cmt1_fixture
        rows = load_cmt1_fixture()
        cat = build_catalog("103", {"prodkind": "CMT1", "codsc": "唯讀測試類別", "quo_rate": 1}, rows)
        source = "\n".join(["平直60X180-----2 面無孔", "平直70X120-----1", "平直70X150-----3", "平彎90X180-----1"])
        choices, analysis = choose_required(source, cat)
        self.assertFalse(bool(analysis["gaps"]), "真實唯讀主檔應可完成合法四張候選")
        result = generate(source, cat, choices)
        self.assertTrue(isinstance(result["frozen"], FrozenPreview), "真實主檔合法四张必須可固定")
        core = existing_math()
        for child in result["children"]:
            fields = core["QuoteItem"].__dataclass_fields__
            items = [core["QuoteItem"](**{k: row[k] for k in fields}) for row in child["calc"]["items"]]
            expected = core["calculate_quote"](items, discount_rate=0, markup_rate=0, tax_rate=0.05)
            self.assertEqual((child["calc"]["total_cost"], child["calc"]["total_price"]),
                             (expected.total_cost, expected.total_price))
            for item in child["calc"]["items"]:
                if item["source"] == "ordqty":
                    matches = [r for r in cat["rules"][item["path"]] if r["code"] == item["driver_code"]]
                    self.assertEqual(len(matches), 1)
                    self.assertEqual((item["stdqty"], item["stdpar"]), (matches[0]["stdqty"], matches[0]["stdpar"]))


if __name__ == "__main__":
    unittest.main()
