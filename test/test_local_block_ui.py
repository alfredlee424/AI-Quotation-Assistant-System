"""六份獨立共用來源的真 AppTest；只用合成主檔及隔離 SQLite。"""
from copy import deepcopy
from dataclasses import replace
import json
import socket
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool
from streamlit.testing.v1 import AppTest

from local_quote import dynamic, dynamic_ui as ui
from local_quote.backend import LocalDatabase
from local_quote.dynamic_presentation import semantic_rows, FIELDS, report_rows
from local_quote.dynamic_storage import DynamicStore, prepare_document
from local_quote.storage import BatchStore, StorageError
from local_block_semantics_fixtures import CASES, V1_FROZEN
from test_local_block_semantics import master, costs, missing
from test_local_storage import SETTINGS, fixture_schema


class SemanticMaster:
    def __init__(self):
        self.cat, self.calls = master(), []

    def categories(self):
        return [deepcopy(self.cat["category"])]

    def catalog(self, code):
        assert code == "SYN", "UI 必須傳入選到的合成類別，不能硬編 CMT1"
        self.calls.append(code)
        return deepcopy(self.cat)


def semantic_tables(app):
    return [d.value.to_dict("records") for d in app.dataframe if "群組內適用品項" in d.value.columns]


class BlockUITests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("禁止網路"))
        guard.start()
        self.addCleanup(guard.stop)
        self.engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False})
        fixture_schema(self.engine)
        self.addCleanup(self.engine.dispose)
        db = LocalDatabase(replace(SETTINGS, workgroup="T"), engine=self.engine, testing=True)
        self.master, self.store, self.legacy = SemanticMaster(), DynamicStore(db), BatchStore(db)
        self.sql = []
        event.listen(self.engine, "before_cursor_execute", lambda c, u, s, p, x, m: self.sql.append(s))
        services = patch.object(ui, "services", return_value=(self.master, self.store, self.legacy))
        services.start()
        self.addCleanup(services.stop)
        self.app = AppTest.from_string("from local_quote.dynamic_ui import main\nmain()", default_timeout=30).run()
        self.clean()

    def clean(self):
        self.assertFalse(self.app.exception, str(self.app.exception))

    @property
    def ws(self):
        return self.app.session_state.dq_workspace

    def click(self, key):
        self.app.button(key=key).click().run()
        self.clean()

    def fill(self, source):
        self.app.selectbox(key="dq_category").select("SYN")
        self.app.text_input(key="dq_actor").input("tester")
        self.app.text_input(key="dq_reason").input("獨立合成來源核對")
        self.app.run()
        self.app.text_area(key="dq_source").input(source).run()
        self.click("dq_analyze")
        return self.ws.analysis

    def pair(self, group, quantities):
        analyses, tables = [], []
        for style in (1, 2):
            with self.subTest(case=f"{group}-{style}"):
                self.click("dq_new")
                a = self.fill(CASES[f"{group}-{style}"])
                analyses.append(a)
                shown = semantic_tables(self.app)
                tables.append(shown)
                self.assertEqual(len(shown), 4)
                self.assertEqual(len(self.app.tabs), 4)
                self.assertFalse(a["global_issues"])
                self.assertEqual([c["product_qty"] for c in a["children"]], quantities)
                self.assertEqual([t.label for t in self.app.tabs],
                                 [f"第 {i} 張（{q}件）" for i, q in enumerate(quantities, 1)])
                for child, rows in zip(a["children"], shown):
                    self.assertEqual(rows, semantic_rows(child))
                    self.assertFalse(child["ready"])
                    self.assertTrue(self.app.button(key="dq_freeze:" + child["id"]).disabled)
                    self.assertTrue(any(r["範圍"] == "共用" and r["群組內適用品項"] == "1、2、3、4" for r in rows))
                    self.assertTrue(all(r["來源原文"] == CASES[f"{group}-{style}"].splitlines()[int(r["來源行"]) - 1]
                                        for r in rows))
                    for req in child["requirements"]:
                        if req["kind"] not in {"units", "external_confirmation"}:
                            continue
                        prefix = f"dq_custom:{child['id']}:{req['id']}"
                        self.assertFalse(any((w.key or "").startswith(prefix) for w in self.app.text_input))
                        if req["kind"] == "external_confirmation":
                            resolution = f"dq_resolution:{child['id']}:{req['source']['fragment_id']}:text"
                            self.assertFalse(any(w.key == resolution for w in self.app.text_area))
                self.assertFalse(any("需要建立客製化新件" in e.label for e in self.app.expander))
                self.assertFalse(any(m.label == "本張報價（含稅）" for m in self.app.metric))
                self.assertEqual([m.value for m in self.app.metric if m.label == "已知成本小計（非完整合計）"],
                                 [c["calc"]["known_cost"] for c in a["children"]])
                self.assertFalse(self.ws.frozen)
                self.assertFalse(any(s.startswith("INSERT") for s in self.sql))
        # 比較實際 UI 表格中非來源／識別欄，不以核心 equality 自我證明 UI。
        keys = ("範圍", "群組內適用品項", "部位", "需求類型", "屬性", "需求", "每件量", "數量基準",
                "可算本張總量", "是否適用", "狀態", "候選", "可掛接部位", "已選規格")
        project = lambda views: [[tuple(r[k] for k in keys) for r in rows] for rows in views]
        self.assertEqual(project(tables[0]), project(tables[1]))
        self.assertEqual(costs(analyses[0]), costs(analyses[1]))
        self.assertEqual(missing(analyses[0]), missing(analyses[1]))
        self.assertEqual(self.master.calls, ["SYN", "SYN"])
        return tables[0]

    def test_pair_one_scope_no_hole_known_cost(self):
        tables = self.pair(1, [2, 1, 6, 2])
        first = tables[0]
        hole = next(r for r in first if r["部位"] == "桌面孔" and r["需求類型"] == "外部確認")
        self.assertEqual((hole["狀態"], hole["是否適用"], hole["可算本張總量"]), ("不適用", "否", ""))
        self.assertTrue(any(r["部位"] == "隔板" and r["狀態"] == "待圖面／訂單核對" for r in first))
        self.assertEqual([m.value for m in self.app.metric], ["536.00", "302.00", "1812.00", "604.00"])
        self.assertEqual([next(r["可算本張總量"] for r in rows if r["需求類型"] == "挖孔") for rows in tables[1:]],
                         ["1", "6", "2"])

    def test_pair_two_role_targets_units_and_distinct_parts(self):
        tables = self.pair(2, [2, 1, 2, 2])
        for index, rows in enumerate(tables):
            targeted = next(r for r in rows if r["部位"] == "隔板孔")
            self.assertEqual(targeted["範圍"], "特定")
            self.assertEqual(targeted["群組內適用品項"], "1、2" if index < 2 else "3、4")
            self.assertEqual(targeted["狀態"], "待圖面／訂單核對" if index < 2 else "已匹配")
            self.assertTrue(any(r["狀態"] == "待單位" and r["可算本張總量"] == "" for r in rows))
            self.assertEqual({r["部位"] for r in rows if r["需求類型"] == "面材"}, {"桌面", "桌腳"})
            self.assertEqual(next(r["每件量"] for r in rows if r["需求類型"] == "線盒"), "1")
            self.assertEqual(next(r["每件量"] for r in rows if r["需求類型"] == "挖孔"), "1")
        self.assertTrue(any("產品角色：主席／外型：平直" == t.value for t in self.app.text))
        self.assertTrue(any("產品角色：列席／外型：平彎" == t.value for t in self.app.text))

    def test_pair_three_hole_totals_not_forbidden_leg_counts(self):
        tables = self.pair(3, [2, 1, 4, 2])
        holes = [next(r for r in rows if r["需求類型"] == "挖孔") for rows in tables]
        self.assertEqual([r["每件量"] for r in holes], ["1", "1", "2", "2"])
        self.assertEqual([r["可算本張總量"] for r in holes], ["2", "1", "8", "4"])
        for rows in tables:
            ban = next(r for r in rows if r["部位"] == "桌腳走線孔")
            self.assertEqual((ban["每件量"], ban["可算本張總量"]), ("2", ""))
            self.assertIn("非付費孔數", ban["數量說明"])
            self.assertTrue(any(r["解析片語"] == "照圖挖孔" and r["狀態"] == "待圖面／訂單核對" for r in rows))

    def test_complete_block_freeze_save_history_uses_persisted_semantics(self):
        # 獨立正例，不是把六例的靠單／圖面刪掉來假稱原句通過。
        source = "工單：完整區塊\n平直70X120-----2\n平彎70X140-----1\n共用：\n817胡\n\n噴胡\n腳2片不挖走線孔"
        a = self.fill(source)
        self.assertTrue(all(c["ready"] for c in a["children"]), a["gaps"])
        before = semantic_tables(self.app)
        for child in a["children"]:
            cid = child["id"]
            self.click("dq_freeze:" + cid)
            frozen = self.ws.frozen[cid]
            self.app.checkbox(key="dq_confirm:" + frozen.confirmation_id).check().run()
            self.click("dq_save:" + cid)
            receipt = self.store.read(frozen.confirmation_id)
            self.assertEqual(receipt["document"], json.loads(frozen.payload))
            self.assertEqual(receipt["document"]["child"]["normalized_requirements"], child["normalized_requirements"])
            self.assertEqual(tuple(report_rows(child, self.master.cat, receipt=receipt)[0]), FIELDS)
        self.assertEqual(semantic_tables(self.app), before)
        saved = list(self.ws.saved.values())
        self.click("dq_new")
        with patch.object(self.master, "catalog", side_effect=AssertionError("歷史不得查主檔")), \
                patch.object(self.master, "categories", side_effect=AssertionError("歷史不得查類別")):
            self.click("dq_history:new:list")
            for receipt, expected in zip(saved, before):
                self.app.selectbox(key="dq_history:new:selected").select(receipt["confirmation_id"]).run()
                self.click("dq_history:new:read")
                self.assertEqual(semantic_tables(self.app), [expected])

    def test_units_full_fragment_restatement_only_one_child(self):
        a = self.fill("彎角3X3.25-----2\n彎角3X3.25-----1")
        child = a["children"][0]
        req = next(r for r in child["requirements"] if r["kind"] == "units")
        prefix = f"dq_resolution:{child['id']}:{req['source']['fragment_id']}"
        self.app.text_area(key=prefix + ":text").input("彎角70X70公分")
        self.app.checkbox(key=prefix + ":scope").check()
        self.click("FormSubmitter:" + prefix + "-套用重述及作用範圍")
        first, second = self.ws.analysis["children"]
        self.assertTrue(first["ready"], first["issues"])
        self.assertFalse(second["ready"])
        self.assertNotIn("待單位", [r["狀態"] for r in semantic_tables(self.app)[0]])
        self.assertIn("待單位", [r["狀態"] for r in semantic_tables(self.app)[1]])
        self.assertTrue(any("3X3.25" in r["來源原文"] for r in semantic_tables(self.app)[0]))

    def test_scope_boundary_help_and_global_guidance(self):
        self.assertIn("共用：\n", self.app.text_area(key="dq_source").proto.placeholder)
        self.assertIn("空行不結束", self.app.text_area(key="dq_source").proto.help)
        a = self.fill("平直70X120-----1\n共用：\n817胡\n平直70X120-----1\n噴胡")
        self.assertTrue(a["global_issues"])
        self.assertTrue(any("多行區塊" in w.value and "空行不結束" in w.value for w in self.app.warning))
        self.assertTrue(all(self.app.button(key="dq_freeze:" + c["id"]).disabled for c in a["children"]))

    def test_v1_literal_show_and_legal_v1_store_history_without_new_parser(self):
        original = deepcopy(V1_FROZEN)
        self.assertGreater(len(original["frozen_audit"]["actor"]), 8)
        with self.assertRaises(StorageError):
            prepare_document(original)  # 不截短原 fixture，不放寬 schema。
        script = ("import streamlit as st\nfrom local_quote.dynamic_ui import _show\n"
                  "d = st.session_state.document\n"
                  "_show(d['child'], d['context']['catalog_snapshot'], key='v1')")
        app = AppTest.from_string(script, default_timeout=30)
        app.session_state.document = original
        app.run()
        self.assertFalse(app.exception)
        self.assertFalse(semantic_tables(app))
        self.assertTrue(any("舊版歷史未記錄" in c.value for c in app.caption))
        db = LocalDatabase(SETTINGS, engine=self.engine, testing=True)
        store = DynamicStore(db)
        with patch.object(dynamic.normalization, "parse", side_effect=AssertionError("v1 不重跑新版解析")):
            analysis = dynamic._rebuild(original["context"])
            audit = dict(original["frozen_audit"], actor="tester")
            legal = dynamic.freeze(analysis, original["child"]["id"], **audit)
            self.assertEqual(legal["version"], "dynamic-1")
            self.assertEqual(legal["child"], original["child"])
            frozen = prepare_document(legal)
            receipt = store.save(frozen, confirmed=True, current=analysis,
                                 trusted_catalog_digest=analysis["catalog_digest"])
            self.assertEqual(store.read(frozen.confirmation_id), receipt)
            with patch.object(ui, "services", return_value=(self.master, store, self.legacy)):
                self.click("dq_history:new:list")
                self.app.selectbox(key="dq_history:new:selected").select(frozen.confirmation_id).run()
                self.click("dq_history:new:read")
                self.assertFalse(semantic_tables(self.app))
                self.assertTrue(any("舊版歷史未記錄" in c.value for c in self.app.caption))
        self.assertEqual(V1_FROZEN, original)


if __name__ == "__main__":
    unittest.main()
