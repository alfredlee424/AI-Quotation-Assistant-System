"""從 business.SAMPLE 到持久流程的真實整合測試，不使用既有 typed fixture。"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "venv/bin/python"
WORKER = ROOT / "test/acceptance_worker.py"


def worker(directory, body, *, initialize=False, expected=0):
    result = subprocess.run([str(PYTHON), "-I", "-B", str(WORKER),
                             "initialize" if initialize else "open", str(directory)],
                            input=textwrap.dedent(body), cwd=ROOT,
                            env={"PATH": "/usr/bin:/bin", "PYTHON_DOTENV_DISABLED": "1",
                                 "PYTHONDONTWRITEBYTECODE": "1"},
                            text=True, capture_output=True, timeout=90)
    assert result.returncode == expected, result.stdout + result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


@pytest.fixture
def directory(tmp_path):
    target = tmp_path.resolve() / "private"
    worker(target, "", initialize=True)
    return target


def test_full_flow_source_numbers_confirmations_and_self_contained_documents(directory):
    worker(directory, '''
from decimal import Decimal
from dataclasses import asdict
v = create()
doc = v["document"]
assert v["phase"] == "PREVIEW" and v["stage"] == 0
assert doc["fixed_request"]["source"]["raw_text"] == b.SAMPLE
children = doc["children"]
assert [c["fixed_child"]["product_qty"] for c in children] == [2, 1, 6, 2]
assert [c["suffix"] for c in children] == ["001", "002", "003", "004"]
assert len({c["ref_no"].rsplit("_", 1)[0] for c in children}) == 1
assert all(c["fixed_child"]["rows"][0]["seq_no"] == "00001" for c in children)
totals = json.loads(doc["fixed_request"]["totals_json"])
for key in totals:
    assert Decimal(totals[key]) == sum(Decimal(c["fixed_child"]["amounts"][key]) for c in children)
rejected(lambda: w.save(v["id"], v["context"]))
rejected(lambda: w.confirm(v["id"], 2, v["token"], v["context"]))
rejected(lambda: w.confirm(v["id"], True, v["token"], v["context"]))
rejected(lambda: w.confirm(v["id"], 1, "bad-token", v["context"]))
first = w.confirm(v["id"], 1, v["token"], v["context"])
rejected(lambda: w.save(v["id"], v["context"]))
rejected(lambda: w.confirm(v["id"], 1, v["token"], v["context"]))
rejected(lambda: w.confirm(v["id"], 2, v["token"], v["context"]))
second = w.confirm(v["id"], 2, first["token"], v["context"])
assert [r["stage"] for r in second["confirmations"]] == [1, 2]
assert len({r["preview_digest"] for r in second["confirmations"]}) == 1
saved = w.save(v["id"], v["context"])
assert saved["saved"]["fixed_request"] == doc["fixed_request"]
assert saved["document"] == doc
assert w.save(v["id"], v["context"]) == saved == w.retrieve(v["id"])
assert create() == saved
documents = w.child_documents(v["id"])
assert len(documents) == 4
for child in documents:
    d = child["structured"]
    assert b.WARNING in child["text"] and b.SAMPLE in child["text"]
    assert d["source"]["raw_text"] == b.SAMPLE
    assert d["fixed_child"]["rows"] and d["fixed_child"]["selections"]
    assert d["source_history"]["formal_gaps"]
    assert not any(g["formal_approved"] for g in d["source_history"]["formal_gaps"])
    assert not any(d[k] for k in ("can_quote", "can_confirm", "can_save", "formal_approved", "importable"))
    rejected(lambda: w.create(d, **options()))
    rejected(lambda: w.create(child["text"], **options()))
    rejected(lambda: w.save(d, v["context"]))
    rejected(lambda: w.confirm(v["id"], 1, "", asdict(v["context"])))
assert len(w.list_batches()) == 1 and w.list_batches()[0]["phase"] == "SAVED"
verify_directory(directory)
''')


def test_same_context_dedupe_and_history_do_not_rebuild_or_reprice(directory):
    record = worker(directory, '''
v = confirmed(create())
saved = w.save(v["id"], v["context"])
emit({"id": v["id"], "saved": saved["saved"], "documents": w.child_documents(v["id"])})
''')
    worker(directory, f'''
expected = {record!r}
def forbidden(*args, **kwargs):
    raise AssertionError("歷史、重繪、確認或重試不得查價／配號")
b.build_request = b._Master = b._configuration = forbidden
w._registry.reserve = forbidden
assert w.retrieve(expected["id"])["saved"] == expected["saved"]
assert w.preview(expected["id"])["saved"] == expected["saved"]
assert w.child_documents(expected["id"]) == expected["documents"]
v = create()
assert v["id"] == expected["id"] and v["saved"] == expected["saved"]
assert w.save(v["id"], v["context"])["saved"] == expected["saved"]
assert w.synchronize_context(v["id"], v["context"])
assert len(w.list_batches()) == 1
''')


def test_confirmation_download_and_preview_never_reprice(directory):
    worker(directory, '''
v = create()
def forbidden(*args, **kwargs):
    raise AssertionError("不得查價或建構新內容")
b.build_request = b._Master = b._configuration = forbidden
from engine import quote_batch_trial
quote_batch_trial._calculation = forbidden
w._registry.reserve = forbidden
assert w.preview(v["id"])["document"] == v["document"]
assert w.child_documents(v["id"])
v = confirmed(v)
assert w.save(v["id"], v["context"])["saved"]
''')


@pytest.mark.parametrize("mutation", [
    'raw_text=c.raw_text + "\\n"',
    'raw_text=c.raw_text.replace("70X120", "70X121")',
    'raw_text="\\n".join(c.raw_text.split("\\n")[:2] + [c.raw_text.split("\\n")[3], c.raw_text.split("\\n")[2]] + c.raw_text.split("\\n")[4:])',
    'raw_text=c.raw_text.replace("腳粒鎖好", "腳粒未鎖")',
    'actor="另一合成操作人"',
    'discount_rate="0.1"',
    'accepted_assumptions=c.accepted_assumptions[:-1]',
    'source_confirmed=False',
    'business_version="other-version"',
    'master_version="other-settings"',
])
def test_context_mutation_permanently_revokes_both_confirmations(directory, mutation):
    worker(directory, f'''
from dataclasses import replace
v = confirmed(create())
c = v["context"]
changed = replace(c, {mutation})
assert not w.synchronize_context(v["id"], changed)
assert not w.synchronize_context(v["id"], c)
old = w.preview(v["id"])
assert old["invalidated"] and old["stage"] == 0 and old["token"] is None
assert len(old["revoked_confirmations"]) == 2
rejected(lambda: w.save(v["id"], c))
rejected(lambda: w.confirm(v["id"], 1, v["token"], c))
rejected(lambda: create())
assert w.child_documents(v["id"])[0]["structured"]["invalidated"]
''')


@pytest.mark.parametrize("operation", ["confirm", "save"])
def test_stale_context_in_operation_is_persistently_invalidated(directory, operation):
    worker(directory, f'''
from dataclasses import replace
v = confirmed(create())
c = replace(v["context"], actor="異動操作者")
if {operation!r} == "save":
    rejected(lambda: w.save(v["id"], c))
else:
    rejected(lambda: w.confirm(v["id"], 1, "old", c))
w.close()
w = Workflow(directory)
assert w.preview(v["id"])["invalidated"]
rejected(lambda: w.save(v["id"], v["context"]))
''')


def test_new_version_has_new_identity_trace_and_non_recycled_numbers(directory):
    worker(directory, '''
v = confirmed(create())
old = w.save(v["id"], v["context"])
new = create(discount_rate="0.1", new_version_of=v["id"])
assert new["version"] == 1 and new["predecessor"] == v["id"]
assert all(new["identity"][key] != v["identity"][key] for key in ("batch_id", "request_id", "preview_identity", "save_id"))
assert new["stage"] == 0 and new["token"] != v["token"]
history = json.loads(new["document"]["fixed_request"]["source"]["history_json"])
assert history["input_context"]["predecessor"] == v["identity"]["request_id"]
assert history["input_context"]["revision"] == 1
assert new["document"]["children"][0]["ref_no"] != old["document"]["children"][0]["ref_no"]
assert w.retrieve(v["id"])["saved"] == old["saved"]
assert w.retrieve(v["id"])["invalidated"]
rejected(lambda: w.save(v["id"], v["context"]))
assert create(discount_rate="0.1", new_version_of=v["id"])["id"] == new["id"]
rejected(lambda: create(discount_rate="0", new_version_of=v["id"]))
new = confirmed(new)
assert w.save(new["id"], new["context"])["saved"]
assert len(w.list_batches()) == 2
''')


def test_failed_new_version_build_also_revokes_old_confirmations(directory):
    worker(directory, '''
v = confirmed(create())
rejected(lambda: create(raw=b.SAMPLE.replace("70X120", "70X121"), new_version_of=v["id"]))
assert w.preview(v["id"])["invalidated"]
assert len(w.list_batches()) == 1
rejected(lambda: create())
new = create(new_version_of=v["id"])
assert new["version"] == 1 and new["id"] != v["id"]
''')


@pytest.mark.parametrize("table,condition", [
    ("development_atomic_child", "NEW.position=2"),
    ("development_atomic_child", "NEW.position=4"),
    ("development_atomic_source_link", "NEW.position=2"),
    ("development_atomic_source_link", "NEW.position=4"),
    ("development_atomic_cost", "NEW.position=2"),
])
def test_atomic_save_failure_has_no_half_batch_and_keeps_reservation(directory, table, condition):
    worker(directory, f'''
from contextlib import closing
import sqlite3
v = confirmed(create())
reservation = v["document"]["reservation"]
with closing(sqlite3.connect(directory / "snapshot.sqlite")) as db:
    db.execute("CREATE TRIGGER injected_failure BEFORE INSERT ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    db.commit()
rejected(lambda: w.save(v["id"], v["context"]), WorkflowOperationError)
assert w.retrieve(v["id"])["phase"] == "SAVE_FAILED"
assert w.list_batches()[0]["phase"] == "SAVE_FAILED"
with closing(sqlite3.connect(directory / "snapshot.sqlite")) as db:
    for name in ("development_atomic_batch", "development_atomic_child", "development_atomic_source_link", "development_atomic_cost"):
        assert db.execute("SELECT count(*) FROM " + name).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM quote_number_reservation").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM quote_number_reserved_child").fetchone()[0] == 4
    db.execute("DROP TRIGGER injected_failure")
    db.commit()
w.close()
w = Workflow(directory)
retry = w.save(v["id"], v["context"])
assert retry["saved"]["reservation"] == reservation
assert retry["document"] == v["document"]
''')


@pytest.mark.parametrize("phase", ["reserve_before", "reserve_after", "save_after"])
def test_response_lost_retry_preserves_committed_identity_and_result(directory, phase):
    worker(directory, f'''
from contextlib import closing
import sqlite3
if {phase!r}.startswith("reserve"):
    original = w._registry.reserve
    def lost(request):
        with closing(sqlite3.connect(directory / "workflow.sqlite")) as db:
            # registry 尚未執行就能從另一連線讀到已提交的完整持久身份。
            row = db.execute("SELECT immutable FROM acceptance_workflow_batch WHERE id=?", (request.batch_id,)).fetchone()
            assert row and json.loads(row[0])["identity"]["request_id"] == request.request_id
        if {phase!r} == "reserve_after":
            original(request)
        raise OSError("synthetic response lost")
    w._registry.reserve = lost
    rejected(lambda: create(), WorkflowOperationError)
    batch = w.list_batches()[0]
    assert batch["phase"] == ("RESERVATION_FAILED" if {phase!r} == "reserve_before" else "PREVIEW")
    identity = batch["identity"]
    w._registry.reserve = original
    b.build_request = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("重试不能新建"))
    v = create()
    assert v["identity"] == identity
    assert len(w.list_batches()) == 1
else:
    v = confirmed(create())
    original = w._snapshots.save
    committed = []
    def lost(request):
        committed.append(original(request))
        raise OSError("synthetic save response lost")
    w._snapshots.save = lost
    rejected(lambda: w.save(v["id"], v["context"]), WorkflowOperationError)
    w._snapshots.save = lambda *args: (_ for _ in ()).throw(AssertionError("已保存只應查回"))
    recovered = w.save(v["id"], v["context"])
    assert recovered["saved"] == committed[0]
    assert create()["saved"] == committed[0]
''')


@pytest.mark.parametrize("phase", ["before_reserve", "after_reserve", "after_save"])
def test_process_death_and_restart_reconcile_original_request(directory, phase):
    worker(directory, f'''
if {phase!r} in ("before_reserve", "after_reserve"):
    original = w._registry.reserve
    def crash(request):
        if {phase!r} == "after_reserve":
            original(request)
        os._exit(71)
    w._registry.reserve = crash
    create()
else:
    v = confirmed(create())
    original = w._snapshots.save
    def crash(request):
        original(request)
        os._exit(71)
    w._snapshots.save = crash
    w.save(v["id"], v["context"])
''', expected=71)
    record = worker(directory, '''
b.build_request = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("重啟不得新建身份"))
listing = w.list_batches()
assert len(listing) == 1
v = create()
assert v["id"] == listing[0]["id"]
if v["stage"] == 0:
    v = confirmed(v)
v = w.save(v["id"], v["context"])
emit({"id": v["id"], "saved": v["saved"], "created_at": v["created_at"], "doc": v["document"]})
''')
    worker(directory, f'''
expected = {record!r}
v = w.retrieve(expected["id"])
assert v["saved"] == expected["saved"]
assert v["created_at"] == expected["created_at"] and v["document"] == expected["doc"]
assert create()["saved"] == expected["saved"]
''')


def test_parallel_create_confirmation_cas_and_save_across_processes(directory):
    body = 'v = create(); emit({"id": v["id"], "document": v["document"], "token": v["token"]})'
    with ThreadPoolExecutor(max_workers=3) as pool:
        created = list(pool.map(lambda _: worker(directory, body), range(3)))
    assert created[0] == created[1] == created[2]
    batch_id = created[0]["id"]
    token = created[0]["token"]
    for stage in (1, 2):
        body = f'''
v = w.preview({batch_id!r})
try:
    v = w.confirm(v["id"], {stage}, {token!r}, v["context"])
    emit({{"accepted": True, "token": v["token"]}})
except WorkflowError:
    emit({{"accepted": False}})
'''
        with ThreadPoolExecutor(max_workers=2) as pool:
            confirmations = list(pool.map(lambda _: worker(directory, body), range(2)))
        assert sum(result["accepted"] for result in confirmations) == 1
        token = next(result["token"] for result in confirmations if result["accepted"])
        worker(directory, f'assert w.preview({batch_id!r})["stage"] == {stage}')
    body = f'v = w.preview({batch_id!r}); emit(w.save(v["id"], v["context"])["saved"])'
    with ThreadPoolExecutor(max_workers=3) as pool:
        saved = list(pool.map(lambda _: worker(directory, body), range(3)))
    assert saved[0] == saved[1] == saved[2]
    worker(directory, 'assert len(w.list_batches()) == 1')


def test_parallel_reservation_retry_uses_same_persisted_request(directory):
    batch = worker(directory, '''
w._registry.reserve = lambda *args: (_ for _ in ()).throw(OSError("synthetic"))
rejected(lambda: create(), WorkflowOperationError)
emit(w.list_batches()[0])
''')
    body = f'''
b.build_request = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不可重建"))
v = create()
assert v["identity"] == {batch["identity"]!r}
emit(v["document"])
'''
    with ThreadPoolExecutor(max_workers=3) as pool:
        result = list(pool.map(lambda _: worker(directory, body), range(3)))
    assert result[0] == result[1] == result[2]


def test_unknown_identity_documents_and_typed_requests_are_rejected(directory):
    worker(directory, '''
v = create()
for operation in (w.preview, w.resume, w.retrieve, w.child_documents):
    rejected(lambda: operation("0" * 32))
    rejected(lambda: operation(v["document"]))
rejected(lambda: w.create(b.build_request(directory, b.SAMPLE, **options()), **options()))
rejected(lambda: create(new_version_of="0" * 32))
assert len(w.list_batches()) == 1
''')


@pytest.mark.parametrize("mutation", [
    "UPDATE acceptance_workflow_batch SET state='{}'",
    "UPDATE acceptance_workflow_batch SET state_digest='wrong'",
    "UPDATE acceptance_workflow_meta SET count=0",
    "DELETE FROM acceptance_workflow_meta",
    "DROP TABLE acceptance_workflow_batch",
    "DROP TRIGGER acceptance_workflow_immutable",
])
def test_corrupt_workflow_never_recreates_or_repairs_identity(directory, mutation):
    worker(directory, f'''
from contextlib import closing
import sqlite3
v = create()
with closing(sqlite3.connect(directory / "workflow.sqlite")) as db:
    db.execute({mutation!r})
    db.commit()
for operation in (lambda: create(), lambda: w.resume(v["id"]), lambda: w.list_batches(), lambda: Workflow(directory)):
    rejected(operation)
with closing(sqlite3.connect(directory / "snapshot.sqlite")) as db:
    assert db.execute("SELECT count(*) FROM quote_number_reservation").fetchone()[0] == 1
''')


def test_immutable_identity_payload_cannot_be_updated_or_deleted(directory):
    worker(directory, '''
from contextlib import closing
import sqlite3
v = create()
with closing(sqlite3.connect(directory / "workflow.sqlite")) as db:
    for sql in ("UPDATE acceptance_workflow_batch SET id='changed'",
                "UPDATE acceptance_workflow_batch SET immutable='{}'",
                "UPDATE acceptance_workflow_batch SET context_hash='changed'",
                "DELETE FROM acceptance_workflow_batch"):
        rejected(lambda: db.execute(sql), sqlite3.IntegrityError)
assert w.preview(v["id"])["document"] == v["document"]
''')


@pytest.mark.parametrize("mutation", [
    "UPDATE quote_number_reserved_child SET reserved_ref_no='tampered' WHERE position=2",
    "DELETE FROM quote_number_reserved_child WHERE position=4",
    "DELETE FROM quote_number_reserved_child; DELETE FROM quote_number_reservation",
    "UPDATE development_atomic_child SET payload='{}' WHERE position=4",
    "DELETE FROM development_atomic_source_link WHERE position=2",
    "UPDATE development_atomic_batch SET request_id='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'",
])
def test_corrupt_reservation_or_saved_documents_are_not_repaired(directory, mutation):
    worker(directory, f'''
from contextlib import closing
import sqlite3
v = confirmed(create())
w.save(v["id"], v["context"])
with closing(sqlite3.connect(directory / "snapshot.sqlite")) as db:
    db.execute("PRAGMA foreign_keys=OFF")
    db.executescript({mutation!r})
    db.commit()
for operation in (lambda: create(), lambda: w.resume(v["id"]), lambda: w.save(v["id"], v["context"]), lambda: w.child_documents(v["id"])):
    rejected(operation)
''')


def test_explicit_schema_initialization_preserves_bootstrap_identity(tmp_path):
    # 不透過 worker 自動初始化流程，測試建構子不默默補表。
    body = '''
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from acceptance.bootstrap import initialize_directory, verify_directory
from acceptance.workflow import Workflow, WorkflowError
directory = initialize_directory(sys.argv[1])
before = (directory / "manifest.json").read_bytes()
try:
    Workflow(directory)
except WorkflowError:
    pass
else:
    raise AssertionError("不應隱式建流程表")
w = Workflow.initialize(directory)
assert not w.list_batches()
assert {p.name for p in directory.iterdir()} == {".four-quote-acceptance", "manifest.json", "master.sqlite", "snapshot.sqlite", "workflow.sqlite"}
assert (directory / "manifest.json").read_bytes() == before
verify_directory(directory)
try:
    Workflow.initialize(directory)
except WorkflowError:
    pass
else:
    raise AssertionError("不應覆寫流程表")
w.close()
'''
    result = subprocess.run([str(PYTHON), "-I", "-B", "-c", textwrap.dedent(body), str(tmp_path.resolve() / "private")],
                            cwd=ROOT, env={"PATH": "/usr/bin:/bin", "PYTHON_DOTENV_DISABLED": "1"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_new_version_recovers_saved_predecessor_after_lost_response(directory):
    worker(directory, '''
v = confirmed(create())
original = w._snapshots.save
committed = []
def lost(request):
    committed.append(original(request))
    raise OSError("synthetic response lost")
w._snapshots.save = lost
rejected(lambda: w.save(v["id"], v["context"]), WorkflowOperationError)
new = create(new_version_of=v["id"])
assert new["id"] != v["id"] and new["version"] == 1
old = w.retrieve(v["id"])
assert old["invalidated"] and old["saved"] == committed[0]
assert old["phase"] == "SAVED" and len(old["revoked_confirmations"]) == 2
assert w.child_documents(v["id"])
''')


def test_parallel_explicit_new_version_has_one_successor(directory):
    old = worker(directory, 'emit(create()["id"])')
    body = f'v = create(new_version_of={old!r}); emit({{"id": v["id"], "document": v["document"]}})'
    with ThreadPoolExecutor(max_workers=3) as pool:
        versions = list(pool.map(lambda _: worker(directory, body), range(3)))
    assert versions[0] == versions[1] == versions[2]
    worker(directory, f'''
assert len(w.list_batches()) == 2
assert w.preview({old!r})["invalidated"]
assert w.preview({versions[0]["id"]!r})["version"] == 1
''')


def test_same_process_thread_create_and_confirmation_are_serialized(directory):
    worker(directory, '''
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=3) as pool:
    views = list(pool.map(lambda _: create(), range(3)))
assert views[0] == views[1] == views[2]
v = views[0]
def attempt(_):
    try:
        return w.confirm(v["id"], 1, v["token"], v["context"])["stage"]
    except WorkflowError:
        return 0
with ThreadPoolExecutor(max_workers=3) as pool:
    results = list(pool.map(attempt, range(3)))
assert sorted(results) == [0, 0, 1]
assert w.preview(v["id"])["stage"] == 1
''')


@pytest.mark.parametrize("stage", [1, 2])
def test_confirmation_response_lost_cannot_double_advance(directory, stage):
    worker(directory, f'''
v = create()
v = w.confirm(v["id"], 1, v["token"], v["context"])
if {stage} == 2:
    v = w.confirm(v["id"], 2, v["token"], v["context"])
os._exit(71)
''', expected=71)
    worker(directory, f'''
batch = w.list_batches()[0]
v = w.preview(batch["id"])
assert v["stage"] == {stage}
receipt = v["confirmations"][-1]
rejected(lambda: w.confirm(v["id"], receipt["stage"], receipt["token"], v["context"]))
assert w.preview(v["id"])["stage"] == {stage}
if {stage} == 1:
    rejected(lambda: w.save(v["id"], v["context"]))
    v = w.confirm(v["id"], 2, v["token"], v["context"])
assert w.save(v["id"], v["context"])["saved"]
''')
