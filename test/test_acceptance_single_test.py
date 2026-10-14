"""單次測試契約；保留舊兩次契約，真實配號與原子保存不替換答案。"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_acceptance_workflow import worker
from test_acceptance_ui import worker as ui_worker


@pytest.fixture
def directory(tmp_path):
    target = tmp_path.resolve() / "single-test"
    worker(target, "assert not w.list_batches()", initialize=True)
    return target


def test_single_event_fixed_four_no_repricing_or_formal_write(directory):
    worker(directory, '''
from dataclasses import asdict
import sqlite3
v = w.generate(b.SAMPLE)
assert v["confirmation_mode"] == b.TEST_CONFIRMATION
assert v["context"].actor == "本機測試"
assert not v["context"].accepted_assumptions and v["context"].source_confirmed is False
assert v["stage"] == 0 and v["next_stage"] is None and not v["confirmations"]
source = v["document"]["fixed_request"]["source"]
assert "已明確接受合成示範設定" not in source["history_json"]
assert "自動套用測試預設合成設定" in source["history_json"]
def forbidden(*args, **kwargs):
    raise AssertionError("不得重算、查價、正式保存或假造兩次確認")
b.build_request = b._Master = forbidden
w.confirm = w.save = forbidden
from engine import quote_batch_trial
quote_batch_trial._calculation = forbidden
from database import repository
repository._session = repository.save_quote_snapshot = forbidden
assert w.generate(b.SAMPLE)["id"] == v["id"]
assert w.generate(b.SAMPLE, active_batch=v["id"])["document"] == v["document"]
rejected(lambda: w.complete_test(v["id"], "wrong", v["context"]))
rejected(lambda: w.complete_test(v["id"], v["token"], asdict(v["context"])))
assert not w.retrieve(v["id"])["confirmations"]
saved = w.complete_test(v["id"], v["token"], v["context"])
assert saved["document"] == v["document"]
assert saved["saved"]["fixed_request"] == v["document"]["fixed_request"]
assert [r["event"] for r in saved["confirmations"]] == ["確認並測試保存"]
assert saved["stage"] == 1 and saved["phase"] == "SAVED"
assert w.complete_test(v["id"], v["token"], v["context"]) == saved
assert w.retrieve(v["id"]) == saved
assert len(w.child_documents(v["id"])) == 4
for name in ("master.sqlite", "workflow.sqlite", "snapshot.sqlite"):
    with sqlite3.connect(directory / name) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='ordqdt_ai'").fetchall()
''')


def test_modes_cannot_use_each_others_confirmation_api(directory):
    worker(directory, '''
old = create()
rejected(lambda: w.complete_test(old["id"], old["token"], old["context"]))
assert w.retrieve(old["id"])["stage"] == 0
new = w.generate(b.SAMPLE)
rejected(lambda: w.confirm(new["id"], 1, new["token"], new["context"]))
rejected(lambda: w.save(new["id"], new["context"]))
assert w.retrieve(new["id"])["stage"] == 0
rejected(lambda: w.create(b.SAMPLE, **options(), confirmation_mode=b.TEST_CONFIRMATION))
rejected(lambda: w.create(b.SAMPLE, **options(), confirmation_mode="unknown"))
old = confirmed(old)
saved = w.save(old["id"], old["context"])
assert len(saved["confirmations"]) == 2 and saved["confirmation_mode"] == b.LEGACY_CONFIRMATION
assert len(w.list_batches()) == 2
''')


@pytest.mark.parametrize("field", ["raw", "discount"])
def test_changed_input_and_switch_back_never_revive_old_confirmation(directory, field):
    worker(directory, f'''
v = w.generate(b.SAMPLE)
original_save = w._snapshots.save
def fail(request):
    raise RuntimeError("fail")
w._snapshots.save = fail
rejected(lambda: w.complete_test(v["id"], v["token"], v["context"]), WorkflowOperationError)
raw = b.SAMPLE + "\\n" if {field!r} == "raw" else b.SAMPLE
discount = "0.05" if {field!r} == "discount" else "0"
assert not w.synchronize_context(v["id"], w.test_context(raw, discount))
assert not w.synchronize_context(v["id"], v["context"])
old = w.retrieve(v["id"])
assert old["stage"] == 0 and len(old["revoked_confirmations"]) == 1
rejected(lambda: w.complete_test(v["id"], v["token"], v["context"]))
new = w.generate(raw, discount_rate=discount, active_batch=v["id"])
assert new["predecessor"] == v["id"] and new["version"] == 1
assert not new["confirmations"] and new["token"] != v["token"]
rejected(lambda: w.complete_test(new["id"], v["token"], new["context"]))
w._snapshots.save = original_save
saved = w.complete_test(new["id"], new["token"], new["context"])
assert len(saved["confirmations"]) == 1
# 回切原文自動建立下一組；以舊歷史為入口也會找到最新後繼。
third = w.generate(b.SAMPLE, active_batch=v["id"])
assert third["predecessor"] == new["id"] and third["stage"] == 0
assert w.retrieve(new["id"])["saved"] == saved["saved"]
''')


@pytest.mark.parametrize("point", ["before_save", "after_save"])
def test_process_death_after_one_event_restarts_same_identity(directory, point):
    worker(directory, f'''
v = w.generate(b.SAMPLE)
original = w._snapshots.save
def die(request):
    if {point!r} == "after_save":
        original(request)
    os._exit(71)
w._snapshots.save = die
w.complete_test(v["id"], v["token"], v["context"])
''', expected=71)
    worker(directory, '''
def deny(*args, **kwargs):
    raise AssertionError("不得重算")
b.build_request = b._Master = deny
v = w.retrieve(w.list_batches()[0]["id"])
assert len(v["confirmations"]) == 1
saved = w.complete_test(v["id"], v["confirmations"][0]["token"], v["context"])
assert saved["phase"] == "SAVED" and len(saved["confirmations"]) == 1
assert len(saved["saved"]["fixed_request"]["children"]) == 4
assert len(w.list_batches()) == 1
''')


def test_parallel_generation_completion_and_double_click_keep_one_group(directory):
    body = 'v = w.generate(b.SAMPLE); emit({"id": v["id"], "token": v["token"]})'
    with ThreadPoolExecutor(max_workers=2) as pool:
        views = list(pool.map(lambda _: worker(directory, body), range(2)))
    assert views[0] == views[1]
    view = views[0]
    body = f'''
def deny(*args, **kwargs):
    raise AssertionError("不得重新查價")
b.build_request = b._Master = deny
v = w.retrieve({view['id']!r})
saved = w.complete_test(v["id"], {view['token']!r}, v["context"])
emit({{"digest": saved["saved"]["final_digest"], "receipts": saved["confirmations"]}})
'''
    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = list(pool.map(lambda _: worker(directory, body), range(2)))
    assert saved[0] == saved[1]
    assert len(saved[0]["receipts"]) == 1
    worker(directory, '''
assert len(w.list_batches()) == 1
with w._engine.connect() as db:
    assert db.exec_driver_sql("SELECT count(*) FROM development_atomic_batch").scalar() == 1
    assert db.exec_driver_sql("SELECT count(*) FROM development_atomic_child").scalar() == 4
    assert db.exec_driver_sql("SELECT count(*) FROM quote_number_reservation").scalar() == 1
''')


def test_same_process_threads_complete_one_event(directory):
    worker(directory, '''
from concurrent.futures import ThreadPoolExecutor
v = w.generate(b.SAMPLE)
with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(lambda _: w.complete_test(v["id"], v["token"], v["context"]), range(2)))
assert results[0] == results[1]
assert len(results[0]["confirmations"]) == 1
''')


def test_prepare_concurrent_new_empty_and_existing_store_never_overwrites(tmp_path):
    target = tmp_path.resolve() / "prepare"
    ui_worker(target, 'assert not tables("master.sqlite") and not tables("workflow.sqlite")')
    body = '''
workflow = Workflow.prepare(directory)
assert workflow.list_batches() == []
workflow.close()
emit(hashlib.sha256((directory / "master.sqlite").read_bytes()).hexdigest())
'''
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ui_worker(target, body, initialize=False), range(2)))
    assert results[0] == results[1]
    ui_worker(target, '''
workflow = Workflow(directory)
v = workflow.generate(business.SAMPLE)
workflow.complete_test(v["id"], v["token"], v["context"])
workflow.close()
before = {p.name: p.read_bytes() for p in directory.iterdir()}
workflow = Workflow.prepare(directory)
workflow.close()
assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
''', initialize=False)


@pytest.mark.parametrize("name", ["master.sqlite", "workflow.sqlite"])
def test_prepare_unknown_nonempty_database_is_not_repaired(tmp_path, name):
    ui_worker(tmp_path.resolve() / "unknown", f'''
with closing(sqlite3.connect(directory / {name!r})) as db:
    db.execute("CREATE TABLE unknown (value TEXT)")
    db.execute("INSERT INTO unknown VALUES ('keep')")
    db.commit()
before = (directory / {name!r}).read_bytes()
try:
    Workflow.prepare(directory)
except ValueError:
    pass
else:
    raise AssertionError("不可修復未知結構")
assert (directory / {name!r}).read_bytes() == before
''')


@pytest.mark.parametrize("saved", [False, True])
def test_restart_legacy_history_readable_and_new_ui_never_fakes_old_events(tmp_path, saved):
    target = tmp_path.resolve() / "legacy"
    old = worker(target, f'''
v = confirmed(create())
if {saved!r}:
    v = w.save(v["id"], v["context"])
emit({{"id": v["id"], "document": v["document"], "confirmations": v["confirmations"]}})
''', initialize=True)
    ui_worker(target, f'''
no_pricing = True
app = clean(app_start())
v = history(app, {old['id']!r})
assert v["document"] == {old['document']!r}
assert v["confirmations"] == {old['confirmations']!r}
assert v["confirmation_mode"] == "legacy-two-step"
verified_tables(app)
assert "save" not in {{b.key for b in app.button}}
assert not {{"confirm_1", "confirm_2"}} & {{b.key for b in app.button}}
assert all(count == 0 for count in counts.values())
''', initialize=False)
