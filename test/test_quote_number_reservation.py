"""僅合成開發 request；檔案型 SQLite 獨立連線，無真實等待／ERP／模型。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from threading import Barrier, Event
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.dialects import mssql, mysql
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.schema import CreateTable

from database.quote_number_reservation_schema import reservation_schema
from engine.quote_number_reservation import (
    DOCUMENT_TYPE, TAIPEI, DevelopmentReservationChild, DevelopmentReservationRequest,
    QuoteNumberReservationService, ReservationConflict, ReservationTimeout,
    UnsafeClock, taiwan_prefix,
)


NOW = datetime(2026, 10, 2, 8, 30, 45, tzinfo=TAIPEI)


def ident(value):
    return f"{value:032x}"


def request(number=1, count=4, workgroup="103"):
    batch, order = ident(number * 10000), ident(number * 10000 + 3)
    children = tuple(DevelopmentReservationChild(
        workgroup, batch, order, ident(number * 10000 + 100 + i),
        ident(number * 10000 + 2000 + i), ident(number * 10000 + 4000 + i),
        (2, 1, 6, 2)[i % 4], json.dumps({"description": f"合成品項 {i}", "amount": "0.34",
                                      "cost_rows": [{"seq_no": "00001", "path": "TEST"}]}))
        for i in range(count))
    return DevelopmentReservationRequest(
        workgroup, batch, ident(number * 10000 + 1), ident(number * 10000 + 2),
        ident(number * 10000 + 4), order, 0, "isolated-development-test",
        '{"source":"synthetic","rules_version":"dev-1","approvals":[]}', children)


def open_engine(path):
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def foreign_keys(dbapi, _):
        dbapi.execute("PRAGMA foreign_keys=ON")

    return engine


@pytest.fixture
def registry(tmp_path):
    engine = open_engine(tmp_path / "reservation.sqlite")
    metadata, _, _ = reservation_schema()
    metadata.create_all(engine)  # 明確且僅對 tmp_path 建立兩張新表。
    yield engine
    engine.dispose()


class FakeTime:
    def __init__(self, wall=NOW, advance_wall=True):
        self.wall, self.ticks, self.sleeps, self.advance_wall = wall, 0.0, [], advance_wall

    def clock(self):
        return self.wall

    def monotonic(self):
        return self.ticks

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.ticks += seconds
        if self.advance_wall:
            self.wall += timedelta(seconds=seconds)


def service(engine, timer=None, **kwargs):
    timer = timer or FakeTime()
    return QuoteNumberReservationService(engine, clock=timer.clock, monotonic=timer.monotonic,
                                         sleep=timer.sleep, **kwargs)


def counts(engine):
    _, header, child = reservation_schema()
    with engine.connect() as connection:
        return tuple(connection.execute(select(func.count()).select_from(t)).scalar()
                     for t in (header, child))


def test_four_items_eleven_pieces_fixed_mapping_and_full_digest(registry, isolated_db):
    from database.models import QuoteSnapshotDocument, ordqdt_ai
    req = request()
    result = service(registry).reserve(req)
    assert [row["reserved_ref_no"] for row in result["children"]] == [
        f"20261002083045_{i:03d}" for i in range(1, 5)]
    assert sum(c.product_qty for c in req.children) == 11
    assert [row["child_quote_id"] for row in result["children"]] == [c.child_quote_id for c in req.children]
    assert all(len(row["reserved_ref_no"]) == 18 for row in result["children"])
    assert json.loads(result["reservation"]["payload"]) == json.loads(json.dumps(asdict(req)))
    assert result["reservation"]["state"] == "RESERVED"
    assert result["document_type"] == DOCUMENT_TYPE
    assert all(not result[key] for key in ("can_save", "can_confirm", "can_quote"))
    assert counts(registry) == (1, 4)
    assert set(inspect(registry).get_table_names()) == {
        "quote_number_reservation", "quote_number_reserved_child"}
    with isolated_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == db.query(ordqdt_ai).count() == 0


@pytest.mark.parametrize("stamp,expected", [
    (NOW, "20261002083045"),
    (datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc), "20261002000000"),
    (datetime(2026, 12, 31, 16, 0, tzinfo=timezone.utc), "20270101000000"),
    (datetime(2028, 2, 28, 16, 0, tzinfo=timezone.utc), "20280229000000"),
    (NOW.replace(microsecond=999999), "20261002083045"),
])
def test_taiwan_format(stamp, expected):
    assert taiwan_prefix(stamp) == expected


@pytest.mark.parametrize("stamp", [NOW.replace(tzinfo=None), None, "20261002083045"])
def test_naive_or_invalid_clock_rejected(registry, stamp):
    with pytest.raises(UnsafeClock):
        service(registry, FakeTime(stamp)).reserve(request())
    assert counts(registry) == (0, 0)


@pytest.mark.parametrize("count", [1, 999])
def test_capacity_success(registry, count):
    result = service(registry).reserve(request(count=count))
    assert len(result["children"]) == count
    assert result["children"][-1]["suffix"] == f"{count:03d}"


@pytest.mark.parametrize("count", [0, 1000])
def test_capacity_rejected_before_connection(registry, count):
    with pytest.raises(ValueError):
        service(registry).reserve(request(count=count))
    assert counts(registry) == (0, 0)


@pytest.mark.parametrize("field,value", [
    ("workgroup", "104"), ("batch_id", ident(777)), ("source_order_id", ident(777)),
    ("product_qty", 0), ("product_qty", -1), ("product_qty", True),
    ("product_qty", 1.1), ("product_qty", 1000000000),
    ("child_quote_id", "x"), ("source_item_id", None),
    ("fixed_content_json", "{}"), ("fixed_content_json", "[]"),
    ("fixed_content_json", '{"x":NaN}'), ("fixed_content_json", '{"x":1,"x":2}'),
])
def test_child_invalid(registry, field, value):
    req = request()
    req = replace(req, children=(replace(req.children[0], **{field: value}), *req.children[1:]))
    with pytest.raises(ValueError):
        service(registry).reserve(req)
    assert counts(registry) == (0, 0)


@pytest.mark.parametrize("field", ["child_quote_id", "source_item_id", "configuration_line_id"])
def test_duplicate_child_mapping(registry, field):
    req = request()
    req = replace(req, children=(req.children[0], replace(req.children[1], **{
        field: getattr(req.children[0], field)}), *req.children[2:]))
    with pytest.raises(ValueError, match="重複"):
        service(registry).reserve(req)
    assert counts(registry) == (0, 0)


@pytest.mark.parametrize("field,value", [
    ("workgroup", "1"), ("workgroup", None), ("batch_id", "invalid"),
    ("revision", -1), ("revision", True), ("origin", ""), ("origin", "x" * 81),
    ("fixed_context_json", "null"), ("fixed_context_json", '{"v":Infinity}'),
    ("request_version", "future"), ("schema_version", "future"),
    ("children", []), ("children", ({},)),
])
def test_invalid_request(registry, field, value):
    with pytest.raises(ValueError):
        service(registry).reserve(replace(request(), **{field: value}))
    assert counts(registry) == (0, 0)


@pytest.mark.parametrize("field", ["request_id", "preview_identity"])
def test_identity_separation(registry, field):
    req = request()
    with pytest.raises(ValueError, match="分離"):
        service(registry).reserve(replace(req, **{field: req.batch_id}))


@pytest.mark.parametrize("field,value", [
    ("origin", "other"), ("revision", 1), ("fixed_context_json", '{"source":"changed"}'),
    ("request_id", ident(777)), ("preview_identity", ident(778)),
    ("source_batch_id", ident(779)), ("request_version", "future"), ("schema_version", "future"),
])
def test_existing_identity_changed_rejected(registry, field, value):
    req = request()
    original = service(registry).reserve(req)
    with pytest.raises(ReservationConflict):
        service(registry).reserve(replace(req, **{field: value}))
    assert service(registry).reserve(req) == original
    assert counts(registry) == (1, 4)


@pytest.mark.parametrize("change", ["order", "content", "qty", "workgroup", "batch", "request", "preview"])
def test_same_identity_cannot_reassign(registry, change):
    req = request()
    service(registry).reserve(req)
    if change == "order":
        changed = replace(req, children=tuple(reversed(req.children)))
    elif change in ("content", "qty"):
        fields = {"fixed_content_json": '{"description":"changed"}'} if change == "content" else {"product_qty": 3}
        changed = replace(req, children=(replace(req.children[0], **fields), *req.children[1:]))
    elif change == "workgroup":
        changed = replace(req, workgroup="104", children=tuple(replace(c, workgroup="104") for c in req.children))
    else:
        other = request(2)
        field = {"batch": "batch_id", "request": "request_id", "preview": "preview_identity"}[change]
        changed = replace(other, **{field: getattr(req, field)})
        if change == "batch":
            changed = replace(changed, children=tuple(replace(c, batch_id=req.batch_id) for c in changed.children))
    with pytest.raises(ReservationConflict):
        service(registry).reserve(changed)
    assert counts(registry) == (1, 4)


def test_restart_idempotent_without_clock_even_after_day_or_rollback(registry):
    req = request()
    original = service(registry).reserve(req)
    new_engine = open_engine(registry.url.database)
    try:
        clock = Mock(side_effect=AssertionError("冪等查回不應讀 wall clock"))
        restarted = QuoteNumberReservationService(new_engine, clock=clock)
        assert restarted.reserve(req) == original
        clock.assert_not_called()
        for stamp in (NOW + timedelta(days=2), NOW - timedelta(days=1)):
            assert service(new_engine, FakeTime(stamp)).reserve(req) == original
    finally:
        new_engine.dispose()


def test_result_mutation_does_not_change_registry(registry):
    req = request()
    result = service(registry).reserve(req)
    result["children"][0]["reserved_ref_no"] = "fake"
    result["reservation"]["payload"] = "{}"
    assert service(registry).reserve(req)["children"][0]["reserved_ref_no"] == "20261002083045_001"


@pytest.mark.parametrize("workgroup", ["103", "104"])
def test_same_second_waits_real_next_second_globally(registry, workgroup):
    timer = FakeTime()
    allocator = service(registry, timer)
    first = allocator.reserve(request())
    second = allocator.reserve(request(2, workgroup=workgroup))
    assert first["reservation"]["prefix"] == "20261002083045"
    assert second["reservation"]["prefix"] == "20261002083046"
    assert timer.wall.second == 46 and timer.sleeps
    assert counts(registry) == (2, 8)


def test_cross_midnight_collision(registry):
    timer = FakeTime(NOW.replace(hour=23, minute=59, second=59))
    allocator = service(registry, timer)
    allocator.reserve(request())
    result = allocator.reserve(request(2))
    assert result["reservation"]["prefix"] == "20261003000000"


@pytest.mark.parametrize("limit", ["deadline", "attempts", "frozen_monotonic"])
def test_stuck_second_is_bounded(registry, limit):
    timer = FakeTime(advance_wall=False)
    kwargs = {"max_attempts": 3} if limit != "deadline" else {"timeout": 0.12}
    if limit == "frozen_monotonic":
        timer.monotonic = lambda: 0.0
    allocator = service(registry, timer, **kwargs)
    allocator.reserve(request())
    with pytest.raises(ReservationTimeout):
        allocator.reserve(request(2))
    assert 0 < len(timer.sleeps) <= 3
    assert counts(registry) == (1, 4)


def test_clock_rollback_during_wait(registry):
    timer = FakeTime()
    service(registry).reserve(request())

    def backwards(_):
        timer.wall -= timedelta(microseconds=1)

    timer.sleep = backwards
    with pytest.raises(UnsafeClock):
        service(registry, timer).reserve(request(2))
    assert counts(registry) == (1, 4)


def test_restart_new_request_clock_behind_registry_rejected(registry):
    service(registry).reserve(request())
    with pytest.raises(UnsafeClock, match="早於"):
        service(registry, FakeTime(NOW - timedelta(seconds=1))).reserve(request(2))


@pytest.mark.parametrize("ticks", [[float("nan")], [1.0, 0.0], [0.0, float("inf")]])
def test_invalid_monotonic(registry, ticks):
    timer = FakeTime()
    values = iter(ticks)
    timer.monotonic = lambda: next(values)
    with pytest.raises(UnsafeClock):
        service(registry, timer).reserve(request())
    assert counts(registry) == (0, 0)


def test_sleep_failure_has_no_partial_batch(registry):
    service(registry).reserve(request())
    timer = FakeTime()
    timer.sleep = Mock(side_effect=RuntimeError("sleep failed"))
    with pytest.raises(RuntimeError, match="sleep failed"):
        service(registry, timer).reserve(request(2))
    assert counts(registry) == (1, 4)


def test_sleep_holds_no_write_lock(registry):
    service(registry).reserve(request())
    timer = FakeTime()
    original_sleep = timer.sleep

    def unlocked(seconds):
        with registry.connect() as connection:
            connection.exec_driver_sql("PRAGMA busy_timeout=0")
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            connection.rollback()
        original_sleep(seconds)

    timer.sleep = unlocked
    service(registry, timer).reserve(request(2))
    assert counts(registry) == (2, 8)


@pytest.mark.parametrize("position", [2, 4])
def test_child_insert_failure_rolls_back_whole_batch(registry, position):
    with registry.begin() as connection:
        connection.exec_driver_sql(f"""CREATE TRIGGER fail_child BEFORE INSERT ON quote_number_reserved_child
            WHEN NEW.position = {position} BEGIN SELECT RAISE(ABORT, 'injected child failure'); END""")
    timer = FakeTime()
    with pytest.raises(IntegrityError, match="injected child failure"):
        service(registry, timer).reserve(request())
    assert counts(registry) == (0, 0) and timer.sleeps == []
    with registry.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER fail_child")
    assert len(service(registry).reserve(request())["children"]) == 4


def test_header_noncollision_integrity_error_not_retried(registry):
    with registry.begin() as connection:
        connection.exec_driver_sql("""CREATE TRIGGER fail_header BEFORE INSERT ON quote_number_reservation
            BEGIN SELECT RAISE(ABORT, 'injected header failure'); END""")
    timer = FakeTime()
    with pytest.raises(IntegrityError, match="injected header failure"):
        service(registry, timer).reserve(request())
    assert counts(registry) == (0, 0) and timer.sleeps == []


def test_child_unique_error_is_not_prefix_retry(registry):
    req = request()
    service(registry).reserve(req)
    other = request(2)
    other = replace(other, children=(replace(other.children[0], child_quote_id=req.children[0].child_quote_id),
                                     *other.children[1:]))
    timer = FakeTime(NOW + timedelta(seconds=1))
    with pytest.raises(IntegrityError, match="child_quote_id"):
        service(registry, timer).reserve(other)
    assert counts(registry) == (1, 4) and timer.sleeps == []


def test_missing_database_schema_no_implicit_initialization(tmp_path):
    engine = open_engine(tmp_path / "empty.sqlite")
    try:
        with pytest.raises(OperationalError, match="no such table"):
            service(engine).reserve(request())
        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


def test_unavailable_database_propagates(tmp_path):
    engine = open_engine(tmp_path / "missing" / "db.sqlite")
    try:
        with pytest.raises(OperationalError, match="unable to open"):
            service(engine).reserve(request())
    finally:
        engine.dispose()


def test_existing_writer_lock_has_bounded_retry(registry):
    with registry.connect() as caller:
        caller.exec_driver_sql("BEGIN IMMEDIATE")
        with pytest.raises(ReservationTimeout):
            service(registry, max_attempts=3).reserve(request())
        assert caller.in_transaction()
        caller.rollback()
    assert counts(registry) == (0, 0)


def test_reject_caller_connection_and_session_without_commit(registry):
    from sqlalchemy.orm import Session
    with registry.connect() as caller:
        caller.exec_driver_sql("CREATE TABLE caller_owned (value INTEGER)")
        caller.commit()
        caller.exec_driver_sql("INSERT INTO caller_owned VALUES (1)")
        with pytest.raises(ValueError, match="外部"):
            service(caller)
        assert caller.in_transaction()
        caller.rollback()
        assert caller.exec_driver_sql("SELECT count(*) FROM caller_owned").scalar() == 0
    with Session(registry) as session:
        with pytest.raises(ValueError, match="外部"):
            service(session)


def test_foreign_keys_must_be_explicit(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'disabled.sqlite'}")
    try:
        reservation_schema()[0].create_all(engine)
        with pytest.raises(ValueError, match="foreign_keys"):
            service(engine).reserve(request())
    finally:
        engine.dispose()


@pytest.mark.parametrize("dialect", [mysql.dialect(), mssql.dialect()], ids=["mysql", "mssql"])
def test_ddl_compile_only_and_runtime_block(registry, dialect, monkeypatch):
    metadata, _, _ = reservation_schema()
    for table in metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert "CREATE TABLE" in ddl
    monkeypatch.setattr(registry.dialect, "name", dialect.name)
    with pytest.raises(NotImplementedError, match="runtime"):
        service(registry)


@pytest.mark.parametrize("options", [
    {"timeout": 0}, {"timeout": float("inf")}, {"poll_interval": float("nan")},
    {"max_attempts": 0}, {"max_attempts": True}, {"max_attempts": 10001},
])
def test_invalid_retry_policy(registry, options):
    with pytest.raises(ValueError):
        service(registry, **options)


@pytest.mark.parametrize("damage", ["missing_child", "digest", "prefix", "time", "state"])
def test_registry_corruption_never_repairs_or_reallocates(registry, damage):
    req = request()
    service(registry).reserve(req)
    _, header, child = reservation_schema()
    with registry.begin() as connection:
        if damage == "missing_child":
            connection.execute(child.delete().where(child.c.position == 4))
        elif damage == "prefix":
            connection.execute(child.update().where(child.c.position == 1).values(reserved_ref_no="invalid"))
        elif damage == "state":
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.execute(header.update().values(state="ISSUED"))
        else:
            connection.execute(header.update().values(**{
                "content_digest" if damage == "digest" else "reserved_at":
                "0" * 64 if damage == "digest" else (NOW + timedelta(days=1)).isoformat()}))
    with pytest.raises(ReservationConflict):
        service(registry).reserve(req)


@pytest.mark.parametrize("mode", ["idempotent", "different_batch", "timeout", "content_conflict"])
def test_file_database_independent_engine_unique_key_race(registry, mode):
    """兩方先讀到空 registry，再競爭 INSERT；真的 DB UNIQUE，不用 mocked IntegrityError。"""
    follower_engine = open_engine(registry.url.database)
    ready, leader_done = Barrier(2), Event()
    collisions, connections = [], []
    req1 = request()
    req2 = request() if mode in ("idempotent", "content_conflict") else request(2)
    if mode == "content_conflict":
        req2 = replace(req2, fixed_context_json='{"source":"changed"}')

    def hook(engine, follower):
        first = True

        @event.listens_for(engine, "before_cursor_execute")
        def before(connection, cursor, statement, parameters, context, many):
            nonlocal first
            if first and statement.startswith("INSERT INTO quote_number_reservation "):
                first = False
                connections.append(id(connection.connection.driver_connection))
                ready.wait(timeout=5)
                if follower:
                    assert leader_done.wait(timeout=5)

        @event.listens_for(engine, "handle_error")
        def error(context):
            collisions.append(str(context.original_exception))

    hook(registry, False)
    hook(follower_engine, True)

    def leader():
        try:
            return service(registry).reserve(req1)
        finally:
            leader_done.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(leader)
            b = pool.submit(lambda: service(follower_engine, FakeTime(advance_wall=mode != "timeout")).reserve(req2))
            first = a.result(timeout=10)
            if mode in ("timeout", "content_conflict"):
                with pytest.raises(ReservationTimeout if mode == "timeout" else ReservationConflict):
                    b.result(timeout=10)
            else:
                second = b.result(timeout=10)
        assert len(set(connections)) == 2
        assert any("UNIQUE constraint failed" in error for error in collisions)
        if mode == "idempotent":
            assert first == second and counts(registry) == (1, 4)
        elif mode == "different_batch":
            assert first["reservation"]["prefix"] != second["reservation"]["prefix"]
            assert counts(registry) == (2, 8)
        else:
            assert counts(registry) == (1, 4)
    finally:
        follower_engine.dispose()


@pytest.mark.parametrize("field,value", [("workgroup", "104"), ("batch_id", ident(999)), ("prefix", "20261002083046")])
def test_composite_foreign_key_rejects_wrong_owner(registry, field, value):
    service(registry).reserve(request())
    _, _, child = reservation_schema()
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        with registry.begin() as connection:
            connection.execute(child.update().where(child.c.position == 1).values(**{field: value}))


@pytest.mark.parametrize("field,value", [("schema_version", "v2"), ("request_version", "v2"),
                                         ("child_count", 0), ("child_count", 1000), ("state", "ISSUED")])
def test_header_database_constraints(registry, field, value):
    service(registry).reserve(request())
    _, header, _ = reservation_schema()
    with pytest.raises(IntegrityError):
        with registry.begin() as connection:
            connection.execute(header.update().values(**{field: value}))


def test_schema_not_registered_on_formal_base():
    from database.connection import Base
    before = set(Base.metadata.tables)
    metadata, _, _ = reservation_schema()
    assert set(Base.metadata.tables) == before
    assert not (set(metadata.tables) & before)


@pytest.mark.parametrize("part", ["whole", "header", "child"])
def test_reservation_rejected_by_all_existing_document_entries(registry, monkeypatch, part):
    from agent.multi_quote import from_single_quote, new_multi_quote
    from agent.quote_batch import checked_quote_batch
    from database import repository
    from engine.multi_trial import checked_internal_trial
    from engine.multi_snapshot import build_snapshot_layout
    from engine.preview import checked_preview, freeze_preview
    from engine.quote_batch_trial import checked_batch_trial
    from agent.tools import create_quote
    from engine.snapshot import build_snapshot_items, create_quote_snapshot

    result = service(registry).reserve(request())
    doc = {"whole": result, "header": result["reservation"], "child": result["children"][0]}[part]
    deny = Mock(side_effect=AssertionError("不得查正式 DB"))
    monkeypatch.setattr(repository, "_session", deny)
    draft = new_multi_quote()
    for call in (
        lambda: freeze_preview(doc), lambda: checked_preview({"preview": doc}, "fake"),
        lambda: create_quote(doc, preview_id="fake"),
        lambda: build_snapshot_items(doc, {}, "fake"),
        lambda: build_snapshot_items(None, doc, "fake"),
        lambda: create_quote_snapshot(None, {}, preview=doc),
        lambda: repository.save_quote_snapshot(doc),
        lambda: repository.save_quote_snapshot([doc]),
        lambda: repository.save_quote_snapshot([{"ref_no": "fake"}], preview=doc),
        lambda: from_single_quote(doc),
        lambda: checked_internal_trial(draft, doc, actor="測試"),
        lambda: checked_batch_trial(draft, doc, actor="測試"),
        lambda: checked_quote_batch(draft, doc, actor="測試"),
        lambda: build_snapshot_layout(draft, doc, actor="測試"),
        lambda: service(registry).reserve(doc),
    ):
        with pytest.raises(ValueError):
            call()
    deny.assert_not_called()


@pytest.mark.parametrize("kind", ["old_trial", "batch_trial", "expansion"])
def test_real_internal_trial_not_promoted_to_reservation(registry, kind):
    from quote_batch_fixtures import four_items
    from agent.quote_batch import build_quote_batch
    from engine.multi_trial import build_internal_trial
    from engine.quote_batch_trial import build_batch_trial
    batch, draft = four_items(reviewed=False)
    builder = {"old_trial": build_internal_trial, "batch_trial": build_batch_trial,
               "expansion": build_quote_batch}[kind]
    trial = builder(draft, actor="測試", source_batch=batch)
    with pytest.raises(ValueError, match="typed"):
        service(registry).reserve(trial)
    assert counts(registry) == (0, 0)


@pytest.mark.parametrize("pool_name", ["StaticPool", "SingletonThreadPool"])
def test_shared_connection_pool_rejected_without_touching_caller(tmp_path, pool_name):
    from sqlalchemy import pool
    engine = create_engine(f"sqlite:///{tmp_path / 'shared.sqlite'}", poolclass=getattr(pool, pool_name))
    try:
        with engine.connect() as caller:
            caller.exec_driver_sql("CREATE TABLE caller_owned (value INTEGER)")
            caller.commit()
            caller.exec_driver_sql("INSERT INTO caller_owned VALUES (1)")
            with pytest.raises(ValueError, match="共享"):
                service(engine)
            assert caller.in_transaction()
            assert caller.exec_driver_sql("SELECT count(*) FROM caller_owned").scalar() == 1
            caller.rollback()
            assert caller.exec_driver_sql("SELECT count(*) FROM caller_owned").scalar() == 0
    finally:
        engine.dispose()


def test_separate_checkout_never_commits_caller_transaction(registry):
    with registry.connect() as caller:
        caller.exec_driver_sql("CREATE TABLE caller_owned (value INTEGER)")
        caller.commit()
        caller.exec_driver_sql("INSERT INTO caller_owned VALUES (1)")
        with pytest.raises(ReservationTimeout):
            service(registry, max_attempts=2).reserve(request())
        assert caller.in_transaction()
        caller.rollback()
        assert caller.exec_driver_sql("SELECT count(*) FROM caller_owned").scalar() == 0
    assert counts(registry) == (0, 0)


def test_full_reference_unique_across_batches(registry):
    first = service(registry).reserve(request())
    service(registry, FakeTime(NOW + timedelta(seconds=1))).reserve(request(2))
    _, _, child = reservation_schema()
    with pytest.raises(IntegrityError, match="reserved_ref_no"):
        with registry.begin() as connection:
            connection.execute(child.update().where(child.c.batch_id == request(2).batch_id).where(
                child.c.position == 1).values(reserved_ref_no=first["children"][0]["reserved_ref_no"]))


def test_invalid_request_never_connects(registry, monkeypatch):
    allocator = service(registry)
    connect = Mock(side_effect=AssertionError("無效 request 不得連線"))
    monkeypatch.setattr(registry, "connect", connect)
    with pytest.raises(ValueError):
        allocator.reserve(replace(request(), children=()))
    connect.assert_not_called()


def test_no_application_or_model_entry_imports_reservation():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    paths = [root / "app.py", *(root / "utils").glob("*.py"), *(root / "agent").glob("*.py")]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "quote_number_reservation" not in text
