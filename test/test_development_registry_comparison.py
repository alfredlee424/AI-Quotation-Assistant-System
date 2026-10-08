"""T43 雙庫對帳：僅暫存合成資料；相等不代表完整、最新或可恢復配號。"""
from contextlib import closing
from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from engine import development_registry_comparison as comparison
from engine.quote_number_reservation import QuoteNumberReservationService
from test_quote_number_reservation import registry, request, NOW, open_engine
from test_development_atomic_snapshot import database, request as atomic_request, reserve, service
from test_development_schema_inspection import image, alter_empty_table


def clone(source, target):
    assert not target.exists() and source != target
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as left:
        with closing(sqlite3.connect(target)) as right:
            left.backup(right)
    return target


def reserve_number(engine, number=1, *, workgroup="103", seconds=0):
    return QuoteNumberReservationService(engine, clock=lambda: NOW + timedelta(seconds=seconds)).reserve(
        request(number, workgroup=workgroup))


@pytest.fixture
def pair(registry, tmp_path):
    reserve_number(registry)
    left = Path(registry.url.database)
    return left, clone(left, tmp_path / "candidate.sqlite")


def mutate(path, sql, parameters=()):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(sql, parameters)
        connection.commit()


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("state", ["empty", "reserved", "saved"])
def test_equal_clones_are_readonly_and_never_authorize(database, tmp_path, journal, state):
    path = Path(database.url.database)
    with closing(sqlite3.connect(path)) as keeper:
        keeper.execute(f"PRAGMA journal_mode={journal}")
        if state != "empty":
            req = atomic_request()
            reserve(database, req)
            if state == "saved":
                service(database).save(req)
        candidate = clone(path, tmp_path / "clone.sqlite")
        before = image(path), image(candidate)
        report = comparison.compare_development_registries(path, candidate)
        assert report.contents_match and not report.identity_conflicts
        assert [row.reference_rows for row in report.tables] == ([0, 0] if state == "empty" else [1, 4])
        assert all(row.reference_digest == row.candidate_digest for row in report.tables)
        assert not any((report.can_quote, report.can_confirm, report.can_save, report.can_resume_numbering))
        assert report.report_version == comparison.REPORT_VERSION
        assert "不是跨庫同一復原點" in report.warning and "不代表最新" in report.warning
        assert comparison.compare_development_registries(path, candidate) == report
        assert (image(path), image(candidate)) == before
        with pytest.raises(FrozenInstanceError):
            report.tables = ()


def test_registry_only_schema_does_not_require_or_create_snapshot_tables(pair):
    before = tuple(image(path) for path in pair)
    report = comparison.compare_development_registries(*pair)
    assert report.contents_match
    assert tuple(row.table for row in report.tables) == comparison.TABLES
    assert tuple(image(path) for path in pair) == before


@pytest.mark.parametrize("reverse", [False, True])
def test_old_backup_missing_whole_reservation_detected_in_both_directions(registry, tmp_path, reverse):
    path = Path(registry.url.database)
    reserve_number(registry)
    old = clone(path, tmp_path / "old.sqlite")
    reserve_number(registry, 2, workgroup="104", seconds=1)
    report = comparison.compare_development_registries(*( (old, path) if reverse else (path, old)))
    assert not report.contents_match
    assert [row.extra_rows if reverse else row.missing_rows for row in report.tables] == [1, 4]
    assert all(row.changed_rows == 0 for row in report.tables)
    assert not report.can_resume_numbering


@pytest.mark.parametrize("table,where,expected", [
    (comparison.HEADER, "1=1", (1, 0)),
    (comparison.CHILD, "position=2", (0, 1)),
    (comparison.CHILD, "position IN (2,4)", (0, 2)),
])
def test_missing_header_or_children_reported_without_repair(pair, table, where, expected):
    mutate(pair[1], f"DELETE FROM {table} WHERE {where}")
    before = tuple(image(path) for path in pair)
    report = comparison.compare_development_registries(*pair)
    assert tuple(row.missing_rows for row in report.tables) == expected
    assert not report.contents_match
    assert tuple(image(path) for path in pair) == before


@pytest.mark.parametrize("column,value", [
    ("workgroup", "104"), ("request_id", "a" * 32), ("preview_identity", "b" * 32),
    ("prefix", "20261003083045"), ("reserved_at", "2026-10-03T08:30:45+08:00"),
    ("origin", "changed"), ("child_count", 3), ("content_digest", "0" * 64),
    ("payload", '{"source":"private changed content"}'),
])
def test_every_mutable_header_projection_detected_even_with_same_primary_key(pair, column, value):
    mutate(pair[1], f"UPDATE {comparison.HEADER} SET {column}=?", (value,))
    report = comparison.compare_development_registries(*pair)
    assert report.tables[0].changed_rows == 1 and not report.contents_match
    assert report.tables[0].missing_rows == report.tables[0].extra_rows == 0
    assert "private changed content" not in repr(report)


@pytest.mark.parametrize("column,value", [
    ("workgroup", "104"), ("prefix", "20261003083045"), ("source_item_id", "a" * 32),
    ("configuration_line_id", "b" * 32), ("position", 5), ("suffix", "005"),
    ("reserved_ref_no", "20261003083045_001"),
])
def test_every_child_mapping_projection_detected(pair, column, value):
    mutate(pair[1], f"UPDATE {comparison.CHILD} SET {column}=? WHERE position=1", (value,))
    report = comparison.compare_development_registries(*pair)
    assert report.tables[1].changed_rows == 1 and not report.contents_match


def test_payload_compared_directly_not_trusting_stored_digest_or_json_equivalence(pair):
    mutate(pair[1], f"UPDATE {comparison.HEADER} SET payload=payload || ' '")
    report = comparison.compare_development_registries(*pair)
    assert not report.contents_match and report.tables[0].changed_rows == 1


def test_same_counts_and_high_water_do_not_prove_same_history(registry, tmp_path):
    left = Path(registry.url.database)
    reserve_number(registry, 1)
    reserve_number(registry, 3, seconds=2)
    right = clone(left, tmp_path / "fork.sqlite")
    mutate(right, f"UPDATE {comparison.HEADER} SET origin='changed old row' WHERE prefix=?",
           ("20261002083045",))
    report = comparison.compare_development_registries(left, right)
    assert report.tables[0].reference_rows == report.tables[0].candidate_rows == 2
    assert report.tables[0].changed_rows == 1 and not report.contents_match


def test_same_second_different_batches_and_workgroups_are_global_conflicts(registry, tmp_path):
    left = Path(registry.url.database)
    right = clone(left, tmp_path / "fork.sqlite")
    reserve_number(registry, 1)
    target = open_engine(right)
    try:
        reserve_number(target, 2, workgroup="104")
    finally:
        target.dispose()
    report = comparison.compare_development_registries(left, right)
    assert [(r.missing_rows, r.extra_rows) for r in report.tables] == [(1, 1), (4, 4)]
    assert report.identity_conflicts == (
        comparison.RegistryIdentityConflict(comparison.HEADER, "prefix", 1),
        comparison.RegistryIdentityConflict(comparison.CHILD, "reserved_ref_no", 4),
    )


def test_reused_request_preview_child_identity_reported(pair):
    # 模擬兩份各自有唯一鍵的分岔庫；兩端相同唯一值屬於不同批次主鍵。
    for table in comparison.TABLES:
        mutate(pair[1], f"UPDATE {table} SET batch_id=?", ("f" * 32,))
    report = comparison.compare_development_registries(*pair)
    assert [(item.column, item.count) for item in report.identity_conflicts] == [
        ("request_id", 1), ("preview_identity", 1), ("prefix", 1),
        ("child_quote_id", 4), ("reserved_ref_no", 4),
    ]


@pytest.mark.parametrize("column", ["schema_version", "request_version", "state"])
def test_unknown_persisted_version_or_state_rejected_even_if_both_copies_equal(pair, column):
    for path in pair:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(f"UPDATE {comparison.HEADER} SET {column}='unsupported'")
            connection.commit()
    with pytest.raises(comparison.RegistryComparisonError, match="未知持久版本"):
        comparison.compare_development_registries(*pair)


@pytest.mark.parametrize("damage", ["payload", "missing_child"])
def test_identical_corruption_is_not_integrity_certification(pair, damage):
    for path in pair:
        mutate(path, (f"UPDATE {comparison.HEADER} SET payload='not JSON'" if damage == "payload" else
                      f"DELETE FROM {comparison.CHILD} WHERE position=4"))
    report = comparison.compare_development_registries(*pair)
    assert report.contents_match  # 只證明两份相同；不解析 payload 或驗證全批業務完整性。
    assert "不代表最新、有效或未遭共同竄改" in report.warning
    assert not report.can_resume_numbering


def test_missing_cost_not_hidden_as_full_snapshot_health(database, tmp_path):
    req = atomic_request()
    reserve(database, req)
    service(database).save(req)
    path = Path(database.url.database)
    candidate = clone(path, tmp_path / "missing-cost.sqlite")
    mutate(candidate, "DELETE FROM development_atomic_cost WHERE position=2")
    assert comparison.compare_development_registries(path, candidate).contents_match
    from engine.development_atomic_contract import identity_of
    from engine.development_atomic_snapshot import AtomicSnapshotConflict
    target = open_engine(candidate)
    try:
        with pytest.raises(AtomicSnapshotConflict):
            service(target).read(identity_of(req))
    finally:
        target.dispose()


@pytest.mark.parametrize("table", comparison.TABLES)
@pytest.mark.parametrize("side", [0, 1])
def test_missing_tables_fail_instead_of_empty_success(pair, table, side):
    mutate(pair[side], f"DROP TABLE {table}")
    before = tuple(image(path) for path in pair)
    with pytest.raises(comparison.RegistryComparisonError, match="結構不符"):
        comparison.compare_development_registries(*pair)
    assert tuple(image(path) for path in pair) == before


@pytest.mark.parametrize("damage", ["view", "trigger", "index", "column", "unique", "version"])
def test_schema_drift_is_rejected_before_data_reads(registry, tmp_path, damage, monkeypatch):
    left = Path(registry.url.database)
    right = clone(left, tmp_path / "drift.sqlite")
    if damage == "view":
        mutate(right, f"DROP TABLE {comparison.CHILD}")
        mutate(right, f"CREATE VIEW {comparison.CHILD} AS SELECT 'secret' AS payload")
    elif damage == "trigger":
        mutate(right, f"CREATE TRIGGER extra AFTER INSERT ON {comparison.CHILD} BEGIN SELECT 'secret'; END")
    elif damage == "index":
        mutate(right, f"CREATE INDEX extra ON {comparison.CHILD}(suffix)")
    else:
        old, new = {"column": ("payload TEXT NOT NULL", "payload BLOB NOT NULL"),
                    "unique": ("UNIQUE (prefix)", "UNIQUE (prefix, origin)"),
                    "version": ("quote-number-registry-dev-v1", "future")}[damage]
        alter_empty_table(right, comparison.HEADER, old, new)
    original = comparison._read_table
    calls = []

    def observe(connection, table, *args):
        calls.append(table.name)
        return original(connection, table, *args)

    monkeypatch.setattr(comparison, "_read_table", observe)
    with pytest.raises(comparison.RegistryComparisonError, match="結構不符"):
        comparison.compare_development_registries(left, right)
    assert calls == list(comparison.TABLES)  # 只有參考端被讀；候選端不讀任何 payload。


@pytest.mark.parametrize("sql", [
    "SELECT payload FROM development_atomic_batch", "SELECT count(*) FROM development_atomic_cost",
    "SELECT payload FROM legacy", "DELETE FROM quote_number_reserved_child",
    "UPDATE quote_number_reservation SET origin='bad'", "INSERT INTO quote_number_reservation DEFAULT VALUES",
    "DROP TABLE quote_number_reservation", "CREATE TABLE forbidden(x)",
    "ATTACH ':memory:' AS other", "PRAGMA writable_schema=ON", "PRAGMA user_version=1",
    "SELECT load_extension('forbidden')", "SELECT random()", "COMMIT",
])
def test_actual_read_connection_denies_unrelated_reads_and_mutation(pair, monkeypatch, sql):
    for path in pair:
        mutate(path, "CREATE TABLE legacy(payload TEXT)")
    original = comparison._read_table
    calls = []

    def probe(connection, table, *args):
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(sql).fetchall()
        calls.append(table.name)
        return original(connection, table, *args)

    monkeypatch.setattr(comparison, "_read_table", probe)
    before = tuple(image(path) for path in pair)
    assert comparison.compare_development_registries(*pair).contents_match
    assert calls == list(comparison.TABLES) * 2
    assert tuple(image(path) for path in pair) == before


def test_report_contains_no_paths_identifiers_numbers_payload_or_sql(pair):
    report = comparison.compare_development_registries(*pair)
    text = repr(report) + report.warning
    assert all(str(path) not in text for path in pair)
    assert request().batch_id not in text
    assert all(word not in text for word in ("20261002083045", "synthetic", "合成品項", "0.34", "SELECT"))


@pytest.mark.parametrize("value", [None, "sqlite:///missing", ":memory:", 12])
@pytest.mark.parametrize("side", [0, 1])
def test_invalid_path_types_before_io(pair, value, side):
    args = list(pair)
    args[side] = value
    with pytest.raises(TypeError):
        comparison.compare_development_registries(*args)


@pytest.mark.parametrize("timeout", [0, -1, 31, float("inf"), float("nan"), True, "2"])
def test_invalid_timeout_before_io(pair, timeout):
    with pytest.raises(ValueError):
        comparison.compare_development_registries(*pair, timeout=timeout)


def test_missing_directory_symlink_same_file_and_hardlink_rejected(pair, tmp_path):
    missing = tmp_path / "missing.sqlite"
    symlink = tmp_path / "symlink.sqlite"
    symlink.symlink_to(pair[0])
    hardlink = tmp_path / "hardlink.sqlite"
    hardlink.hardlink_to(pair[0])
    for bad in (missing, tmp_path, symlink, pair[0], hardlink):
        with pytest.raises(ValueError):
            comparison.compare_development_registries(pair[0], bad)
    assert not missing.exists()


def test_corrupt_file_and_midscan_error_are_sanitized_without_partial_report(pair, tmp_path, monkeypatch):
    bad = tmp_path / "sensitive-path.sqlite"
    bad.write_bytes(b"sensitive raw data")
    with pytest.raises(comparison.RegistryComparisonError) as caught:
        comparison.compare_development_registries(pair[0], bad)
    assert "sensitive" not in str(caught.value)
    original = comparison._read_table
    count = 0

    def fail(connection, table, *args):
        nonlocal count
        count += 1
        if count == 4:
            raise sqlite3.OperationalError("sensitive SQL and parameters")
        return original(connection, table, *args)

    with monkeypatch.context() as patch:
        patch.setattr(comparison, "_read_table", fail)
        with pytest.raises(comparison.RegistryComparisonError) as caught:
            comparison.compare_development_registries(*pair)
        assert "sensitive" not in str(caught.value)
    assert comparison.compare_development_registries(*pair).contents_match
    assert bad.read_bytes() == b"sensitive raw data"


@pytest.mark.parametrize("limit", ["header_rows", "child_rows", "row_bytes", "total_bytes"])
def test_capacity_limits_fail_without_truncating_or_reading_oversize_payload(pair, monkeypatch, limit):
    if limit.endswith("rows"):
        monkeypatch.setattr(comparison, "MAX_ROWS", {
            comparison.HEADER: 0 if limit == "header_rows" else 10,
            comparison.CHILD: 3 if limit == "child_rows" else 10})
    else:
        monkeypatch.setattr(comparison, "MAX_ROW_BYTES" if limit == "row_bytes" else "MAX_TOTAL_BYTES", 100)
    with pytest.raises(comparison.RegistryComparisonError, match="容量"):
        comparison.compare_development_registries(*pair)


def test_utf8_and_embedded_nul_count_bytes_before_fetch(pair, monkeypatch):
    mutate(pair[1], f"UPDATE {comparison.HEADER} SET payload=?", ("x\0" + "漢" * 5000,))
    monkeypatch.setattr(comparison, "MAX_ROW_BYTES", 12000)
    with pytest.raises(comparison.RegistryComparisonError, match="容量"):
        comparison.compare_development_registries(*pair)


@pytest.mark.parametrize("column,value", [("payload", b"blob"), ("child_count", 1.5)])
def test_sqlite_storage_type_drift_rejected(pair, column, value):
    mutate(pair[1], f"UPDATE {comparison.HEADER} SET {column}=?", (value,))
    with pytest.raises(comparison.RegistryComparisonError, match="儲存型別"):
        comparison.compare_development_registries(*pair)


def test_special_filename_uri_cannot_enable_writes(pair, tmp_path):
    special = clone(pair[1], tmp_path / "?mode=rw# 對帳.sqlite")
    before = special.read_bytes()
    assert comparison.compare_development_registries(pair[0], special).contents_match
    assert special.read_bytes() == before


def test_external_write_transaction_not_committed_or_rolled_back(pair, registry):
    with closing(sqlite3.connect(pair[0])) as writer:
        writer.execute(f"UPDATE {comparison.HEADER} SET origin='uncommitted'")
        for bad in (writer, registry):
            with pytest.raises(TypeError):
                comparison.compare_development_registries(bad, pair[1])
        assert comparison.compare_development_registries(*pair).contents_match
        assert writer.in_transaction
        assert writer.execute(f"SELECT origin FROM {comparison.HEADER}").fetchone() == ("uncommitted",)
        writer.rollback()


def test_busy_failure_releases_own_transaction_and_retry_works(pair):
    with closing(sqlite3.connect(pair[1])) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(comparison.RegistryComparisonError):
            comparison.compare_development_registries(*pair, timeout=0.01)
        assert writer.in_transaction
        writer.rollback()
    assert comparison.compare_development_registries(*pair).contents_match


def test_deadline_failure_no_partial_success(pair, monkeypatch):
    ticks = iter([0, 3])
    with monkeypatch.context() as patch:
        patch.setattr(comparison.time, "monotonic", lambda: next(ticks))
        with pytest.raises(comparison.RegistryComparisonError, match="逾時"):
            comparison.compare_development_registries(*pair)
    assert comparison.compare_development_registries(*pair).contents_match


@pytest.mark.parametrize("side", [0, 1])
def test_wal_structure_header_children_are_same_snapshot_and_next_read_detects_change(pair, monkeypatch, side):
    original = comparison._read_table
    calls = 0
    with closing(sqlite3.connect(pair[side])) as writer:
        writer.execute("PRAGMA journal_mode=WAL")

        def change(connection, table, *args):
            nonlocal calls
            result = original(connection, table, *args)
            calls += 1
            if calls == side * 2 + 1:
                writer.execute(f"UPDATE {comparison.HEADER} SET origin='committed later'")
                writer.execute(f"DELETE FROM {comparison.CHILD} WHERE position=4")
                writer.commit()
            return result

        with monkeypatch.context() as patch:
            patch.setattr(comparison, "_read_table", change)
            assert comparison.compare_development_registries(*pair).contents_match
        report = comparison.compare_development_registries(*pair)
        assert report.tables[0].changed_rows == 1
        assert (report.tables[1].missing_rows if side else report.tables[1].extra_rows) == 1


def test_between_database_change_does_not_make_report_current_or_cross_database_atomic(pair, monkeypatch):
    original = comparison._read_registry

    def change_after_read(path, *args):
        result = original(path, *args)
        if path == pair[0]:
            mutate(path, f"UPDATE {comparison.HEADER} SET origin='after reference transaction'")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(comparison, "_read_registry", change_after_read)
        report = comparison.compare_development_registries(*pair)
        assert report.contents_match and not report.can_resume_numbering
        assert "兩庫各自讀取快照" in report.warning
    assert not comparison.compare_development_registries(*pair).contents_match


def test_exact_capacity_boundary_accepted_and_oversize_payload_not_selected(pair, monkeypatch):
    sizes = []
    _, *tables = comparison.reservation_schema()
    with closing(sqlite3.connect(pair[0])) as connection:
        for table in tables:
            expr = "+".join(f'length(CAST("{column.name}" AS BLOB))' for column in table.columns)
            sizes.extend(row[0] for row in connection.execute(f'SELECT {expr} FROM "{table.name}"'))
    monkeypatch.setattr(comparison, "MAX_ROWS", {comparison.HEADER: 1, comparison.CHILD: 4})
    monkeypatch.setattr(comparison, "MAX_ROW_BYTES", max(sizes))
    monkeypatch.setattr(comparison, "MAX_TOTAL_BYTES", sum(sizes))
    assert comparison.compare_development_registries(*pair).contents_match

    mutate(pair[1], f"UPDATE {comparison.HEADER} SET payload=payload || 'x'")
    original = comparison._read_table
    statements = []

    def trace(connection, table, *args):
        connection.set_trace_callback(statements.append)
        return original(connection, table, *args)

    monkeypatch.setattr(comparison, "_read_table", trace)
    with pytest.raises(comparison.RegistryComparisonError, match="容量"):
        comparison.compare_development_registries(*pair)
    content_queries = [sql for sql in statements if sql.startswith('SELECT "batch_id"')]
    assert len(content_queries) == 2  # 僅參考端兩表；超量候選端尚未取得 payload。


def test_row_insertion_order_does_not_change_comparison(pair):
    with closing(sqlite3.connect(pair[1])) as connection:
        rows = connection.execute(f"SELECT * FROM {comparison.CHILD} ORDER BY position DESC").fetchall()
        connection.execute(f"DELETE FROM {comparison.CHILD}")
        connection.executemany(f"INSERT INTO {comparison.CHILD} VALUES ({','.join('?' for _ in rows[0])})", rows)
        connection.commit()
    assert comparison.compare_development_registries(*pair).contents_match


def test_fresh_process_import_no_connection_private_files_network_or_services(pair):
    root = Path(__file__).resolve().parents[1]
    code = r'''
from pathlib import Path
import sys
root, left, right = map(Path, sys.argv[1:])
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).resolve()
        if path.name.startswith(".env") or path.is_relative_to(root / "logs"):
            raise AssertionError("private file access")
    if event.startswith("socket."):
        raise AssertionError("network access")
    if event == "import" and args[0] in {
        "config", "database.connection", "database.models", "database.repository", "engine.pricing",
        "engine.snapshot", "engine.quote_number_reservation", "engine.development_atomic_snapshot",
        "streamlit", "openai",
    }:
        raise AssertionError("production dependency")
sys.addaudithook(audit)
sys.path.insert(0, str(root))
import sqlite3
connect = sqlite3.connect
sqlite3.connect = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("import-time connection"))
from engine.development_registry_comparison import compare_development_registries
sqlite3.connect = connect
assert all(path.is_file() and not path.is_relative_to(root) for path in (left, right))
report = compare_development_registries(left, right)
assert report.contents_match and not report.can_resume_numbering
print(report)
'''
    result = subprocess.run([sys.executable, "-I", "-c", code, str(root), *map(str, pair)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == repr(comparison.compare_development_registries(*pair))
