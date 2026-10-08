"""離線接收契約與真實四種 producer 相容；全部資料為隔離合成內容。"""
from dataclasses import FrozenInstanceError, replace
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import UniqueConstraint

import development_inspection as producer
import development_report_validation as cli
from engine import development_report_validation as validation
from database.development_atomic_schema import development_atomic_schema
from development_atomic_fixtures import change_row, ident, request
from test_development_atomic_snapshot import database, reserve, service
from test_development_integrity_inspection import dump, mutate
from test_development_inspection_cli import arguments
from test_development_snapshot_comparison import clear, clone, pair, populate


ROOT = Path(__file__).resolve().parents[1]


def encode(document):
    return json.dumps(document, ensure_ascii=True).encode("utf8")


def document(command, paths):
    args = producer._arguments(arguments(command, paths))
    result, code = producer._document(command, producer._run(args))
    return json.loads(encode(result)), code


def validate(data, operation=None, code=None):
    return validation.validate_development_report(encode(data),
        expected_operation=operation or data["operation"],
        expected_exit_code=data["exit_code"] if code is None else code)


@pytest.fixture
def reports(pair):
    return {command: document(command, pair)[0] for command in producer.COMMANDS}


@pytest.mark.parametrize("command", producer.COMMANDS)
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("state", ["empty", "reserved", "saved"])
def test_real_producer_reports_pass_without_changing_database(database, tmp_path, command, journal, state):
    left = Path(database.url.database)
    mutate(left, f"PRAGMA journal_mode={journal}")
    if state != "empty":
        reserve(database, request())
    if state == "saved":
        service(database).save(request())
    paths = left, clone(left, tmp_path / "candidate.sqlite")
    before = tuple(dump(path) for path in paths)
    data, code = document(command, paths)
    result = validate(data)
    assert code == result.inspection_exit_code == 0
    assert result.operation == command and result.status == "completed"
    assert all(getattr(result, flag) is False for flag in validation.FLAGS)
    assert result.warning == validation.WARNING
    assert not hasattr(result, "report")
    with pytest.raises(FrozenInstanceError):
        result.can_quote = True
    assert tuple(dump(path) for path in paths) == before


@pytest.mark.parametrize("command", ["schema", "registry", "snapshots"])
@pytest.mark.parametrize("reverse", [False, True])
def test_real_differences_are_valid_but_not_success(pair, command, reverse):
    if command == "schema":
        mutate(pair[0], "DROP TABLE development_atomic_cost")
    else:
        clear(pair[1])
        if reverse:
            pair = pair[::-1]
    data, code = document(command, pair)
    assert code == 1 and validate(data).inspection_exit_code == 1
    assert validate(data).status == "differences"


@pytest.mark.parametrize("command", ["registry", "snapshots"])
@pytest.mark.parametrize("kind", ["rewrite", "global", "composite"])
def test_real_changed_content_and_identity_conflicts(pair, command, kind):
    clear(pair[1])
    req = request()
    if kind == "rewrite":
        req = change_row(req, original_qty="1.000000000000000002")
    elif kind == "global":
        req = replace(req, workgroup="104", batch_id=ident(80), request_id=ident(81), preview_identity=ident(82))
    else:
        req = replace(req, children=tuple(replace(child, child_quote_id=ident(900 + i))
                                         for i, child in enumerate(req.children)))
    populate(pair[1], req)
    data, code = document(command, pair)
    assert code == 1 and validate(data).inspection_exit_code == 1


def test_pinned_contract_matches_current_schema_and_services(reports):
    metadata, *_ = development_atomic_schema()
    for name, table in metadata.tables.items():
        actual = {tuple(column.name for column in constraint.columns) for constraint in table.constraints
                  if isinstance(constraint, UniqueConstraint)}
        assert actual == set(validation.UNIQUE_KEYS[name])
    for command, data in reports.items():
        assert data["report"]["report_version"] == validation.VERSIONS[command]
        assert data["report"]["warning"] == validation.WARNINGS[command]
    assert reports["schema"]["report"]["checked_tables"] == sorted(validation.TABLES)


@pytest.mark.parametrize("command", producer.COMMANDS)
@pytest.mark.parametrize("kind", ["missing", "unknown", "version", "warning", "flag", "status", "exit_bool", "operation"])
def test_envelope_rejects_unknown_or_contradictory_values(reports, command, kind):
    data = reports[command]
    if kind == "missing":
        del data["report"]
    elif kind == "unknown":
        data["private"] = "secret-input"
    else:
        key, value = {"version": ("output_version", "future"), "warning": ("warning", "secret-input"),
                      "flag": ("can_quote", 0), "status": ("status", "differences"),
                      "exit_bool": ("exit_code", False), "operation": ("operation", "unknown")}[kind]
        data[key] = value
    with pytest.raises(validation.ReportValidationError, match=validation.ERROR):
        validate(data, command, 0)


@pytest.mark.parametrize("command", producer.COMMANDS)
@pytest.mark.parametrize("kind", ["missing", "unknown", "version", "warning", "flag"])
def test_nested_reports_are_strict(reports, command, kind):
    data = reports[command]
    report = data["report"]
    if kind == "missing":
        del report["warning"]
    elif kind == "unknown":
        report["customer"] = "secret-input"
    else:
        report[{"version": "report_version", "warning": "warning", "flag": "can_save"}[kind]] = "secret-input"
    with pytest.raises(validation.ReportValidationError):
        validate(data)


@pytest.mark.parametrize("payload", [b"", b"{}", b"[]", b"null", b"true", b"--help secret-input", b"\xff",
    b'\xef\xbb\xbf{}', b'{"secret-input":NaN}', b'{"secret-input":Infinity}', b'{"secret-input":1.0}',
    b'{"x":0,"x":1}', b'{"x":{"y":0,"y":1}}', b"{}{}", b"[" * 2000 + b"]" * 2000,
    b" " * (validation.MAX_REPORT_BYTES + 1), "{}", {}, bytearray(b"{}")])
def test_invalid_serialization_is_sanitized(payload):
    with pytest.raises(validation.ReportValidationError) as caught:
        validation.validate_development_report(payload, expected_operation="schema", expected_exit_code=0)
    assert str(caught.value) == validation.ERROR
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert not hasattr(caught.value, "doc")


@pytest.mark.parametrize("operation,code", [(None, 0), ([], 0), ("unknown", 0), ("schema", True),
    ("schema", "0"), ("schema", 0.0), ("schema", 2), ("schema", 3), ("schema", 130), ("integrity", 1)])
def test_independent_expectations_are_required(reports, operation, code):
    with pytest.raises(validation.ReportValidationError):
        validation.validate_development_report(encode(reports["schema"]),
            expected_operation=operation, expected_exit_code=code)


def test_whole_document_only_and_exact_byte_limit(reports):
    data = reports["schema"]
    payload = encode(data)
    padded = payload + b" " * (validation.MAX_REPORT_BYTES - len(payload))
    assert validation.validate_development_report(padded, expected_operation="schema", expected_exit_code=0)
    for invalid in (payload[:-1], payload + payload, payload + b"secret-input", padded + b" ",
                    payload.replace(b'"exit_code": 0', b'"exit_code": 0, "exit_code": 0')):
        with pytest.raises(validation.ReportValidationError):
            validation.validate_development_report(invalid, expected_operation="schema", expected_exit_code=0)


@pytest.mark.parametrize("key,value", [("verified_saved_batches", 0), ("reservation_only_batches", 1),
    ("schema_digest", "ABC"), ("can_resume_numbering", True)])
@pytest.mark.parametrize("side", ["integrity", "reference_integrity", "candidate_integrity"])
def test_integrity_cross_checks_and_nested_metadata(reports, side, key, value):
    data = reports["integrity" if side == "integrity" else "snapshots"]
    target = data["report"] if side == "integrity" else data["report"][side]
    target[key] = value
    with pytest.raises(validation.ReportValidationError):
        validate(data)


@pytest.mark.parametrize("name", validation.TABLES)
@pytest.mark.parametrize("value", [-1, True, "4", 0.5, 100001])
def test_invalid_counts(reports, name, value):
    data = reports["integrity"]
    data["report"]["table_counts"][name] = value
    with pytest.raises(validation.ReportValidationError):
        validate(data)


@pytest.mark.parametrize("kind", ["tables", "duplicate", "order", "rows", "missing", "changed", "digest", "matched", "conflict"])
@pytest.mark.parametrize("command", ["registry", "snapshots"])
def test_comparison_consistency(reports, command, kind):
    data = reports[command]
    report = data["report"]
    if kind == "tables":
        report["tables"].pop()
    elif kind == "duplicate":
        report["tables"][1] = report["tables"][0]
    elif kind == "order":
        report["tables"].reverse()
    elif kind == "conflict":
        report["identity_conflicts"] = [{"table": validation.HEADER, "count": 1,
            ("column" if command == "registry" else "columns"): "prefix" if command == "registry" else ["prefix"]}]
    elif kind == "matched":
        report["contents_match"] = 1
    else:
        key, value = {"rows": ("candidate_rows", 0), "missing": ("missing_rows", 2),
                      "changed": ("changed_rows", 2), "digest": ("candidate_digest", "0" * 64)}[kind]
        report["tables"][0][key] = value
    with pytest.raises(validation.ReportValidationError):
        validate(data)


@pytest.mark.parametrize("kind", ["unknown", "duplicate", "order", "count", "columns"])
@pytest.mark.parametrize("command", ["registry", "snapshots"])
def test_conflict_allowlist_and_bounds(pair, command, kind):
    clear(pair[1])
    populate(pair[1], replace(request(), workgroup="104", batch_id=ident(80), request_id=ident(81), preview_identity=ident(82)))
    data, _ = document(command, pair)
    conflicts = data["report"]["identity_conflicts"]
    assert len(conflicts) >= 2
    if kind == "duplicate":
        conflicts.append(conflicts[0])
    elif kind == "order":
        conflicts.reverse()
    elif kind == "unknown":
        conflicts[0]["table"] = "secret-input"
    elif kind == "count":
        conflicts[0]["count"] = 100000
    else:
        conflicts[0]["column" if command == "registry" else "columns"] = "secret-input"
    with pytest.raises(validation.ReportValidationError):
        validate(data)


def test_schema_issues_and_digests_are_cross_checked(reports):
    for issues in ([{"table": "secret-input", "code": "missing"}],
                   [{"table": validation.COST, "code": "unknown"}],
                   [{"table": validation.COST, "code": "missing"}] * 2,
                   [{"table": validation.COST, "code": "missing"},
                    {"table": validation.COST, "code": "columns_mismatch"}]):
        data = json.loads(encode(reports["schema"]))
        data.update(status="differences", exit_code=1)
        data["report"].update(schema_matches=False, observed_digest="0" * 64, issues=issues)
        with pytest.raises(validation.ReportValidationError):
            validate(data)
    reports["schema"]["report"]["observed_digest"] = "0" * 64
    with pytest.raises(validation.ReportValidationError):
        validate(reports["schema"])


def cli_args(command="schema", code=0):
    return ["--development-only", "--operation", command, "--inspection-exit-code", str(code)]


@pytest.mark.parametrize("args", [[], ["--operation", "schema"], ["--help"],
    cli_args() + ["--development-only"], cli_args() + ["--operation", "schema"],
    cli_args() + ["--inspection-exit-code", "0"], cli_args() + ["--unknown", "secret-input"],
    ["--development-only", "--oper", "schema", "--inspection-exit-code", "0"], cli_args(code=3)])
def test_cli_rejection_and_help_do_not_read_stdin(monkeypatch, capsys, args):
    def deny(*args):
        pytest.fail("unexpected stdin read")
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=SimpleNamespace(read=deny)))
    code = cli.main(args)
    output = capsys.readouterr()
    assert code == (0 if args == ["--help"] else 2)
    assert "secret-input" not in output.out + output.err
    if code:
        assert output == ("", cli.ARGUMENT_ERROR + "\n")


@pytest.mark.parametrize("command", producer.COMMANDS)
def test_cli_reads_bounded_once_and_outputs_only_summary(reports, monkeypatch, capsys, command):
    calls = []
    def read(size):
        calls.append(size)
        return encode(reports[command])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=SimpleNamespace(read=read)))
    assert cli.main(cli_args(command)) == 0
    output = capsys.readouterr()
    summary = json.loads(output.out)
    assert output.err == "" and summary["operation"] == command
    assert summary["validation_version"] == validation.VALIDATION_VERSION
    assert summary["warning"] == validation.WARNING
    assert all(summary[flag] is False for flag in validation.FLAGS)
    assert "report" not in summary and "digest" not in output.out
    assert calls == [validation.MAX_REPORT_BYTES + 1]


@pytest.mark.parametrize("phase", ["read", "validate", "write", "flush", "interrupt"])
def test_cli_failures_never_echo_input(reports, monkeypatch, capsys, phase):
    def fail(*args, **kwargs):
        raise (KeyboardInterrupt if phase == "interrupt" else OSError)("secret-input")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(encode(reports["schema"]))))
        if phase in ("read", "interrupt"):
            patch.setattr(sys.stdin.buffer, "read", fail)
        elif phase == "validate":
            patch.setattr(cli, "validate_development_report", fail)
        else:
            patch.setattr(sys, "stdout", SimpleNamespace(write=fail if phase == "write" else lambda value: None,
                                                       flush=fail if phase == "flush" else lambda: None))
        assert cli.main(cli_args()) == (130 if phase == "interrupt" else 3)
    output = capsys.readouterr()
    assert output.out == "" and "secret-input" not in output.err


WORKER = r'''
import sys
from pathlib import Path
root = Path(sys.argv[1])
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).resolve()
        if path.name.startswith(".env") or path.is_relative_to(root / "logs") or path.suffix == ".sqlite":
            raise AssertionError("private file or database")
    if event.startswith(("socket.", "sqlite3.connect")):
        raise AssertionError("connection")
    if event == "import" and (args[0].startswith(("database", "sqlalchemy")) or args[0] in {
        "config", "dotenv", "streamlit", "openai", "engine.development_schema_inspection",
        "engine.development_integrity_inspection", "engine.development_snapshot_comparison",
        "engine.development_registry_comparison", "engine.development_atomic_snapshot", "engine.quote_number_reservation",
    }):
        raise AssertionError("service import")
sys.addaudithook(audit)
sys.path.insert(0, str(root))
import development_report_validation as cli
raise SystemExit(cli.main(sys.argv[2:]))
'''


@pytest.mark.parametrize("command", producer.COMMANDS)
def test_fresh_process_offline_even_after_sources_disappear(reports, pair, tmp_path, command):
    payload = encode(reports[command])
    for path in pair:
        path.unlink()
    result = subprocess.run([sys.executable, "-c", WORKER, str(ROOT), *cli_args(command)],
                            input=payload, capture_output=True, cwd=tmp_path, timeout=10)
    assert result.returncode == 0 and result.stderr == b""
    summary = json.loads(result.stdout)
    assert summary["operation"] == command and summary["warning"] == validation.WARNING
    # 同份過期報告仍可通過，明確證明此功能不能宣稱資料庫仍有效。
    assert "未重新檢查資料庫" in summary["warning"]


def test_actual_script_preserves_difference_exit_and_rejects_truncation(pair, tmp_path):
    clear(pair[1])
    data, _ = document("snapshots", pair)
    payload = encode(data)
    for raw, code in ((payload, 1), (payload[:-1], 3), (payload + b"secret-input", 3)):
        result = subprocess.run([sys.executable, str(ROOT / "development_report_validation.py"), *cli_args("snapshots", 1)],
                                input=raw, capture_output=True, cwd=tmp_path, timeout=10)
        assert result.returncode == code
        assert b"secret-input" not in result.stdout + result.stderr
        if code == 1:
            assert json.loads(result.stdout)["status"] == "differences"
        else:
            assert result.stdout == b"" and result.stderr.decode() == cli.VALIDATION_ERROR + "\n"


@pytest.mark.parametrize("flag", validation.FLAGS)
@pytest.mark.parametrize("level", ["envelope", "report", "reference_integrity", "candidate_integrity"])
def test_all_authority_flags_are_disabled_at_every_level(reports, flag, level):
    data = reports["snapshots"]
    target = data if level == "envelope" else data["report"] if level == "report" else data["report"][level]
    target[flag] = True
    with pytest.raises(validation.ReportValidationError):
        validate(data)


@pytest.mark.parametrize("level", ["counts", "table", "reference_integrity", "candidate_integrity", "conflict", "issue"])
def test_unknown_fields_are_rejected_in_all_nested_shapes(reports, pair, level):
    data = reports["snapshots"]
    if level == "counts":
        target = data["report"]["reference_integrity"]["table_counts"]
    elif level == "table":
        target = data["report"]["tables"][0]
    elif level in ("reference_integrity", "candidate_integrity"):
        target = data["report"][level]
    elif level == "issue":
        mutate(pair[0], "DROP TABLE development_atomic_cost")
        data, _ = document("schema", pair)
        target = data["report"]["issues"][0]
    else:
        clear(pair[1])
        populate(pair[1], replace(request(), batch_id=ident(80)))
        data, _ = document("snapshots", pair)
        target = data["report"]["identity_conflicts"][0]
    target["secret-input"] = "private"
    with pytest.raises(validation.ReportValidationError):
        validate(data)


@pytest.mark.parametrize("name,value", [(validation.CHILD, 3), (validation.LINK, 3),
    (validation.COST, 3), (validation.RESERVED_CHILD, 5), (validation.BATCH, 2), (validation.HEADER, 2)])
def test_plausible_but_inconsistent_integrity_counts_are_rejected(reports, name, value):
    data = reports["integrity"]
    data["report"]["table_counts"][name] = value
    with pytest.raises(validation.ReportValidationError):
        validate(data)


def test_individually_valid_integrity_must_match_snapshot_table_counts(reports):
    data = reports["snapshots"]
    data["report"]["candidate_integrity"]["table_counts"][validation.COST] += 1
    # 單獨摘要的數量可自洽，但與外層逐表對帳不一致仍拒絕。
    validation._integrity(data["report"]["candidate_integrity"])
    with pytest.raises(validation.ReportValidationError):
        validate(data)


def test_common_fabricated_digest_is_not_authenticated(reports):
    data = reports["schema"]
    data["report"]["expected_digest"] = data["report"]["observed_digest"] = "a" * 64
    result = validate(data)
    assert result.inspection_exit_code == 0 and not result.can_resume_numbering
    assert "可能過期或偽造" in result.warning


def test_cli_failure_after_success_never_reuses_old_result(reports, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(encode(reports["schema"]))))
    assert cli.main(cli_args()) == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"secret-input")))
    assert cli.main(cli_args()) == 3
    assert capsys.readouterr() == ("", cli.VALIDATION_ERROR + "\n")


@pytest.mark.parametrize("args,code", [([], 2), (["--help"], 0), (cli_args(), 3), (cli_args(code=3), 2)])
def test_fresh_process_help_denial_and_invalid_input_are_offline(tmp_path, args, code):
    result = subprocess.run([sys.executable, "-c", WORKER, str(ROOT), *args], input=b"secret-input",
                            capture_output=True, cwd=tmp_path, timeout=10)
    assert result.returncode == code
    assert b"secret-input" not in result.stdout + result.stderr
    if code:
        assert result.stdout == b""
