"""六表雙庫唯讀對帳；破壞性反例僅作用於暫存合成副本。"""
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from database.development_atomic_schema import development_atomic_schema
from engine import development_integrity_inspection as integrity
from engine import development_snapshot_comparison as comparison
from engine.development_atomic_contract import build_development_reservation_request
from engine.development_registry_comparison import compare_development_registries
from engine.quote_number_reservation import QuoteNumberReservationService
from development_atomic_fixtures import SHARED, change_row, ident, request
from test_development_atomic_snapshot import database, reserve, service
from test_development_integrity_inspection import COLUMNS, dump, mutate
from test_quote_number_reservation import NOW, open_engine, request as reservation_request


def clone(source, target):
    assert not target.exists()
    with closing(sqlite3.connect(source)) as left, closing(sqlite3.connect(target)) as right:
        left.backup(right)
    return target


@pytest.fixture
def pair(database, tmp_path):
    reserve(database, request())
    service(database, clock=lambda: NOW).save(request())
    path = Path(database.url.database)
    return path, clone(path, tmp_path / "candidate.sqlite")


def table_results(report):
    return {item.table: item for item in report.tables}


def clear(path, tables=comparison.TABLES):
    for name in reversed(tables):
        mutate(path, f'DELETE FROM "{name}"')


def populate(path, req, *, saved=True, clock=NOW):
    engine = open_engine(path)
    try:
        QuoteNumberReservationService(engine, clock=lambda: clock).reserve(
            build_development_reservation_request(req))
        if saved:
            service(engine, clock=lambda: clock).save(req)
    finally:
        engine.dispose()


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("state", ["empty", "reserved", "saved"])
def test_equal_snapshots_are_readonly_private_and_never_authorized(database, tmp_path, journal, state):
    left = Path(database.url.database)
    mutate(left, f"PRAGMA journal_mode={journal}")
    if state != "empty":
        reserve(database, request())
    if state == "saved":
        service(database).save(request())
    right = clone(left, tmp_path / "candidate.sqlite")
    before = (dump(left), dump(right))
    report = comparison.compare_development_snapshots(left, right)
    assert report.contents_match and report.identity_conflicts == ()
    assert tuple(item.table for item in report.tables) == comparison.TABLES
    assert report.reference_integrity == report.candidate_integrity
    assert report.reference_integrity.verified_saved_batches == (state == "saved")
    assert report.reference_integrity.reservation_only_batches == (state == "reserved")
    assert all(item.reference_digest == item.candidate_digest for item in report.tables)
    assert report.report_version == comparison.REPORT_VERSION
    assert "同一復原點" in report.warning and "權威" in report.warning
    assert all(getattr(report, name) is False for name in (
        "can_quote", "can_confirm", "can_save", "can_resume_numbering"))
    for secret in (str(left), str(right), SHARED, "SYNTHETIC-ORDER", ident(1), "20261002083045_001"):
        assert secret not in repr(report)
    with pytest.raises(FrozenInstanceError):
        report.tables = ()
    assert comparison.compare_development_snapshots(left, right) == report
    assert before == (dump(left), dump(right))


@pytest.mark.parametrize("tables", [comparison.TABLES, comparison.TABLES[2:]])
def test_whole_group_loss_is_reported_even_when_each_side_passes_integrity(pair, tables):
    left, right = pair
    clear(right, tables)
    integrity.inspect_development_integrity(right)
    if tables == comparison.TABLES[2:]:
        assert compare_development_registries(left, right).contents_match
    report = comparison.compare_development_snapshots(left, right)
    assert not report.contents_match
    counts = dict(zip(comparison.TABLES, (1, 4, 1, 4, 4, 16)))
    assert report.reference_integrity.verified_saved_batches == 1
    assert report.candidate_integrity.verified_saved_batches == 0
    for item in report.tables:
        assert item.missing_rows == (counts[item.table] if item.table in tables else 0)
        assert item.extra_rows == item.changed_rows == 0
    reverse = comparison.compare_development_snapshots(right, left)
    for a, b in zip(report.tables, reverse.tables, strict=True):
        assert (a.missing_rows, a.extra_rows) == (b.extra_rows, b.missing_rows)
        assert a.reference_digest == b.candidate_digest


def test_old_backup_missing_later_reservation_is_not_latest(pair, database):
    QuoteNumberReservationService(database, clock=lambda: NOW + timedelta(seconds=1)).reserve(
        reservation_request(number=9))
    report = comparison.compare_development_snapshots(*pair)
    assert not report.contents_match
    assert report.reference_integrity.reservation_only_batches == 1
    assert table_results(report)[integrity.HEADER].missing_rows == 1


def test_valid_same_identity_rewrite_is_detected_not_just_counts(pair):
    left, right = pair
    clear(right)
    # 真正重建完整合法文件及持久摘要，不能只測未同步的單欄破壞。
    populate(right, change_row(request(), original_qty="1.000000000000000002"))
    assert integrity.inspect_development_integrity(left) == integrity.inspect_development_integrity(right)
    report = comparison.compare_development_snapshots(*pair)
    assert not report.contents_match
    result = table_results(report)
    # 每張子文件皆固定整批摘要，因此只有一筆成本改變仍影響全部四張子文件。
    for name, count in ((integrity.HEADER, 1), (integrity.BATCH, 1), (integrity.CHILD, 4), (integrity.COST, 1)):
        assert result[name].changed_rows == count
        assert result[name].reference_digest != result[name].candidate_digest
    assert result[integrity.LINK].changed_rows == result[integrity.RESERVED_CHILD].changed_rows == 0
    assert all(item.missing_rows == item.extra_rows == 0 for item in report.tables)


def test_registry_equal_but_different_snapshot_creation_time_is_detected(pair):
    left, right = pair
    clear(right, comparison.TABLES[2:])
    engine = open_engine(right)
    try:
        service(engine, clock=lambda: NOW + timedelta(seconds=1)).save(request())
    finally:
        engine.dispose()
    assert compare_development_registries(left, right).contents_match
    report = comparison.compare_development_snapshots(*pair)
    assert not report.contents_match
    assert table_results(report)[integrity.BATCH].changed_rows == 1


def test_cross_workgroup_fork_reports_global_numbers_and_save_identity_conflicts(pair):
    _, right = pair
    clear(right)
    req = replace(request(), workgroup="104", batch_id=ident(80), request_id=ident(81), preview_identity=ident(82))
    populate(right, req)
    report = comparison.compare_development_snapshots(*pair)
    conflicts = {(item.table, item.columns): item.count for item in report.identity_conflicts}
    assert conflicts[(integrity.HEADER, ("prefix",))] == 1
    assert conflicts[(integrity.RESERVED_CHILD, ("reserved_ref_no",))] == 4
    assert conflicts[(integrity.RESERVED_CHILD, ("child_quote_id",))] == 4
    assert conflicts[(integrity.BATCH, ("save_id",))] == 1
    assert conflicts[(integrity.CHILD, ("ref_no",))] == 4
    assert not report.contents_match


def test_composite_unique_source_mapping_conflict_is_not_ignored(pair):
    _, right = pair
    clear(right)
    req = replace(request(), children=tuple(replace(child, child_quote_id=ident(900 + i))
                                          for i, child in enumerate(request().children)))
    populate(right, req)
    conflicts = {(item.table, item.columns): item.count
                 for item in comparison.compare_development_snapshots(*pair).identity_conflicts}
    assert conflicts[(integrity.LINK, ("batch_id", "source_item_id"))] == 4
    assert conflicts[(integrity.LINK, ("batch_id", "configuration_line_id"))] == 4
    assert conflicts[(integrity.CHILD, ("batch_id", "position"))] == 4


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("table", comparison.TABLES)
def test_partial_loss_on_either_side_rejects_entire_comparison(pair, side, table):
    mutate(pair[side], f'DELETE FROM "{table}" WHERE rowid=(SELECT min(rowid) FROM "{table}")')
    before = tuple(dump(path) for path in pair)
    with pytest.raises(comparison.SnapshotComparisonError):
        comparison.compare_development_snapshots(*pair)
    assert tuple(dump(path) for path in pair) == before


@pytest.mark.parametrize("table,column,integer", COLUMNS)
def test_identical_corruption_of_every_column_is_not_a_success(pair, table, column, integer):
    expression = f'"{column}" + 1000' if integer else f'"{column}" || \'sensitive-tampering\''
    for path in pair:
        mutate(path, f'UPDATE "{table}" SET "{column}"={expression} '
                     f'WHERE rowid=(SELECT min(rowid) FROM "{table}")')
    with pytest.raises(comparison.SnapshotComparisonError) as caught:
        comparison.compare_development_snapshots(*pair)
    assert "sensitive" not in str(caught.value) and str(pair[0]) not in str(caught.value)


def test_common_whole_deletion_and_common_old_backups_can_still_match(pair):
    for path in pair:
        clear(path)
    report = comparison.compare_development_snapshots(*pair)
    assert report.contents_match and not report.can_resume_numbering
    assert "整組刪除" in report.warning


def test_common_valid_rewrite_is_not_authenticity_proof(pair):
    before = comparison.compare_development_snapshots(*pair)
    for path in pair:
        clear(path)
        populate(path, change_row(request(), original_qty="1.000000000000000002"))
    after = comparison.compare_development_snapshots(*pair)
    assert after.contents_match and after != before
    assert not after.can_resume_numbering and "一致重寫" in after.warning


def test_standalone_reservation_contract_is_allowed_without_claiming_saved(database, tmp_path):
    QuoteNumberReservationService(database, clock=lambda: NOW).reserve(reservation_request())
    left = Path(database.url.database)
    right = clone(left, tmp_path / "standalone.sqlite")
    report = comparison.compare_development_snapshots(left, right)
    assert report.contents_match
    assert report.reference_integrity.reservation_only_batches == 1
    assert report.reference_integrity.verified_saved_batches == 0


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_uncommitted_partial_change_is_not_visible_and_writer_is_untouched(pair, side, journal):
    mutate(pair[side], f"PRAGMA journal_mode={journal}")
    baseline = comparison.compare_development_snapshots(*pair)
    with closing(sqlite3.connect(pair[side])) as writer:
        writer.execute(f"DELETE FROM {integrity.COST} WHERE seq_no='00002'")
        assert comparison.compare_development_snapshots(*pair) == baseline
        assert writer.in_transaction
        writer.commit()
    with pytest.raises(comparison.SnapshotComparisonError):
        comparison.compare_development_snapshots(*pair)


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("payload", ['{"secret":NaN}', '{"x":1,"x":2}', '[1]', '{}', '{"secret":"\\ud800"}'])
def test_invalid_json_never_leaks_raw_values_or_returns_partial_report(pair, side, payload):
    mutate(pair[side], f"UPDATE {integrity.BATCH} SET payload=?", (payload,))
    with pytest.raises(comparison.SnapshotComparisonError) as caught:
        comparison.compare_development_snapshots(*pair)
    assert "secret" not in str(caught.value) and str(pair[side]) not in str(caught.value)


@pytest.mark.parametrize("side", [0, 1])
def test_blob_storage_is_rejected_not_coerced(pair, side):
    mutate(pair[side], f"UPDATE {integrity.COST} SET payload=CAST(payload AS BLOB)")
    with pytest.raises(comparison.SnapshotComparisonError):
        comparison.compare_development_snapshots(*pair)


def test_row_insertion_order_is_irrelevant(pair):
    baseline = comparison.compare_development_snapshots(*pair)
    with closing(sqlite3.connect(pair[1])) as connection:
        for name in comparison.TABLES:
            rows = connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid DESC').fetchall()
            connection.execute(f'DELETE FROM "{name}"')
            connection.executemany(f'INSERT INTO "{name}" VALUES ({",".join("?" for _ in rows[0])})', rows)
        connection.commit()
    assert comparison.compare_development_snapshots(*pair) == baseline


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("sql", [
    "DROP TABLE development_atomic_cost",
    "ALTER TABLE development_atomic_cost ADD COLUMN extra TEXT",
    "CREATE TRIGGER unexpected AFTER INSERT ON development_atomic_cost BEGIN SELECT 1; END",
])
def test_drifted_schema_is_rejected_before_that_side_reads_content(pair, monkeypatch, side, sql):
    mutate(pair[side], sql)
    original = integrity._read_table
    reads = []

    def read(connection, table, deadline):
        reads.append(table.name)
        return original(connection, table, deadline)

    monkeypatch.setattr(integrity, "_read_table", read)
    with pytest.raises(comparison.SnapshotComparisonError, match="結構"):
        comparison.compare_development_snapshots(*pair)
    assert len(reads) == side * 6


@pytest.mark.parametrize("timeout", [0, -1, True, None, "2", float("nan"), float("inf"), 31])
def test_invalid_timeout(pair, timeout):
    with pytest.raises(ValueError):
        comparison.compare_development_snapshots(*pair, timeout=timeout)


def test_invalid_paths_and_external_connections_are_rejected(pair, database, tmp_path):
    left, right = pair
    missing = tmp_path / "missing.sqlite"
    link = tmp_path / "link.sqlite"
    link.symlink_to(right)
    hardlink = tmp_path / "hardlink.sqlite"
    hardlink.hardlink_to(left)
    for value in (missing, tmp_path, link, left, hardlink):
        with pytest.raises(ValueError):
            comparison.compare_development_snapshots(left, value)
    with closing(sqlite3.connect(left)) as writer:
        writer.execute(f"DELETE FROM {integrity.COST} WHERE seq_no='00002'")
        for value in (str(right), right.as_uri(), writer, database, None):
            with pytest.raises(TypeError):
                comparison.compare_development_snapshots(left, value)
        assert comparison.compare_development_snapshots(*pair).contents_match
        assert writer.in_transaction
        writer.rollback()
    assert not missing.exists()


@pytest.mark.parametrize("side", [0, 1])
def test_non_database_errors_are_sanitized(pair, side):
    pair[side].write_bytes(b"sensitive content")
    with pytest.raises(comparison.SnapshotComparisonError) as caught:
        comparison.compare_development_snapshots(*pair)
    assert "sensitive" not in str(caught.value) and str(pair[side]) not in str(caught.value)


@pytest.mark.parametrize("limit", ["rows", "row_bytes", "total_bytes"])
def test_all_six_table_capacity_checks_precede_content_reads(pair, monkeypatch, limit):
    if limit == "rows":
        monkeypatch.setattr(integrity, "MAX_ROWS", {**integrity.MAX_ROWS, integrity.COST: 15})
    else:
        monkeypatch.setattr(integrity, "MAX_ROW_BYTES" if limit == "row_bytes" else "MAX_TOTAL_BYTES", 1)
    monkeypatch.setattr(integrity, "_read_table", lambda *a: pytest.fail("read before all preflights"))
    with pytest.raises(comparison.SnapshotComparisonError, match="容量"):
        comparison.compare_development_snapshots(*pair)


def test_second_side_capacity_failure_does_not_return_first_side_report(pair, monkeypatch):
    # 參考端空、候選端完整；限制只在讀候選端時觸發。
    clear(pair[0])
    monkeypatch.setattr(integrity, "MAX_ROWS", {**integrity.MAX_ROWS, integrity.COST: 15})
    original = integrity._read_table
    reads = []

    def read(connection, table, deadline):
        reads.append(table.name)
        return original(connection, table, deadline)

    monkeypatch.setattr(integrity, "_read_table", read)
    with pytest.raises(comparison.SnapshotComparisonError, match="容量"):
        comparison.compare_development_snapshots(*pair)
    assert tuple(reads) == comparison.TABLES


def test_exact_byte_capacity_and_special_uri_filename(pair, tmp_path, monkeypatch):
    special = clone(pair[1], tmp_path / "隔離 ?mode=rw&#.sqlite")
    sizes, counts = [], {}
    metadata, *_ = development_atomic_schema()
    with closing(sqlite3.connect(special)) as connection:
        for name in comparison.TABLES:
            expr = "+".join(f'length(CAST("{column.name}" AS BLOB))' for column in metadata.tables[name].columns)
            values = connection.execute(f'SELECT {expr} FROM "{name}"').fetchall()
            sizes.extend(size for (size,) in values)
            counts[name] = len(values)
    monkeypatch.setattr(integrity, "MAX_ROWS", counts)
    monkeypatch.setattr(integrity, "MAX_ROW_BYTES", max(sizes))
    monkeypatch.setattr(integrity, "MAX_TOTAL_BYTES", sum(sizes))
    assert comparison.compare_development_snapshots(pair[0], special).contents_match
    monkeypatch.setattr(integrity, "MAX_TOTAL_BYTES", sum(sizes) - 1)
    with pytest.raises(comparison.SnapshotComparisonError, match="容量"):
        comparison.compare_development_snapshots(pair[0], special)


def test_actual_connections_are_readonly_and_no_services_are_constructed(pair, monkeypatch):
    for path in pair:
        mutate(path, "CREATE TABLE private_data(secret TEXT)")
    original = integrity._read_table
    connections = []

    def probe(connection, table, deadline):
        assert connection.in_transaction
        if connection not in connections:
            connections.append(connection)
            for sql in (f"DELETE FROM {integrity.COST}", "CREATE TABLE forbidden(x)",
                        "SELECT * FROM private_data", "ATTACH ':memory:' AS other", "COMMIT",
                        "PRAGMA writable_schema=ON", "SELECT random()"):
                with pytest.raises(sqlite3.DatabaseError):
                    connection.execute(sql)
        return original(connection, table, deadline)

    monkeypatch.setattr(integrity, "_read_table", probe)
    monkeypatch.setattr(integrity, "inspect_development_integrity", lambda *a: pytest.fail("separate inspection"))
    monkeypatch.setattr(integrity.DevelopmentAtomicSnapshotService, "__init__", lambda *a, **k: pytest.fail("service"))
    monkeypatch.setattr(integrity.QuoteNumberReservationService, "__init__", lambda *a, **k: pytest.fail("registry"))
    assert comparison.compare_development_snapshots(*pair).contents_match
    assert len(connections) == 2
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("stage", ["schema", "content", "summary"])
def test_wal_validation_and_comparison_use_same_snapshot_per_side(pair, monkeypatch, side, stage):
    for path in pair:
        mutate(path, "PRAGMA journal_mode=WAL")
    baseline = comparison.compare_development_snapshots(*pair)
    module, method = {"schema": (comparison, "_table_signature"), "content": (integrity, "_read_table"),
                      "summary": (comparison, "_summarize")}[stage]
    original = getattr(module, method)
    calls = 0

    def change(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == side * 6 + 1:
            mutate(pair[side], f"DELETE FROM {integrity.COST} WHERE seq_no='00002'")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(module, method, change)
        assert comparison.compare_development_snapshots(*pair) == baseline
    with pytest.raises(comparison.SnapshotComparisonError):
        comparison.compare_development_snapshots(*pair)


def test_cross_database_time_gap_is_not_a_simultaneous_snapshot(pair, monkeypatch):
    original = comparison._read_snapshot

    def change(path, *args):
        result = original(path, *args)
        if path == pair[0]:
            clear(path, comparison.TABLES[2:])
        return result

    with monkeypatch.context() as patch:
        patch.setattr(comparison, "_read_snapshot", change)
        assert comparison.compare_development_snapshots(*pair).contents_match
    assert not comparison.compare_development_snapshots(*pair).contents_match


@pytest.mark.parametrize("side", [0, 1])
def test_locked_database_does_not_commit_external_writer(pair, side):
    with closing(sqlite3.connect(pair[side])) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(comparison.SnapshotComparisonError):
            comparison.compare_development_snapshots(*pair, timeout=0.01)
        assert writer.in_transaction
        writer.rollback()


@pytest.mark.parametrize("stage", ["first_read", "second_read", "comparison"])
def test_shared_deadline_never_resets_or_returns_late_success(pair, monkeypatch, stage):
    method = "_compare" if stage == "comparison" else "_read_snapshot"
    original = getattr(comparison, method)
    calls = 0

    def expire(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == (2 if stage == "second_read" else 1):
            monkeypatch.setattr(comparison.time, "monotonic", lambda: args[-1] + 1)
        return result

    monkeypatch.setattr(comparison, method, expire)
    with pytest.raises(comparison.SnapshotComparisonError, match="逾時"):
        comparison.compare_development_snapshots(*pair)


def test_midread_failure_closes_both_connections_without_reusing_success(pair, monkeypatch):
    baseline = comparison.compare_development_snapshots(*pair)
    original = integrity._read_table
    connections = []

    def fail(connection, table, deadline):
        if connection not in connections:
            connections.append(connection)
        if len(connections) == 2 and table.name == integrity.COST:
            raise sqlite3.OperationalError("sensitive SQL and parameters")
        return original(connection, table, deadline)

    with monkeypatch.context() as patch:
        patch.setattr(integrity, "_read_table", fail)
        with pytest.raises(comparison.SnapshotComparisonError) as caught:
            comparison.compare_development_snapshots(*pair)
        assert "sensitive" not in str(caught.value)
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
    assert comparison.compare_development_snapshots(*pair) == baseline


def test_fresh_process_import_is_inert_private_files_and_network_forbidden(pair):
    root = Path(__file__).resolve().parents[1]
    code = r'''
from pathlib import Path
import sys
root, left, right = map(Path, sys.argv[1:])
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        p = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).resolve()
        if p.name.startswith(".env") or p.is_relative_to(root / "logs"):
            raise AssertionError("private file")
    if event.startswith("socket."):
        raise AssertionError("network")
    if event == "import" and args[0] in {
        "config", "database.connection", "database.models", "database.repository",
        "engine.pricing", "engine.snapshot", "streamlit", "openai",
    }:
        raise AssertionError("production dependency")
sys.addaudithook(audit)
sys.path.insert(0, str(root))
import sqlite3
connect = sqlite3.connect
sqlite3.connect = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("import connection"))
from engine.development_snapshot_comparison import compare_development_snapshots
sqlite3.connect = connect
assert all(path.is_file() and not path.is_relative_to(root) for path in (left, right))
report = compare_development_snapshots(left, right)
assert report.contents_match and not report.can_resume_numbering
print(report)
'''
    result = subprocess.run([sys.executable, "-I", "-c", code, str(root), *(str(path) for path in pair)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == repr(comparison.compare_development_snapshots(*pair))
