"""新版工作階段離線測試；不讀設定、不連真 DB、不修改舊流程。"""
from copy import deepcopy
import socket
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine

from local_quote.dynamic_storage import DynamicStore
from local_quote.dynamic_workflow import DynamicWorkspace
from local_quote.backend import LocalDatabase
from test_local_dynamic import SOURCE, ROOT, BOARD, BOX, fixture
from test_local_dynamic_storage import AUDIT, SETTINGS
from test_local_storage import fixture_schema


class DynamicWorkspaceTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("禁止網路"))
        network.start()
        self.addCleanup(network.stop)
        self.engine = create_engine("sqlite:///:memory:")
        fixture_schema(self.engine)
        self.addCleanup(self.engine.dispose)
        self.store = DynamicStore(LocalDatabase(SETTINGS, engine=self.engine, testing=True))
        self.ws = DynamicWorkspace()
        self.ws.set_input(SOURCE + "\n" + SOURCE, "CMT1")
        self.ws.load_catalog(fixture())
        self.ids = [c["id"] for c in self.ws.analysis["children"]]

    def freeze_all(self):
        return [self.ws.freeze(cid, **AUDIT) for cid in self.ids]

    def test_set_input_analyze_load_catalog_no_implicit_price_reload(self):
        master = Mock()
        master.catalog.return_value = fixture()
        self.ws.load_catalog(master)
        self.freeze_all()
        for cid in self.ids:
            receipt = self.ws.confirm(cid, self.store, confirmed=True)
            self.assertEqual(receipt, self.store.read(receipt["confirmation_id"]))
        master.catalog.assert_called_once_with("CMT1")

    def test_single_child_edit_revokes_only_affected_unsaved_frozen(self):
        first, second = self.freeze_all()
        self.ws.edit(self.ids[0], overrides={BOARD: dict(AUDIT, values={"compri": "800"})})
        self.assertNotIn(self.ids[0], self.ws.frozen)
        self.assertEqual(self.ws.frozen[self.ids[1]], second)
        self.assertIn(self.ids[0], self.ws.revoked)
        with self.assertRaises(ValueError):
            self.ws.confirm(self.ids[0], self.store, confirmed=True)
        receipt = self.ws.confirm(self.ids[1], self.store, confirmed=True)
        self.assertEqual(receipt["confirmation_id"], second.confirmation_id)
        self.ws.edit(self.ids[0], overrides={})
        self.assertNotIn(self.ids[0], self.ws.frozen)  # 改回也不復活
        self.assertNotEqual(self.ws.freeze(self.ids[0], **AUDIT).confirmation_id, first.confirmation_id)

    def test_noop_edit_and_analyze_keep_same_uuid(self):
        first, second = self.freeze_all()
        self.ws.edit(self.ids[0], choices={})
        self.ws.analyze()
        self.assertEqual(self.ws.frozen[self.ids[0]], first)
        self.assertEqual(self.ws.freeze(self.ids[0], **AUDIT), first)
        self.assertEqual(self.ws.frozen[self.ids[1]], second)

    def test_repeat_freeze_cannot_silently_replace_human_audit(self):
        frozen = self.ws.freeze(self.ids[0], **AUDIT)
        for audit in (dict(AUDIT, actor="another"), dict(AUDIT, reason="different"),
                      dict(AUDIT, time="2026-10-15T08:00:00+08:00")):
            with self.assertRaisesRegex(ValueError, "人工稽核"):
                self.ws.freeze(self.ids[0], **audit)
        self.assertEqual(self.ws.frozen[self.ids[0]], frozen)

    def test_external_source_identity_preserved_as_audit_not_overwritten_by_uuid(self):
        source = dict(text=SOURCE, document_id="external-workorder-1")
        self.ws.set_input(source)
        self.assertEqual(self.ws.source, source)
        self.ws.analyze()
        cid = self.ws.analysis["children"][0]["id"]
        self.ws.freeze(cid, **AUDIT)
        changed = dict(source, text=SOURCE + "\n")
        self.ws.set_input(changed)
        self.assertEqual(self.ws.frozen, {})
        self.assertEqual(self.ws.source, changed)
        cat = fixture()
        cat["options"][BOARD][0]["compri"] = "701"
        self.ws.load_catalog(cat)
        self.assertEqual(self.ws.source["document_id"], source["document_id"])

    def test_saved_child_disabled_other_child_independent_and_receipt_number(self):
        self.freeze_all()
        saved = self.ws.confirm(self.ids[0], self.store, confirmed=True)
        self.assertFalse(self.ws.can_edit(self.ids[0]))
        self.assertTrue(self.ws.can_edit(self.ids[1]))
        self.assertIn(saved["ref_no"], self.ws.notice)
        with self.assertRaisesRegex(ValueError, "已保存"):
            self.ws.edit(self.ids[0], overrides={})
        with self.assertRaisesRegex(ValueError, "已保存"):
            self.ws.freeze(self.ids[0], **AUDIT)
        self.ws.edit(self.ids[1], overrides={BOARD: dict(AUDIT, values={"compri": "900"})})
        self.assertEqual(self.ws.confirm(self.ids[0], Mock(), confirmed=True), saved)
        self.assertEqual(self.store.read(saved["confirmation_id"]), saved)

    def test_source_change_clears_current_state_not_history_and_revert_new_draft(self):
        self.freeze_all()
        saved = self.ws.confirm(self.ids[0], self.store, confirmed=True)
        old_source = self.ws.source
        self.ws.set_input(SOURCE + "\n" + SOURCE + "\n")
        self.assertIsNone(self.ws.analysis)
        self.assertEqual(self.ws.frozen, {})
        self.assertEqual(self.ws.saved, {})
        self.assertIn(saved["confirmation_id"], self.ws.history)
        self.assertIn("新的草稿", self.ws.notice)
        self.assertEqual(self.store.read(saved["confirmation_id"]), saved)
        self.ws.set_input(old_source["text"])
        self.ws.analyze()
        self.assertNotEqual([c["id"] for c in self.ws.analysis["children"]], self.ids)
        self.assertEqual(self.ws.frozen, {})

    def test_source_identical_noop_and_catalog_changed_new_draft(self):
        frozen = self.freeze_all()
        self.assertFalse(self.ws.set_input(SOURCE + "\n" + SOURCE, "CMT1"))
        self.ws.load_catalog(fixture())
        self.assertEqual(list(self.ws.frozen.values()), frozen)
        cat = fixture()
        cat["options"][BOARD][0]["compri"] = "800"
        self.ws.load_catalog(cat)
        self.assertEqual(self.ws.frozen, {})
        self.assertNotEqual([c["id"] for c in self.ws.analysis["children"]], self.ids)

    def test_catalog_category_change_and_bad_reload_do_not_keep_stale_state(self):
        self.freeze_all()
        before = self.ws.catalog
        with self.assertRaises(ValueError):
            self.ws.load_catalog(fixture(code="OTHER"))
        self.assertEqual(self.ws.catalog, before)
        self.ws.set_input(SOURCE, "OTHER")
        self.assertIsNone(self.ws.catalog)
        self.assertEqual(self.ws.frozen, {})
        with self.assertRaises(ValueError):
            self.ws.analyze()

    def test_store_failure_retry_same_frozen_even_if_commit_response_lost(self):
        frozen = self.ws.freeze(self.ids[0], **AUDIT)
        real = self.store.save
        receipts = []

        def committed_but_response_lost(*args, **kwargs):
            receipts.append(real(*args, **kwargs))
            raise OSError("離線模擬提交後斷線")

        with patch.object(self.store, "save", side_effect=committed_but_response_lost), self.assertRaises(OSError):
            self.ws.confirm(self.ids[0], self.store, confirmed=True)
        self.assertEqual(self.ws.frozen[self.ids[0]], frozen)
        self.assertEqual(self.ws.saved, {})
        self.assertEqual(self.ws.confirm(self.ids[0], self.store, confirmed=True), receipts[0])
        self.assertEqual(len(self.store.list_saved()), 1)

    def test_edit_choices_customs_overrides_resolutions_child_local(self):
        # 每種 mapping 都是該張 identity -> value，未提供的種類不可被清空。
        self.ws.set_input(SOURCE + "\n配件：每桌2個線盒\n" + SOURCE)
        analysis = self.ws.analyze()
        cid, other = [c["id"] for c in analysis["children"]]
        gap = next(g for g in analysis["gaps"] if g["candidates"])
        self.ws.edit(cid, choices={gap["id"]: {"path": BOX, "code": "BLACK"}})
        self.assertTrue(self.ws.analysis["children"][0]["ready"])
        self.ws.edit(cid, overrides={BOX: dict(AUDIT, values={"compri": "101"})})
        self.assertEqual(self.ws.analysis["children"][0]["selections"][BOX]["code"], "BLACK")
        self.assertNotIn(BOX, self.ws.analysis["children"][1]["selections"])
        self.ws.edit(cid, choices={}, customs={gap["id"]: dict(AUDIT, path=BOX, code="TMP:B", codsc="合成客製盒", compri="99")})
        self.assertTrue(self.ws.analysis["children"][0]["ready"])
        self.ws.freeze(cid, **AUDIT)
        self.ws.set_input("平直2X4台尺-----2\n" + SOURCE)
        analysis = self.ws.analyze()
        gap = next(g for g in analysis["gaps"] if g["kind"] == "units")
        self.ws.edit(gap["child_id"], resolutions={gap["id"]: dict(AUDIT, text="平直70X120")})
        self.assertTrue(all(c["ready"] for c in self.ws.analysis["children"]))

    def test_caller_mutations_cannot_replace_trusted_current_catalog_or_history(self):
        cat = fixture()
        self.ws.load_catalog(cat)
        cat["options"][BOARD][0]["compri"] = "0"
        analysis = self.ws.analysis
        analysis["children"][0]["ready"] = False
        self.ws.catalog["options"][BOARD][0]["compri"] = "0"
        self.ws.source["text"] = "changed"
        self.assertTrue(self.ws.analysis["children"][0]["ready"])
        self.assertEqual(self.ws.catalog["options"][BOARD][0]["compri"], "700")
        self.ws.freeze(self.ids[0], **AUDIT)
        saved = self.ws.confirm(self.ids[0], self.store, confirmed=True)
        saved["document"]["child"]["calc"]["total_price"] = "0"
        self.ws.saved.clear()
        self.assertNotEqual(self.ws.saved[self.ids[0]]["document"]["child"]["calc"]["total_price"], "0")

    def test_invalid_edit_and_confirm_do_not_write_or_mutate_frozen(self):
        frozen = self.freeze_all()
        for action in (lambda: self.ws.edit("wrong-child", choices={}),
                       lambda: self.ws.edit(self.ids[0], unknown={}),
                       lambda: self.ws.edit(self.ids[0], choices=None),
                       lambda: self.ws.confirm(self.ids[0], self.store, confirmed=False)):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(list(self.ws.frozen.values()), frozen)
        self.assertEqual(self.store.list_saved(), [])

    def test_missing_structure_or_cost_cannot_freeze_or_confirm(self):
        for mode in ("structure", "cost"):
            cat = fixture()
            if mode == "structure":
                cat["options"][ROOT] = []
            else:
                cat["options"][BOARD][0]["compri"] = None
            result = self.ws.load_catalog(cat)
            cid = result["children"][0]["id"]
            with self.assertRaises(ValueError):
                self.ws.freeze(cid, **AUDIT)
            with self.assertRaises(ValueError):
                self.ws.confirm(cid, self.store, confirmed=True)
        self.assertEqual(self.store.list_saved(), [])


if __name__ == "__main__":
    unittest.main()
