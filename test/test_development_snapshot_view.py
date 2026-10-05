"""T30 固定來源、封印、唯讀 SQL、完整安全匯出與正式入口隔離。"""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import DatabaseError

from development_atomic_fixtures import SHARED, ident, request
from development_snapshot_view_fixtures import database, saved, readonly_engine
from test_development_atomic_snapshot import reserve, service, registry_rows
from engine.development_atomic_contract import canonical, digest, identity_of
from engine.development_snapshot_view import (
    DevelopmentSnapshotReader, VerifiedDevelopmentSnapshot, batch_view, child_view,
    export_snapshot, WARNING, CONFIDENTIAL, RESERVATION_WARNING, VIEW_VERSION,
)


def test_complete_four_eleven_raw_values_and_exact_original_totals(saved):
    handle = saved.reader.read(saved.identity)
    result = batch_view(saved.reader, handle)
    summary = result["content"]["summary"]
    assert summary["child_count"] == 4 and summary["product_qty"] == 11
    assert summary["batch_totals_json"] == saved.req.totals_json
    assert summary["batch_totals"]["total_price"] == "1.40"
    assert result["content"]["saved_snapshot"] == saved.document
    for i, child in enumerate(saved.req.children):
        view = child_view(saved.reader, handle, child.child_quote_id)
        content = view["content"]
        assert content["child"] == saved.document["children"][i]
        assert content["fixed_batch"] == {k: v for k, v in saved.document["fixed_request"].items() if k != "children"}
        assert content["reservation_child"] == saved.document["reservation"]["children"][i]
        assert content["batch_context"]["identity"] == asdict(saved.identity)
        assert content["batch_context"]["final_digest"] == saved.document["final_digest"]
        fixed = content["child"]["fixed_child"]
        assert fixed["shared_description"] == SHARED
        assert fixed["rows"][0]["seq_no"] == "00001"
        assert fixed["rows"][0]["original_qty"] == "1.000000000000000001"
        assert [r["path"] for r in fixed["rows"]] == [r.path for r in child.rows]
        assert fixed["amounts"]["tax_rounding_difference"] == "-0.01"
        assert fixed["amounts"]["cost_display_difference"] == "-0.005"
        assert "非本張金額" in content["batch_total_meaning"]
    assert "無孔有桌下走線" in result["content"]["saved_snapshot"]["children"][0]["fixed_child"]["description"]
    assert registry_rows(saved.database) == saved.registry_before


def test_reader_verifies_all_six_tables_under_readonly_authorizer(saved):
    assert saved.actions == [] and saved.statements == []  # 建構不開連線。
    assert not hasattr(saved.reader, "save") and not hasattr(saved.reader, "reserve")
    handle = saved.reader.read(saved.identity)
    reads = {table for action, table in saved.actions if action == sqlite3.SQLITE_READ}
    assert {"development_atomic_batch", "development_atomic_child", "development_atomic_source_link",
            "development_atomic_cost", "quote_number_reservation", "quote_number_reserved_child"} <= reads
    count = len(saved.statements)
    for child in (None, *(c.child_quote_id for c in saved.req.children)):
        for fmt in ("json", "txt"):
            export_snapshot(saved.reader, handle, child_quote_id=child, format=fmt)
    assert len(saved.statements) == count
    assert not any(action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE)
                   for action, _ in saved.actions)
    # 不只宣稱唯讀：實際 DML／DDL 全遭 authorizer 阻擋。
    for sql in ("INSERT INTO development_atomic_batch(batch_id) VALUES ('x')",
                "UPDATE development_atomic_cost SET path='x'", "DELETE FROM development_atomic_cost",
                "CREATE TABLE forbidden(x)", "DROP TABLE development_atomic_cost"):
        with saved.engine.connect() as connection, pytest.raises(DatabaseError):
            connection.exec_driver_sql(sql)
    assert registry_rows(saved.database) == saved.registry_before


def test_snapshot_does_not_use_master_price_formula_reserve_or_save(saved, monkeypatch):
    from test_quote_batch_review import forbid_side_effects
    service_type = type(service(saved.database))
    spies = forbid_side_effects(monkeypatch)
    save_spy = Mock(side_effect=AssertionError("不得保存"))
    monkeypatch.setattr(service_type, "save", save_spy)
    with saved.database.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE synthetic_current_master (compri TEXT, policy TEXT)")
        connection.exec_driver_sql("INSERT INTO synthetic_current_master VALUES ('0.10', 'original')")
    first = saved.reader.read(saved.identity)
    before = export_snapshot(saved.reader, first).data
    with saved.database.begin() as connection:
        connection.exec_driver_sql("UPDATE synthetic_current_master SET compri='999.99', policy='changed'")
    # 真正異動隔離庫合成主檔，價格入口也已換成失敗替身；讀取只依賴保存內容。
    second = saved.reader.read(saved.identity)
    assert export_snapshot(saved.reader, second).data == before
    assert not any(name == "synthetic_current_master" for action, name in saved.actions)
    for spy in [*spies, save_spy]:
        spy.assert_not_called()


def test_arbitrary_dictionary_rehash_and_cross_reader_handle_rejected(saved):
    handle = saved.reader.read(saved.identity)
    raw = deepcopy(saved.document)
    raw["children"].pop()
    raw["final_digest"] = digest(canonical({k: v for k, v in raw.items() if k != "final_digest"}))
    candidates = [saved.document, raw, {}, json.loads(export_snapshot(saved.reader, handle).data),
                  replace(handle, _payload=canonical(raw)),
                  VerifiedDevelopmentSnapshot(saved.identity, canonical(raw), digest(canonical(raw))),
                  replace(handle, identity=replace(saved.identity, workgroup="104"))]
    for candidate in candidates:
        with pytest.raises(ValueError):
            batch_view(saved.reader, candidate)
        with pytest.raises(ValueError):
            export_snapshot(saved.reader, candidate)
    other = DevelopmentSnapshotReader(saved.engine)
    with pytest.raises(ValueError):
        batch_view(other, handle)
    with pytest.raises(ValueError):
        batch_view(Mock(read=lambda _: saved.document), handle)
    # 改展示複本不改 handle，也不能以匯出字典重匯入。
    view = batch_view(saved.reader, handle)
    view["content"]["saved_snapshot"]["children"].clear()
    assert len(batch_view(saved.reader, handle)["content"]["saved_snapshot"]["children"]) == 4


def test_invalid_identity_schema_and_legacy_dict_never_sql(saved):
    for candidate in (asdict(saved.identity), {}, replace(saved.identity, schema_version="legacy"),
                      replace(saved.identity, workgroup="../"), replace(saved.identity, save_id="../secret")):
        with pytest.raises(ValueError):
            saved.reader.read(candidate)
    assert saved.statements == []


def test_absent_wrong_workgroup_and_crossbatch_identity_fail_closed(saved):
    assert saved.reader.read(replace(saved.identity, batch_id=ident(101), request_id=ident(102),
                                     preview_identity=ident(103), save_id=ident(104))) is None
    for field in ("workgroup", "batch_id", "request_id", "preview_identity", "save_id"):
        with pytest.raises(ValueError):
            saved.reader.read(replace(saved.identity, **{field: "104" if field == "workgroup" else ident(999)}))


@pytest.mark.parametrize("table,operation", [
    ("child", "delete"), ("link", "delete"), ("cost", "delete"),
    ("batch", "payload"), ("child", "payload"), ("link", "payload"), ("cost", "payload"),
    ("registry", "delete"), ("registry", "payload"),
])
def test_full_read_rejects_missing_or_tampered_sibling_no_partial_handle(saved, table, operation):
    target = service(saved.database)
    if table == "registry":
        target = target._registry.children if operation == "delete" else target._registry.header
    else:
        target = getattr(target, table)
    with saved.database.connect() as connection:
        # 模擬管理員／歷史損壞；只在測試 writer 暫時停用 FK，reader 仍強制 FK ON。
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        statement = target.delete() if operation == "delete" else target.update().values(payload="{}")
        connection.execute(statement)
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    with pytest.raises(ValueError):
        saved.reader.read(saved.identity)


def test_download_fixed_after_database_change_until_explicit_refresh(saved):
    handle = saved.reader.read(saved.identity)
    before = export_snapshot(saved.reader, handle).data
    cost = service(saved.database).cost
    with saved.database.begin() as connection:
        connection.execute(cost.update().values(payload="{}"))
    calls = len(saved.statements)
    assert export_snapshot(saved.reader, handle).data == before
    assert len(saved.statements) == calls
    with pytest.raises(ValueError):
        saved.reader.read(saved.identity)


def test_child_selection_only_in_verified_batch_not_path_or_ref(saved):
    handle = saved.reader.read(saved.identity)
    count = len(saved.statements)
    for candidate in (ident(999), "../../secret", saved.document["children"][0]["ref_no"],
                      saved.document["children"][0], "TEST\\PAID"):
        with pytest.raises(ValueError):
            child_view(saved.reader, handle, candidate)
    assert len(saved.statements) == count


def test_json_text_outputs_complete_safe_names_and_unchanged_hostile_text(database):
    raw = '</pre><script>alert("secret")</script> ![leak](https://invalid/secret) [x](file:///private) =HYPERLINK("x") ../資料 & <>'
    req = request()
    line = replace(req.source.lines[1], text=raw)
    lines = (req.source.lines[0], line, *req.source.lines[2:])
    req = replace(req, source=replace(req.source, lines=lines, raw_text="\n".join(l.text for l in lines)),
                  children=(replace(req.children[0], description=raw, label=raw,
                                    drawings_json=' { "原文" : "<img src=https://invalid/>" } '), *req.children[1:]))
    reserve(database, req)
    document = service(database).save(req)
    reader = DevelopmentSnapshotReader(database)
    handle = reader.read(identity_of(req))
    for child_id in (None, *(c.child_quote_id for c in req.children)):
        expected = batch_view(reader, handle) if child_id is None else child_view(reader, handle, child_id)
        js = export_snapshot(reader, handle, child_quote_id=child_id)
        txt = export_snapshot(reader, handle, child_quote_id=child_id, format="txt")
        assert json.loads(js.data) == expected
        assert re.fullmatch(r"development-readonly-103-[0-9a-f]{32}(?:-[0-9a-f]{32})?\.json", js.filename)
        assert txt.mime == "text/plain; charset=utf-8" and js.mime == "application/json"
        rendered = txt.data.decode()
        assert raw in rendered and WARNING in rendered and CONFIDENTIAL in rendered
        assert RESERVATION_WARNING in rendered and SHARED in rendered
        assert json.dumps(expected, ensure_ascii=False, indent=2) in rendered
        assert expected["document_version"] == VIEW_VERSION
        assert expected["content_digest"] == digest(canonical({k: v for k, v in expected.items() if k != "content_digest"}))
        assert all(expected[k] is False for k in ("can_quote", "can_confirm", "can_save"))
    assert batch_view(reader, handle)["content"]["saved_snapshot"] == document
    with pytest.raises(ValueError):
        export_snapshot(reader, handle, format="html")


def test_long_source_and_raw_json_whitespace_not_truncated(database):
    req = request()
    long_text = "合成完整來源 不可截斷 & < > \" " * 2000 + "來源尾端"
    lines = (replace(req.source.lines[0], text=long_text), *req.source.lines[1:])
    req = replace(req, source=replace(req.source, lines=lines, raw_text="\n".join(l.text for l in lines),
                                     history_json=' { "完整來源歷史" : "非簽章" } '))
    reserve(database, req)
    service(database).save(req)
    reader = DevelopmentSnapshotReader(database)
    handle = reader.read(identity_of(req))
    for child_id in (None, req.children[0].child_quote_id):
        output = export_snapshot(reader, handle, child_quote_id=child_id, format="txt").data.decode()
        assert long_text in output and "來源尾端" in output
        view = json.loads(export_snapshot(reader, handle, child_quote_id=child_id).data)
        fixed = view["content"]["saved_snapshot"]["fixed_request"] if child_id is None else view["content"]["fixed_batch"]
        assert fixed["source"]["raw_text"] == req.source.raw_text
        assert fixed["source"]["history_json"] == req.source.history_json


def test_new_documents_rejected_formal_trial_review_reserve_and_atomic(saved, monkeypatch):
    from database import repository
    from engine.preview import freeze_preview, checked_preview
    from engine.snapshot import build_snapshot_items, create_quote_snapshot
    from agent.tools import create_quote
    from agent.multi_quote import from_single_quote
    from agent.quote_batch import checked_quote_batch
    from engine.multi_trial import checked_internal_trial
    from engine.quote_batch_trial import checked_batch_trial
    from engine.quote_batch_review import BatchReviewSession
    from engine.multi_snapshot import build_snapshot_layout
    from engine.quote_number_reservation import QuoteNumberReservationService
    from quote_batch_fixtures import four_items
    batch, draft = four_items(reviewed=False)
    deny = Mock(side_effect=AssertionError("不得連正式資料庫"))
    monkeypatch.setattr(repository, "_session", deny)
    handle = saved.reader.read(saved.identity)
    for original in (batch_view(saved.reader, handle), child_view(saved.reader, handle, saved.req.children[0].child_quote_id)):
        for alteration in ("original", "flags", "strip_type", "strip_type_and_flags"):
            doc = deepcopy(original)
            if alteration in ("flags", "strip_type_and_flags"):
                doc.update(can_quote=True, can_save=True, can_confirm=True)
            if alteration in ("strip_type", "strip_type_and_flags"):
                doc.pop("document_type")
            for call in (
                lambda: freeze_preview(doc), lambda: checked_preview({"preview": doc}, "fake"),
                lambda: create_quote(doc, preview_id="fake"), lambda: build_snapshot_items(doc, {}, "fake"),
                lambda: create_quote_snapshot(None, {}, preview=doc), lambda: repository.save_quote_snapshot(doc),
                lambda: repository.save_quote_snapshot([doc]),
                lambda: repository.save_quote_snapshot([{"ref_no": "fake"}], preview=doc), lambda: from_single_quote(doc),
                lambda: checked_quote_batch(draft, doc, actor="測試", source_batch=batch),
                lambda: checked_internal_trial(draft, doc, actor="測試", source_batch=batch),
                lambda: checked_batch_trial(draft, doc, actor="測試", source_batch=batch),
                lambda: build_snapshot_layout(draft, doc, actor="測試", source_batch=batch),
                lambda: BatchReviewSession().freeze(draft, doc, actor="測試", source_batch=batch),
                lambda: QuoteNumberReservationService(saved.database).reserve(doc),
                lambda: service(saved.database).save(doc),
            ):
                with pytest.raises((ValueError, TypeError)):
                    call()
    deny.assert_not_called()


def test_independent_entry_imports_no_production_dependencies_or_sql():
    code = '''
import sys
import development_snapshot_viewer
from engine.development_snapshot_view import DevelopmentSnapshotReader
assert not {'config', 'database.connection', 'database.repository', 'database.models',
            'engine.pricing', 'engine.preview', 'engine.snapshot', 'agent.tools'} & sys.modules.keys()
'''
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, timeout=20)
    root = Path(__file__).resolve().parents[1]
    for path in ("engine/development_snapshot_view.py", "utils/development_snapshot_ui.py", "development_snapshot_viewer.py"):
        text = (root / path).read_text()
        assert "unsafe_allow_html" not in text and "st.markdown" not in text and "cache_data" not in text
