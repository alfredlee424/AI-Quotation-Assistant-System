"""純報表測試：不讀主檔／設定、不使用真實來源。"""
from copy import deepcopy
import csv
import io
import unittest

from local_quote.dynamic import analyze, freeze
from local_quote.dynamic_presentation import FIELDS, report_rows, safe_cell, screen_rows, semantic_rows, tsv
from test_local_dynamic import fixture, SOURCE, BOARD, ROOT


class PresentationTests(unittest.TestCase):
    def test_exact_thirteen_columns_missing_and_zero_no_fake_ref(self):
        cat = fixture()
        cat["options"][BOARD][0]["compri"] = None
        child = analyze(SOURCE, cat)["children"][0]
        rows = report_rows(child, cat)
        parsed = list(csv.reader(io.StringIO(tsv(rows)), delimiter="\t"))
        self.assertEqual(tuple(parsed[0]), FIELDS)
        self.assertTrue(all(len(r) == 13 for r in parsed))
        by_path = {r["path"]: r for r in rows}
        self.assertEqual(by_path[ROOT]["compri"], "0")
        self.assertIsNone(by_path[BOARD]["compri"])
        self.assertEqual(by_path[BOARD]["qty"], "2")
        self.assertTrue(all(r["ref_no"] == r["usrno"] == r["abndat"] == r["abntim"] == "" for r in rows))
        self.assertEqual(parsed[-1][8], "")

    def test_saved_projection_uses_persisted_identity_actor_and_time(self):
        cat = fixture()
        analysis = analyze(SOURCE, cat)
        child = analysis["children"][0]
        doc = freeze(analysis, child["id"], actor="saved", reason="已核對", time="2026-10-14T01:00:00+08:00")
        receipt = dict(document=doc, ref_no="DQ20261014020304_019", confirmed_at="2026-10-14T02:03:04+08:00")
        fake = deepcopy(child)
        fake["items"][0]["compri"] = "99999"
        for row in report_rows(fake, {}, receipt=receipt):
            self.assertEqual((row["ref_no"], row["usrno"], row["abndat"], row["abntim"]),
                             (receipt["ref_no"], "saved", "2026.10.14", "02:03:04"))
        self.assertEqual(report_rows(fake, {}, receipt=receipt)[0]["compri"], "0")

    def test_formula_injection_ordinary_numbers_paths_dates_and_quotes(self):
        for value in ("0", "-1", "+2.50", "1e-3", "-2E+5", ROOT, "2026.10.14", "00:01:02", 'a"b', "70*120"):
            self.assertEqual(safe_cell(value), value)
        for value in ("=1+1", "+SUM(A1)", "-cmd|'x'!A0", "@SUM(A1)", " \t\r=1", "\u200b=1", "＝1+1", "\x00@x"):
            self.assertEqual(safe_cell(value), "'" + value)
        text = '=HYPERLINK("x")\t\nnext'
        rows = [{"spdsc": text, "compri": "0"}]
        parsed = list(csv.reader(io.StringIO(tsv(rows)), delimiter="\t"))
        self.assertEqual(len(parsed), 2)
        self.assertEqual(len(parsed[1]), 13)
        self.assertEqual(parsed[1][5], "'" + text)
        self.assertEqual(parsed[1][8], "0")

    def test_dataframe_export_also_escapes_and_never_changes_snapshot(self):
        cat = fixture()
        child = analyze(SOURCE, cat)["children"][0]
        child["items"][0]["codsc"] = "=1+1"
        before = deepcopy(child)
        rows = screen_rows(child, cat)
        self.assertEqual(rows[0]["spdsc"], "'=1+1")
        self.assertEqual(child, before)


class SemanticPresentationTests(unittest.TestCase):
    def child(self, text, **kwargs):
        from test_local_block_semantics import master
        return analyze(text, master(**kwargs))["children"][0]

    def test_override_conflict_and_duplicate_keep_all_origins(self):
        child = self.child("平直70X120-----2 面808白橡\n共用：\n817胡")
        rows = [r for r in semantic_rows(child) if r["需求類型"] == "面材"]
        self.assertEqual([r["狀態"] for r in rows], ["已匹配", "已被覆寫"])
        self.assertEqual([r["是否適用"] for r in rows], ["是", "否"])
        self.assertEqual([r["優先級"] for r in rows], ["20", "10"])
        self.assertEqual([r["可算本張總量"] for r in rows], ["2", ""])
        self.assertEqual(rows[0]["覆寫需求"], rows[1]["需求識別"])
        conflict = semantic_rows(self.child("平直70X120-----2 面808白橡/817胡"))
        conflicts = [r for r in conflict if r["狀態"] == "衝突"]
        self.assertEqual(len(conflicts), 2)
        self.assertTrue(all(r["衝突需求"] and r["可算本張總量"] == "" for r in conflicts))
        duplicate = semantic_rows(self.child("平直70X120-----2\n共用：817胡\n共用：817胡"))
        self.assertTrue(any(r["狀態"] == "重複（不重計）" and r["可算本張總量"] == "" for r in duplicate))

    def test_pending_master_unknown_and_matched_missing_price_not_ready(self):
        rows = semantic_rows(self.child("平直70X121-----2 火星加工"))
        self.assertIn("待主檔／候選確認", [r["狀態"] for r in rows])
        self.assertIn("未知", [r["狀態"] for r in rows])
        child = self.child("平直70X120-----2\n共用：817胡", missing_price=True)
        self.assertFalse(child["ready"])
        material = next(r for r in semantic_rows(child) if r["需求類型"] == "面材")
        self.assertEqual(material["狀態"], "已匹配")
        self.assertEqual(material["可算本張總量"], "2")
        self.assertIn("817", material["已選規格"])
        self.assertTrue(material["候選"] and material["關聯需求"] and material["可掛接部位"])

    def test_all_semantic_cells_safe_and_snapshot_unchanged(self):
        child = self.child("平直70X120-----2")
        req = child["normalized_requirements"][0]
        for field in ("value", "part", "kind", "attribute", "id", "status"):
            req[field] = "\u200b＝1+1"
        for field in ("raw", "text", "scope"):
            req["origin"][field] = "\t@SUM(A1)"
        for field in ("targets", "override", "conflict", "custom_paths", "requirement_ids"):
            req[field] = ["=HYPERLINK(\"x\")"]
        child["group"] = "-cmd|'x'!A0"
        before = deepcopy(child)
        row = semantic_rows(child)[0]
        for field in ("需求", "部位", "需求類型", "屬性", "需求識別", "狀態", "來源原文", "解析片語", "範圍",
                      "群組內適用品項", "覆寫需求", "衝突需求", "可掛接部位", "關聯需求", "群組"):
            self.assertTrue(row[field].startswith("'"), (field, row[field]))
        self.assertEqual(child, before)

    def test_no_normalized_legacy_and_total_quantity_projection(self):
        self.assertEqual(semantic_rows({"items": []}), [])
        child = self.child("平直70X120-----2")
        req = child["normalized_requirements"][0]
        req.update(basis="total", quantity="3")
        row = semantic_rows(child)[0]
        self.assertEqual((row["每件量"], row["可算本張總量"], row["數量基準"]), ("", "3", "本張總量"))


if __name__ == "__main__":
    unittest.main()
