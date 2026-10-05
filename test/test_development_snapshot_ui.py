"""真實 Streamlit AppTest：只讀查閱、身份失效、來源隔離與下載安全。"""
from dataclasses import asdict, replace
import json
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from development_atomic_fixtures import ident, SHARED, request
from development_snapshot_view_fixtures import database, saved, readonly_engine
from test_development_atomic_snapshot import reserve, service, registry_rows
from engine.development_snapshot_view import DevelopmentSnapshotReader, export_snapshot
from utils import development_snapshot_ui as ui


APP = '''
import streamlit as st
from development_snapshot_viewer import main
main(st.session_state.get("injected_reader"))
'''


def start(reader=None):
    app = AppTest.from_string(APP, default_timeout=15)
    app.session_state["injected_reader"] = reader
    return app.run()


def fill(app, identity):
    for name, value in asdict(identity).items():
        app.text_input(key=ui.INPUT_PREFIX + name).set_value(value)
    return app.run()


def load(app):
    return next(b for b in app.button if b.label == ui.LOAD).click().run()


def result(app):
    return app.session_state.filtered_state.get(ui.STATE_KEY)


def downloads(app):
    return app.get("download_button")


def assert_cleared(app):
    assert not app.exception and result(app) is None and not downloads(app)
    assert not any("已核驗整批：" in t.value or "本張固定精確金額" in t.value for t in app.text)


def test_default_entry_disabled_no_engine_or_data_creation(monkeypatch):
    from sqlalchemy.engine import Engine
    deny = Mock(side_effect=AssertionError("不得連線"))
    monkeypatch.setattr(Engine, "connect", deny)
    app = AppTest.from_file("../development_snapshot_viewer.py").run()
    assert not app.exception and any("功能未啟用" in t.value for t in app.info)
    assert not app.text_input and not app.button and not downloads(app)
    deny.assert_not_called()


def test_no_implicit_read_before_click_rerun_select_or_download(saved, monkeypatch):
    import streamlit as st
    original = DevelopmentSnapshotReader.read
    calls = []

    def read(self, identity):
        calls.append(1)
        return original(self, identity)

    monkeypatch.setattr(DevelopmentSnapshotReader, "read", read)
    app = fill(start(saved.reader), saved.identity)
    app.run()
    assert not calls and not saved.statements and not downloads(app)
    captured = {}
    real_download = st.download_button

    def capture(label, data, **kwargs):
        captured[label] = (data, kwargs)
        return real_download(label, data, **kwargs)

    monkeypatch.setattr(st, "download_button", capture)
    load(app)
    assert not app.exception and not app.error and calls == [1]
    assert len(downloads(app)) == 4
    assert any("4 張／11 件" in t.value for t in app.text)
    assert any(SHARED in t.value for t in app.text)
    assert [b.label for b in app.button] == [ui.LOAD]
    statements = len(saved.statements)
    fixed = result(app)["snapshot"]
    for child in saved.req.children:
        app.selectbox(key=ui.SELECT_KEY).set_value(child.child_quote_id).run()
        assert not app.exception and not app.error
        expected = export_snapshot(saved.reader, fixed, child_quote_id=child.child_quote_id).data
        assert captured["下載本張開發 JSON"][0] == expected
        assert json.loads(expected)["content"]["child"]["fixed_child"]["child_quote_id"] == child.child_quote_id
    app.run()
    assert calls == [1] and len(saved.statements) == statements
    assert all(options["on_click"] == "ignore" for data, options in captured.values())
    assert result(app)["snapshot"] == fixed
    load(app)  # 同身份再按才重新完整驗證。
    assert calls == [1, 1] and len(saved.statements) > statements
    assert registry_rows(saved.database) == saved.registry_before


@pytest.mark.parametrize("name", list(ui.LABELS))
def test_each_identity_field_change_clears_download_and_restore_does_not_revive(saved, name):
    app = load(fill(start(saved.reader), saved.identity))
    count = len(saved.statements)
    original = getattr(saved.identity, name)
    app.text_input(key=ui.INPUT_PREFIX + name).set_value("changed").run()
    assert_cleared(app)
    app.text_input(key=ui.INPUT_PREFIX + name).set_value(original).run()
    assert_cleared(app)
    assert len(saved.statements) == count


def test_invalid_schema_or_identity_no_sql_and_no_partial_display(saved):
    app = fill(start(saved.reader), saved.identity)
    for name, value in (("schema_version", "formal-v1"), ("workgroup", "../"), ("save_id", "https://secret")):
        fill(app, saved.identity)
        app.text_input(key=ui.INPUT_PREFIX + name).set_value(value).run()
        load(app)
        assert_cleared(app)
        assert app.error
    assert saved.statements == []


def test_reader_replacement_disable_and_source_context_clear_even_same_identity(saved):
    app = load(fill(start(saved.reader), saved.identity))
    original = saved.reader
    for replacement in (DevelopmentSnapshotReader(saved.engine, source_context="other-scope"), None, original):
        count = len(saved.statements)
        app.session_state["injected_reader"] = replacement
        app.run()
        assert_cleared(app)
        assert len(saved.statements) == count
    # 回到原來源也不復活，必須重新填身份／明確查閱。
    load(fill(app, saved.identity))
    assert not app.exception and result(app)


def test_cross_database_same_identity_never_reuses_session_snapshot(saved, tmp_path):
    from database.development_atomic_schema import development_atomic_schema
    from test_quote_number_reservation import open_engine
    other_db = open_engine(tmp_path / "other.sqlite")
    development_atomic_schema()[0].create_all(other_db)
    req = replace(saved.req, origin="another-isolated-database")
    reserve(other_db, req)
    service(other_db).save(req)
    engine, actions, statements = readonly_engine(other_db)
    try:
        reader = DevelopmentSnapshotReader(engine, source_context=saved.reader.source_context)
        app = load(fill(start(saved.reader), saved.identity))
        app.session_state["injected_reader"] = reader
        app.run()
        assert_cleared(app)
        assert not statements
        load(app)
        assert not app.exception and not app.error
        doc = json.loads(export_snapshot(reader, result(app)["snapshot"]).data)
        assert doc["content"]["saved_snapshot"]["fixed_request"]["origin"] == req.origin
    finally:
        engine.dispose()
        other_db.dispose()


def test_source_context_change_on_reader_clears_without_sql(saved, monkeypatch):
    app = load(fill(start(saved.reader), saved.identity))
    count = len(saved.statements)
    changed = object()
    monkeypatch.setattr(DevelopmentSnapshotReader, "source_context", property(lambda self: changed))
    app.run()
    assert_cleared(app)
    monkeypatch.undo()
    app.run()
    assert_cleared(app)
    assert len(saved.statements) == count


def test_read_failure_clears_previous_numbers_and_error_does_not_expose_payload(saved, monkeypatch):
    app = load(fill(start(saved.reader), saved.identity))
    original = DevelopmentSnapshotReader.read
    monkeypatch.setattr(DevelopmentSnapshotReader, "read", Mock(side_effect=RuntimeError("SECRET-DB-URL-PAYLOAD")))
    load(app)
    assert_cleared(app)
    assert app.error and not any("SECRET" in e.value for e in app.error)
    app.run()
    assert_cleared(app)
    monkeypatch.setattr(DevelopmentSnapshotReader, "read", original)
    load(app)
    assert not app.exception and not app.error and result(app) and len(downloads(app)) == 4


def test_absent_wrong_identity_and_corrupt_sibling_clear_all_data(saved):
    app = load(fill(start(saved.reader), saved.identity))
    identities = [replace(saved.identity, workgroup="104"),
                  replace(saved.identity, batch_id=ident(999)),
                  replace(saved.identity, batch_id=ident(101), request_id=ident(102),
                          preview_identity=ident(103), save_id=ident(104))]
    for identity in identities:
        load(fill(app, identity))
        assert_cleared(app)
        assert app.error
    load(fill(app, saved.identity))
    cost = service(saved.database).cost
    with saved.database.begin() as connection:
        connection.execute(cost.delete().where(cost.c.child_quote_id == saved.req.children[-1].child_quote_id))
    before = len(saved.statements)
    app.run()  # 當次固定 snapshot 仍可看；不能假稱 rerun 已核驗目前 DB。
    assert not app.exception and result(app) and len(saved.statements) == before
    load(app)
    assert_cleared(app)
    assert app.error


def test_tampered_session_handle_and_export_failure_clear_before_any_numbers(saved, monkeypatch):
    app = load(fill(start(saved.reader), saved.identity))
    stored = result(app)
    app.session_state[ui.STATE_KEY] = {**stored, "snapshot": replace(stored["snapshot"], _seal="fake")}
    app.run()
    assert_cleared(app)
    load(app)
    monkeypatch.setattr(ui, "export_snapshot", Mock(side_effect=RuntimeError("SECRET")))
    app.run()
    assert_cleared(app)
    assert not any("SECRET" in e.value for e in app.error)


def test_user_labels_source_and_drawings_only_render_as_safe_text(database, monkeypatch):
    import streamlit as st
    hostile = '![secret](https://invalid/image) <script>secret</script> [link](https://invalid/) =1+1 & <>'
    req = request()
    req = replace(req, children=(replace(req.children[0], label=hostile, drawings_json=json.dumps({"raw": hostile})), *req.children[1:]))
    reserve(database, req)
    service(database).save(req)
    reader = DevelopmentSnapshotReader(database)
    forbidden = Mock(side_effect=AssertionError("原文不可使用 Markdown／HTML"))
    monkeypatch.setattr(st, "markdown", forbidden)
    monkeypatch.setattr(st, "html", forbidden)
    app = load(fill(start(reader), ui.DevelopmentSnapshotIdentity(**{k: getattr(req, k) for k in ui.LABELS})))
    assert not app.exception and not app.error and len(downloads(app)) == 4
    assert any(hostile in t.value for t in app.text)
    forbidden.assert_not_called()
