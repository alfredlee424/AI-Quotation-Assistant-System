"""整庫唯讀驗收；所有破壞只作用於暫存合成資料庫。"""
from contextlib import closing
from dataclasses import asdict, replace
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import Integer

from database.development_atomic_schema import development_atomic_schema
from engine import development_integrity_inspection as inspection
from engine.development_atomic_contract import build_development_reservation_request, canonical, digest, identity_of
from engine.development_registry_comparison import compare_development_registries
from engine.development_schema_inspection import inspect_development_schema
from engine.quote_number_reservation import QuoteNumberReservationService
from development_atomic_fixtures import ident, request, SHARED
from test_development_atomic_snapshot import database, reserve, service
from test_quote_number_reservation import NOW, request as reservation_request


@pytest.fixture
def saved(database):
    req = request()
    reserve(database, req)
    service(database).save(req)
    return Path(database.url.database)


def mutate(path, sql, parameters=()):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(sql, parameters)
        connection.commit()


def dump(path):
    with closing(sqlite3.connect(path)) as connection:
        return tuple(connection.iterdump())


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_complete_snapshot_is_readonly_counted_and_not_authorized(saved, journal):
    mutate(saved, f"PRAGMA journal_mode={journal}")
    before = dump(saved)
    report = inspection.inspect_development_integrity(saved)
    assert dict(report.table_counts) == dict(zip(inspection.TABLES, (1, 4, 1, 4, 4, 16)))
    assert report.verified_saved_batches == 1 and report.reservation_only_batches == 0
    assert all(getattr(report, flag) is False for flag in (
        "can_quote", "can_confirm", "can_save", "can_resume_numbering"))
    assert "最新性" in report.warning and report.report_version.endswith("v1")
    assert str(saved) not in repr(report) and SHARED not in repr(report)
    assert inspection.inspect_development_integrity(saved) == report
    assert dump(saved) == before


def test_empty_six_tables_are_not_proof_of_backup_completeness(database):
    report = inspection.inspect_development_integrity(Path(database.url.database))
    assert report.verified_saved_batches == report.reservation_only_batches == 0
    assert all(count == 0 for _, count in report.table_counts)
    assert not report.can_resume_numbering


@pytest.mark.parametrize("atomic", [False, True])
def test_reservation_only_is_valid_but_never_recycled(database, atomic):
    req = build_development_reservation_request(request()) if atomic else reservation_request()
    QuoteNumberReservationService(database, clock=lambda: NOW).reserve(req)
    path = Path(database.url.database)
    before = dump(path)
    report = inspection.inspect_development_integrity(path)
    assert report.reservation_only_batches == 1 and report.verified_saved_batches == 0
    assert dump(path) == before and not report.can_resume_numbering


def test_multiple_workgroups_saved_and_reserved_only(saved, database):
    req = replace(request(), workgroup="104", batch_id=ident(81), request_id=ident(82),
                  preview_identity=ident(83), save_id=ident(84),
                  children=tuple(replace(child, child_quote_id=ident(900 + i))
                                 for i, child in enumerate(request().children)))
    QuoteNumberReservationService(database, clock=lambda: NOW + timedelta(seconds=1)).reserve(
        build_development_reservation_request(req))
    service(database).save(req)
    QuoteNumberReservationService(database, clock=lambda: NOW + timedelta(seconds=2)).reserve(
        reservation_request(number=8))
    report = inspection.inspect_development_integrity(saved)
    assert (report.verified_saved_batches, report.reservation_only_batches) == (2, 1)
    assert service(database).read(identity_of(req))["fixed_request"] == json.loads(canonical(asdict(req)))


@pytest.mark.parametrize("table", inspection.TABLES)
def test_missing_row_is_rejected_without_repair(saved, table):
    mutate(saved, f'DELETE FROM "{table}" WHERE rowid=(SELECT min(rowid) FROM "{table}")')
    before = dump(saved)
    with pytest.raises(inspection.IntegrityInspectionError):
        inspection.inspect_development_integrity(saved)
    assert dump(saved) == before


@pytest.mark.parametrize("table", inspection.TABLES)
def test_missing_table_is_not_empty_success(saved, table):
    mutate(saved, f'DROP TABLE "{table}"')
    with pytest.raises(inspection.IntegrityInspectionError, match="結構"):
        inspection.inspect_development_integrity(saved)


METADATA, *_ = development_atomic_schema()
COLUMNS = [(name, column.name, isinstance(column.type, Integer))
           for name in inspection.TABLES for column in METADATA.tables[name].columns]


@pytest.mark.parametrize("table,column,integer", COLUMNS)
def test_every_persisted_column_is_verified(saved, table, column, integer):
    expression = f'"{column}" + 1000' if integer else f'"{column}" || \'tampered\''
    mutate(saved, f'UPDATE "{table}" SET "{column}"={expression} '
                  f'WHERE rowid=(SELECT min(rowid) FROM "{table}")')
    with pytest.raises(inspection.IntegrityInspectionError):
        inspection.inspect_development_integrity(saved)


@pytest.mark.parametrize("table", (inspection.RESERVED_CHILD, inspection.CHILD, inspection.LINK, inspection.COST))
def test_orphan_extra_rows_are_not_missed_by_batch_iteration(saved, table):
    with closing(sqlite3.connect(saved)) as connection:
        columns = [item[1] for item in connection.execute(f'PRAGMA table_info("{table}")')]
        row = list(connection.execute(f'SELECT * FROM "{table}" LIMIT 1').fetchone())
        row[columns.index("batch_id")] = ident(98765)
        row[columns.index("child_quote_id")] = ident(98766)
        for name in ("reserved_ref_no", "ref_no"):
            if name in columns:
                row[columns.index(name)] = "20261002083046_001"
        connection.execute(f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in row)})', row)
        connection.commit()
    with pytest.raises(inspection.IntegrityInspectionError):
        inspection.inspect_development_integrity(saved)


def test_registry_match_and_schema_match_do_not_hide_missing_cost(saved, tmp_path):
    candidate = tmp_path / "candidate.sqlite"
    with closing(sqlite3.connect(saved)) as source, closing(sqlite3.connect(candidate)) as target:
        source.backup(target)
    mutate(candidate, f"DELETE FROM {inspection.COST} WHERE seq_no='00002'")
    assert inspect_development_schema(candidate).schema_matches
    assert compare_development_registries(saved, candidate).contents_match
    with pytest.raises(inspection.IntegrityInspectionError):
        inspection.inspect_development_integrity(candidate)


def test_self_consistent_old_backup_can_pass_but_is_not_latest(saved, tmp_path, database):
    old = tmp_path / "old.sqlite"
    with closing(sqlite3.connect(saved)) as source, closing(sqlite3.connect(old)) as target:
        source.backup(target)
    QuoteNumberReservationService(database, clock=lambda: NOW + timedelta(seconds=1)).reserve(
        reservation_request(number=9))
    assert inspection.inspect_development_integrity(old).verified_saved_batches == 1
    assert inspection.inspect_development_integrity(saved).reservation_only_batches == 1
    assert not compare_development_registries(saved, old).contents_match


@pytest.mark.parametrize("payload", ['{"x":1,"x":2}', '{"secret":NaN}', '[1]', '{}',
                                     '{"secret":1.5}', '{"children":[]}', '{"secret":"\\ud800"}'])
@pytest.mark.parametrize("table", [inspection.HEADER, inspection.BATCH])
def test_invalid_json_is_sanitized(saved, payload, table):
    mutate(saved, f"UPDATE {table} SET payload=?", (payload,))
    with pytest.raises(inspection.IntegrityInspectionError) as caught:
        inspection.inspect_development_integrity(saved)
    assert "secret" not in str(caught.value) and str(saved) not in str(caught.value)


def test_rehashed_batch_payload_does_not_override_fixed_registry(saved):
    with closing(sqlite3.connect(saved)) as connection:
        payload = connection.execute(f"SELECT payload FROM {inspection.BATCH}").fetchone()[0]
    data = json.loads(payload)
    data["fixed_request"]["children"][0]["shared_description"] = "tampered"
    data["fixed_digest"] = digest(canonical(data["fixed_request"]))
    data.pop("final_digest")
    data["final_digest"] = digest(canonical(data))
    mutate(saved, f"UPDATE {inspection.BATCH} SET payload=?,fixed_digest=?,final_digest=?",
           (canonical(data), data["fixed_digest"], data["final_digest"]))
    with pytest.raises(inspection.IntegrityInspectionError):
        inspection.inspect_development_integrity(saved)


@pytest.mark.parametrize("timeout", [0, -1, True, None, "2", float("nan"), float("inf"), 31])
def test_bad_timeout(saved, timeout):
    with pytest.raises(ValueError):
        inspection.inspect_development_integrity(saved, timeout=timeout)


def test_bad_paths_do_not_create_files_or_accept_external_transactions(saved, tmp_path, database):
    missing = tmp_path / "missing.sqlite"
    link = tmp_path / "link.sqlite"
    link.symlink_to(saved)
    for path in (missing, tmp_path, link):
        with pytest.raises(ValueError):
            inspection.inspect_development_integrity(path)
    with closing(sqlite3.connect(saved)) as writer:
        writer.execute(f"UPDATE {inspection.HEADER} SET origin='uncommitted'")
        for value in (str(saved), saved.as_uri(), writer, database, None):
            with pytest.raises(TypeError):
                inspection.inspect_development_integrity(value)
        assert inspection.inspect_development_integrity(saved).verified_saved_batches == 1
        assert writer.in_transaction
        writer.rollback()
    assert not missing.exists()


def test_invalid_database_error_is_sanitized(tmp_path):
    path = tmp_path / "sensitive.sqlite"
    path.write_text("sensitive content")
    with pytest.raises(inspection.IntegrityInspectionError) as caught:
        inspection.inspect_development_integrity(path)
    assert "sensitive" not in str(caught.value)


@pytest.mark.parametrize("limit", ["rows", "row_bytes", "total_bytes"])
def test_all_capacity_preflight_precedes_any_payload_read(saved, monkeypatch, limit):
    if limit == "rows":
        monkeypatch.setattr(inspection, "MAX_ROWS", {**inspection.MAX_ROWS, inspection.COST: 15})
    else:
        monkeypatch.setattr(inspection, "MAX_ROW_BYTES" if limit == "row_bytes" else "MAX_TOTAL_BYTES", 1)
    monkeypatch.setattr(inspection, "_read_table", lambda *args: pytest.fail("payload read before preflight"))
    with pytest.raises(inspection.IntegrityInspectionError, match="容量"):
        inspection.inspect_development_integrity(saved)


def test_blob_storage_is_not_coerced(saved):
    mutate(saved, f"UPDATE {inspection.COST} SET payload=CAST(payload AS BLOB)")
    with pytest.raises(inspection.IntegrityInspectionError, match="儲存型別"):
        inspection.inspect_development_integrity(saved)


def test_lock_timeout_does_not_commit_writer(saved):
    with closing(sqlite3.connect(saved)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(inspection.IntegrityInspectionError):
            inspection.inspect_development_integrity(saved, timeout=0.01)
        assert writer.in_transaction
        writer.rollback()


def test_deadline_after_validation_never_returns_success(saved, monkeypatch):
    original = inspection._verify_contents

    def expire(rows, deadline):
        result = original(rows, deadline)
        monkeypatch.setattr(inspection.time, "monotonic", lambda: deadline + 1)
        return result

    monkeypatch.setattr(inspection, "_verify_contents", expire)
    with pytest.raises(inspection.IntegrityInspectionError, match="逾時"):
        inspection.inspect_development_integrity(saved)


@pytest.mark.parametrize("stage", ["schema", "content"])
def test_wal_schema_and_all_six_tables_share_one_snapshot(saved, monkeypatch, stage):
    mutate(saved, "PRAGMA journal_mode=WAL")
    baseline = inspection.inspect_development_integrity(saved)
    method = "_table_signature" if stage == "schema" else "_read_table"
    original = getattr(inspection, method)
    changed = False

    def change(connection, *args):
        nonlocal changed
        result = original(connection, *args)
        if not changed:
            changed = True
            mutate(saved, f"DELETE FROM {inspection.COST} WHERE seq_no='00002'")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(inspection, method, change)
        assert inspection.inspect_development_integrity(saved) == baseline
    with pytest.raises(inspection.IntegrityInspectionError):
        inspection.inspect_development_integrity(saved)


def test_real_connection_authorizer_denies_writes_other_tables_and_attach(saved, monkeypatch):
    mutate(saved, "CREATE TABLE private_data (secret TEXT)")
    original = inspection._read_table
    seen = []

    def probe(connection, table, deadline):
        assert connection.in_transaction
        if not seen:
            for sql in (f"DELETE FROM {inspection.COST}", "CREATE TABLE forbidden(x)",
                        "SELECT * FROM private_data", "ATTACH ':memory:' AS other", "COMMIT",
                        "PRAGMA writable_schema=ON", "SELECT random()"):
                with pytest.raises(sqlite3.DatabaseError):
                    connection.execute(sql)
        seen.append(table.name)
        return original(connection, table, deadline)

    monkeypatch.setattr(inspection, "_read_table", probe)
    monkeypatch.setattr(inspection.DevelopmentAtomicSnapshotService, "__init__", lambda *a, **k: pytest.fail("service"))
    monkeypatch.setattr(inspection.QuoteNumberReservationService, "__init__", lambda *a, **k: pytest.fail("registry"))
    assert inspection.inspect_development_integrity(saved).verified_saved_batches == 1
    assert tuple(seen) == inspection.TABLES


def test_midread_failure_closes_connection_and_does_not_reuse_success(saved, monkeypatch):
    inspection.inspect_development_integrity(saved)
    original = inspection._read_table
    connections = []

    def fail(connection, table, deadline):
        connections.append(connection)
        if table.name == inspection.COST:
            raise sqlite3.OperationalError("sensitive SQL and parameters")
        return original(connection, table, deadline)

    with monkeypatch.context() as patch:
        patch.setattr(inspection, "_read_table", fail)
        with pytest.raises(inspection.IntegrityInspectionError) as caught:
            inspection.inspect_development_integrity(saved)
        assert "sensitive" not in str(caught.value)
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")
    assert inspection.inspect_development_integrity(saved).verified_saved_batches == 1


def test_fresh_process_has_no_private_network_or_production_dependencies(saved):
    root = Path(__file__).resolve().parents[1]
    code = r'''
from pathlib import Path
import sys
root, path = map(Path, sys.argv[1:])
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
from engine.development_integrity_inspection import inspect_development_integrity
sqlite3.connect = connect
assert path.is_file() and not path.is_relative_to(root)
report = inspect_development_integrity(path)
assert report.verified_saved_batches == 1 and not report.can_resume_numbering
print(report)
'''
    result = subprocess.run([sys.executable, "-I", "-c", code, str(root), str(saved)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == repr(inspection.inspect_development_integrity(saved))


def test_exact_capacity_boundaries_and_special_uri_filename(saved, tmp_path, monkeypatch):
    special = tmp_path / "隔離 ?mode=rw&#.sqlite"
    sizes, counts = [], {}
    with closing(sqlite3.connect(saved)) as source, closing(sqlite3.connect(special)) as target:
        source.backup(target)
        for name in inspection.TABLES:
            expr = "+".join(f'length(CAST("{column.name}" AS BLOB))'
                            for column in METADATA.tables[name].columns)
            values = source.execute(f'SELECT {expr} FROM "{name}"').fetchall()
            sizes.extend(size for (size,) in values)
            counts[name] = len(values)
    before = dump(special)
    monkeypatch.setattr(inspection, "MAX_ROWS", counts)
    monkeypatch.setattr(inspection, "MAX_ROW_BYTES", max(sizes))
    monkeypatch.setattr(inspection, "MAX_TOTAL_BYTES", sum(sizes))
    assert inspection.inspect_development_integrity(special).verified_saved_batches == 1
    assert dump(special) == before
    monkeypatch.setattr(inspection, "MAX_TOTAL_BYTES", sum(sizes) - 1)
    with pytest.raises(inspection.IntegrityInspectionError, match="容量"):
        inspection.inspect_development_integrity(special)


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_uncommitted_partial_change_is_invisible_and_external_transaction_is_untouched(saved, journal):
    mutate(saved, f"PRAGMA journal_mode={journal}")
    baseline = inspection.inspect_development_integrity(saved)
    with closing(sqlite3.connect(saved)) as writer:
        writer.execute(f"DELETE FROM {inspection.COST} WHERE seq_no='00002'")
        assert inspection.inspect_development_integrity(saved) == baseline
        assert writer.in_transaction
        writer.commit()
    with pytest.raises(inspection.IntegrityInspectionError):
        inspection.inspect_development_integrity(saved)


@pytest.mark.parametrize("change", ["missing_version", "unknown_version", "extra_field", "reverse_children"])
def test_reservation_only_payload_cannot_hide_missing_fields_or_wrong_order(database, change):
    reserve(database, request())
    path = Path(database.url.database)
    with closing(sqlite3.connect(path)) as connection:
        payload = connection.execute(f"SELECT payload FROM {inspection.HEADER}").fetchone()[0]
    data = json.loads(payload)
    if change == "missing_version":
        data.pop("request_version")
    elif change == "unknown_version":
        data["request_version"] = "unknown"
    elif change == "extra_field":
        data["secret"] = "not a contract field"
    else:
        data["children"].reverse()
    payload = canonical(data)
    mutate(path, f"UPDATE {inspection.HEADER} SET payload=?,content_digest=?", (payload, digest(payload)))
    with pytest.raises(inspection.IntegrityInspectionError):
        inspection.inspect_development_integrity(path)


@pytest.mark.parametrize("sql", [
    "ALTER TABLE development_atomic_cost ADD COLUMN extra TEXT",
    "CREATE TRIGGER unexpected AFTER INSERT ON development_atomic_cost BEGIN SELECT 1; END",
    "CREATE INDEX unexpected ON development_atomic_cost(path) WHERE position=1",
])
def test_schema_drift_rejected_before_content_access(saved, monkeypatch, sql):
    mutate(saved, sql)
    monkeypatch.setattr(inspection, "_read_table", lambda *a: pytest.fail("read drifted schema"))
    with pytest.raises(inspection.IntegrityInspectionError, match="結構"):
        inspection.inspect_development_integrity(saved)


def test_no_batch_header_does_not_hide_orphan_rows_or_authorize_recovery(saved):
    mutate(saved, f"DELETE FROM {inspection.BATCH}")
    with pytest.raises(inspection.IntegrityInspectionError):
        inspection.inspect_development_integrity(saved)
    for name in (inspection.COST, inspection.LINK, inspection.CHILD):
        mutate(saved, f"DELETE FROM {name}")
    report = inspection.inspect_development_integrity(saved)
    assert report.verified_saved_batches == 0 and report.reservation_only_batches == 1
    assert not report.can_resume_numbering  # 無外部紀錄時不能判斷是否原本曾保存。


def test_row_insertion_order_is_irrelevant(saved):
    baseline = inspection.inspect_development_integrity(saved)
    with closing(sqlite3.connect(saved)) as connection:
        for name in (inspection.RESERVED_CHILD, inspection.CHILD, inspection.LINK, inspection.COST):
            rows = connection.execute(f"SELECT * FROM {name} ORDER BY rowid DESC").fetchall()
            connection.execute(f"DELETE FROM {name}")
            connection.executemany(f'INSERT INTO {name} VALUES ({",".join("?" for _ in rows[0])})', rows)
        connection.commit()
    assert inspection.inspect_development_integrity(saved) == baseline
