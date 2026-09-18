"""Mock SDK、真實配置／計價／SQLite 的 LLM 端到端契約。"""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import core
from agent.state import QuoteStatus, check_missing_fields, new_quote_draft
from agent.tools import search_product
from agent.rule_parser import _match_options_in_text
from cmt1_fixture import current_selections
from database.models import QuoteSnapshotDocument, ordqdt_ai
from engine.preview import freeze_preview


@pytest.fixture
def sdk(monkeypatch, cmt1_db):
    import openai
    create = Mock(name="chat.completions.create")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    constructor = Mock(return_value=client)
    monkeypatch.setattr(openai, "OpenAI", constructor)
    monkeypatch.setattr(openai, "AzureOpenAI", constructor)
    monkeypatch.setattr(core, "USE_LLM", True)

    def response(proposal=None, *, calls=True, tool_name="propose_quote_changes", raw=None):
        tool_calls = [SimpleNamespace(function=SimpleNamespace(
            name=tool_name, arguments=raw if raw is not None else json.dumps(proposal, ensure_ascii=False)
        ))] if calls else []
        create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            tool_calls=tool_calls, content="模型自由文字：價格999999（不得呈現）"))])
        return create

    return response


def assert_no_writes(sessions):
    with sessions() as db:
        assert db.query(QuoteSnapshotDocument).count() == 0
        assert db.query(ordqdt_ai).count() == 0


def test_llm_tools_expose_only_validated_proposal():
    assert [tool["function"]["name"] for tool in core.TOOLS_SCHEMA] == ["propose_quote_changes"]
    from agent.tools import TOOLS_SCHEMA, TOOL_REGISTRY
    assert "create_quote" not in TOOL_REGISTRY
    assert "create_quote" not in {tool["function"]["name"] for tool in TOOLS_SCHEMA}


def test_search_product_prefers_invdoc(cmt1_db):
    result = search_product("辦公桌")
    assert result["source"] == "invdoc"
    assert result["results"][0]["prodkind"] == "CMT1"
    assert result["results"][0]["quo_rate"] == 1


def test_missing_fields_uses_real_ordstr_before_config(cmt1_db):
    draft = new_quote_draft()
    draft.update(prodkind="CMT1", qty=1)
    missing = check_missing_fields(draft)
    assert missing
    assert r"CMT1\A001" in draft["allowed_options"]
    assert draft["status"] == QuoteStatus.WAITING_FOR_INPUT


def test_product_context_does_not_silently_choose_overlapping_categories(monkeypatch):
    monkeypatch.setattr(core.repo, "get_product_categories", lambda **kw: [
        {"prodkind": "CMT1", "codsc": "環式會議桌", "quo_rate": 1.3},
        {"prodkind": "MT", "codsc": "會議桌", "quo_rate": 1.2},
    ])
    draft = new_quote_draft()
    assert core.LLMAgent._prepare_product_context("環式會議桌", draft)
    assert not draft.get("prodkind")
    assert core.LLMAgent._prepare_product_context("CMT1", draft) is None
    assert draft["prodkind"] == "CMT1"


def test_allowed_option_number_is_saved_to_full_path_selection(cmt1_db):
    draft = new_quote_draft()
    draft.update(prodkind="CMT1", qty=1)
    draft["allowed_options"] = {r"CMT1\A001\S010": [
        {"code": "C001", "codsc": "平直"}, {"code": "C002", "codsc": "平彎"},
    ]}
    assert core._apply_allowed_option("1", draft)
    assert draft["selections"][r"CMT1\A001\S010"]["code"] == "C001"


def test_option_matching_prefers_full_description_over_similar_options():
    options = [{"code": "C001", "codsc": "817胡桃木"}, {"code": "C002", "codsc": "932揚胡桃"}]
    result = _match_options_in_text("顏色：817胡桃木", r"CMT1\A001\S005\S001", options, "美耐皿")
    assert result["code"] == "C001"


def test_option_matching_returns_similar_candidates_when_no_full_description():
    options = [{"code": "C001", "codsc": "40*13.5 銀鋁單掀"}, {"code": "C003", "codsc": "60*13.5 銀鋁單掀"}]
    result = _match_options_in_text("銀鋁單掀", r"CMT1\A001\S019\S020", options, "線盒")
    assert [item["code"] for item in result["candidate_options"]] == ["C001", "C003"]
    assert "code" not in result


def test_empty_draft_calls_model_before_missing_checks_with_full_price_free_catalog(sdk, monkeypatch, cmt1_db):
    create = sdk({"changes": [], "questions": ["請選擇產品"]})

    def forbidden(*args, **kwargs):
        raise AssertionError("LLM 模式不得先以規則 parser 改草稿")

    monkeypatch.setattr(core, "parse_and_update", forbidden)
    draft = new_quote_draft()
    message, updated = core.run_quote_agent("想要一張桌子", draft)
    create.assert_called_once()
    call = create.call_args.kwargs
    assert call["tool_choice"]["function"]["name"] == "propose_quote_changes"
    context = json.loads(call["messages"][1]["content"])
    assert len(context["catalogs"]["CMT1"]["nodes"]) == 55
    assert r"CMT1\B001\S005\S000\S052" in context["catalogs"]["CMT1"]["options"]
    assert context["current_draft"]["selections"] == {}
    def assert_safe(value):
        if isinstance(value, dict):
            assert not set(value) & {"compri", "price", "total_price", "stdqty", "stdpar", "quo_rate"}
            for item in value.values():
                assert_safe(item)
        elif isinstance(value, list):
            for item in value:
                assert_safe(item)
    assert_safe(context)
    assert updated["status"] == QuoteStatus.WAITING_FOR_INPUT
    assert not updated.get("preview")
    assert "999999" not in message
    assert_no_writes(cmt1_db)


def test_valid_changes_from_empty_draft_are_programmatically_previewed(sdk, cmt1_db):
    proposal = {"prodkind": "CMT1", "qty": 1, "changes": [
        {"op": "set", "path": p, "code": item["code"]} for p, item in current_selections().items()
    ], "questions": []}
    create = sdk(proposal)
    message, draft = core.run_quote_agent("請按需求選擇", new_quote_draft())
    create.assert_called_once()
    assert draft["status"] == QuoteStatus.PREVIEW
    assert draft["calc_result"]["total_cost"] == 2833.08
    assert draft["preview"]["calc"] == draft["calc_result"]
    assert "2,833.08" in message
    assert "999999" not in message
    assert_no_writes(cmt1_db)


@pytest.mark.parametrize("invalid", ["price", "path", "code", "qty_nan", "line_qty_zero", "extra_top"])
def test_malicious_or_invalid_proposals_are_atomic_and_invalidate_preview(sdk, current_draft, cmt1_db, invalid):
    freeze_preview(current_draft)
    before = deepcopy(current_draft)
    proposal = {"qty": 3, "changes": [{"op": "set", "path": r"CMT1\A001", "code": "C003"}], "questions": []}
    bad = {"op": "set", "path": r"CMT1\A001\S004", "code": "C001"}
    if invalid == "price":
        bad["compri"] = .01
    elif invalid == "path":
        bad["path"] = r"CMT1\A001\NOT_IN_CATALOG"
    elif invalid == "code":
        bad["code"] = "INVALID"
    elif invalid == "qty_nan":
        proposal["qty"] = float("nan")
    elif invalid == "line_qty_zero":
        bad["line_qty"] = 0
    else:
        proposal["total_price"] = 1
    proposal["changes"].append(bad)
    sdk(proposal)
    message, updated = core.run_quote_agent("更改需求", current_draft)
    assert updated["selections"] == before["selections"]
    assert updated["qty"] == before["qty"]
    assert updated["revision"] == before["revision"]
    assert updated["status"] == QuoteStatus.WAITING_FOR_INPUT
    assert not updated.get("preview")
    assert updated["calc_result"] is None
    assert "999999" not in message
    assert_no_writes(cmt1_db)


@pytest.mark.parametrize("kind", ["questions", "no_tool", "wrong_tool", "malformed", "api_error", "no_changes"])
def test_questions_no_tool_or_api_errors_never_keep_preview(sdk, current_draft, cmt1_db, kind):
    freeze_preview(current_draft)
    proposal = {"changes": [], "questions": ["模型捏造價格999999"] if kind == "questions" else []}
    create = sdk(proposal, calls=kind != "no_tool",
                 tool_name="create_quote" if kind == "wrong_tool" else "propose_quote_changes",
                 raw="{broken" if kind == "malformed" else None)
    if kind == "api_error":
        create.side_effect = RuntimeError("離線 SDK 錯誤")
    message, draft = core.run_quote_agent("重新處理需求", current_draft)
    create.assert_called_once()
    assert draft["status"] == QuoteStatus.WAITING_FOR_INPUT
    assert not draft.get("preview")
    assert draft["calc_result"] is None
    assert "999999" not in message
    assert_no_writes(cmt1_db)


@pytest.mark.parametrize("text", ["確認", "確認建立報價單", "是", "OK"])
def test_exact_confirm_only_instructs_ui_and_never_writes(sdk, current_draft, cmt1_db, text):
    create = sdk({"changes": [], "questions": []})
    freeze_preview(current_draft)
    before = deepcopy(current_draft)
    message, draft = core.run_quote_agent(text, current_draft)
    assert draft == before
    assert "確定建立" in message
    create.assert_not_called()
    assert_no_writes(cmt1_db)
