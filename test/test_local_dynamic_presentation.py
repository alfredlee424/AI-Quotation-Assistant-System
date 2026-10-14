"""純報表測試：不讀主檔／設定、不使用真實來源。"""
from copy import deepcopy
import csv
import io
import unittest

from local_quote.dynamic import analyze, freeze
from local_quote.dynamic_presentation import FIELDS, report_rows, safe_cell, screen_rows, tsv
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


if __name__ == "__main__":
    unittest.main()
