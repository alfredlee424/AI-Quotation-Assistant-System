"""本機複合規格識別回歸；全部合成資料與隔離 SQLite，不連現場主檔。"""
from copy import deepcopy
import socket
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine

from local_quote import dynamic, dynamic_v1
from local_quote.backend import LocalDatabase
from local_quote.business import MasterSource
from local_quote.catalog import option_identity
from local_quote.dynamic_storage import DynamicStore, prepare_document
from test_local_dynamic import fixture, SOURCE, BOX
from test_local_storage import SETTINGS, fixture_schema


AUDIT = dict(actor="tester", reason="複合規格核對", time="2026-10-19T10:00:00+08:00")


class CatalogVariantTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("禁止網路"))
        guard.start()
        self.addCleanup(guard.stop)
        self.cat = fixture()
        self.cat["category"]["codsc"] = "環式會議桌"
        for option in self.cat["options"][BOX]:
            option["code"] = "SAME"
        self.source = SOURCE + "\n配件：線盒共3個"

    def choose(self, description="黑線盒"):
        a = dynamic.analyze(self.source, self.cat)
        child = a["children"][0]
        req = next(r for r in child["requirements"] if r["text"] == "線盒")
        choice = dict(path=BOX, code="SAME", codsc=description)
        return dynamic.analyze(self.source, self.cat, choices={child["id"]: {req["id"]: choice}})

    def test_duplicate_code_variants_preserved_deterministically(self):
        cat = dynamic._catalog(self.cat)
        reversed_cat = deepcopy(self.cat)
        reversed_cat["options"][BOX].reverse()
        self.assertEqual(cat, dynamic._catalog(reversed_cat))
        self.assertEqual(len(cat["options"][BOX]), 2)
        self.assertEqual({o["codsc"] for o in cat["options"][BOX]}, {"銀線盒", "黑線盒"})
        with self.assertRaisesRegex(ValueError, "代碼不唯一"):
            dynamic_v1._catalog(self.cat)

    def test_ambiguous_code_only_and_forged_price_are_rejected(self):
        a = dynamic.analyze(self.source, self.cat)
        c = a["children"][0]
        req = next(r for r in c["requirements"] if r["text"] == "線盒")
        self.assertEqual(len(req["candidates"]), 2)
        self.assertIsNone(req["selected"])
        for choice in (dict(path=BOX, code="SAME"), dict(path=BOX, code="SAME", codsc="不存在"),
                       dict(path=BOX, code="SAME", codsc="黑線盒", compri="1")):
            result = dynamic.analyze(self.source, self.cat, choices={c["id"]: {req["id"]: choice}})
            self.assertFalse(result["children"][0]["ready"])
            self.assertTrue(any(g["kind"] == "invalid_choice" for g in result["gaps"]))

    def test_full_identity_selects_correct_name_price_and_fixed_snapshot(self):
        for name, price in (("銀線盒", "120"), ("黑線盒", "140")):
            a = self.choose(name)
            c = a["children"][0]
            self.assertTrue(c["ready"], c["issues"])
            row = next(i for i in c["items"] if i["path"] == BOX)
            self.assertEqual((row["codsc"], row["compri"], row["qty"]), (name, price, "3"))
            doc = dynamic.freeze(a, c["id"], **AUDIT)
            self.assertEqual(dynamic.validate_document(doc), c)
            self.assertEqual(option_identity(self.cat, row), dict(path=BOX, code="SAME", codsc=name))

    def test_same_code_different_names_are_not_merged_across_requirements(self):
        a = dynamic.analyze(SOURCE + "\n配件：每桌1個銀線盒\n配件：每桌1個黑線盒", self.cat)
        self.assertFalse(a["children"][0]["ready"])
        self.assertTrue(any(g["kind"] == "conflict" for g in a["gaps"]))

    def test_identical_full_key_and_blank_names_still_fail_with_path(self):
        for price in ("120", "999"):
            bad = deepcopy(self.cat)
            duplicate = deepcopy(bad["options"][BOX][0])
            duplicate["compri"] = price
            bad["options"][BOX].append(duplicate)
            with self.assertRaises(ValueError) as error:
                dynamic._catalog(bad)
            self.assertIn(BOX, str(error.exception))
        bad = deepcopy(self.cat)
        bad["options"][BOX][0]["codsc"] = " "
        with self.assertRaisesRegex(ValueError, "名稱缺少"):
            dynamic._catalog(bad)

    def test_category_title_matches_selected_master_without_becoming_demand(self):
        for title in ("環式會議桌(CMT1)", "環式會議桌（CMT1）"):
            a = dynamic.analyze(title + "\n\n" + SOURCE, self.cat)
            self.assertFalse(a["global_issues"])
            self.assertEqual(a["source_ledger"][0]["scope"], "category_header")
            self.assertTrue(a["children"][0]["ready"])
        for title in ("環式會議桌(OTHER)", "不同產品(CMT1)"):
            a = dynamic.analyze(title + "\n" + SOURCE, self.cat)
            self.assertTrue(a["global_issues"])
            self.assertFalse(a["children"][0]["ready"])

    def test_master_source_reads_full_key_and_roundtrip_keeps_variant(self):
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        metadata = fixture_schema(engine)
        wg = self.cat["workgroup"]
        tables = {
            "invdoc": [dict(self.cat["category"], workgroup=wg, ordkind="1")],
            "ordstr": list(self.cat["nodes"].values()),
            "ordspd": [dict(workgroup=wg, optno=k, optdesc=v) for k, v in self.cat["labels"].items()],
            "ordspe": [o for options in self.cat["options"].values() for o in options],
            "ordqty": [r for rules in self.cat["rules"].values() for r in rules],
        }
        with engine.begin() as conn:
            for name, rows in tables.items():
                for row in rows:
                    conn.execute(metadata.tables[name].insert(), row)
        db = LocalDatabase(SETTINGS, engine=engine, testing=True)
        with self.assertRaisesRegex(ValueError, "代碼不唯一"):
            MasterSource(db).catalog("CMT1")
        catalog = MasterSource(db, allow_code_variants=True).catalog("CMT1")
        self.assertEqual(len(catalog["options"][BOX]), 2)
        a = self.choose()
        doc = dynamic.freeze(a, a["children"][0]["id"], **AUDIT)
        frozen = prepare_document(doc)
        store = DynamicStore(db)
        receipt = store.save(frozen, confirmed=True, current=a, trusted_catalog_digest=a["catalog_digest"])
        self.assertEqual(store.read(frozen.confirmation_id), receipt)
        with db.read() as conn:
            row = conn.execute(metadata.tables["ordqdt_ai"].select().where(metadata.tables["ordqdt_ai"].c.path == BOX)).mappings().one()
        self.assertEqual((row["spc_code"], row["spdsc"], row["compri"]), ("SAME", "黑線盒", 140))


if __name__ == "__main__":
    unittest.main()
