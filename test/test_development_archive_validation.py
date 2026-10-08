"""存檔／回條離線核驗、真實 producer 相容與拒絕路徑。"""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

import development_archive_validation as cli
import development_inspection as producer
import development_inspection_archive as archive
import development_inspection_run as runner
from engine import development_archive_validation as validation
from development_atomic_fixtures import request
from test_development_atomic_snapshot import database, reserve, service
from test_development_integrity_inspection import dump, mutate
from test_development_snapshot_comparison import clear, clone, pair
from test_development_inspection_archive import destination, document, options


def encode(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True) + "\n").encode()


def receipt_for(payload):
    doc = json.loads(payload)
    return {"archive_version": archive.ARCHIVE_VERSION, "run_id": doc["run_id"],
            "operation": doc["operation"], "status": doc["status"],
            "inspection_exit_code": doc["inspection_exit_code"], "publication": "published",
            "byte_count": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "warning": archive.WARNING, **dict.fromkeys(archive.FLAGS, False)}


def check(payload, receipt, document):
    return validation.validate_development_archive(payload, receipt, expected_operation=document["operation"],
                                                    expected_run_id=document["run_id"])


def inputs(tmp_path, document):
    payload = archive._encode(document, document["operation"], document["inspection_exit_code"])
    paths = tmp_path / "archived.json", tmp_path / "receipt.json"
    for path, content in zip(paths, (payload, encode(receipt_for(payload)))):
        path.write_bytes(content)
        path.chmod(0o600)
    return paths


def args(paths, document):
    return ["--development-only", "--operation", document["operation"], "--run-id", document["run_id"],
            "--archive", str(paths[0]), "--receipt", str(paths[1])]


@pytest.mark.parametrize("operation", producer.COMMANDS)
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("state", ["empty", "reserved", "saved"])
def test_real_published_pair(database, tmp_path, destination, capsys, operation, journal, state):
    path = Path(database.url.database)
    mutate(path, f"PRAGMA journal_mode={journal}")
    if state != "empty":
        reserve(database, request())
    if state == "saved":
        service(database).save(request())
    paths = path, clone(path, tmp_path / "candidate.sqlite")
    before = tuple(dump(p) for p in paths)
    assert archive.main(options(operation, paths, destination)) == 0
    output = capsys.readouterr()
    assert output.err == ""
    payload = (destination / "report.json").read_bytes()
    doc = json.loads(payload)
    result = check(payload, output.out.encode(), doc)
    assert result.operation == operation and result.inspection_exit_code == 0
    assert all(getattr(result, key) is False for key in archive.FLAGS)
    with pytest.raises(FrozenInstanceError):
        result.status = "changed"
    assert tuple(dump(p) for p in paths) == before
    # 原庫消失後仍能核驗；沒有重新連線或查價。
    for p in paths:
        p.unlink()
    assert check(payload, output.out.encode(), doc) == result


@pytest.mark.parametrize("operation", ["schema", "registry", "snapshots"])
def test_real_difference_exit_one(pair, destination, tmp_path, capsys, operation):
    if operation == "schema":
        mutate(pair[0], "DROP TABLE development_atomic_cost")
    else:
        clear(pair[1])
    assert archive.main(options(operation, pair, destination)) == 1
    receipt = capsys.readouterr().out.encode()
    path = destination / "report.json"
    doc = json.loads(path.read_bytes())
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_bytes(receipt)
    receipt_path.chmod(0o600)
    assert cli.main(args((path, receipt_path), doc)) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["inspection_exit_code"] == 1 and not output.err


@pytest.mark.parametrize("target", ["document", "receipt"])
@pytest.mark.parametrize("field,value", [("run_id", str(uuid.UUID(int=0))), ("operation", "snapshots"),
    ("status", "secret"), ("inspection_exit_code", False), ("inspection_exit_code", 1),
    ("warning", "secret"), ("unknown", "secret"), *[(key, True) for key in archive.FLAGS]])
def test_envelope_changes_rejected_even_with_new_hash(document, target, field, value):
    doc = deepcopy(document)
    if target == "document":
        doc[field] = value
    payload = encode(doc)
    receipt = receipt_for(payload)
    if target == "receipt":
        receipt[field] = value
    with pytest.raises(validation.ArchiveValidationError, match=validation.ERROR):
        check(payload, encode(receipt), document)


@pytest.mark.parametrize("target,field,value", [("document", "run_version", "future"),
    ("document", "validation_version", "future"), ("receipt", "archive_version", "future"),
    ("receipt", "publication", "pending"), ("receipt", "byte_count", True),
    ("receipt", "byte_count", 0), ("receipt", "sha256", "A" * 64)])
def test_versions_and_receipt_contract(document, target, field, value):
    doc = deepcopy(document)
    if target == "document":
        doc[field] = value
    payload = encode(doc)
    receipt = receipt_for(payload)
    if target == "receipt":
        receipt[field] = value
    with pytest.raises(validation.ArchiveValidationError):
        check(payload, encode(receipt), document)


@pytest.mark.parametrize("target", ["document", "receipt"])
def test_every_missing_field_rejected(document, target):
    for field in (document if target == "document" else receipt_for(encode(document))):
        doc = deepcopy(document)
        receipt = receipt_for(encode(doc))
        if target == "document":
            del doc[field]
            payload = encode(doc)
            receipt.update(byte_count=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        else:
            del receipt[field]
            payload = encode(doc)
        with pytest.raises(validation.ArchiveValidationError):
            check(payload, encode(receipt), document)


@pytest.mark.parametrize("level", ["inspection", "report"])
@pytest.mark.parametrize("field,value", [("unknown", "secret"), ("can_quote", True), ("warning", "secret")])
def test_nested_contract_revalidated(document, level, field, value):
    doc = deepcopy(document)
    target = doc["inspection"] if level == "inspection" else doc["inspection"]["report"]
    target[field] = value
    payload = encode(doc)
    with pytest.raises(validation.ArchiveValidationError):
        check(payload, encode(receipt_for(payload)), document)


@pytest.mark.parametrize("target", ["document", "receipt"])
@pytest.mark.parametrize("bad", [b"", b"{}", b"\xff", b"\xef\xbb\xbf{}", b"{}{}", b"[]",
    b'{"x":1,"x":1}', b'{"x":NaN}', b'{"x":1.0}', b'[' * 1200 + b']' * 1200, b'{"x":'])
def test_invalid_serialization(document, target, bad):
    payload = encode(document)
    receipt = encode(receipt_for(payload))
    with pytest.raises(validation.ArchiveValidationError) as caught:
        check(bad if target == "document" else payload, bad if target == "receipt" else receipt, document)
    assert str(caught.value) == validation.ERROR
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("target", ["document", "receipt"])
@pytest.mark.parametrize("value", [None, {}, "text", bytearray(b"{}"), memoryview(b"{}")])
def test_only_bytes_accepted(document, target, value):
    payload = encode(document)
    receipt = encode(receipt_for(payload))
    with pytest.raises(validation.ArchiveValidationError):
        check(value if target == "document" else payload, value if target == "receipt" else receipt, document)


def test_exact_capacity_and_original_bytes(document):
    payload = encode(document)
    padded = payload + b" " * (validation.MAX_ARCHIVE_BYTES - len(payload))
    receipt = encode(receipt_for(padded))
    padded_receipt = receipt + b" " * (validation.MAX_RECEIPT_BYTES - len(receipt))
    assert check(padded, padded_receipt, document).inspection_exit_code == 0
    for p, r in [(padded + b" ", padded_receipt), (padded, padded_receipt + b" "),
                 (payload, padded_receipt), (payload[:-1], encode(receipt_for(payload)))]:
        with pytest.raises(validation.ArchiveValidationError):
            check(p, r, document)


def test_valid_duplicate_key_rejected(document):
    payload = encode(document)
    for duplicate in (payload.replace(b'{', b'{"can_quote":false,', 1),
                      payload.replace(b'"inspection": {', b'"inspection": {"can_quote":false,', 1)):
        receipt = receipt_for(payload)
        receipt.update(byte_count=len(duplicate), sha256=hashlib.sha256(duplicate).hexdigest())
        with pytest.raises(validation.ArchiveValidationError):
            check(duplicate, encode(receipt), document)


@pytest.mark.parametrize("operation,run_id", [(None, "x"), ([], "x"), ("unknown", "x"),
    ("schema", None), ("schema", "x"), ("schema", str(uuid.uuid1())), ("schema", str(uuid.uuid4()).upper())])
def test_invalid_expectation(operation, run_id):
    with pytest.raises(validation.ArchiveValidationError):
        validation.validate_development_archive(b"", b"", expected_operation=operation, expected_run_id=run_id)


def test_mismatched_pairs_and_expected_run(document):
    first = encode(document)
    doc = deepcopy(document)
    doc["run_id"] = str(uuid.uuid4())
    second = encode(doc)
    with pytest.raises(validation.ArchiveValidationError):
        check(first, encode(receipt_for(second)), document)
    with pytest.raises(validation.ArchiveValidationError):
        check(second, encode(receipt_for(second)), document)
    # 完整換成另一組且 caller 也改預期仍可通過；不是防重放／來源認證。
    assert check(second, encode(receipt_for(second)), doc).warning == validation.WARNING


def test_consistent_forgery_is_not_authenticated(document):
    doc = deepcopy(document)
    report = doc["inspection"]["report"]
    report["expected_digest"] = report["observed_digest"] = "a" * 64
    payload = encode(doc)
    assert check(payload, encode(receipt_for(payload)), doc).can_resume_numbering is False


def test_cli_success_and_failure_do_not_reuse_output(tmp_path, document, capsys):
    paths = inputs(tmp_path, document)
    before = tuple(p.read_bytes() for p in paths)
    assert cli.main(args(paths, document)) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert not output.err and result["validation_version"] == validation.VALIDATION_VERSION
    assert "run_id" not in result and "sha256" not in result and "inspection" not in result
    assert str(tmp_path) not in output.out
    assert tuple(p.read_bytes() for p in paths) == before
    paths[1].write_bytes(b"broken secret")
    assert cli.main(args(paths, document)) == 3
    assert capsys.readouterr() == ("", cli.VALIDATION_ERROR + "\n")


@pytest.mark.parametrize("extra", [["--operation", "schema"], ["--oper", "schema"], ["--unknown"],
    ["--run-id", "x"], ["--inspection-exit-code", "0"], ["--archive", "secret"]])
def test_invalid_options_do_not_open(tmp_path, document, monkeypatch, capsys, extra):
    monkeypatch.setattr(cli, "_open_input", lambda *a: pytest.fail("opened"))
    assert cli.main(args((tmp_path / "a.json", tmp_path / "b.json"), document) + extra) == 2
    assert capsys.readouterr() == ("", cli.ARGUMENT_ERROR + "\n")


def test_help_and_missing_consent_do_not_open(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_open_input", lambda *a: pytest.fail("opened"))
    assert cli.main(["--help"]) == 0
    assert validation.WARNING in "".join(capsys.readouterr().out.split())
    assert cli.main([]) == 2
    assert capsys.readouterr() == ("", cli.ARGUMENT_ERROR + "\n")


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "dangling", "fifo", "shared",
                                  "same", "hardlink", "oversize", "workspace", "private", "uri"])
def test_bad_files_never_read(tmp_path, document, monkeypatch, capsys, kind):
    paths = list(inputs(tmp_path, document))
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
        paths[1].write_bytes(b" " * (validation.MAX_RECEIPT_BYTES + 1))
    elif kind == "workspace":
        monkeypatch.setattr(cli, "ROOT", tmp_path)
    elif kind == "private":
        paths[1] = tmp_path / ".env.json"
    else:
        paths[1] = paths[1].as_uri()
    monkeypatch.setattr(cli, "_read_input", lambda *a: pytest.fail("read"))
    assert cli.main(args(paths, document)) == (2 if kind in ("private", "uri") else 3)
    assert capsys.readouterr().out == ""


def test_short_reads_and_file_mutation(tmp_path, document, monkeypatch, capsys):
    paths = inputs(tmp_path, document)
    real = os.read
    monkeypatch.setattr(os, "read", lambda fd, size: real(fd, min(size, 17)))
    assert cli.main(args(paths, document)) == 0
    capsys.readouterr()
    def changed(fd, size):
        content = real(fd, size)
        if content:
            paths[0].write_bytes(paths[0].read_bytes() + b" ")
        return content
    monkeypatch.setattr(os, "read", changed)
    assert cli.main(args(paths, document)) == 3
    assert capsys.readouterr() == ("", cli.VALIDATION_ERROR + "\n")


@pytest.mark.parametrize("error,code", [(OSError("secret"), 3), (KeyboardInterrupt(), 130)])
def test_read_failure_closes_both(tmp_path, document, monkeypatch, capsys, error, code):
    paths = inputs(tmp_path, document)
    closed = []
    close = os.close
    def closing(fd):
        closed.append(fd)
        close(fd)
    def fail(*a):
        raise error
    monkeypatch.setattr(os, "close", closing)
    monkeypatch.setattr(cli, "_read_input", fail)
    assert cli.main(args(paths, document)) == code
    output = capsys.readouterr()
    assert not output.out and "secret" not in output.err
    assert len(closed) == 2 and len(set(closed)) == 2
    for fd in closed:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("kind", ["write", "short", "flush"])
def test_output_failure(tmp_path, document, monkeypatch, capsys, kind):
    paths = inputs(tmp_path, document)
    class Output:
        def write(self, text):
            if kind == "write":
                raise OSError("secret")
            return len(text) - 1 if kind == "short" else len(text)
        def flush(self):
            if kind == "flush":
                raise OSError("secret")
    monkeypatch.setattr(sys, "stdout", Output())
    assert cli.main(args(paths, document)) == 3
    assert capsys.readouterr().err == cli.VALIDATION_ERROR + "\n"


def test_fresh_process_isolation(tmp_path, document):
    paths = inputs(tmp_path, document)
    script = r'''
import sys
from pathlib import Path
root = Path(sys.argv[1])
def audit(event, args):
    if event == "import" and (args[0].split(".")[0] in {
        "sqlalchemy", "sqlite3", "database", "agent", "streamlit", "openai", "dotenv", "config"
    } or args[0] in {"development_inspection_run", "development_inspection_archive",
        "engine.development_schema_inspection", "engine.development_integrity_inspection",
        "engine.development_registry_comparison", "engine.development_snapshot_comparison"}):
        raise RuntimeError("forbidden dependency")
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "sqlite3.connect"}:
        raise RuntimeError("forbidden execution")
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).resolve()
        if path.name.startswith(".env") or path.is_relative_to(root / "logs"):
            raise RuntimeError("private input")
sys.addaudithook(audit)
sys.path.insert(0, str(root))
import development_archive_validation
raise SystemExit(development_archive_validation.main(sys.argv[2:]))
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(cli.ROOT), *args(paths, document)],
                            cwd=tmp_path, env={}, capture_output=True, timeout=10)
    assert result.returncode == 0 and result.stderr == b""
    assert json.loads(result.stdout)["warning"] == validation.WARNING


def test_real_script_exit_and_help(tmp_path, document):
    paths = inputs(tmp_path, document)
    for arguments, code in [(args(paths, document), 0), ([], 2), (["--help"], 0)]:
        result = subprocess.run([sys.executable, str(cli.ROOT / "development_archive_validation.py"), *arguments],
                                cwd=tmp_path, env={}, capture_output=True, timeout=10)
        assert result.returncode == code
        assert b"Traceback" not in result.stderr


@pytest.mark.parametrize("target", ["document", "receipt"])
@pytest.mark.parametrize("transform", [lambda p: b"\xef\xbb\xbf" + p, lambda p: p + b"{}",
    lambda p: p[:-3], lambda p: p.replace(b"false", b"0", 1),
    lambda p: p.replace(b"false", b"0.0", 1), lambda p: p.replace(b"false", b"NaN", 1)])
def test_corrupted_valid_documents(document, target, transform):
    payload = encode(document)
    if target == "document":
        payload = transform(payload)
        receipt = receipt_for(encode(document))
        receipt.update(byte_count=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        receipt = encode(receipt)
    else:
        receipt = transform(encode(receipt_for(payload)))
    with pytest.raises(validation.ArchiveValidationError):
        check(payload, receipt, document)


def test_platform_rejected_before_open(tmp_path, document, monkeypatch, capsys):
    arguments = args((tmp_path / "a.json", tmp_path / "b.json"), document)
    parsed = cli._arguments(arguments)
    monkeypatch.setattr(cli, "_arguments", lambda *a: parsed)
    monkeypatch.setattr(cli, "_open_input", lambda *a: pytest.fail("opened"))
    # 不改全域 os.name，避免影響 pytest 的 Path 平台選擇。
    from types import SimpleNamespace
    monkeypatch.setattr(cli, "os", SimpleNamespace(name="nt"))
    assert cli.main(arguments) == 3
    assert capsys.readouterr() == ("", cli.VALIDATION_ERROR + "\n")


def test_replaced_file_after_open_is_rejected(tmp_path, document, monkeypatch, capsys):
    paths = inputs(tmp_path, document)
    read = cli._read_input
    def replacing(fd, info, maximum):
        if maximum == validation.MAX_ARCHIVE_BYTES:
            replacement = tmp_path / "replacement.json"
            replacement.write_bytes(paths[0].read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, paths[0])
        return read(fd, info, maximum)
    monkeypatch.setattr(cli, "_read_input", replacing)
    assert cli.main(args(paths, document)) == 3
    assert capsys.readouterr() == ("", cli.VALIDATION_ERROR + "\n")


def test_mutation_of_first_file_during_second_read(tmp_path, document, monkeypatch, capsys):
    paths = inputs(tmp_path, document)
    read = cli._read_input
    def changing(fd, info, maximum):
        if maximum == validation.MAX_RECEIPT_BYTES:
            paths[0].write_bytes(paths[0].read_bytes() + b" ")
        return read(fd, info, maximum)
    monkeypatch.setattr(cli, "_read_input", changing)
    assert cli.main(args(paths, document)) == 3
    assert capsys.readouterr() == ("", cli.VALIDATION_ERROR + "\n")


def test_read_capacity_and_growth_are_bounded(tmp_path, document, monkeypatch, capsys):
    paths = inputs(tmp_path, document)
    sizes = []
    def endless(fd, size):
        sizes.append(size)
        return b" " * size
    monkeypatch.setattr(os, "read", endless)
    assert cli.main(args(paths, document)) == 3
    assert sum(sizes) == validation.MAX_ARCHIVE_BYTES + 1
    assert max(sizes) <= 16384
    assert capsys.readouterr() == ("", cli.VALIDATION_ERROR + "\n")


def test_owner_check_before_read(tmp_path, document, monkeypatch, capsys):
    paths = inputs(tmp_path, document)
    uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: uid + 1)
    monkeypatch.setattr(cli, "_read_input", lambda *a: pytest.fail("read"))
    assert cli.main(args(paths, document)) == 3
    assert capsys.readouterr() == ("", cli.VALIDATION_ERROR + "\n")
