"""真實 Streamlit 人工條件核對；不觸碰單品或正式保存。"""
from test_conditions import condition_case, record, HOLE
from test_requirement_review_ui import start, current, widget


def open_editor(batch, draft):
    app = start(batch, draft)
    app.session_state.current_quote = {"marker": "existing-single"}
    widget(app, "checkbox", "編輯共用條件與局部例外").check().run()
    return app


def fill(app, draft):
    widget(app, "selectbox", "要求狀態").select("FORBIDDEN")
    widget(app, "multiselect", "條件來源需求").set_value([draft.requirements[-1].requirement_id])
    widget(app, "text_input", "條件依據識別／版本").set_value("核對-v1")
    widget(app, "text_area", "條件核對理由（重新核對須重新填寫）").set_value("人工核對作用範圍")


def test_ui_record_keeps_source_single_and_database_unchanged(condition_case, cmt1_db):
    from database.models import QuoteSnapshotDocument, ordqdt_ai
    batch, draft = condition_case
    app = open_editor(batch, draft)
    fill(app, draft)
    widget(app, "button", "保存條件核對紀錄（非工程核准）").click().run()
    assert not app.exception and not app.error
    updated = current(app)
    assert updated.conditions[0].state == "FORBIDDEN"
    assert updated.revision == draft.revision + 1
    assert updated.source == draft.source and updated.blockers == draft.blockers
    assert updated.lines == draft.lines
    assert app.session_state.current_quote == {"marker": "existing-single"}
    with cmt1_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == db.query(ordqdt_ai).count() == 0


def test_ui_missing_evidence_is_atomic(condition_case):
    batch, draft = condition_case
    app = open_editor(batch, draft)
    widget(app, "button", "保存條件核對紀錄（非工程核准）").click().run()
    assert app.error and not app.exception and current(app) == draft


def test_ui_explicit_exception_only_affects_selected_line(condition_case):
    batch, draft = condition_case
    draft = record(batch, draft)
    app = open_editor(batch, draft)
    widget(app, "selectbox", "條件作用範圍").select("line").run()
    fill(app, draft)
    widget(app, "multiselect", "條件適用明細").set_value([draft.lines[1].line_id])
    widget(app, "multiselect", "明確覆蓋的較廣範圍條件（不自動依先後決定）").set_value([draft.conditions[0].condition_id])
    widget(app, "button", "保存條件核對紀錄（非工程核准）").click().run()
    assert not app.error and not app.exception
    assert current(app).conditions[-1].targets == (draft.lines[1].line_id,)
    assert current(app).conditions[-1].overrides == (draft.conditions[0].condition_id,)


def test_ui_withdraw_requires_reason_and_keeps_record(condition_case):
    batch, draft = condition_case
    draft = record(batch, draft)
    app = open_editor(batch, draft)
    widget(app, "selectbox", "條件紀錄").select(draft.conditions[0].condition_id).run()
    widget(app, "button", "撤回此條件（保留歷史及來源阻擋）").click().run()
    assert app.error and current(app) == draft
    widget(app, "text_area", "條件核對理由（重新核對須重新填寫）").set_value("來源待重新確認")
    widget(app, "button", "撤回此條件（保留歷史及來源阻擋）").click().run()
    assert not app.error and not app.exception
    assert current(app).conditions[0].status == "WITHDRAWN"
    assert current(app).blockers == draft.blockers


def test_ui_explicit_catalog_binding_saved(condition_case):
    batch, draft = condition_case
    app = open_editor(batch, draft)
    widget(app, "checkbox", "查詢並設定條件正式部位（唯讀主檔）").check().run()
    fill(app, draft)
    paths = [element for element in app.selectbox if element.label.startswith("條件正式部位｜")]
    assert len(paths) == 2
    for path in paths:
        path.select(HOLE)
    widget(app, "button", "保存條件核對紀錄（非工程核准）").click().run()
    assert not app.error and not app.exception
    assert len(current(app).conditions[0].bindings) == 2
    assert all(binding.path == HOLE for binding in current(app).conditions[0].bindings)


def test_ui_catalog_failure_does_not_offer_save_or_modify_draft(condition_case, monkeypatch):
    batch, draft = condition_case
    app = open_editor(batch, draft)
    def unavailable(*args, **kwargs):
        raise RuntimeError("不可用")
    monkeypatch.setattr("engine.configuration.load_catalog", unavailable)
    widget(app, "checkbox", "查詢並設定條件正式部位（唯讀主檔）").check().run()
    assert app.error and not app.exception
    assert not any(element.label == "保存條件核對紀錄（非工程核准）" for element in app.button)
    assert current(app) == draft
