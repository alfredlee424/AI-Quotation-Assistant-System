"""跨明細 LLM 提案：替身 SDK、整批原子驗證與人工套用契約。"""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import config
from agent import multi_quote_agent as agent
from agent.multi_quote import apply_multi_command, apply_multi_proposal, export_multi_draft, from_single_quote


@pytest.fixture
def multi(current_draft):
    draft = from_single_quote(current_draft)
    draft = apply_multi_command(draft, {"op": "add", "label": "第二張測試桌"}, draft_id=draft.draft_id,
                                expected_revision=draft.revision, actor="測試", reason="建立第二筆")
    values = current_draft["selections"].values()
    return apply_multi_command(draft, {"op": "configure", "line_id": draft.lines[1].line_id,
                                      "proposal": {"prodkind": "CMT1", "qty": 2, "changes": [
                                          {"op": "set", "path": value["path"], "code": value["code"]} for value in values]}},
                                draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="選配")


@pytest.fixture
def sdk(monkeypatch):
    import openai
    create, close = Mock(), Mock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=close)
    constructor = Mock(return_value=client)
    monkeypatch.setattr(openai, "OpenAI", constructor)
    monkeypatch.setattr(openai, "AzureOpenAI", constructor)
    monkeypatch.setattr(config, "USE_LLM", True)
    monkeypatch.setattr(config, "USE_AZURE", False)

    def respond(proposal=None, *, raw=None, count=1, tool=agent.TOOL_NAME):
        calls = [SimpleNamespace(function=SimpleNamespace(name=tool, arguments=(
            json.dumps(proposal, ensure_ascii=False) if raw is None else raw))) for _ in range(count)]
        create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            tool_calls=calls, content="自由回覆報價 987654321 不應被使用"))])
        return create
    return SimpleNamespace(respond=respond, create=create, close=close, constructor=constructor)


def proposal(draft):
    return {"draft_id": draft.draft_id, "revision": draft.revision,
            "updates": [{"line_id": draft.lines[0].line_id, "proposal": {"qty": 3}},
                        {"line_id": draft.lines[1].line_id, "proposal": {"qty": 4}}], "questions": []}


def apply(draft, content, scope=None):
    return apply_multi_proposal(draft, content, allowed_line_ids=scope or tuple(line.line_id for line in draft.lines),
                                actor="測試", reason="測試整批")


def prepare(draft, sdk, content=None):
    sdk.respond(content or proposal(draft))
    return agent.prepare_multi_proposal(draft, "第一筆3件，第二筆4件", actor="測試",
                                       allowed_line_ids=tuple(line.line_id for line in draft.lines))


def test_entire_batch_has_one_revision_and_does_not_mutate_original(multi):
    before = export_multi_draft(multi)
    updated = apply(multi, proposal(multi))
    assert updated.revision == multi.revision + 1
    assert [line.configuration["qty"] for line in updated.lines] == [3, 4]
    assert [line.revision for line in updated.lines] == [line.revision + 1 for line in multi.lines]
    assert len(updated.changes) == len(multi.changes) + 1
    assert updated.changes[-1].operation == "multi_proposal"
    assert updated.blockers == multi.blockers and updated.source == multi.source
    assert export_multi_draft(multi) == before


def test_second_invalid_line_rolls_back_first_valid_change(multi):
    content = proposal(multi)
    content["updates"][1]["proposal"] = {"changes": [{"op": "set", "path": r"CMT1\INVALID", "code": "C001"}]}
    before = export_multi_draft(multi)
    with pytest.raises(ValueError):
        apply(multi, content)
    assert export_multi_draft(multi) == before


@pytest.mark.parametrize("mutation", ["wrong_draft", "stale", "bool_revision", "extra", "duplicate_line", "outside_scope", "price",
                                     "clear_questions", "duplicate_path", "bad_question_target", "empty_question", "unknown_operation", "empty"])
def test_invalid_envelopes_and_targets_fail_atomically(multi, mutation):
    content = proposal(multi)
    if mutation == "wrong_draft":
        content["draft_id"] = "wrong"
    elif mutation == "stale":
        content["revision"] -= 1
    elif mutation == "bool_revision":
        content["revision"] = False
    elif mutation == "extra":
        content["price"] = 10
    elif mutation == "duplicate_line":
        content["updates"].append(deepcopy(content["updates"][0]))
    elif mutation == "outside_scope":
        content["updates"][1]["line_id"] = "another-line"
    elif mutation == "price":
        content["updates"][0]["proposal"]["compri"] = 1
    elif mutation == "clear_questions":
        content["updates"][0]["proposal"]["questions"] = []
    elif mutation == "duplicate_path":
        content["updates"][0]["proposal"] = {"changes": [
            {"op": "set", "path": r"CMT1\A001", "code": "C003"},
            {"op": "set", "path": r"CMT1\A001", "code": "C005"}]}
    elif mutation == "bad_question_target":
        content["questions"] = [{"target_id": "other", "text": "尺寸？"}]
    elif mutation == "empty_question":
        content["questions"] = [{"target_id": multi.draft_id, "text": " "}]
    elif mutation == "unknown_operation":
        content["updates"][0] = {"line_id": multi.lines[0].line_id, "op": "remove"}
    else:
        content["updates"] = []
    before = export_multi_draft(multi)
    with pytest.raises(ValueError):
        apply(multi, content)
    assert export_multi_draft(multi) == before


def test_explicit_scope_cannot_be_expanded_by_model(multi):
    with pytest.raises(ValueError, match="越界"):
        apply(multi, proposal(multi), scope=(multi.lines[0].line_id,))


def test_scoped_questions_are_additive_stable_and_do_not_clear_gates(multi):
    content = proposal(multi)
    content["questions"] = [{"target_id": multi.lines[0].line_id, "text": "第一筆每件圓孔數量為何？"},
                            {"target_id": multi.draft_id, "text": "共用條件適用哪些明細？"}]
    updated = apply(multi, content)
    assert len(updated.questions) == 2 and all(question.status == "OPEN" for question in updated.questions)
    assert updated.lines[0].configuration["questions"][-1] == "第一筆每件圓孔數量為何？"
    assert updated.lines[1].configuration["questions"] == multi.lines[1].configuration["questions"]
    assert updated.blockers[:len(multi.blockers)] == multi.blockers
    assert updated.blockers[-1] == "共用條件適用哪些明細？"
    assert updated.lines[0].revision == multi.lines[0].revision + 1  # 配置及追問同批不加兩次。
    repeated = {**content, "revision": updated.revision, "updates": []}
    with pytest.raises(ValueError, match="已存在"):
        apply(updated, repeated)


def test_question_only_proposal_works_without_product_master(multi, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("純追問不應驗證產品主檔")
    monkeypatch.setattr("engine.configuration.load_catalog", forbidden)
    content = {"draft_id": multi.draft_id, "revision": multi.revision, "updates": [],
               "questions": [{"target_id": multi.lines[1].line_id, "text": "請指定第二筆尺寸"}]}
    updated = apply(multi, content)
    assert updated.lines[0] == multi.lines[0]
    assert updated.lines[1].revision == multi.lines[1].revision + 1


def test_model_context_excludes_costs_history_archived_and_unselected_lines(multi):
    multi.source["private_marker"] = "PRIVATE-SOURCE-123456789"
    multi.lines[0].configuration["selections"][r"CMT1\A001"]["compri"] = 112233445566
    multi.lines[1].configuration["product_name"] = "UNSELECTED-MARKER"
    context = agent.build_multi_context(multi, (multi.lines[0].line_id,))
    text = json.dumps(context, ensure_ascii=False)
    assert "PRIVATE-SOURCE" not in text and "112233445566" not in text and "UNSELECTED-MARKER" not in text
    def walk(value):
        if isinstance(value, dict):
            assert not set(value) & {"compri", "source_compri", "stdqty", "stdpar", "calc", "source", "changes", "archived_lines"}
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(context)
    assert context["lines"][0]["display_number"] == 1
    assert context["catalogs"]


def test_prepare_then_commit_is_explicit_and_frozen(multi, sdk, cmt1_db):
    from database.models import QuoteSnapshotDocument, ordqdt_ai
    before = export_multi_draft(multi)
    pending = prepare(multi, sdk)
    assert export_multi_draft(multi) == before
    assert [line["qty"] for line in pending.after["lines"]] == [3, 4]
    assert "987654321" not in json.dumps(as_safe_dict(pending), ensure_ascii=False)
    committed = agent.commit_multi_proposal(multi, pending)
    assert committed.revision == multi.revision + 1
    assert committed.changes[-1].details["request"] == pending.request
    assert committed.status == "CONFIGURATION_ONLY"
    sdk.close.assert_called_once()
    with pytest.raises(ValueError, match="已變更"):
        agent.commit_multi_proposal(committed, pending)
    with cmt1_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == db.query(ordqdt_ai).count() == 0


def as_safe_dict(prepared):
    from dataclasses import asdict
    return asdict(prepared)


@pytest.mark.parametrize("mutation", ["proposal", "after", "request", "scope", "draft_qty", "source", "actor"])
def test_mutated_prepared_or_base_content_is_rejected(multi, sdk, mutation):
    pending = prepare(multi, sdk)
    if mutation == "proposal":
        pending.proposal["updates"][0]["proposal"]["qty"] = 88
    elif mutation == "after":
        pending.after["lines"][0]["qty"] = 88
    elif mutation == "request":
        pending = replace(pending, request="another")
    elif mutation == "scope":
        pending = replace(pending, allowed_line_ids=(multi.lines[0].line_id,))
    elif mutation == "actor":
        pending = replace(pending, actor="其他人")
    elif mutation == "draft_qty":
        multi.lines[0].configuration["qty"] = 88
    else:
        multi.source["changed"] = True
    with pytest.raises(ValueError, match="已變更"):
        agent.commit_multi_proposal(multi, pending)


@pytest.mark.parametrize("kind", ["no_tool", "two_tools", "wrong_tool", "malformed", "duplicate_keys", "nan", "oversized", "api_failure"])
def test_sdk_failures_leave_draft_untouched(multi, sdk, kind):
    kwargs = {}
    if kind == "no_tool": kwargs["count"] = 0
    if kind == "two_tools": kwargs["count"] = 2
    if kind == "wrong_tool": kwargs["tool"] = "create_quote"
    if kind == "malformed": kwargs["raw"] = "not-json"
    if kind == "duplicate_keys": kwargs["raw"] = '{"draft_id":"one","draft_id":"two"}'
    if kind == "nan": kwargs["raw"] = '{"qty":NaN}'
    if kind == "oversized": kwargs["raw"] = "x" * (agent.MAX_RESPONSE_CHARS + 1)
    sdk.respond(proposal(multi), **kwargs)
    if kind == "api_failure": sdk.create.side_effect = RuntimeError("offline")
    before = export_multi_draft(multi)
    with pytest.raises((ValueError, RuntimeError)):
        agent.prepare_multi_proposal(multi, "修改數量", allowed_line_ids=tuple(line.line_id for line in multi.lines), actor="測試")
    assert export_multi_draft(multi) == before
    sdk.close.assert_called_once()


@pytest.mark.parametrize("scope", [(), ("other",), ("repeat", "repeat"), (True,), []])
def test_bad_scope_is_rejected_before_model_call(multi, sdk, scope):
    with pytest.raises(ValueError):
        agent.prepare_multi_proposal(multi, "改數量", allowed_line_ids=scope, actor="測試")
    sdk.constructor.assert_not_called()


def test_disabled_model_and_oversized_context_do_not_call_sdk(multi, sdk, monkeypatch):
    monkeypatch.setattr(config, "USE_LLM", False)
    with pytest.raises(ValueError, match="未啟用"):
        prepare(multi, sdk)
    monkeypatch.setattr(config, "USE_LLM", True)
    monkeypatch.setattr(agent, "MAX_CONTEXT_CHARS", 1)
    with pytest.raises(ValueError, match="過大"):
        prepare(multi, sdk)
    sdk.constructor.assert_not_called()


def test_azure_uses_configured_sdk_without_live_network(multi, sdk, monkeypatch):
    monkeypatch.setattr(config, "USE_AZURE", True)
    monkeypatch.setattr(config, "AZURE_OPENAI_ENDPOINT", "https://test.invalid")
    prepare(multi, sdk)
    assert sdk.constructor.call_args.kwargs["azure_endpoint"] == "https://test.invalid"
    assert sdk.constructor.call_args.kwargs["max_retries"] == 0


def test_valid_database_code_not_offered_to_model_is_rejected(multi, sdk, monkeypatch):
    real_context = agent.build_multi_context
    def limited(draft, scope):
        context = real_context(draft, scope)
        path = r"CMT1\A001"
        context["catalogs"]["CMT1"]["options"][path] = [
            option for option in context["catalogs"]["CMT1"]["options"][path] if option["code"] != "C003"]
        return context
    monkeypatch.setattr(agent, "build_multi_context", limited)
    content = proposal(multi)
    content["updates"][0]["proposal"] = {"changes": [{"op": "set", "path": r"CMT1\A001", "code": "C003"}]}
    with pytest.raises(ValueError, match="未提供"):
        prepare(multi, sdk, content)


def test_master_name_change_before_commit_requires_new_review(multi, sdk, cmt1_db):
    from database.models import Ordspe
    pending = prepare(multi, sdk)
    with cmt1_db() as db:
        option = db.query(Ordspe).filter_by(path=r"CMT1\A001", code="C005").one()
        option.codsc = "已改版尺寸"
        db.commit()
    before = export_multi_draft(multi)
    with pytest.raises(ValueError, match="核對內容不同"):
        agent.commit_multi_proposal(multi, pending)
    assert export_multi_draft(multi) == before


def test_source_changed_after_prepare_cannot_commit(sdk):
    from agent.multi_quote import from_work_order
    from agent.work_orders import parse_work_orders, reclassify_line
    batch = parse_work_orders("20990101-1測試\n平直70X70-----1\n位置待確認")
    draft = from_work_order(batch, batch.orders[0].order_id)
    content = {"draft_id": draft.draft_id, "revision": draft.revision, "updates": [],
               "questions": [{"target_id": draft.lines[0].line_id, "text": "請指定正式產品"}]}
    sdk.respond(content)
    pending = agent.prepare_multi_proposal(draft, "請核對規格", allowed_line_ids=(draft.lines[0].line_id,),
                                           actor="測試", source_batch=batch)
    changed = reclassify_line(batch, line_id=batch.lines[-1].line_id, kind="note", expected_revision=0,
                              actor="測試", reason="修正分類")
    with pytest.raises(ValueError, match="來源工單"):
        agent.commit_multi_proposal(draft, pending, source_batch=changed)


def test_context_and_proposal_reject_nan_quantities_before_any_commit(multi, sdk):
    content = proposal(multi)
    content["updates"][1]["proposal"]["qty"] = float("inf")
    with pytest.raises(ValueError, match="非有限"):
        prepare(multi, sdk, content)
