"""接收紀錄完整離線核驗、真實 producer 及拒絕／認證界線。"""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest

import development_archive_validation as reader
import development_reception_validation as cli
import development_inspection_receive as receive
from engine import development_reception_validation as validation
from development_atomic_fixtures import request
from test_development_atomic_snapshot import database, reserve, service
from test_development_integrity_inspection import dump, mutate
from test_development_snapshot_comparison import clear, clone, pair
from test_development_inspection_archive import destination, document
from test_development_inspection_receive import args as receive_args, synthetic
from test_development_archive_validation import encode, receipt_for


@pytest.fixture
def documents(synthetic):
    payload, receipt = synthetic
    record, _ = receive._encode(payload, receipt, "schema", 0)
    return payload, record


def check(payload, record, expected=None):
    expected = json.loads(payload) if expected is None else expected
    return validation.validate_development_reception(payload, record,
        expected_operation=expected["operation"], expected_run_id=expected["run_id"])


def inputs(tmp_path, documents):
    paths = tmp_path / "report.json", tmp_path / "reception.json"
    for path, payload in zip(paths, documents):
        path.write_bytes(payload)
        path.chmod(0o600)
    return paths


def args(paths, payload):
    document = json.loads(payload)
    return ["--development-only", "--operation", document["operation"], "--run-id", document["run_id"],
            "--archive", str(paths[0]), "--reception", str(paths[1])]


@pytest.mark.parametrize("operation", receive.inspection.COMMANDS)
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("state", ["empty", "reserved", "saved"])
def test_real_reception_chain(database, tmp_path, destination, capsys, operation, journal, state):
    path = Path(database.url.database)
    mutate(path, f"PRAGMA journal_mode={journal}")
    if state != "empty":
        reserve(database, request())
    if state == "saved":
        service(database).save(request())
    sources = path, clone(path, tmp_path / "candidate.sqlite")
    before = tuple(dump(p) for p in sources)
    assert receive.main(receive_args(operation, sources, destination)) == 0
    output = capsys.readouterr()
    assert not output.err
    paths = destination / "report.json", destination / "reception.json"
    contents = tuple(p.read_bytes() for p in paths)
    result = check(*contents)
    assert result.operation == operation and result.recorded_archive_process_exit_code == 0
    assert all(getattr(result, flag) is False for flag in receive.archive.FLAGS)
    with pytest.raises(FrozenInstanceError):
        result.status = "changed"
    assert tuple(dump(p) for p in sources) == before
    for p in sources:
        p.unlink()
    assert cli.main(args(paths, contents[0])) == 0
    output = capsys.readouterr()
    assert not output.err and json.loads(output.out)["warning"] == validation.WARNING
    assert tuple(p.read_bytes() for p in paths) == contents
    assert check(*contents) == result


@pytest.mark.parametrize("operation", ["schema", "registry", "snapshots"])
def test_real_difference_stays_one(pair, destination, capsys, operation):
    if operation == "schema":
        mutate(pair[0], "DROP TABLE development_atomic_cost")
    else:
        clear(pair[1])
    assert receive.main(receive_args(operation, pair, destination)) == 1
    capsys.readouterr()
    paths = destination / "report.json", destination / "reception.json"
    assert cli.main(args(paths, paths[0].read_bytes())) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["recorded_archive_process_exit_code"] == result["inspection_exit_code"] == 1
    assert not output.err


@pytest.mark.parametrize("field,value", [("reception_version", "future"), ("run_id", str(uuid.uuid4())),
    ("operation", "snapshots"), ("status", "secret"), ("warning", "secret"), ("unknown", "secret"),
    ("archive_process_exit_code", False), ("archive_process_exit_code", 1), ("archive_process_exit_code", -9),
    ("inspection_exit_code", False), ("inspection_exit_code", 1), ("receipt_byte_count", True),
    ("receipt_byte_count", 0), ("receipt_sha256", "A" * 64), ("receipt_utf8", None),
    ("receipt_utf8", "\ud800"), *[(key, True) for key in receive.archive.FLAGS]])
def test_record_changes(documents, field, value):
    payload, raw = documents
    record = json.loads(raw)
    record[field] = value
    with pytest.raises(validation.ReceptionValidationError, match=validation.ERROR):
        check(payload, encode(record))


@pytest.mark.parametrize("field,value", [("operation", "snapshots"), ("status", "secret"),
    ("inspection_exit_code", False), ("inspection_exit_code", 1), ("validation_version", "future"),
    ("warning", "secret"), ("unknown", "secret"), *[(key, 0) for key in receive.archive.FLAGS]])
def test_stored_validation_is_not_trusted(documents, field, value):
    payload, raw = documents
    record = json.loads(raw)
    record["validation"][field] = value
    with pytest.raises(validation.ReceptionValidationError):
        check(payload, encode(record))


@pytest.mark.parametrize("level", ["record", "validation"])
def test_all_missing_fields(documents, level):
    payload, raw = documents
    original = json.loads(raw)
    for field in (original if level == "record" else original["validation"]):
        record = deepcopy(original)
        del (record if level == "record" else record["validation"])[field]
        with pytest.raises(validation.ReceptionValidationError):
            check(payload, encode(record))


@pytest.mark.parametrize("bad", [b"", b"{}", b"[]", b"\xff", b"\xef\xbb\xbf{}", b"{}{}",
    b'{"x":1,"x":1}', b'{"x":NaN}', b'{"x":1.0}', b'[' * 1200 + b']' * 1200,
    None, {}, "text", bytearray(b"{}"), memoryview(b"{}")])
@pytest.mark.parametrize("target", [0, 1])
def test_invalid_input_types_and_encoding(documents, bad, target):
    values = list(documents)
    values[target] = bad
    with pytest.raises(validation.ReceptionValidationError) as caught:
        check(*values, expected=json.loads(documents[0]))
    assert str(caught.value) == validation.ERROR and caught.value.__suppress_context__


@pytest.mark.parametrize("transform", [lambda p: b"\xef\xbb\xbf" + p, lambda p: p + b"{}",
    lambda p: p[:-3], lambda p: p.replace(b"false", b"0", 1),
    lambda p: p.replace(b"false", b"0.0", 1), lambda p: p.replace(b"false", b"NaN", 1),
    lambda p: p.replace(b"{", b'{"can_quote":false,', 1)])
def test_corrupted_valid_record(documents, transform):
    with pytest.raises(validation.ReceptionValidationError):
        check(documents[0], transform(documents[1]))


@pytest.mark.parametrize("level", ["receipt", "inspection", "report"])
@pytest.mark.parametrize("field,value", [("unknown", "secret"), ("warning", "secret"), ("can_save", True)])
def test_nested_contract_rechecked_after_rehash(documents, level, field, value):
    payload, raw = documents
    record = json.loads(raw)
    receipt = json.loads(record["receipt_utf8"])
    if level == "receipt":
        receipt[field] = value
    else:
        doc = json.loads(payload)
        target = doc["inspection"] if level == "inspection" else doc["inspection"]["report"]
        target[field] = value
        payload = encode(doc)
        receipt.update(byte_count=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    changed = encode(receipt)
    record.update(receipt_utf8=changed.decode(), receipt_byte_count=len(changed),
                  receipt_sha256=hashlib.sha256(changed).hexdigest())
    with pytest.raises(validation.ReceptionValidationError):
        check(payload, encode(record))


def test_exact_capacities_and_raw_receipt_bytes(documents):
    payload, raw = documents
    payload += b" " * (reader.MAX_ARCHIVE_BYTES - len(payload))
    receipt = encode(receipt_for(payload))
    receipt += b" " * (reader.MAX_RECEIPT_BYTES - len(receipt))
    record, _ = receive._encode(payload, receipt, "schema", 0)
    record += b" " * (validation.MAX_RECEPTION_BYTES - len(record))
    assert check(payload, record).inspection_exit_code == 0
    for p, r in [(payload + b" ", record), (payload, record + b" "), (payload[:-1], record)]:
        with pytest.raises(validation.ReceptionValidationError):
            check(p, r)
    doc = json.loads(record)
    doc["receipt_utf8"] += " "
    doc["receipt_byte_count"] += 1
    doc["receipt_sha256"] = hashlib.sha256(doc["receipt_utf8"].encode()).hexdigest()
    with pytest.raises(validation.ReceptionValidationError):
        check(payload, encode(doc))
    doc = json.loads(raw)
    doc["receipt_utf8"] += " "
    with pytest.raises(validation.ReceptionValidationError):
        check(documents[0], encode(doc))


def test_mismatch_replay_and_consistent_forgery_boundary(documents):
    payload, record = documents
    doc = json.loads(payload)
    original = deepcopy(doc)
    doc["run_id"] = str(uuid.uuid4())
    doc["inspection"]["report"]["expected_digest"] = "a" * 64
    doc["inspection"]["report"]["observed_digest"] = "a" * 64
    forged = encode(doc)
    forged_record, _ = receive._encode(forged, encode(receipt_for(forged)), "schema", 0)
    for p, r, expected in [(payload, forged_record, original), (forged, record, doc),
                           (forged, forged_record, original)]:
        with pytest.raises(validation.ReceptionValidationError):
            check(p, r, expected)
    # 沒有執行存檔／接收仍可共同偽造，且可以重放；核驗只檢查內容。
    first = check(forged, forged_record)
    assert check(forged, forged_record) == first and first.can_resume_numbering is False
    # 外層排版不是簽章；不要求接收紀錄本身的外部摘要。
    assert check(forged, json.dumps(json.loads(forged_record), indent=2).encode()) == first


@pytest.mark.parametrize("operation,run_id", [(None, "x"), ([], "x"), ("unknown", "x"),
    ("schema", None), ("schema", "x"), ("schema", str(uuid.uuid1())),
    ("schema", str(uuid.uuid4()).upper())])
def test_invalid_expectation(documents, operation, run_id):
    with pytest.raises(validation.ReceptionValidationError):
        validation.validate_development_reception(*documents, expected_operation=operation, expected_run_id=run_id)


def test_cli_summary_and_no_stale_output(tmp_path, documents, capsys):
    paths = inputs(tmp_path, documents)
    assert cli.main(args(paths, documents[0])) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert not output.err and result["validation_version"] == validation.VALIDATION_VERSION
    assert set(result) == {"operation", "inspection_exit_code", "recorded_archive_process_exit_code", "status",
                           "validation_version", "warning", *receive.archive.FLAGS}
    assert str(tmp_path) not in output.out and json.loads(documents[0])["run_id"] not in output.out
    paths[1].write_bytes(b"secret broken input")
    assert cli.main(args(paths, documents[0])) == 3
    assert capsys.readouterr() == ("", cli.VALIDATION_ERROR + "\n")


@pytest.mark.parametrize("extra", [["--operation", "schema"], ["--oper", "schema"], ["--unknown"],
    ["--run-id", "x"], ["--archive-process-exit-code", "0"], ["--receipt", "secret"],
    ["--reception", "secret"], ["--development-only"]])
def test_invalid_options_never_open(tmp_path, documents, monkeypatch, capsys, extra):
    monkeypatch.setattr(cli, "_open_input", lambda *a: pytest.fail("opened"))
    assert cli.main(args((tmp_path / "a.json", tmp_path / "b.json"), documents[0]) + extra) == 2
    assert capsys.readouterr() == ("", cli.ARGUMENT_ERROR + "\n")


def test_help_and_missing_consent_never_open(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_open_input", lambda *a: pytest.fail("opened"))
    assert cli.main(["--help"]) == 0
    assert validation.WARNING in "".join(capsys.readouterr().out.split())
    assert cli.main([]) == 2
    assert capsys.readouterr() == ("", cli.ARGUMENT_ERROR + "\n")


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "dangling", "fifo", "shared",
    "same", "hardlink", "oversize", "workspace", "private", "uri", "owner"])
def test_bad_files_before_content_read(tmp_path, documents, monkeypatch, capsys, kind):
    paths = list(inputs(tmp_path, documents))
    if kind in ("missing", "directory", "symlink", "dangling", "fifo", "hardlink"):
        paths[1].unlink()
        if kind == "directory":
            paths[1].mkdir()
        elif kind in ("symlink", "dangling"):
            paths[1].symlink_to(paths[0] if kind == "symlink" else tmp_path / "absent")
        elif kind == "fifo":
            os.mkfifo(paths[1], 0o600)
        elif kind == "hardlink":
            paths[1].hardlink_to(paths[0])
    elif kind == "shared":
        paths[1].chmod(0o644)
    elif kind == "same":
        paths[1] = paths[0]
    elif kind == "oversize":
        paths[1].write_bytes(b" " * (validation.MAX_RECEPTION_BYTES + 1))
    elif kind == "workspace":
        monkeypatch.setattr(reader, "ROOT", tmp_path)
    elif kind == "private":
        paths[1] = tmp_path / ".env.json"
    elif kind == "owner":
        uid = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: uid + 1)
    else:
        paths[1] = paths[1].as_uri()
    monkeypatch.setattr(cli, "_read_input", lambda *a: pytest.fail("read"))
    assert cli.main(args(paths, documents[0])) == (2 if kind in ("private", "uri") else 3)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("change", ["grow", "replace", "first_during_second"])
def test_file_changes_close_both(tmp_path, documents, monkeypatch, capsys, change):
    paths = inputs(tmp_path, documents)
    read = cli._read_input
    def changing(fd, info, maximum):
        if change == "grow" or (change == "first_during_second" and maximum == validation.MAX_RECEPTION_BYTES):
            paths[0].write_bytes(paths[0].read_bytes() + b" ")
        elif change == "replace":
            replacement = tmp_path / "replacement.json"
            replacement.write_bytes(documents[0])
            replacement.chmod(0o600)
            os.replace(replacement, paths[0])
        return read(fd, info, maximum)
    monkeypatch.setattr(cli, "_read_input", changing)
    assert cli.main(args(paths, documents[0])) == 3
    assert capsys.readouterr() == ("", cli.VALIDATION_ERROR + "\n")


def test_short_reads_and_bounded_growth(tmp_path, documents, monkeypatch, capsys):
    paths = inputs(tmp_path, documents)
    read = os.read
    monkeypatch.setattr(os, "read", lambda fd, size: read(fd, min(size, 17)))
    assert cli.main(args(paths, documents[0])) == 0
    capsys.readouterr()
    sizes = []
    def endless(fd, size):
        sizes.append(size)
        return b" " * size
    monkeypatch.setattr(os, "read", endless)
    assert cli.main(args(paths, documents[0])) == 3
    assert sum(sizes) == reader.MAX_ARCHIVE_BYTES + 1 and max(sizes) <= 16384
    assert capsys.readouterr() == ("", cli.VALIDATION_ERROR + "\n")


@pytest.mark.parametrize("error,code", [(OSError("secret"), 3), (KeyboardInterrupt(), 130)])
def test_failure_closes_descriptors(tmp_path, documents, monkeypatch, capsys, error, code):
    paths = inputs(tmp_path, documents)
    closed = []
    close = os.close
    def closing(fd):
        closed.append(fd)
        close(fd)
    def fail(*a):
        raise error
    monkeypatch.setattr(os, "close", closing)
    monkeypatch.setattr(cli, "_read_input", fail)
    assert cli.main(args(paths, documents[0])) == code
    output = capsys.readouterr()
    assert not output.out and "secret" not in output.err
    assert len(closed) == len(set(closed)) == 2
    for fd in closed:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("kind", ["write", "short", "flush"])
def test_output_failure(tmp_path, documents, monkeypatch, capsys, kind):
    paths = inputs(tmp_path, documents)
    class Output:
        def write(self, text):
            if kind == "write":
                raise OSError("secret")
            return len(text) - 1 if kind == "short" else len(text)
        def flush(self):
            if kind == "flush":
                raise OSError("secret")
    monkeypatch.setattr(sys, "stdout", Output())
    assert cli.main(args(paths, documents[0])) == 3
    assert capsys.readouterr().err == cli.VALIDATION_ERROR + "\n"


def test_platform_rejected_before_open(tmp_path, documents, monkeypatch, capsys):
    arguments = args((tmp_path / "a.json", tmp_path / "b.json"), documents[0])
    parsed = cli._arguments(arguments)
    monkeypatch.setattr(cli, "_arguments", lambda *a: parsed)
    monkeypatch.setattr(cli, "_open_input", lambda *a: pytest.fail("opened"))
    monkeypatch.setattr(cli, "os", SimpleNamespace(name="nt"))
    assert cli.main(arguments) == 3
    assert capsys.readouterr() == ("", cli.VALIDATION_ERROR + "\n")


def test_fresh_process_import_and_execution_isolation(tmp_path, documents):
    paths = inputs(tmp_path, documents)
    script = r'''
import os
import sys
from pathlib import Path
root = Path(sys.argv[1])
def audit(event, args):
    if event == "import" and (args[0].split(".")[0] in {
        "sqlalchemy", "sqlite3", "database", "agent", "streamlit", "openai", "dotenv", "config"
    } or args[0] in {"development_inspection_receive", "development_inspection_run", "development_inspection_archive",
        "engine.development_schema_inspection", "engine.development_integrity_inspection",
        "engine.development_registry_comparison", "engine.development_snapshot_comparison"}):
        raise RuntimeError("forbidden dependency")
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "sqlite3.connect"}:
        raise RuntimeError("forbidden execution")
    if event == "open":
        if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise RuntimeError("write denied")
        if isinstance(args[0], (str, bytes)):
            path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).resolve()
            if any(p.startswith(".env") for p in path.parts) or path.is_relative_to(root / "logs"):
                raise RuntimeError("private input")
sys.addaudithook(audit)
sys.path.insert(0, str(root))
import development_reception_validation
raise SystemExit(development_reception_validation.main(sys.argv[2:]))
'''
    for arguments, code in [(args(paths, documents[0]), 0), ([], 2), (["--help"], 0)]:
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(reader.ROOT), *arguments],
                                cwd=tmp_path, env={}, capture_output=True, timeout=10)
        assert result.returncode == code and b"Traceback" not in result.stderr
        if arguments and arguments[0] != "--help":
            assert not result.stderr and json.loads(result.stdout)["warning"] == validation.WARNING


def test_real_script_exits(tmp_path, documents):
    paths = inputs(tmp_path, documents)
    for arguments, code in [(args(paths, documents[0]), 0), ([], 2), (["--help"], 0)]:
        result = subprocess.run([sys.executable, "-B", str(reader.ROOT / "development_reception_validation.py"),
                                 *arguments], cwd=tmp_path, env={}, capture_output=True, timeout=10)
        assert result.returncode == code and b"Traceback" not in result.stderr


def test_old_and_new_document_types_are_not_interchangeable(documents):
    payload, record = documents
    receipt = json.loads(record)["receipt_utf8"].encode()
    with pytest.raises(validation.ReceptionValidationError):
        check(payload, receipt)
    with pytest.raises(receive.validation.ArchiveValidationError):
        receive.validation.validate_development_archive(payload, record,
            expected_operation="schema", expected_run_id=json.loads(payload)["run_id"])
    _, publication_summary = receive._encode(payload, receipt, "schema", 0)
    with pytest.raises(validation.ReceptionValidationError):
        check(payload, publication_summary.encode())


@pytest.mark.parametrize("operation", receive.inspection.COMMANDS)
def test_two_real_runs_cannot_exchange_records(pair, destination, capsys, operation):
    assert receive.main(receive_args(operation, pair, destination)) == 0
    capsys.readouterr()
    first = tuple((destination / name).read_bytes() for name in ("report.json", "reception.json"))
    second_args = receive_args(operation, pair, destination, "second-reception.json")
    second_args[second_args.index("--report-name") + 1] = "second-report.json"
    assert receive.main(second_args) == 0
    capsys.readouterr()
    second = tuple((destination / name).read_bytes() for name in ("second-report.json", "second-reception.json"))
    assert json.loads(first[0])["run_id"] != json.loads(second[0])["run_id"]
    assert check(*first).operation == check(*second).operation == operation
    for payload, record in ((first[0], second[1]), (second[0], first[1])):
        with pytest.raises(validation.ReceptionValidationError):
            check(payload, record)


@pytest.mark.parametrize("operation", ["schema", "registry", "snapshots"])
def test_actual_script_difference_and_invalid_exit(pair, destination, capsys, operation):
    if operation == "schema":
        mutate(pair[0], "DROP TABLE development_atomic_cost")
    else:
        clear(pair[1])
    assert receive.main(receive_args(operation, pair, destination)) == 1
    capsys.readouterr()
    paths = destination / "report.json", destination / "reception.json"
    arguments = args(paths, paths[0].read_bytes())
    command = [sys.executable, "-B", str(reader.ROOT / "development_reception_validation.py"), *arguments]
    result = subprocess.run(command, env={}, capture_output=True, timeout=10)
    assert result.returncode == 1 and result.stderr == b""
    assert json.loads(result.stdout)["recorded_archive_process_exit_code"] == 1
    paths[1].write_bytes(paths[1].read_bytes()[:-3])
    result = subprocess.run(command, env={}, capture_output=True, timeout=10)
    assert result.returncode == 3 and not result.stdout
    assert result.stderr.decode() == cli.VALIDATION_ERROR + "\n"


def test_second_input_failure_closes_first(tmp_path, documents, monkeypatch, capsys):
    paths = inputs(tmp_path, documents)
    paths[1].unlink()
    closed = []
    close = os.close
    def closing(fd):
        closed.append(fd)
        close(fd)
    monkeypatch.setattr(os, "close", closing)
    assert cli.main(args(paths, documents[0])) == 3
    assert len(closed) == 1
    with pytest.raises(OSError):
        os.fstat(closed[0])
    assert capsys.readouterr() == ("", cli.VALIDATION_ERROR + "\n")
