"""UI/工作階段/入口整合；只用主檔替身及隔離寫入，不讀私人設定。"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from local_quote import bootstrap, ui
from local_quote.business import analyze, generate
from local_quote.workflow import Workspace
from test_local_business import SOURCE, ORIGINAL, catalog, choose_required


class Master:
    def __init__(self):
        self.calls = 0
        self.cat = catalog()

    def categories(self):
        return [self.cat["category"], {"prodkind": "ZZ", "codsc": "資料庫新類別", "quo_rate": 1}]

    def catalog(self, code):
        self.calls += 1
        return deepcopy(self.cat if code == "CMT1" else catalog("ZZ"))


class Store:
    def __init__(self):
        self.calls = 0
        self.saved = None

    def save(self, frozen, *, confirmed, input_digest):
        assert confirmed and json.loads(frozen.payload)["input_digest"] == input_digest
        self.calls += 1
        p = json.loads(frozen.payload)
        self.saved = {"batch_id": p["preview_id"], "ref_nos": [f"20990101000000_{n:03d}" for n in range(1, 5)],
                      "confirmed_at": "2099-01-01T00:00:00+08:00", "children": [{"child": c} for c in p["children"]]}
        return self.saved

    def list_saved(self):
        return [self.saved] if self.saved else []

    def read(self, batch_id):
        assert batch_id == self.saved["batch_id"]
        return self.saved


def ready():
    master = Master()
    ws = Workspace()
    ws.set_input("CMT1", SOURCE)
    ws.choices, _ = choose_required(SOURCE, master.cat)
    ws.generate(master)
    assert ws.result["frozen"]
    return ws, master


def test_unselected_never_reads_parses_or_prices():
    master = Mock()
    with pytest.raises(ValueError):
        Workspace(source=SOURCE).generate(master)
    master.catalog.assert_not_called()


@pytest.mark.parametrize("change", ["category", "source", "choice"])
def test_input_or_choice_invalidates_fixed_save(change):
    ws, master = ready()
    if change == "category":
        ws.set_input("ZZ", SOURCE)
    elif change == "source":
        ws.set_input("CMT1", SOURCE + "\n未知工序")
    else:
        key = next(iter(ws.choices))
        ws.choose(key, None)
    assert ws.result is None
    store = Mock()
    with pytest.raises(ValueError):
        ws.confirm(store)
    store.save.assert_not_called()


def test_confirmation_fixed_no_master_price_query_and_retry():
    ws, master = ready()
    master.catalog = Mock(side_effect=AssertionError("must not query"))
    store = Store()
    a = ws.confirm(store)
    assert a == ws.confirm(store)
    assert len(a["ref_nos"]) == 4
    master.catalog.assert_not_called()


def test_tampered_source_without_set_input_still_cannot_save():
    ws, _ = ready()
    ws.source += "\n變更"
    with pytest.raises(ValueError, match="內容已變更"):
        ws.confirm(Store())


def test_choose_never_calculates(monkeypatch):
    ws, _ = ready()
    monkeypatch.setattr("local_quote.business._calculate", Mock(side_effect=AssertionError("must not calculate")))
    key = next(iter(ws.choices))
    ws.choose(key, ws.choices[key])
    assert ws.result is None and ws.analysis


def app(monkeypatch):
    master, store = Master(), Store()
    monkeypatch.setattr(ui, "services", lambda: (master, store))
    a = AppTest.from_string("from local_quote.ui import main\nmain()", default_timeout=20).run()
    assert not a.exception
    return a, master, store


def button(a, label):
    return next(b for b in a.button if b.label == label)


def fill(a):
    a.selectbox(key="local_category").select("CMT1").run()
    a.text_area(key="local_source").input(SOURCE).run()
    button(a, "產生四張成本明細").click().run()
    for _ in range(30):
        pending = [s for s in a.selectbox if s.key.startswith("local_choice:") and s.value is None]
        if not pending:
            break
        # 模擬人工點合法候選，只存在於測試，不是 runtime 預選。
        gap_key = pending[0].key.split(":", 2)[2]
        q = next(q for q in a.session_state.local_workspace.analysis["questions"] if q["key"] == gap_key)
        pending[0].select(q["candidates"][0]["value"]).run()
        assert not a.exception
    assert not [s for s in a.selectbox if s.key.startswith("local_choice:") and s.value is None]
    button(a, "產生四張成本明細").click().run()
    assert not a.exception


def test_ui_category_before_source_and_not_preselected(monkeypatch):
    a, master, store = app(monkeypatch)
    assert a.selectbox[0].value is None
    assert "資料庫新類別（ZZ）" in a.selectbox[0].options
    assert a.text_area[0].disabled
    assert master.calls == 0 and store.calls == 0
    assert not any(b.label == "確認並完成報價" for b in a.button)


def test_ui_four_cost_tables_then_one_confirmation_and_readonly_history(monkeypatch):
    a, master, store = app(monkeypatch)
    fill(a)
    assert store.calls == 0
    assert len(a.tabs) == 4
    assert len([d for d in a.dataframe if len(d.value.columns) == 12]) == 4
    assert all(d.value.iloc[0, 0] == "00001" for d in a.dataframe if len(d.value.columns) == 12)
    queries = master.calls
    button(a, "確認並完成報價").click().run()
    assert not a.exception and store.calls == 1
    assert master.calls == queries
    assert not any(b.label == "確認並完成報價" for b in a.button)
    button(a, "載入已保存批次").click().run()
    next(s for s in a.selectbox if s.label == "已保存批次").select(store.saved).run()
    button(a, "查回四張").click().run()
    assert not a.exception and master.calls == queries and store.calls == 1


def test_ui_switch_category_removes_confirm(monkeypatch):
    a, master, store = app(monkeypatch)
    fill(a)
    a.selectbox(key="local_category").select("ZZ").run()
    assert not a.exception
    assert not any(b.label == "確認並完成報價" for b in a.button)
    assert a.session_state.local_workspace.result is None and store.calls == 0


def test_ui_failed_save_retains_same_preview_for_retry(monkeypatch):
    a, master, store = app(monkeypatch)
    fill(a)
    before = a.session_state.local_workspace.result["frozen"]
    original = store.save
    calls = []

    def fail_once(frozen, **kwargs):
        calls.append(frozen)
        if len(calls) == 1:
            raise ValueError("隔離測試暫時故障")
        return original(frozen, **kwargs)

    store.save = fail_once
    queries = master.calls
    button(a, "確認並完成報價").click().run()
    assert not a.exception and a.session_state.local_workspace.result["frozen"] == before
    button(a, "確認並完成報價").click().run()
    assert not a.exception and calls == [before, before] and master.calls == queries


def test_new_cost_rows_readable_by_legacy_repository(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import repository
    from local_quote.backend import LocalDatabase, Settings
    from local_quote.storage import BatchStore
    from test_local_storage import fixture_schema
    engine = create_engine("sqlite:///:memory:")
    try:
        fixture_schema(engine)
        db = LocalDatabase(Settings("mysql+pymysql://fixture:fixture@localhost/isolated", "103"),
                           engine=engine, testing=True)
        ws, _ = ready()
        saved = ws.confirm(BatchStore(db))
        monkeypatch.setattr(repository, "_session", sessionmaker(bind=engine))
        for doc in saved["children"]:
            rows = repository.get_quote_snapshot(doc["ref_no"], workgroup="103")
            items = doc["child"]["calc"]["items"]
            assert len(rows) == len(items)
            for actual, expected in zip(rows, items):
                assert actual["path"] == expected["path"]
                assert actual["spc_code"] == expected["spc_code"]
                assert actual["qty"] == float(expected["qty"])
    finally:
        engine.dispose()


def test_ui_original_missing_keeps_all_four_no_save(monkeypatch):
    a, master, store = app(monkeypatch)
    a.selectbox(key="local_category").select("CMT1").run()
    a.text_area[0].input(ORIGINAL).run()
    button(a, "產生四張成本明細").click().run()
    assert not a.exception and len(a.tabs) == 4
    assert [c["product_qty"] for c in a.session_state.local_workspace.analysis["children"]] == [2, 1, 6, 2]
    assert not any(b.label == "確認並完成報價" for b in a.button)
    assert store.calls == 0


def test_launcher_mode_before_acceptance_import_and_fresh_process(monkeypatch):
    import four_quote_acceptance as launcher
    original_launch = bootstrap.launch
    launch = Mock(return_value=0)
    monkeypatch.setattr(bootstrap, "launch", launch)
    assert launcher.main(["--local-db"]) == 0
    launch.assert_called_once_with(worker=False)
    for args in (["--local-db", "--initialize"], ["--local-db", "--directory", "x"], []):
        with pytest.raises(SystemExit):
            launcher.main(args)
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    monkeypatch.setenv("OPENAI_API_KEY", "canary")
    monkeypatch.setenv("DB_CONN_STR", "canary")
    assert original_launch() == 0
    args, kwargs = run.call_args
    assert "--local-db" in args[0] and "-I" in args[0]
    assert kwargs["env"]["MAPBOX_API_KEY"] == ""
    assert not any(k in kwargs["env"] for k in ("DB_CONN_STR", "OPENAI_API_KEY"))


def test_launcher_import_has_no_acceptance_or_database_side_effects():
    code = "import four_quote_acceptance,sys; assert not any(n in sys.modules for n in ('acceptance.bootstrap','config','database.connection','local_quote.backend'))"
    result = subprocess.run(["./venv/bin/python", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
