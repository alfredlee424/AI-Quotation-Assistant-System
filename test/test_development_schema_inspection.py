"""T43 唯讀結構檢查：只用暫存合成庫，不是部署核准。"""
from contextlib import closing
from dataclasses import FrozenInstanceError
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from database.development_atomic_schema import development_atomic_schema
from engine import development_schema_inspection as inspection
from test_development_process_integration import run


TABLES = tuple(sorted(development_atomic_schema()[0].tables))


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    engine = create_engine("sqlite:///" + str(path), poolclass=NullPool)
    development_atomic_schema()[0].create_all(engine)
    try:
        yield engine, path
    finally:
        engine.dispose()


def image(path):
    with closing(sqlite3.connect(path)) as connection:
        return tuple(connection.iterdump())


def alter_empty_table(path, table, old, new):
    # 只在空的暫存 fixture 重建指定表，模擬現場漂移，不提供維運入口。
    with closing(sqlite3.connect(path)) as connection:
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        assert old in sql
        connection.execute(f'DROP TABLE "{table}"')
        connection.execute(sql.replace(old, new, 1))
        connection.commit()


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("state", ["empty", "reserved", "saved"])
def test_complete_schema_is_readonly_and_never_authorizes_numbering(database, journal, state):
    engine, path = database
    with closing(sqlite3.connect(path)) as keeper:
        assert keeper.execute(f"PRAGMA journal_mode={journal}").fetchone()[0] == journal.lower()
        if state != "empty":
            run(engine, "reserve" if state == "reserved" else "full")
        before = image(path)
        report = inspection.inspect_development_schema(path)
        assert report.schema_matches and report.issues == ()
        assert report.checked_tables == TABLES
        assert report.expected_digest == report.observed_digest
        assert not any((report.can_quote, report.can_confirm, report.can_save, report.can_resume_numbering))
        assert "未驗證資料完整性" in report.warning
        assert report.report_version == inspection.REPORT_VERSION
        assert inspection.inspect_development_schema(path) == report
        assert image(path) == before
        assert str(path) not in repr(report) and "SYNTHETIC-ORDER" not in repr(report)
        with pytest.raises(FrozenInstanceError):
            report.issues = ()


@pytest.mark.parametrize("missing", TABLES)
def test_each_missing_table_reported_even_with_empty_header(database, missing):
    _, path = database
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f'DROP TABLE "{missing}"')
    before = image(path)
    report = inspection.inspect_development_schema(path)
    assert not report.schema_matches
    assert inspection.SchemaIssue(missing, "missing") in report.issues
    assert report.expected_digest != report.observed_digest
    assert image(path) == before


def test_empty_file_is_six_missing_tables_not_ready_or_implicitly_initialized(tmp_path):
    path = tmp_path / "empty.sqlite"
    path.touch()
    report = inspection.inspect_development_schema(path)
    assert report.issues == tuple(inspection.SchemaIssue(name, "missing") for name in TABLES)
    assert path.read_bytes() == b""


def test_uri_special_characters_are_literal_filename_not_options(database, tmp_path):
    path = tmp_path / "synthetic ?mode=rw# 唯讀.sqlite"
    with closing(sqlite3.connect(database[1])) as source, closing(sqlite3.connect(path)) as target:
        source.backup(target)
    before = path.read_bytes()
    assert inspection.inspect_development_schema(path).schema_matches
    assert path.read_bytes() == before


def test_view_cannot_impersonate_required_table(database):
    _, path = database
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE development_atomic_cost")
        connection.execute("CREATE VIEW development_atomic_cost AS SELECT 'private text' AS payload")
    report = inspection.inspect_development_schema(path)
    assert inspection.SchemaIssue("development_atomic_cost", "not_table") in report.issues
    assert "private text" not in repr(report)


@pytest.mark.parametrize("old,new,section", [
    ("path VARCHAR(256)", "path VARCHAR(255)", "columns"),
    ("payload TEXT NOT NULL", "payload BLOB NOT NULL", "columns"),
    ("payload TEXT NOT NULL", "payload TEXT", "columns"),
    ("payload TEXT NOT NULL", "payload TEXT NOT NULL DEFAULT 'sensitive default'", "columns"),
    ("payload TEXT NOT NULL", "renamed_payload TEXT NOT NULL", "columns"),
    ("PRIMARY KEY (batch_id, child_quote_id, seq_no)", "PRIMARY KEY (batch_id, seq_no, child_quote_id)", "columns"),
    ("CONSTRAINT uq_dar_path UNIQUE (batch_id, child_quote_id, path),", "", "indexes"),
    ("UNIQUE (batch_id, child_quote_id, path)", "UNIQUE (batch_id, path)", "indexes"),
    ("path VARCHAR(256)", "path VARCHAR(256) COLLATE NOCASE", "indexes"),
    ("CONSTRAINT fk_dar_child FOREIGN KEY(batch_id, child_quote_id) REFERENCES development_atomic_child (batch_id, child_quote_id),", "", "foreign_keys"),
    ("REFERENCES development_atomic_child (batch_id, child_quote_id)", "REFERENCES development_atomic_child (child_quote_id, batch_id)", "foreign_keys"),
    ("REFERENCES development_atomic_child (batch_id, child_quote_id)", "REFERENCES development_atomic_child (batch_id, child_quote_id) ON DELETE CASCADE", "foreign_keys"),
    ("position BETWEEN 1 AND 99999", "position BETWEEN 0 AND 99999", "definition"),
    ("CONSTRAINT ck_dar_position CHECK (position BETWEEN 1 AND 99999)", "CONSTRAINT ck_dar_position CHECK (1=1)", "definition"),
    ("payload TEXT NOT NULL", "payload TEXT NOT NULL, injected TEXT GENERATED ALWAYS AS ('private') VIRTUAL", "columns"),
])
def test_detects_column_capacity_pk_unique_fk_check_and_generated_drift(database, old, new, section):
    _, path = database
    alter_empty_table(path, "development_atomic_cost", old, new)
    before = image(path)
    report = inspection.inspect_development_schema(path)
    assert inspection.SchemaIssue("development_atomic_cost", section + "_mismatch") in report.issues
    assert not report.schema_matches and image(path) == before
    assert "sensitive default" not in repr(report) and "injected" not in repr(report)


@pytest.mark.parametrize("table,old,new", [
    ("quote_number_reservation", "quote-number-registry-dev-v1", "future-registry"),
    ("quote_number_reservation", "quote-number-request-dev-v1", "future-request"),
    ("development_atomic_batch", "development-atomic-schema-v1", "future-atomic"),
    ("development_atomic_batch", "DEVELOPMENT_ONLY", "development_only"),
])
def test_declared_version_and_literal_case_drift_rejected(database, table, old, new):
    _, path = database
    alter_empty_table(path, table, old, new)
    report = inspection.inspect_development_schema(path)
    assert inspection.SchemaIssue(table, "definition_mismatch") in report.issues


@pytest.mark.parametrize("ddl,section", [
    ("CREATE INDEX extra ON development_atomic_cost(path)", "indexes"),
    ("CREATE UNIQUE INDEX extra ON development_atomic_cost(path) WHERE position > 1", "indexes"),
    ("CREATE INDEX extra ON development_atomic_cost(lower(path))", "indexes"),
    ("CREATE TRIGGER private_trigger AFTER INSERT ON development_atomic_cost BEGIN SELECT 'private text'; END", "extra_objects"),
])
def test_additional_index_or_trigger_requires_review_without_leaking_ddl(database, ddl, section):
    _, path = database
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(ddl)
    report = inspection.inspect_development_schema(path)
    assert inspection.SchemaIssue("development_atomic_cost", section + "_mismatch") in report.issues
    assert "private_trigger" not in repr(report) and "private text" not in repr(report)


def test_unrelated_legacy_tables_and_triggers_ignored_and_unchanged(database):
    _, path = database
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript("CREATE TABLE legacy(payload TEXT); INSERT INTO legacy VALUES ('private');"
                                 "CREATE TRIGGER legacy_trigger AFTER INSERT ON legacy BEGIN SELECT 1; END;")
    before = image(path)
    assert inspection.inspect_development_schema(path).schema_matches
    assert image(path) == before


@pytest.mark.parametrize("sql", [
    "SELECT payload FROM development_atomic_batch", "SELECT count(*) FROM development_atomic_batch",
    "INSERT INTO development_atomic_cost DEFAULT VALUES", "DELETE FROM development_atomic_cost",
    "UPDATE development_atomic_batch SET payload='bad'", "DROP TABLE development_atomic_cost",
    "CREATE TABLE forbidden(x)", "PRAGMA writable_schema=ON", "PRAGMA user_version=1",
    "ATTACH ':memory:' AS other", "SELECT load_extension('forbidden')",
])
def test_actual_inspection_connection_denies_payload_reads_and_all_writes(database, monkeypatch, sql):
    _, path = database
    original = inspection._table_signature
    calls = 0

    def probe(connection, name):
        nonlocal calls
        calls += 1
        if calls == len(TABLES) + 1:  # 第一組是私有記憶體參考庫，第二組是實際受檢唯讀連線。
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute(sql).fetchall()
        return original(connection, name)

    monkeypatch.setattr(inspection, "_table_signature", probe)
    assert inspection.inspect_development_schema(path).schema_matches
    assert calls == 2 * len(TABLES)


@pytest.mark.parametrize("value", [None, "sqlite:///missing", ":memory:", 12])
def test_no_implicit_path_or_connection(value):
    with pytest.raises(TypeError):
        inspection.inspect_development_schema(value)


@pytest.mark.parametrize("timeout", [0, -1, 31, float("inf"), float("nan"), True, "2"])
def test_invalid_timeout_rejected_before_io(database, timeout):
    with pytest.raises(ValueError):
        inspection.inspect_development_schema(database[1], timeout=timeout)


def test_missing_directory_symlink_and_corrupt_files_never_created_or_repaired(tmp_path):
    missing = tmp_path / "missing.sqlite"
    for path in (missing, tmp_path):
        with pytest.raises(ValueError):
            inspection.inspect_development_schema(path)
    assert not missing.exists()
    corrupt = tmp_path / "secret-path.sqlite"
    corrupt.write_bytes(b"private corrupt data")
    link = tmp_path / "link.sqlite"
    link.symlink_to(corrupt)
    with pytest.raises(ValueError):
        inspection.inspect_development_schema(link)
    with pytest.raises(inspection.SchemaInspectionError) as caught:
        inspection.inspect_development_schema(corrupt)
    assert "secret-path" not in str(caught.value) and "private" not in str(caught.value)
    assert corrupt.read_bytes() == b"private corrupt data"


def test_busy_failure_and_retry_do_not_return_previous_success(database):
    _, path = database
    good = inspection.inspect_development_schema(path)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(inspection.SchemaInspectionError):
            inspection.inspect_development_schema(path, timeout=0.01)
        writer.rollback()
    assert inspection.inspect_development_schema(path) == good


def test_external_transaction_not_committed_or_rolled_back(database):
    engine, path = database
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("CREATE TABLE legacy(payload TEXT)")
        writer.execute("INSERT INTO legacy VALUES ('uncommitted')")
        for unsupported in (writer, engine):
            with pytest.raises(TypeError):
                inspection.inspect_development_schema(unsupported)
        assert inspection.inspect_development_schema(path).schema_matches
        assert writer.in_transaction
        assert writer.execute("SELECT count(*) FROM legacy").fetchone() == (1,)
        with closing(sqlite3.connect(path)) as other:
            assert other.execute("SELECT count(*) FROM legacy").fetchone() == (0,)
        writer.rollback()


def test_deadline_failure_clears_operation_and_can_retry(database, monkeypatch):
    path = database[1]
    good = inspection.inspect_development_schema(path)
    ticks = iter([0, 3])
    with monkeypatch.context() as patch:
        patch.setattr(inspection.time, "monotonic", lambda: next(ticks))
        with pytest.raises(inspection.SchemaInspectionError, match="逾時"):
            inspection.inspect_development_schema(path)
    assert inspection.inspect_development_schema(path) == good


def test_mid_scan_error_does_not_return_partial_report_or_raw_error(database, monkeypatch):
    original = inspection._table_signature
    calls = 0

    def fail(connection, name):
        nonlocal calls
        calls += 1
        if calls == len(TABLES) + 3:
            raise sqlite3.OperationalError("sensitive SQL and path")
        return original(connection, name)

    with monkeypatch.context() as patch:
        patch.setattr(inspection, "_table_signature", fail)
        with pytest.raises(inspection.SchemaInspectionError) as caught:
            inspection.inspect_development_schema(database[1])
        assert "sensitive" not in str(caught.value)
    assert inspection.inspect_development_schema(database[1]).schema_matches


def test_wal_ddl_change_is_one_snapshot_and_next_check_detects_change(database, monkeypatch):
    _, path = database
    baseline = inspection.inspect_development_schema(path)
    original = inspection._table_signature
    calls = 0
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")

        def change_after_first_table(connection, name):
            nonlocal calls
            calls += 1
            result = original(connection, name)
            if calls == len(TABLES) + 1:
                writer.execute("ALTER TABLE development_atomic_cost ADD COLUMN drift TEXT")
                writer.commit()
            return result

        with monkeypatch.context() as patch:
            patch.setattr(inspection, "_table_signature", change_after_first_table)
            assert inspection.inspect_development_schema(path) == baseline
    assert not inspection.inspect_development_schema(path).schema_matches


@pytest.mark.parametrize("damage", ["missing_cost", "unknown_version"])
def test_schema_success_does_not_certify_data_or_persisted_version(database, damage):
    engine, path = database
    run(engine, "full")
    with closing(sqlite3.connect(path)) as connection:
        if damage == "missing_cost":
            connection.execute("DELETE FROM development_atomic_cost WHERE rowid=(SELECT min(rowid) FROM development_atomic_cost)")
        else:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute("UPDATE development_atomic_batch SET schema_version='unsupported'")
        connection.commit()
    before = image(path)
    assert inspection.inspect_development_schema(path).schema_matches
    assert run(engine, "view")["error"] == "AtomicSnapshotConflict"
    assert image(path) == before


def test_ddl_tokenizer_preserves_literals_and_quoted_identifiers():
    assert inspection._tokens("CREATE  TABLE x ( a TEXT )") == inspection._tokens("create\ntable x(a text)")
    assert inspection._tokens("CHECK (a='A B')") != inspection._tokens("CHECK (a='a b')")
    assert inspection._tokens('"two  spaces"') != inspection._tokens('"two spaces"')


def test_fresh_process_import_and_check_have_no_production_or_network_dependencies(database):
    root = Path(__file__).resolve().parents[1]
    code = r'''
from pathlib import Path
import sys
root, target = Path(sys.argv[1]), Path(sys.argv[2])
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).resolve()
        if path.name.startswith(".env") or path.is_relative_to(root / "logs"):
            raise AssertionError("private file access")
    if event.startswith("socket."):
        raise AssertionError("network access")
    if event == "import" and args[0] in {
        "config", "database.connection", "database.models", "database.repository",
        "engine.pricing", "engine.snapshot", "engine.quote_number_reservation",
        "engine.development_atomic_snapshot", "streamlit", "openai",
    }:
        raise AssertionError("production dependency")
sys.addaudithook(audit)
sys.path.insert(0, str(root))
import sqlite3
connect = sqlite3.connect
sqlite3.connect = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("import-time connection"))
from engine.development_schema_inspection import inspect_development_schema
sqlite3.connect = connect
assert target.is_file() and not target.is_relative_to(root)
report = inspect_development_schema(target)
assert report.schema_matches
print(report.expected_digest)
'''
    result = subprocess.run([sys.executable, "-I", "-c", code, str(root), str(database[1])],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == inspection.inspect_development_schema(database[1]).expected_digest
