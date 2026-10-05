"""隔離 SQLite 真交易；不使用 ERP、私人來源、正式建表或模型。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier, Event
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.dialects import mssql, mysql
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.schema import CreateTable

from database.development_atomic_schema import development_atomic_schema
from database.quote_number_reservation_schema import reservation_schema
from engine import development_atomic_contract as contract
from engine.development_atomic_snapshot import (
    AtomicSnapshotConflict, AtomicSnapshotTimeout, DevelopmentAtomicSnapshotService,
)
from engine.quote_number_reservation import QuoteNumberReservationService
from development_atomic_fixtures import EVIDENCE, SHARED, change_child, change_row, ident, request
from quote_batch_trial_fixtures import priced_four
from test_quote_number_reservation import FakeTime, NOW, open_engine


@pytest.fixture
def database(tmp_path):
    engine = open_engine(tmp_path / "development.sqlite")
    metadata, *tables = development_atomic_schema()
    # 只在 tmp_path 建立新 registry 與開發四表；無 production metadata。
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def reserve(engine, req):
    return QuoteNumberReservationService(engine, clock=lambda: NOW).reserve(
        contract.build_development_reservation_request(req))


def service(engine, **kwargs):
    return DevelopmentAtomicSnapshotService(engine, **kwargs)


def counts(engine):
    _, *tables = development_atomic_schema()
    with engine.connect() as c:
        return tuple(c.scalar(select(func.count()).select_from(t)) for t in tables)


def registry_rows(engine):
    _, *tables = reservation_schema()
    with engine.connect() as c:
        return [[dict(row) for row in c.execute(select(t)).mappings()] for t in tables]


def test_four_items_eleven_pieces_full_content_and_read_exact(database):
    req = request()
    reservation = reserve(database, req)
    before = registry_rows(database)
    result = service(database).save(req)
    assert counts(database) == (1, 4, 4, 16)
    assert sum(c.product_qty for c in req.children) == 11
    assert result["fixed_request"] == json.loads(contract.validate_request(req))
    assert result["reservation"] == reservation
    assert result["document_type"] == contract.DOCUMENT_TYPE
    assert all(result[key] is False for key in ("can_save", "can_quote", "can_confirm"))
    for i, child in enumerate(result["children"], 1):
        assert child["ref_no"] == f"20261002083045_{i:03d}"
        assert child["fixed_child"]["rows"][0]["seq_no"] == "00001"
        assert child["fixed_child"]["shared_description"] == SHARED
        assert len(child["fixed_child"]["rows"]) == 4  # 結構、付費父、付費子、零價不漏。
        assert child["fixed_child"]["rows"][0]["original_qty"] == "1.000000000000000001"
    assert result == service(database).read(contract.identity_of(req))
    assert result == service(database, clock=Mock(side_effect=AssertionError("不能再讀時鐘"))).save(req)
    assert registry_rows(database) == before
    assert not any("snapshot_document" in name or name == "ordqdt_ai" for name in inspect(database).get_table_names())


def test_two_034_sum_068_and_tax_difference_not_adjusted(database):
    req = request(2)
    reserve(database, req)
    doc = service(database).save(req)
    totals = json.loads(doc["fixed_request"]["totals_json"])
    assert totals["after_discount"] == "0.68"  # 不是舊整單 0.67。
    assert totals["tax_amount"] == "0.04" and totals["total_price"] == "0.70"
    assert [c["fixed_child"]["amounts"]["tax_rounding_difference"] for c in doc["children"]] == ["-0.01"] * 2
    assert service(database).read(contract.identity_of(req)) == doc


@pytest.mark.parametrize("count", [1, 999])
def test_child_capacity(database, count):
    req = request(count)
    reserve(database, req)
    result = service(database, timeout=30).save(req)
    assert len(result["children"]) == count
    assert result["children"][-1]["suffix"] == f"{count:03d}"
    assert counts(database) == (1, count, count, count * 4)


@pytest.mark.parametrize("count", [0, 1000])
def test_child_overflow_rejected_without_connect(database, count):
    deny = Mock(side_effect=AssertionError("invalid input must not connect"))
    event.listen(database, "checkout", deny)
    try:
        with pytest.raises(ValueError):
            service(database).save(request(count))
        deny.assert_not_called()
    finally:
        event.remove(database, "checkout", deny)


@pytest.mark.parametrize("position,expected", [(1, "00001"), (99999, "99999")])
def test_cost_sequence_boundary_without_mass_allocation(position, expected):
    assert contract.cost_sequence(position) == expected


@pytest.mark.parametrize("position", [0, 100000, True, -1, "1"])
def test_cost_sequence_overflow(position):
    with pytest.raises(ValueError):
        contract.cost_sequence(position)


def test_rows_100000_rejected_with_shared_tuple_not_giant_fixture(database):
    req = request(1)
    child = req.children[0]
    with pytest.raises(ValueError):
        service(database).save(change_child(req, rows=(child.rows[0],) * 100000))
    with pytest.raises(ValueError):
        service(database).save(change_child(req, rows=(child.rows[0],) * 100001))


@pytest.mark.parametrize("field,value", [
    ("workgroup", "104"), ("save_id", ident(900)), ("request_id", ident(901)),
    ("preview_identity", ident(902)), ("revision", 1), ("origin", "other-development-origin"),
    ("policy_json", EVIDENCE), ("totals_json", EVIDENCE),
    ("request_version", "future"), ("schema_version", "future"),
])
def test_reserved_request_changed_rejected(database, field, value):
    req = request()
    reserve(database, req)
    before = registry_rows(database)
    with pytest.raises(ValueError):
        service(database).save(replace(req, **{field: value}))
    assert counts(database) == (0, 0, 0, 0) and registry_rows(database) == before


def test_changed_valid_money_after_reservation_rejected(database):
    req = request(1)
    reserve(database, req)
    amounts = replace(req.children[0].amounts, subtotal="0.35")
    totals = json.loads(req.totals_json)
    totals["subtotal"] = "0.35"
    changed = replace(change_child(req, amounts=amounts), totals_json=contract.canonical(totals))
    contract.validate_request(changed)
    with pytest.raises(AtomicSnapshotConflict):
        service(database).save(changed)
    assert counts(database) == (0, 0, 0, 0)


def test_arbitrary_nonempty_reservation_cannot_attach_new_money(database):
    req = request()
    reservation = contract.build_development_reservation_request(req)
    reservation = replace(reservation, fixed_context_json='{"arbitrary":"not the snapshot"}')
    QuoteNumberReservationService(database, clock=lambda: NOW).reserve(reservation)
    with pytest.raises(AtomicSnapshotConflict):
        service(database).save(req)
    assert counts(database) == (0, 0, 0, 0)


@pytest.mark.parametrize("field,value", [
    ("position", 0), ("position", 2), ("position", True), ("source_line_no", 3),
    ("source_item_id", ident(999)), ("configuration_line_id", "x"), ("child_quote_id", "x"),
    ("product_qty", 0), ("product_qty", True), ("product_qty", 1000000000), ("product_qty", 3),
    ("description", "wrong"), ("shared_description", "incomplete"), ("prodkind", "x" * 17),
    ("label", "x" * 257), ("revision", -1), ("rows", ()), ("selections", ()),
    ("dimensions_json", "{}"), ("requirements_json", "[]"), ("conditions_json", '{"a":1,"a":2}'),
    ("drawings_json", '{"value":NaN}'),
])
def test_invalid_child_rejected(database, field, value):
    with pytest.raises(ValueError):
        service(database).save(change_child(request(), **{field: value}))
    assert counts(database) == (0, 0, 0, 0)


@pytest.mark.parametrize("field,value", [
    ("seq_no", "001"), ("seq_no", "00000"), ("seq_no", "00002"), ("seq_no", "100000"),
    ("path", "UNKNOWN"), ("spc_code", "UNKNOWN"), ("part_code", "x" * 33),
    ("part_desc", "x" * 1025), ("opt_desc", "x" * 1025), ("spdsc", "x" * 1025),
    ("product_qty", 0), ("product_qty", True), ("product_qty", 3), ("qty", "1"),
    ("line_qty", "0"), ("original_qty", "0"), ("stdpar", "0"), ("stdqty", "-1"),
    ("compri", "NaN"), ("unit_cost", "Infinity"), ("unit_price", 1.0),
    ("amount", "1e2"), ("amount", "0.01"), ("part_cost", "1000000000000000000"),
    ("original_qty", "0.0000000000000000001"), ("quantity_source", "unrecognized"),
    ("quantity_evidence_json", "{}"), ("price_evidence_json", '{"a":1,"a":1}'),
    ("configuration_json", EVIDENCE), ("unit", "x" * 33),
])
def test_invalid_cost_row_rejected(database, field, value):
    with pytest.raises(ValueError):
        service(database).save(change_row(request(), **{field: value}))
    assert counts(database) == (0, 0, 0, 0)


@pytest.mark.parametrize("change", ["missing", "duplicate", "extra", "reorder", "selection_missing", "selection_duplicate", "selection_long"])
def test_exact_configuration_cost_coverage(database, change):
    req = request()
    child = req.children[0]
    changes = {
        "missing": dict(rows=child.rows[:-1]), "duplicate": dict(rows=(child.rows[0],) + child.rows),
        "extra": dict(rows=child.rows + (replace(child.rows[-1], path="UNKNOWN", seq_no="00005"),)),
        "reorder": dict(rows=tuple(reversed(child.rows))),
        "selection_missing": dict(selections=child.selections[:-1]),
        "selection_duplicate": dict(selections=child.selections + (child.selections[0],)),
        "selection_long": dict(selections=(replace(child.selections[0], path="x" * 257), *child.selections[1:])),
    }
    with pytest.raises(ValueError):
        service(database).save(change_child(req, **changes[change]))


@pytest.mark.parametrize("change", ["raw", "line_text", "missing_item", "duplicate_item", "line_no", "unknown_scope", "empty_scope", "order", "source_id", "order_id"])
def test_source_coverage_and_identity(database, change):
    req = request()
    reserve(database, req)
    source = req.source
    if change == "raw":
        source = replace(source, raw_text=source.raw_text + "\nmissing")
    elif change in ("source_id", "order_id"):
        source = replace(source, **{"source_batch_id" if change == "source_id" else "source_order_id": ident(555)})
    elif change == "order":
        req = replace(req, children=tuple(reversed(req.children)))
    else:
        lines = list(source.lines)
        if change == "line_text":
            lines[1] = replace(lines[1], text="modified")
        elif change == "missing_item":
            lines[1] = replace(lines[1], kind="context", source_item_id=None, applies_to=())
        elif change == "duplicate_item":
            lines[2] = replace(lines[2], source_item_id=lines[1].source_item_id, applies_to=lines[1].applies_to)
        elif change == "line_no":
            lines[1] = replace(lines[1], line_no=99)
        else:
            lines[-1] = replace(lines[-1], applies_to=(ident(777),) if change == "unknown_scope" else ())
        source = replace(source, lines=tuple(lines), raw_text="\n".join(line.text for line in lines))
    with pytest.raises(ValueError):
        service(database).save(replace(req, source=source))
    assert counts(database) == (0, 0, 0, 0)


@pytest.mark.parametrize("value", ["{}", "null", "[]", '{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{"a":1.2}', '{"a":{"x":1,"x":2}}'])
def test_strict_json_rejected(value):
    with pytest.raises(ValueError):
        contract.fixed_object(value)


@pytest.mark.parametrize("field,value", [
    ("subtotal", "0.340"), ("total_cost", 0.34), ("quote_rate", "0"),
    ("discount_rate", "0.1"), ("tax_rate", "0.1"), ("raw_amounts_json", EVIDENCE),
    ("cost_display_difference", "NaN"), ("cost_display_difference", "0"), ("tax_rounding_difference", "0"),
])
def test_money_policy_invalid(database, field, value):
    req = request()
    with pytest.raises(ValueError):
        service(database).save(change_child(req, amounts=replace(req.children[0].amounts, **{field: value})))


@pytest.mark.parametrize("table,position", [("child", 2), ("child", 4), ("link", 2), ("link", 4), ("cost", 2), ("cost", 4)])
def test_failure_rolls_back_every_new_table_leaves_reservation(database, table, position):
    req = request()
    reserve(database, req)
    before = registry_rows(database)
    svc = service(database)
    target = getattr(svc, table)
    with database.begin() as c:
        c.exec_driver_sql(f"CREATE TRIGGER fail_new BEFORE INSERT ON {target.name} WHEN NEW.position = {position} BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    timer = FakeTime()
    with pytest.raises(IntegrityError, match="injected failure"):
        service(database, monotonic=timer.monotonic, sleep=timer.sleep).save(req)
    assert counts(database) == (0, 0, 0, 0)
    assert registry_rows(database) == before and timer.sleeps == []
    with database.begin() as c:
        c.exec_driver_sql("DROP TRIGGER fail_new")
    doc = service(database).save(req)
    assert doc["reservation"]["children"][0]["reserved_ref_no"] == "20261002083045_001"


@pytest.mark.parametrize("damage", ["missing", "child_missing", "payload", "digest", "ref", "position", "source", "configuration", "workgroup"])
def test_registry_damage_rejected_even_on_read(database, damage):
    req = request()
    reserve(database, req)
    service(database).save(req)
    _, header, child = reservation_schema()
    # 故意繞過管理員層 FK；服務必須拒絕，不能 repair。
    with database.connect() as c:
        c.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if damage == "missing":
            c.execute(child.delete())
            c.execute(header.delete())
        elif damage == "child_missing":
            c.execute(child.delete().where(child.c.position == 4))
        elif damage in ("payload", "digest", "workgroup"):
            key, value = {"payload": ("payload", '{"wrong":true}'), "digest": ("content_digest", "0" * 64), "workgroup": ("workgroup", "104")}[damage]
            c.execute(header.update().values(**{key: value}))
        else:
            key, value = {"ref": ("reserved_ref_no", "invalid"), "position": ("position", 5),
                          "source": ("source_item_id", ident(777)), "configuration": ("configuration_line_id", ident(778))}[damage]
            c.execute(child.update().where(child.c.position == 1).values(**{key: value}))
        c.commit()
        c.exec_driver_sql("PRAGMA foreign_keys=ON")
    before = registry_rows(database)
    for call in (lambda: service(database).save(req), lambda: service(database).read(contract.identity_of(req))):
        with pytest.raises(AtomicSnapshotConflict):
            call()
    assert registry_rows(database) == before and counts(database) == (1, 4, 4, 16)


@pytest.mark.parametrize("table,damage", [("batch", "digest"), ("batch", "final"), ("batch", "payload"), ("batch", "time"),
                                        ("child", "missing"), ("child", "payload"), ("link", "missing"), ("link", "payload"),
                                        ("cost", "missing"), ("cost", "payload"), ("cost", "extra")])
def test_existing_snapshot_corruption_never_repairs(database, table, damage):
    req = request()
    reserve(database, req)
    svc = service(database)
    svc.save(req)
    target = getattr(svc, table)
    with database.begin() as c:
        if damage == "missing":
            if table == "child":
                c.execute(svc.cost.delete().where(svc.cost.c.child_quote_id == req.children[-1].child_quote_id))
                c.execute(svc.link.delete().where(svc.link.c.child_quote_id == req.children[-1].child_quote_id))
            c.execute(target.delete().where(target.c.child_quote_id == req.children[-1].child_quote_id))
        elif damage == "extra":
            row = dict(c.execute(select(target).limit(1)).mappings().one())
            row.update(seq_no="00005", position=5, path="EXTRA")
            c.execute(target.insert().values(**row))
        else:
            key, value = {"digest": ("fixed_digest", "0" * 64), "final": ("final_digest", "0" * 64),
                          "payload": ("payload", '{"tampered":true}'), "time": ("created_at", "2026-01-01T00:00:00+00:00")}[damage]
            c.execute(target.update().values(**{key: value}))
    before = counts(database)
    for call in (lambda: svc.save(req), lambda: svc.read(contract.identity_of(req))):
        with pytest.raises(AtomicSnapshotConflict):
            call()
    assert counts(database) == before


def test_missing_reservation_and_caller_result_dict_not_trusted(database):
    req = request()
    with pytest.raises(AtomicSnapshotConflict):
        service(database).save(req)
    reservation = reserve(database, req)
    reservation["children"][0]["reserved_ref_no"] = "fake"
    doc = service(database).save(req)
    assert doc["children"][0]["ref_no"] != "fake"
    with pytest.raises(ValueError):
        service(database).save(reservation)


def test_restart_response_lost_and_return_mutation(database):
    req = request()
    reserve(database, req)
    original = service(database).save(req)
    another = open_engine(database.url.database)
    try:
        restarted = service(another, clock=Mock(side_effect=AssertionError("retry should not create time")))
        assert restarted.save(req) == original
        assert restarted.read(contract.identity_of(req)) == original
        returned = restarted.save(req)
        returned["children"][0]["fixed_child"]["rows"].clear()
        assert restarted.read(contract.identity_of(req)) == original
    finally:
        another.dispose()


def test_fresh_process_reads_exact_version_without_production_import(database):
    req = request()
    reserve(database, req)
    original = service(database).save(req)
    code = """
import json, sys
from sqlalchemy import create_engine, event
from engine.development_atomic_contract import DevelopmentSnapshotIdentity
from engine.development_atomic_snapshot import DevelopmentAtomicSnapshotService
engine = create_engine(sys.argv[1])
event.listen(engine, 'connect', lambda dbapi, _: dbapi.execute('PRAGMA foreign_keys=ON'))
result = DevelopmentAtomicSnapshotService(engine).read(DevelopmentSnapshotIdentity(**json.loads(sys.stdin.read())))
assert not {'config', 'database.connection', 'database.models', 'database.repository', 'engine.pricing', 'engine.preview', 'engine.snapshot'} & sys.modules.keys()
print(json.dumps(result, ensure_ascii=False))
engine.dispose()
"""
    run = subprocess.run([sys.executable, "-c", code, str(database.url)], input=json.dumps(asdict(contract.identity_of(req))),
                         capture_output=True, text=True, timeout=20, check=True)
    assert json.loads(run.stdout) == original


def test_independent_engines_real_unique_race_same_request(database):
    req = request()
    reserve(database, req)
    follower = open_engine(database.url.database)
    barrier, leader_done = Barrier(2), Event()
    collisions, connections = [], []

    def hook(engine, wait):
        first = True

        @event.listens_for(engine, "before_cursor_execute")
        def before(connection, cursor, statement, parameters, context, many):
            nonlocal first
            if first and statement.startswith("INSERT INTO development_atomic_batch "):
                first = False
                connections.append(id(connection.connection.driver_connection))
                barrier.wait(timeout=10)
                if wait:
                    assert leader_done.wait(timeout=10)

        @event.listens_for(engine, "handle_error")
        def error(context):
            collisions.append(str(context.original_exception))

    hook(database, False)
    hook(follower, True)

    def leader():
        try:
            return service(database, timeout=20).save(req)
        finally:
            leader_done.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(leader), pool.submit(lambda: service(follower, timeout=20).save(req))
            assert a.result(timeout=30) == b.result(timeout=30)
        assert len(set(connections)) == 2
        assert any("UNIQUE constraint failed: development_atomic_batch." in value for value in collisions)
        assert counts(database) == (1, 4, 4, 16)
    finally:
        follower.dispose()


def test_concurrent_different_content_rejected_by_full_reservation(database):
    req = request()
    reserve(database, req)
    other = replace(req, origin="other-development-origin")
    follower = open_engine(database.url.database)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(lambda: service(database).save(req))
            b = pool.submit(lambda: service(follower).save(other))
            assert len(a.result(timeout=10)["children"]) == 4
            with pytest.raises(AtomicSnapshotConflict):
                b.result(timeout=10)
        assert counts(database) == (1, 4, 4, 16)
    finally:
        follower.dispose()


def test_busy_retry_is_bounded_and_does_not_commit_external_transaction(database):
    req = request()
    reserve(database, req)
    timer = FakeTime()
    with database.connect() as caller:
        caller.exec_driver_sql("CREATE TABLE caller_owned (value INTEGER)")
        caller.commit()
        caller.exec_driver_sql("INSERT INTO caller_owned VALUES (1)")
        with pytest.raises(AtomicSnapshotTimeout):
            service(database, monotonic=timer.monotonic, sleep=timer.sleep, max_attempts=3).save(req)
        assert caller.in_transaction()
        caller.rollback()
        assert caller.exec_driver_sql("SELECT count(*) FROM caller_owned").scalar() == 0
    assert len(timer.sleeps) == 2 and counts(database) == (0, 0, 0, 0)


def test_read_missing_and_wrong_version(database):
    identity = contract.identity_of(request())
    assert service(database).read(identity) is None
    with pytest.raises(ValueError):
        service(database).read(replace(identity, schema_version="future"))


@pytest.mark.parametrize("dialect", [mysql.dialect(), mssql.dialect()], ids=["mysql", "mssql"])
def test_ddl_compile_only_runtime_rejected(database, dialect, monkeypatch):
    metadata, *tables = development_atomic_schema()
    for table in metadata.sorted_tables:
        assert "CREATE TABLE" in str(CreateTable(table).compile(dialect=dialect))
    monkeypatch.setattr(database.dialect, "name", dialect.name)
    with pytest.raises(NotImplementedError):
        service(database)


def test_no_implicit_schema(tmp_path):
    engine = open_engine(tmp_path / "empty.sqlite")
    try:
        with pytest.raises(OperationalError, match="no such table"):
            service(engine).save(request())
        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("pool_name", ["StaticPool", "SingletonThreadPool"])
def test_shared_pool_rejected(tmp_path, pool_name):
    from sqlalchemy import pool
    engine = create_engine(f"sqlite:///{tmp_path / 'shared.sqlite'}", poolclass=getattr(pool, pool_name))
    try:
        with pytest.raises(ValueError):
            service(engine)
    finally:
        engine.dispose()


def test_memory_foreign_keys_external_connection_and_session_rejected(database, tmp_path):
    from sqlalchemy.orm import Session
    memory = create_engine("sqlite:///:memory:")
    disabled = create_engine(f"sqlite:///{tmp_path / 'disabled.sqlite'}")
    try:
        with pytest.raises(ValueError):
            service(memory)
        with pytest.raises(ValueError, match="foreign_keys"):
            service(disabled).save(request())
        with database.connect() as connection:
            with pytest.raises(ValueError):
                service(connection)
            with pytest.raises(ValueError):
                service(connection.begin())
        with Session(database) as session:
            with pytest.raises(ValueError):
                service(session)
    finally:
        memory.dispose()
        disabled.dispose()


@pytest.mark.parametrize("part", ["whole", "child", "row"])
def test_new_documents_rejected_by_old_formal_entries(database, monkeypatch, part):
    from database import repository
    from engine.preview import freeze_preview, checked_preview
    from engine.snapshot import build_snapshot_items, create_quote_snapshot
    from agent.tools import create_quote
    req = request()
    reserve(database, req)
    result = service(database).save(req)
    with database.connect() as c:
        cost = service(database).cost
        row = json.loads(c.scalar(select(cost.c.payload).limit(1)))
    document = {"whole": result, "child": result["children"][0], "row": row}[part]
    deny = Mock(side_effect=AssertionError("不得呼叫正式 DB"))
    monkeypatch.setattr(repository, "_session", deny)
    for call in (lambda: freeze_preview(document), lambda: checked_preview({"preview": document}, "fake"),
                 lambda: create_quote(document, preview_id="fake"), lambda: build_snapshot_items(document, {}, "fake"),
                 lambda: create_quote_snapshot(None, {}, preview=document), lambda: repository.save_quote_snapshot(document),
                 lambda: repository.save_quote_snapshot([document]),
                 lambda: repository.save_quote_snapshot([{"ref_no": "fake"}], preview=document),
                 lambda: QuoteNumberReservationService(database).reserve(document)):
        with pytest.raises(ValueError):
            call()
    deny.assert_not_called()


@pytest.mark.parametrize("kind", ["old_trial", "batch_trial", "description"])
def test_actual_nonformal_documents_not_promoted(database, kind):
    from quote_batch_fixtures import four_items
    from agent.quote_batch import build_quote_batch
    from engine.multi_trial import build_internal_trial
    from engine.quote_batch_trial import build_batch_trial
    batch, draft = four_items(reviewed=False)
    builder = {"old_trial": build_internal_trial, "batch_trial": build_batch_trial, "description": build_quote_batch}[kind]
    document = builder(draft, actor="開發測試", source_batch=batch)
    with pytest.raises(ValueError, match="typed"):
        service(database).save(document)
    with pytest.raises(ValueError, match="typed"):
        contract.build_development_reservation_request(document)
    assert counts(database) == (0, 0, 0, 0)


@pytest.mark.parametrize("steps", [0, 1, 2])
def test_real_review_even_two_steps_never_promoted(database, priced_four, steps):
    from engine.quote_batch_review import BatchReviewSession
    from engine.quote_batch_trial import build_batch_trial
    batch, draft = priced_four
    trial = build_batch_trial(draft, actor="測試", source_batch=batch)
    session = BatchReviewSession()
    document = session.freeze(draft, trial, actor="測試", source_batch=batch)
    for step in range(1, steps + 1):
        document = session.record(document, draft, trial, actor="測試", source_batch=batch,
                                  step=step, token=document["review_state"]["next_token"])
    assert document["can_save"] is False and len(document["review_state"]["history"]) == steps
    for candidate in (document, trial):
        with pytest.raises(ValueError, match="typed"):
            service(database).save(candidate)
        with pytest.raises(ValueError, match="typed"):
            contract.build_development_reservation_request(candidate)
    assert counts(database) == (0, 0, 0, 0) and registry_rows(database) == [[], []]


def test_legacy_tables_same_database_unchanged(database):
    from database.models import QuoteSnapshotDocument, ordqdt_ai
    # 僅在隔離 file 建立兩張真實歷史表的副本，服務不 import 它們。
    from sqlalchemy import MetaData
    metadata = MetaData()
    documents = QuoteSnapshotDocument.__table__.to_metadata(metadata)
    costs = ordqdt_ai.__table__.to_metadata(metadata)
    metadata.create_all(database)
    with database.begin() as c:
        c.execute(documents.insert().values(workgroup="103", preview_id="legacy", ref_no="LEGACY", payload="legacy-unchanged"))
        c.execute(costs.insert().values(workgroup="103", ref_no="LEGACY", seq_no="00001", path="LEGACY-PATH", part_code="LEGACY"))
    req = request()
    reserve(database, req)
    service(database).save(req)
    with database.connect() as c:
        assert c.scalar(select(func.count()).select_from(documents)) == 1
        assert c.scalar(select(documents.c.payload)) == "legacy-unchanged"
        assert c.scalar(select(func.count()).select_from(costs)) == 1
        assert c.scalar(select(costs.c.path)) == "LEGACY-PATH"


def test_service_does_not_import_ui_models_or_old_save_and_no_production_wiring():
    import ast
    root = Path(__file__).resolve().parents[1]
    for relative in ("engine/development_atomic_contract.py", "engine/development_atomic_snapshot.py", "database/development_atomic_schema.py"):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(name.startswith(("agent", "utils", "database.connection", "database.models", "database.repository", "engine.preview", "engine.snapshot", "engine.pricing", "engine.quote_batch_trial", "engine.quote_batch_review")) for name in imports)
    for folder in ("utils", "agent"):
        for path in (root / folder).glob("*.py"):
            assert "development_atomic" not in path.read_text(encoding="utf-8")
    assert "development_atomic" not in (root / "app.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", ["fk", "check", "row_unique", "header_trigger"])
def test_non_identity_integrity_errors_not_retried(database, kind):
    req = request()
    reserve(database, req)
    if kind == "header_trigger":
        sql = "CREATE TRIGGER failure BEFORE INSERT ON development_atomic_batch BEGIN SELECT RAISE(ABORT, 'header failure'); END"
    else:
        values = ("NEW.batch_id", "NEW.child_quote_id", "NEW.seq_no", "NEW.position", "NEW.path", "NEW.payload")
        if kind == "fk":
            values = ("NEW.batch_id", "'ffffffffffffffffffffffffffffffff'", *values[2:])
        elif kind == "check":
            values = (*values[:3], "0", *values[4:])
        sql = f"""CREATE TRIGGER failure BEFORE INSERT ON development_atomic_cost
        BEGIN INSERT INTO development_atomic_cost (batch_id,child_quote_id,seq_no,position,path,payload)
        VALUES ({','.join(values)}); END"""
    with database.begin() as c:
        c.exec_driver_sql(sql)
    timer = FakeTime()
    with pytest.raises(IntegrityError) as caught:
        service(database, monotonic=timer.monotonic, sleep=timer.sleep).save(req)
    expected = {"fk": "FOREIGN KEY", "check": "CHECK", "row_unique": "UNIQUE", "header_trigger": "header failure"}[kind]
    assert expected in str(caught.value)
    assert timer.sleeps == [] and counts(database) == (0, 0, 0, 0)


def test_sleep_after_busy_holds_no_service_write_lock(database):
    req = request()
    reserve(database, req)
    timer = FakeTime()
    with database.connect() as caller:
        caller.exec_driver_sql("BEGIN IMMEDIATE")

        def unlocked(seconds):
            caller.rollback()
            with database.connect() as check:
                check.exec_driver_sql("PRAGMA busy_timeout=0")
                check.exec_driver_sql("BEGIN IMMEDIATE")
                check.rollback()
            timer.sleep(seconds)

        result = service(database, monotonic=timer.monotonic, sleep=unlocked).save(req)
    assert timer.sleeps and len(result["children"]) == 4


def test_sleep_failure_propagates_without_snapshot(database):
    req = request()
    reserve(database, req)
    with database.connect() as caller:
        caller.exec_driver_sql("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="sleep failed"):
            service(database, sleep=Mock(side_effect=RuntimeError("sleep failed"))).save(req)
    assert counts(database) == (0, 0, 0, 0)


@pytest.mark.parametrize("ticks", [[float("nan")], [1, 0], [0, float("inf")]])
def test_monotonic_failure(database, ticks):
    values = iter(ticks)
    with pytest.raises(ValueError):
        service(database, monotonic=lambda: next(values)).save(request())


@pytest.mark.parametrize("options", [{"max_attempts": 0}, {"max_attempts": True}, {"timeout": 0},
                                    {"timeout": float("inf")}, {"poll_interval": float("nan")}])
def test_retry_policy_limits(database, options):
    with pytest.raises(ValueError):
        service(database, **options)


@pytest.mark.parametrize("stamp", [None, "20260101", datetime(2026, 1, 1)])
def test_creation_clock_must_be_aware(database, stamp):
    req = request()
    reserve(database, req)
    with pytest.raises(ValueError):
        service(database, clock=lambda: stamp).save(req)
    assert counts(database) == (0, 0, 0, 0)


@pytest.mark.parametrize("field,value", [
    ("workgroup", "1"), ("workgroup", True), ("batch_id", "x"), ("save_id", ident(1)),
    ("revision", True), ("revision", -1), ("revision", 1000000000),
    ("origin", ""), ("origin", "x" * 81), ("origin", "a\x00b"),
    ("source", {}), ("children", []), ("children", ({},)),
])
def test_invalid_top_level_contract(database, field, value):
    with pytest.raises(ValueError):
        service(database).save(replace(request(), **{field: value}))
    assert counts(database) == (0, 0, 0, 0)


@pytest.mark.parametrize("field", ["batch_id", "request_id", "preview_identity", "save_id", "workgroup"])
def test_read_rejects_wrong_identity_on_existing(database, field):
    req = request()
    reserve(database, req)
    service(database).save(req)
    identity = contract.identity_of(req)
    with pytest.raises(AtomicSnapshotConflict):
        service(database).read(replace(identity, **{field: "104" if field == "workgroup" else ident(999)}))


def test_capacity_rejects_before_connect_without_truncation(database, monkeypatch):
    req = request()
    monkeypatch.setattr(contract, "MAX_PAYLOAD_BYTES", 2048)
    with pytest.raises(ValueError, match="16 MiB"):
        service(database).save(req)
    assert counts(database) == (0, 0, 0, 0)


def test_no_reserve_price_or_formula_calls_during_save_retry_read(database, monkeypatch):
    req = request()
    reserve(database, req)
    deny = Mock(side_effect=AssertionError("保存不配號、查價或計算"))
    monkeypatch.setattr(QuoteNumberReservationService, "reserve", deny)
    from engine import pricing, calculator
    monkeypatch.setattr(pricing, "calculate_configuration", deny)
    monkeypatch.setattr(calculator, "calculate_quote", deny)
    original = service(database).save(req)
    assert service(database).save(req) == service(database).read(contract.identity_of(req)) == original
    deny.assert_not_called()


def test_registry_is_select_only_with_sqlite_authorizer(database):
    req = request()
    reserve(database, req)
    before = registry_rows(database)
    restricted = open_engine(database.url.database)

    @event.listens_for(restricted, "connect")
    def authorizer(dbapi, _):
        def check(action, table, column, dbname, trigger):
            if table in ("quote_number_reservation", "quote_number_reserved_child") and action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        dbapi.set_authorizer(check)

    try:
        svc = service(restricted)
        doc = svc.save(req)
        assert svc.save(req) == svc.read(contract.identity_of(req)) == doc
        assert registry_rows(database) == before
    finally:
        restricted.dispose()


def test_registry_changed_between_probe_and_insert_rolls_back(database):
    req = request()
    reserve(database, req)
    fired = False

    @event.listens_for(database, "before_cursor_execute")
    def mutate(connection, cursor, statement, parameters, context, many):
        nonlocal fired
        if not fired and statement.startswith("INSERT INTO development_atomic_batch "):
            fired = True
            _, header, _ = reservation_schema()
            with database.begin() as other:
                other.execute(header.update().values(content_digest="0" * 64))

    try:
        with pytest.raises(AtomicSnapshotConflict):
            service(database).save(req)
        assert fired and counts(database) == (0, 0, 0, 0)
    finally:
        event.remove(database, "before_cursor_execute", mutate)


def test_trigger_silent_row_mutation_is_detected_before_commit(database):
    req = request()
    reserve(database, req)
    with database.begin() as c:
        c.exec_driver_sql("""CREATE TRIGGER alter_row AFTER INSERT ON development_atomic_cost
        BEGIN UPDATE development_atomic_cost SET payload='{"changed":true}'
        WHERE batch_id=NEW.batch_id AND child_quote_id=NEW.child_quote_id AND seq_no=NEW.seq_no; END""")
    with pytest.raises(AtomicSnapshotConflict):
        service(database).save(req)
    assert counts(database) == (0, 0, 0, 0)


def test_changed_payload_with_recomputed_hash_still_bound_to_registry(database):
    req = request()
    reserve(database, req)
    doc = service(database).save(req)
    doc["fixed_request"]["origin"] = "forged"
    doc["fixed_digest"] = contract.digest(contract.canonical(doc["fixed_request"]))
    doc["final_digest"] = contract.digest(contract.canonical({k: v for k, v in doc.items() if k != "final_digest"}))
    svc = service(database)
    with database.begin() as c:
        c.execute(svc.batch.update().values(payload=contract.canonical(doc), fixed_digest=doc["fixed_digest"], final_digest=doc["final_digest"]))
    with pytest.raises(AtomicSnapshotConflict):
        svc.read(contract.identity_of(req))


def test_nullpool_supported(database):
    from sqlalchemy.pool import NullPool
    req = request()
    reserve(database, req)
    engine = create_engine(str(database.url), poolclass=NullPool)
    event.listen(engine, "connect", lambda dbapi, _: dbapi.execute("PRAGMA foreign_keys=ON"))
    try:
        doc = service(engine).save(req)
        assert service(engine).read(contract.identity_of(req)) == doc
    finally:
        engine.dispose()


@pytest.mark.parametrize("table,field,value", [("batch", "state", "OFFICIAL"), ("batch", "schema_version", "future"),
                                              ("batch", "child_count", 0), ("child", "row_count", 100000),
                                              ("link", "product_qty", 0), ("cost", "position", 100000),
                                              ("child", "workgroup", "104"), ("link", "child_quote_id", ident(999))])
def test_database_check_and_foreign_keys(database, table, field, value):
    req = request()
    reserve(database, req)
    svc = service(database)
    svc.save(req)
    target = getattr(svc, table)
    with pytest.raises(IntegrityError), database.begin() as c:
        c.execute(target.update().values(**{field: value}))


def test_document_decoder_rejects_extra_missing_and_duplicate_keys():
    payload = contract.validate_request(request())
    assert contract.decode_request(payload) == request()
    document = json.loads(payload)
    document["official_approval"] = True
    with pytest.raises(ValueError):
        contract.decode_request(contract.canonical(document))
    del document["official_approval"]
    del document["children"][0]["rows"][0]["price_evidence_json"]
    with pytest.raises(ValueError):
        contract.decode_request(contract.canonical(document))
    with pytest.raises(ValueError, match="重複"):
        contract.decode_request(payload[:-1] + ',"workgroup":"103"}')


def test_competing_different_batches_same_save_identity_unique_collision(database):
    from datetime import timedelta
    first = request()
    second = replace(first, batch_id=ident(101), request_id=ident(102), preview_identity=ident(103),
                     children=tuple(replace(c, child_quote_id=ident(40000 + i)) for i, c in enumerate(first.children)))
    reserve(database, first)
    QuoteNumberReservationService(database, clock=lambda: NOW + timedelta(seconds=1)).reserve(
        contract.build_development_reservation_request(second))
    follower = open_engine(database.url.database)
    barrier, done = Barrier(2), Event()
    collisions = []

    def hook(engine, wait):
        initial = True

        @event.listens_for(engine, "before_cursor_execute")
        def before(connection, cursor, statement, parameters, context, many):
            nonlocal initial
            if initial and statement.startswith("INSERT INTO development_atomic_batch "):
                initial = False
                barrier.wait(timeout=10)
                if wait:
                    assert done.wait(timeout=10)

        @event.listens_for(engine, "handle_error")
        def error(context):
            collisions.append(str(context.original_exception))

    hook(database, False)
    hook(follower, True)

    def leader():
        try:
            return service(database, timeout=20).save(first)
        finally:
            done.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(leader)
            b = pool.submit(lambda: service(follower, timeout=20).save(second))
            a.result(timeout=30)
            with pytest.raises(AtomicSnapshotConflict):
                b.result(timeout=30)
        assert any("UNIQUE constraint failed: development_atomic_batch.save_id" in error for error in collisions)
        assert counts(database) == (1, 4, 4, 16)
        assert [len(rows) for rows in registry_rows(database)] == [2, 8]
    finally:
        follower.dispose()


@pytest.mark.parametrize("mode", ["sqlalchemy", "driver_autocommit", "driver_isolation"])
def test_autocommit_modes_rejected_before_any_snapshot_write(database, mode):
    req = request()
    reserve(database, req)
    options = {"sqlalchemy": {"isolation_level": "AUTOCOMMIT"},
               "driver_autocommit": {"connect_args": {"autocommit": True}},
               "driver_isolation": {"connect_args": {"isolation_level": None}}}[mode]
    unsafe = create_engine(str(database.url), **options)
    event.listen(unsafe, "connect", lambda dbapi, _: dbapi.execute("PRAGMA foreign_keys=ON"))
    try:
        for call in (lambda: service(unsafe).save(req), lambda: service(unsafe).read(contract.identity_of(req))):
            with pytest.raises(ValueError, match="autocommit"):
                call()
        assert counts(database) == (0, 0, 0, 0)
    finally:
        unsafe.dispose()
