"""真 Streamlit AppTest + 合成主檔／隔離 SQLite；可獨立 unittest，無真 MySQL。"""
from copy import deepcopy
import base64
import json
from pathlib import Path
import random
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool
from streamlit.testing.v1 import AppTest

from local_quote import dynamic_ui as ui
from local_quote.backend import LocalDatabase
from local_quote.dynamic_storage import DynamicStore
from local_quote.dynamic_workflow import DynamicWorkspace
from local_quote.storage import BatchStore, freeze_preview
from test_local_dynamic import fixture, SOURCE, ROOT, BOARD, BOX
from test_local_storage import SETTINGS, fixture_schema, preview


class Master:
    def __init__(self):
        self.cat, self.calls = fixture(), 0

    def categories(self):
        return [self.cat["category"]]

    def catalog(self, code):
        assert code == "CMT1"
        self.calls += 1
        return deepcopy(self.cat)


class DynamicUITests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("禁止網路"))
        guard.start()
        self.addCleanup(guard.stop)
        self.engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
        fixture_schema(self.engine)
        self.addCleanup(self.engine.dispose)
        db = LocalDatabase(SETTINGS, engine=self.engine, testing=True)
        self.master, self.store, self.legacy = Master(), DynamicStore(db), BatchStore(db)
        self.sql = []
        event.listen(self.engine, "before_cursor_execute", lambda c, u, s, p, x, m: self.sql.append(s))
        services = patch.object(ui, "services", return_value=(self.master, self.store, self.legacy))
        services.start()
        self.addCleanup(services.stop)
        self.app = AppTest.from_string("from local_quote.dynamic_ui import main\nmain()", default_timeout=30).run()
        self.clean()

    def clean(self):
        self.assertFalse(self.app.exception, str(self.app.exception))

    def click(self, key):
        self.app.button(key=key).click().run()
        self.clean()

    def fill(self, source=SOURCE + "\n平直70X150-----5"):
        self.app.selectbox(key="dq_category").select("CMT1").run()
        self.app.text_area(key="dq_source").input(source).run()
        self.app.text_input(key="dq_actor").input("tester").run()
        self.app.text_input(key="dq_reason").input("合成驗收人工核對").run()
        self.click("dq_analyze")
        return self.ws.analysis["children"]

    @property
    def ws(self):
        return self.app.session_state.dq_workspace

    def freeze(self, cid):
        self.click("dq_freeze:" + cid)
        self.assertIn(cid, self.ws.frozen, [e.value for e in self.app.error])
        return self.ws.frozen[cid]

    def save(self, cid):
        frozen = self.ws.frozen[cid]
        self.app.checkbox(key="dq_confirm:" + frozen.confirmation_id).check().run()
        self.click("dq_save:" + cid)

    def input_cost(self, cid, path, field, value):
        self.app.text_input(key=f"dq_cost:{cid}:{path}:{field}").input(value).run()
        self.clean()

    def test_initial_no_master_query_no_write_no_preselected_category(self):
        self.assertIsNone(self.app.selectbox(key="dq_category").value)
        self.assertTrue(self.app.text_area(key="dq_source").disabled)
        self.assertEqual(self.master.calls, 0)
        self.assertFalse(self.sql)

    def test_complete_two_unequal_quantities_independent_freeze_save(self):
        children = self.fill()
        self.assertEqual([c["product_qty"] for c in children], [2, 5])
        self.assertEqual(len(self.app.tabs), 2)
        self.assertTrue(all(c["ready"] for c in children))
        self.assertFalse(any(s.startswith("INSERT") for s in self.sql))
        a, b = [c["id"] for c in children]
        self.freeze(a)
        self.freeze(b)
        self.assertTrue(self.app.button(key="dq_save:" + a).disabled)
        self.save(a)
        self.assertIn(a, self.ws.saved)
        self.assertNotIn(b, self.ws.saved)
        self.assertTrue(self.app.button(key="dq_save:" + a).disabled)
        self.assertTrue(self.app.text_input(key=f"dq_cost:{a}:{BOARD}:compri").disabled)
        self.save(b)
        self.assertEqual(len(self.store.list_saved()), 2)
        self.assertEqual(self.master.calls, 1)
        self.assertNotEqual(self.ws.saved[a]["ref_no"], self.ws.saved[b]["ref_no"])

    def test_missing_cost_visible_draft_and_manual_fill_audited(self):
        self.master.cat["options"][BOARD][0]["compri"] = None
        cid = self.fill(SOURCE)[0]["id"]
        self.assertFalse(self.ws.analysis["children"][0]["ready"])
        self.assertTrue(any(m.label == "已知成本小計（非完整合計）" for m in self.app.metric))
        self.assertFalse(any(m.label == "本張報價（含稅）" for m in self.app.metric))
        self.assertTrue(self.app.button(key="dq_freeze:" + cid).disabled)
        self.input_cost(cid, BOARD, "compri", "720")
        child = self.ws.analysis["children"][0]
        self.assertTrue(child["ready"], child["issues"])
        record = next(i for i in child["items"] if i["path"] == BOARD)["audit"][-1]
        self.assertEqual(record["actor"], "tester")
        self.assertIn("time", record)
        self.freeze(cid)
        self.save(cid)

    def test_custom_form_existing_path_missing_usage_then_fill(self):
        cid = self.fill("平直70X140-----2")[0]["id"]
        child = self.ws.analysis["children"][0]
        req = next(r for r in child["requirements"] if r["text"] == "70X140")
        prefix = f"dq_custom:{cid}:{req['id']}"
        self.app.selectbox(key=prefix + ":path").select(ROOT)
        self.app.text_input(key=prefix + ":code").input("TMP:D140")
        self.app.text_input(key=prefix + ":description").input("70*140")
        self.app.text_input(key=prefix + ":price").input("0")
        self.click("FormSubmitter:" + prefix + "-套用本次臨時規格")
        self.input_cost(cid, ROOT, "stdqty", "1")
        self.input_cost(cid, ROOT, "stdpar", "1")
        self.input_cost(cid, BOARD, "stdqty", "5.5")
        self.input_cost(cid, BOARD, "stdpar", "24")
        child = self.ws.analysis["children"][0]
        self.assertTrue(child["ready"], child["issues"])
        self.assertEqual(child["selections"][ROOT]["code"], "TMP:D140")
        self.assertTrue(child["selections"][ROOT]["custom"])
        self.freeze(cid)
        self.save(cid)

    def test_required_structure_conflict_no_shortcut(self):
        self.master.cat["nodes"][BOX]["must_chose"] = "Y"
        self.master.cat["nodes"][ROOT + r"\S019"]["must_chose"] = "Y"
        children = self.fill(SOURCE)
        cid = children[0]["id"]
        self.assertFalse(children[0]["ready"])
        self.assertTrue(any(g["kind"] == "structure" for g in children[0]["issues"]))
        self.assertTrue(self.app.button(key="dq_freeze:" + cid).disabled)
        self.assertFalse(any("忽略" in b.label or "刪缺項" in b.label for b in self.app.button))

    def test_one_saved_other_not_ready(self):
        children = self.fill(SOURCE + "\n平直70X140-----3")
        a, b = [c["id"] for c in children]
        self.assertTrue(children[0]["ready"])
        self.assertFalse(children[1]["ready"])
        self.freeze(a)
        self.save(a)
        self.assertTrue(self.app.button(key="dq_freeze:" + b).disabled)
        self.assertEqual(len(self.store.list_saved()), 1)

    def test_edit_revokes_one_uuid_and_does_not_resurrect_when_changed_back(self):
        a, b = [c["id"] for c in self.fill()]
        old_a, old_b = self.freeze(a), self.freeze(b)
        self.input_cost(a, BOARD, "compri", "701")
        self.assertNotIn(a, self.ws.frozen)
        self.assertEqual(self.ws.frozen[b], old_b)
        self.input_cost(a, BOARD, "compri", "700")
        self.assertNotIn(a, self.ws.frozen)
        new_a = self.freeze(a)
        self.assertNotEqual(old_a.confirmation_id, new_a.confirmation_id)
        self.assertFalse(self.app.checkbox(key="dq_confirm:" + new_a.confirmation_id).value)

    def test_failed_save_retries_same_uuid_and_latest_analysis(self):
        cid = self.fill(SOURCE)[0]["id"]
        original = self.store.save
        calls = []

        def fail_once(frozen, **kwargs):
            calls.append(frozen)
            self.assertEqual(kwargs["current"], self.ws.analysis)
            if len(calls) == 1:
                raise RuntimeError("private driver message must not display")
            return original(frozen, **kwargs)

        self.store.save = fail_once
        frozen = self.freeze(cid)
        self.save(cid)
        self.assertEqual(self.ws.frozen[cid], frozen)
        self.assertNotIn(cid, self.ws.saved)
        self.assertNotIn("private driver", str([e.value for e in self.app.error]))
        self.click("dq_save:" + cid)
        self.assertEqual(calls, [frozen, frozen])
        self.assertIn(cid, self.ws.saved)

    def test_total_basis_and_candidate_choice_audit(self):
        cid = self.fill(SOURCE + "\n配件：線盒共3個")[0]["id"]
        req = next(r for r in self.ws.analysis["children"][0]["requirements"] if r["text"] == "線盒")
        selector = self.app.selectbox(key=f"dq_choice:{cid}:{req['id']}")
        self.assertIsNone(selector.value)
        selector.select(0).run()
        self.clean()
        self.input_cost(cid, BOX, "line_qty", "7")
        item = next(i for i in self.ws.analysis["children"][0]["items"] if i["path"] == BOX)
        self.assertEqual(item["quantity_basis"], "total")
        self.assertEqual(item["qty"], "7")
        frozen = self.freeze(cid)
        events = json.loads(json.loads(frozen.payload)["frozen_audit"]["reason"])["ui_events"]
        self.assertTrue(any("choices" in e["after"] and e["actor"] == "tester" for e in events))

    def test_same_code_different_names_ui_choice_keeps_full_identity_and_price(self):
        for option in self.master.cat["options"][BOX]:
            option["code"] = "SAME"
        cid = self.fill(SOURCE + "\n配件：線盒共3個")[0]["id"]
        req = next(r for r in self.ws.analysis["children"][0]["requirements"] if r["text"] == "線盒")
        index = next(i for i, o in enumerate(req["candidates"]) if o["codsc"] == "黑線盒")
        key = f"dq_choice:{cid}:{req['id']}"
        self.assertIsNone(self.app.selectbox(key=key).value)
        self.app.selectbox(key=key).select(index).run()
        self.clean()
        self.assertEqual(self.app.selectbox(key=key).value, index)
        choice = self.ws.analysis["context"]["inputs"]["choices"][cid][req["id"]]
        self.assertEqual(choice, dict(path=BOX, code="SAME", codsc="黑線盒"))
        self.freeze(cid)
        self.save(cid)
        row = next(i for i in self.ws.saved[cid]["document"]["child"]["items"] if i["path"] == BOX)
        self.assertEqual((row["codsc"], row["compri"]), ("黑線盒", "140"))

    def test_unknown_resolution_requires_confirmed_single_child_scope(self):
        a, b = [c["id"] for c in self.fill(SOURCE + " 未知加工\n" + SOURCE + " 未知加工")]
        child = self.ws.analysis["children"][0]
        req = next(r for r in child["requirements"] if r["text"] == "未知加工")
        prefix = f"dq_resolution:{a}:{req['source']['fragment_id']}"
        self.app.text_area(key=prefix + ":text").input("平直70X120；面無孔")
        self.click("FormSubmitter:" + prefix + "-套用重述及作用範圍")
        self.assertFalse(self.ws.analysis["children"][0]["ready"])
        self.app.checkbox(key=prefix + ":scope").check()
        self.app.text_area(key=prefix + ":text").input("平直70X120 面無孔")
        self.click("FormSubmitter:" + prefix + "-套用重述及作用範圍")
        self.assertTrue(self.ws.analysis["children"][0]["ready"], self.ws.analysis["children"][0]["issues"])
        self.assertFalse(self.ws.analysis["children"][1]["ready"])
        self.assertNotIn(b, self.ws.analysis["context"]["inputs"]["resolutions"])

    def test_new_and_legacy_history_read_without_master_and_clear_stale_on_failure(self):
        p = preview()
        old = self.legacy.save(freeze_preview(p), confirmed=True, input_digest=p["input_digest"])
        cid = self.fill(SOURCE)[0]["id"]
        self.freeze(cid)
        self.save(cid)
        saved = self.ws.saved[cid]
        self.master.catalog = lambda _: self.fail("歷史不得查主檔")
        self.master.categories = lambda: self.fail("歷史不得重新查類別")
        for kind, identity in (("new", saved["confirmation_id"]), ("old", old["batch_id"])):
            prefix = "dq_history:" + kind
            self.click(prefix + ":list")
            self.app.selectbox(key=prefix + ":selected").select(identity).run()
            self.click(prefix + ":read")
            self.assertIsNotNone(self.app.session_state[prefix + ":document"])
        self.assertEqual(self.master.calls, 1)
        self.legacy.read = lambda _: (_ for _ in ()).throw(ValueError("bad history"))
        self.click("dq_history:old:read")
        self.assertNotIn("dq_history:old:document", self.app.session_state)

    def test_source_edit_revokes_all_and_new_source_keeps_saved_history(self):
        a, b = [c["id"] for c in self.fill()]
        self.freeze(a)
        self.freeze(b)
        self.app.text_area(key="dq_source").input(SOURCE + "\n平直70X150-----6").run()
        self.clean()
        self.assertFalse(self.ws.frozen)
        self.assertIsNone(self.ws.analysis)
        self.click("dq_analyze")
        cid = self.ws.analysis["children"][0]["id"]
        self.freeze(cid)
        self.save(cid)
        self.click("dq_new")
        self.assertIsNone(self.app.selectbox(key="dq_category").value)
        self.assertFalse(self.ws.saved)
        self.assertEqual(len(self.store.list_saved()), 1)

    def test_late_choice_callback_after_analysis_cleared_can_reanalyze(self):
        child = self.fill(SOURCE + "\n配件：線盒共3個")[0]
        req = next(r for r in child["requirements"] if r["text"] == "線盒")
        selector = self.app.selectbox(key=f"dq_choice:{child['id']}:{req['id']}")
        revised = "平直70X150-----6"
        # 模擬來源已失效，但瀏覽器上一輪候選事件仍在佇列。
        self.ws.set_input(revised, "CMT1")
        self.assertIsNone(self.ws.analysis)
        selector.select(0)
        self.app.text_area(key="dq_source").input(revised)
        self.click("dq_analyze")
        new = self.ws.analysis["children"][0]
        self.assertEqual(new["product_qty"], 6)
        self.assertTrue(new["ready"])
        self.assertNotIn(BOX, new["selections"])
        self.assertFalse(self.ws.analysis["context"]["inputs"]["choices"])
        self.assertFalse(self.app.session_state["dq_blocked"])
        self.assertFalse(any(s.startswith("INSERT") for s in self.sql))

    def test_source_and_choice_same_rerun_rejects_old_edit_before_main(self):
        child = self.fill(SOURCE + "\n配件：線盒共3個")[0]
        req = next(r for r in child["requirements"] if r["text"] == "線盒")
        self.app.selectbox(key=f"dq_choice:{child['id']}:{req['id']}").select(0)
        self.app.text_area(key="dq_source").input("平直70X150-----7")
        with patch.object(self.ws, "edit", wraps=self.ws.edit) as edit:
            self.click("dq_analyze")
            edit.assert_not_called()
        self.assertEqual(self.ws.analysis["children"][0]["product_qty"], 7)
        self.assertFalse(self.ws.analysis["context"]["inputs"]["choices"])

    def test_late_cost_callback_after_analysis_cleared_can_reanalyze(self):
        child = self.fill(SOURCE)[0]
        self.freeze(child["id"])
        cost = self.app.text_input(key=f"dq_cost:{child['id']}:{BOARD}:compri")
        revised = "平直70X150-----4"
        self.ws.set_input(revised, "CMT1")
        cost.input("999")
        self.app.text_area(key="dq_source").input(revised)
        self.click("dq_analyze")
        self.assertFalse(self.ws.frozen)
        self.assertFalse(self.ws.analysis["context"]["inputs"]["overrides"])
        board = next(i for i in self.ws.analysis["children"][0]["items"] if i["path"] == BOARD)
        self.assertEqual(board["compri"], "700")

    def test_bad_numeric_input_revokes_and_blocks_instead_of_using_previous_value(self):
        cid = self.fill(SOURCE)[0]["id"]
        self.freeze(cid)
        self.input_cost(cid, BOARD, "stdpar", "0")
        self.assertNotIn(cid, self.ws.frozen)
        self.assertFalse(self.ws.analysis["children"][0]["ready"])
        self.assertTrue(self.app.button(key="dq_freeze:" + cid).disabled)
        self.input_cost(cid, BOARD, "stdpar", "24")
        self.assertTrue(self.ws.analysis["children"][0]["ready"])

    def test_capacity_failure_explicit_no_uuid_no_write(self):
        self.master.cat["nodes"][ROOT]["dmark"] = base64.b64encode(random.Random(90210).randbytes(100000)).decode()
        cid = self.fill(SOURCE)[0]["id"]
        self.assertTrue(self.ws.analysis["children"][0]["ready"])
        self.click("dq_freeze:" + cid)
        self.assertTrue(any("65535" in e.value for e in self.app.error))
        self.assertFalse(self.ws.frozen)
        self.assertFalse(any(s.startswith("INSERT") for s in self.sql))

    def test_compressible_large_catalog_freeze_save_and_history_without_ui_api_change(self):
        self.master.cat["nodes"][ROOT]["dmark"] = "容量測試" * 18000
        cid = self.fill(SOURCE)[0]["id"]
        frozen = self.freeze(cid)
        self.save(cid)
        receipt = self.ws.saved[cid]
        self.assertEqual(receipt["document"], json.loads(frozen.payload))
        self.assertEqual(self.store.read(frozen.confirmation_id), receipt)
        self.master.catalog = lambda _: self.fail("歷史不得查主檔")
        self.click("dq_history:new:list")
        self.app.selectbox(key="dq_history:new:selected").select(frozen.confirmation_id).run()
        self.click("dq_history:new:read")
        self.assertIsNotNone(self.app.session_state["dq_history:new:document"])
        self.assertTrue(any("無損壓縮" in item.value and "8 MiB" in item.value for item in self.app.warning))

    def test_actual_download_data_draft_and_saved_thirteen_columns(self):
        import csv
        import io
        from local_quote.dynamic_presentation import FIELDS
        captured = {}
        original = ui.st.download_button

        def capture(label, data, **kwargs):
            captured[kwargs["key"]] = (label, data, kwargs["file_name"])
            return original(label, data, **kwargs)

        with patch.object(ui.st, "download_button", side_effect=capture):
            cid = self.fill(SOURCE)[0]["id"]
            label, data, filename = captured["dq_download:" + cid]
            rows = list(csv.reader(io.StringIO(data), delimiter="\t"))
            self.assertEqual(tuple(rows[0]), FIELDS)
            self.assertTrue(all(len(row) == 13 and row[1] == "" for row in rows[1:]))
            self.assertIn("草稿", label)
            self.assertTrue(filename.startswith("draft-"))
            self.freeze(cid)
            self.save(cid)
            receipt = self.ws.saved[cid]
            self.app.text_input(key="dq_actor").input("other").run()
            label, data, filename = captured["dq_download:" + cid]
            rows = list(csv.DictReader(io.StringIO(data), delimiter="\t"))
            self.assertTrue(all(r["ref_no"] == receipt["ref_no"] and r["usrno"] == "tester" for r in rows))
            self.assertIn("正式已保存", label)
            self.assertTrue(filename.startswith("saved-"))


class CallbackGuardTests(unittest.TestCase):
    """保留相同 child id 的重載也必須拒絕舊來源／主檔版本，不能只驗 id。"""
    def setUp(self):
        self.ws = DynamicWorkspace()
        self.ws.set_input({"text": SOURCE, "document_id": "stable-source"}, "CMT1")
        self.ws.load_catalog(fixture())
        self.child = self.ws.analysis["children"][0]
        self.origin = (self.child["source_digest"], self.child["catalog_digest"])

    def assert_rejected(self, *, current=None, category="CMT1"):
        current = current or self.ws
        before = current.analysis
        state = dict(dq_workspace=current, dq_source=current.source["text"], dq_category=category,
                     dq_actor="tester", dq_reason="核對", cost="999", dq_blocked={})
        with patch.object(ui.st, "session_state", state), patch.object(self.ws, "edit", wraps=self.ws.edit) as edit:
            ui._callback(ui._cost, self.ws, self.child["id"], BOARD, "compri", "cost", origin=self.origin)
            edit.assert_not_called()
        self.assertEqual(current.analysis, before)
        self.assertFalse(state["dq_blocked"])
        self.assertIn("未套用", state["dq_notice"])

    def test_mapping_empty_analysis_is_explicit_validation_error(self):
        self.ws.set_input("平直70X150-----4", "CMT1")
        with self.assertRaisesRegex(ValueError, "分析已失效"):
            ui._mapping(self.ws, self.child["id"], "choices")

    def test_old_source_version_rejected_even_with_same_child_id(self):
        self.ws.set_input({"text": "平直70X150-----4", "document_id": "stable-source"}, "CMT1")
        self.ws.load_catalog(fixture())
        self.assertEqual(self.child["id"], self.ws.analysis["children"][0]["id"])
        self.assert_rejected()

    def test_old_catalog_version_rejected_even_with_same_child_id(self):
        cat = fixture()
        cat["options"][BOARD][0]["compri"] = "800"
        self.ws.load_catalog(cat)
        self.assertEqual(self.child["id"], self.ws.analysis["children"][0]["id"])
        self.assert_rejected()

    def test_old_workspace_rejected_after_new_workspace_created(self):
        current = DynamicWorkspace()
        current.set_input(self.ws.source, "CMT1")
        current.load_catalog(fixture())
        self.assertEqual(self.ws.analysis, current.analysis)
        self.assert_rejected(current=current)

    def test_category_change_before_main_rejects_callback(self):
        self.assert_rejected(category=None)


class IsolationTests(unittest.TestCase):
    def test_new_ui_import_never_loads_global_config_database_or_connects(self):
        root = str(Path(__file__).resolve().parents[1])
        code = '''
import sys, socket
sys.path.insert(0, sys.argv[1])
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
import sqlalchemy
sqlalchemy.create_engine = lambda *a, **k: (_ for _ in ()).throw(AssertionError("engine"))
import local_quote.dynamic_ui, local_quote.dynamic_presentation
assert not any(n in sys.modules for n in ("config", "database.connection", "app", "acceptance.bootstrap"))
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, root], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_worker_routes_new_ui_with_existing_safety_flags(self):
        root = str(Path(__file__).resolve().parents[1])
        code = '''
import sys, os
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from local_quote import bootstrap as b, backend
class DB:
    engine = type("Engine", (), {"dispose": lambda self: None})()
    def __init__(self, settings): pass
    def inspect_schema(self): pass
backend.LocalDatabase = DB
backend.load_settings = lambda path: None
def streamlit(name, run_name):
    assert sys.argv[2] == str(b.ROOT / "local_quote/dynamic_ui.py")
    assert sys.argv[sys.argv.index("--server.address") + 1] == "127.0.0.1"
    assert sys.argv[sys.argv.index("--browser.gatherUsageStats") + 1] == "false"
    assert sys.argv[sys.argv.index("--server.fileWatcherType") + 1] == "none"
    assert Path(os.getcwd()).resolve() == Path(os.environ["HOME"]).resolve()
    assert os.environ["MAPBOX_API_KEY"] == ""
    assert not any(n in sys.modules for n in ("config", "database.connection", "acceptance.bootstrap"))
b.runpy.run_module = streamlit
assert b.launch(worker=True) == 0
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, root], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
