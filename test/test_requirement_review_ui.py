"""真實需求帳本介面操作，仍不保存正式報價。"""
from streamlit.testing.v1 import AppTest

from test_requirement_review import review_case, add_question, record_command, review


def workspace():
    import streamlit as st
    from utils.multi_quote_ui import render_multi_quote_workspace
    render_multi_quote_workspace(st)


def widget(app, kind, label):
    return next(element for element in getattr(app, kind) if element.label == label)


def start(batch, draft):
    app = AppTest.from_function(workspace)
    app.session_state.work_order_batch = batch
    app.session_state.multi_quote_drafts = {draft.draft_id: draft}
    app.session_state.multi_selected = draft.draft_id
    app.run()
    widget(app, "text_input", "操作人（自填紀錄，非核准簽章）").set_value("核對人員")
    return app


def current(app):
    return app.session_state.multi_quote_drafts[app.session_state.multi_selected]


def test_record_metadata_and_split_source_in_ui(review_case):
    batch, draft = review_case
    app = start(batch, draft)
    widget(app, "selectbox", "需求處置").select("metadata")
    widget(app, "text_input", "依據識別與版本（不會自動讀取文件）").set_value("原單-v1")
    widget(app, "text_area", "處置理由／核對說明").set_value("此行只為產品分類標題")
    widget(app, "button", "保存人工處置紀錄（非正式核准）").click().run()
    assert not app.exception and not app.error
    assert current(app).requirements[0].status == "RECORDED"
    target = current(app).requirements[-1]
    widget(app, "selectbox", "待處置需求片段").select(target.requirement_id).run()
    widget(app, "number_input", "從第幾個字元後拆成兩段（含空白及標點）").set_value(4)
    widget(app, "button", "拆分需求片段").click().run()
    assert not app.exception and not app.error
    assert len(current(app).requirements) == len(draft.requirements) + 1
    assert current(app).archived_requirements[-1].requirement_id == target.requirement_id


def test_ui_requires_reference_and_does_not_change_draft_on_error(review_case):
    batch, draft = review_case
    app = start(batch, draft)
    widget(app, "selectbox", "需求處置").select("metadata")
    widget(app, "button", "保存人工處置紀錄（非正式核准）").click().run()
    assert not app.exception and app.error
    assert current(app) == draft


def test_ui_question_resolution_records_evidence_and_keeps_quote_blockers(review_case, cmt1_db):
    from database.models import QuoteSnapshotDocument, ordqdt_ai
    batch, draft = review_case
    draft = add_question(batch, review(batch, draft, record_command(draft)))
    app = start(batch, draft)
    target = next(record for record in draft.requirements if record.status == "RECORDED")
    widget(app, "multiselect", "引用已處置需求").set_value([target.requirement_id])
    widget(app, "text_input", "結案依據識別與版本").set_value("尺寸核對-v2")
    widget(app, "text_area", "具體回答與核對理由").set_value("已比對尺寸與需求；未宣告工程核准")
    widget(app, "button", "記錄問題結案（非工程核准）").click().run()
    assert not app.exception and not app.error
    assert current(app).questions[0].status == "RESOLVED"
    assert current(app).blockers == draft.blockers
    assert current(app).status == "CONFIGURATION_ONLY"
    with cmt1_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == db.query(ordqdt_ai).count() == 0
