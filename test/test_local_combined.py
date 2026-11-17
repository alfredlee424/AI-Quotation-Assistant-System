"""不拆單合併報價離線測試；合成主檔、SQLite、禁止網路。"""
from decimal import Decimal, ROUND_HALF_UP
import socket
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text

from local_quote import combined
from local_quote.backend import LocalDatabase
from local_quote.combined_storage import CombinedStore, prepare_document
from local_quote.combined_workflow import CombinedWorkspace
from local_quote.dynamic import analyze
from local_quote.dynamic_storage import DynamicStore
from test_local_dynamic import BOARD, fixture
from test_local_dynamic_storage import AUDIT, SETTINGS
from test_local_storage import fixture_schema

LEG_BOARD = r"CMT1\B001\S004"
SOURCE = "平直70X120-----2 45寬木腳\n平直70X150-----3 45寬木腳\n平直70X120-----1"


def missing_leg_price():
    cat = fixture()
    for option in cat["options"][LEG_BOARD]:
        option["compri"] = None
    return cat


class CombinedBlockTests(unittest.TestCase):
    def test_repeated_a001_and_b001_blocks_are_numbered_and_checked_separately(self):
        result = analyze(SOURCE, missing_leg_price())
        blocks = combined.blocks(result)
        self.assertEqual([(b["root"], b["number"], b["ordinal"]) for b in blocks],
                         [(r"CMT1\A001", 1, 1), (r"CMT1\B001", 1, 1), (r"CMT1\A001", 2, 2),
                          (r"CMT1\B001", 2, 2), (r"CMT1\A001", 3, 3)])
        self.assertEqual([b["ready"] for b in blocks], [True, False, True, False, True])
        for block in blocks:
            self.assertTrue(all(i["path"].startswith(block["root"]) for i in block["items"]))
            self.assertTrue(all(block["root"] in i["id"] for i in block["issues"]))
        self.assertFalse(combined.summary(result)["complete"])
        with self.assertRaisesRegex(ValueError, r"CMT1\\B001 第1組、CMT1\\B001 第2組"):
            combined.freeze(result, **AUDIT)

    def test_summary_totals_all_blocks_once(self):
        result = analyze(SOURCE, fixture())
        calc = combined.summary(result)
        self.assertTrue(calc["complete"])
        self.assertEqual(calc["block_count"], 5)
        self.assertEqual(Decimal(calc["total_cost"]), sum(Decimal(c["calc"]["total_cost"]) for c in result["children"]))
        self.assertEqual(Decimal(calc["subtotal"]),
                         (Decimal(calc["total_cost"]) * Decimal("1.30")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def test_frozen_document_rejects_tampering_and_stale_source(self):
        result = analyze(SOURCE, fixture())
        document = combined.freeze(result, **AUDIT)
        self.assertEqual(len(combined.validate_document(document, current=result)), 3)
        tampered = dict(document, calc=dict(document["calc"], total_price="1.00"))
        with self.assertRaises(ValueError):
            combined.validate_document(tampered)
        with self.assertRaises(ValueError):
            combined.validate_document(document, current=analyze(SOURCE + "\n平直70X120-----1", fixture()))


class CombinedStorageTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("禁止網路"))
        network.start()
        self.addCleanup(network.stop)
        self.engine = create_engine("sqlite:///:memory:")
        fixture_schema(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = LocalDatabase(SETTINGS, engine=self.engine, testing=True)
        self.store = CombinedStore(self.db)

    def test_one_ref_no_with_all_blocks_and_repeated_paths(self):
        result = analyze(SOURCE, fixture())
        frozen = prepare_document(combined.freeze(result, **AUDIT))
        receipt = self.store.save(frozen, confirmed=True, current=result,
                                  trusted_catalog_digest=result["catalog_digest"])
        self.assertTrue(receipt["ref_no"].startswith("DM"))
        with self.engine.connect() as conn:
            docs = conn.execute(text("SELECT ref_no FROM quote_snapshot_document")).scalars().all()
            rows = conn.execute(text("SELECT ref_no, seq_no, path, prgno FROM ordqdt_ai ORDER BY seq_no")).all()
        self.assertEqual(docs, [receipt["ref_no"]])
        expected = [i["path"] for c in result["children"] for i in c["items"]]
        self.assertEqual([r.path for r in rows], expected)
        self.assertEqual([r.seq_no for r in rows], [f"{n:05d}" for n in range(1, len(expected) + 1)])
        self.assertEqual({r.ref_no for r in rows}, {receipt["ref_no"]})
        self.assertEqual({r.prgno for r in rows}, {"COMBINE1"})
        self.assertEqual(sum(r.path == r"CMT1\A001" for r in rows), 3)
        self.assertEqual(sum(r.path == r"CMT1\B001" for r in rows), 2)
        self.assertEqual(self.store.read(receipt["confirmation_id"]), receipt)
        self.assertEqual(self.store.list_saved(), [receipt])
        # 同 UUID 重試回原 receipt；逐張歷史不誤讀合併單。
        self.assertEqual(self.store.save(frozen, confirmed=True, current=result,
                                         trusted_catalog_digest=result["catalog_digest"]), receipt)
        self.assertEqual(DynamicStore(self.db).list_saved(), [])

    def test_workflow_freeze_revoke_and_lock_after_save(self):
        ws = CombinedWorkspace()
        ws.set_input(SOURCE, "CMT1")
        ws.load_catalog(fixture())
        first_id = ws.analysis["children"][0]["id"]
        with self.assertRaises(ValueError):
            ws.freeze(first_id, **AUDIT)
        first = ws.freeze_combined(**AUDIT)
        self.assertIs(ws.freeze_combined(**AUDIT), first)
        ws.edit(first_id, overrides={BOARD: dict(AUDIT, values={"compri": "800"})})
        self.assertIsNone(ws.combined_frozen)
        self.assertTrue(ws.combined_revoked)
        with self.assertRaises(ValueError):
            ws.confirm_combined(self.store, confirmed=True)
        second = ws.freeze_combined(**AUDIT)
        self.assertNotEqual(second.confirmation_id, first.confirmation_id)
        receipt = ws.confirm_combined(self.store, confirmed=True)
        self.assertEqual(receipt["child_ids"], [c["id"] for c in ws.analysis["children"]])
        self.assertEqual(set(ws.saved), set(receipt["child_ids"]))
        with self.assertRaises(ValueError):
            ws.edit(first_id, overrides={})


class CombinedLauncherTests(unittest.TestCase):
    def test_combined_requires_local_db(self):
        from four_quote_acceptance import main
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            main(["--directory", "/nonexistent", "--combined"])


if __name__ == "__main__":
    unittest.main()
