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


def test_product_context_does_not_silently_choose_separately_mentioned_categories(monkeypatch):
    monkeypatch.setattr(core.repo, "get_product_categories", lambda **kw: [
        {"prodkind": "CMT1", "codsc": "環式會議桌", "quo_rate": 1.3},
        {"prodkind": "MT", "codsc": "會議桌", "quo_rate": 1.2},
    ])
    draft = new_quote_draft()
    assert core.LLMAgent._prepare_product_context("環式會議桌和會議桌", draft)
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


@pytest.mark.parametrize("text", ["平直", "1. 平直", "1 平直", "１．平直",
                                 r"平直（代碼：C001，路徑：CMT1\A001\S010）"])
def test_named_choice_updates_draft_without_model(sdk, current_draft, caplog, text):
    """回歸：模型即使只會追問，明確的候選回答也必須直接增列。"""
    path = r"CMT1\A001\S010"
    current_draft["selections"].pop(path)
    check_missing_fields(current_draft)
    assert next(iter(current_draft["allowed_options"])) == path
    before = deepcopy(current_draft["selections"])
    create = sdk({"changes": [], "questions": ["請指定部件"]})
    message, draft = core.run_quote_agent(text, current_draft)
    create.assert_not_called()
    assert draft["selections"][path]["code"] == "C001"
    assert path not in draft["allowed_options"]
    assert "已選：" in message
    assert "需求仍有歧義" not in message
    assert "代碼：" not in message
    assert "CMT1\\" not in message
    assert all(draft["selections"][p]["code"] == item["code"] for p, item in before.items())
    events = [json.loads(record.message) for record in caplog.records if record.name == "ai_quote"]
    routes = [e["result"] for e in events if e["action"] == "quote_selection_route"]
    assert all(route["shortcut_eligible"] for route in routes)
    applied = [e["result"] for e in events if e["action"] == "quote_proposal_applied"]
    assert all(not e["changed_paths"] and not e["removed_paths"] for e in applied)
    assert all(path in e["added_paths"] for e in applied)
    assert all(e["question_count"] == 0 for e in applied)


def test_diagnose_number_choice_is_applied_without_model(sdk, current_draft, caplog):
    path = r"CMT1\A001\S010"
    current_draft["selections"].pop(path)
    check_missing_fields(current_draft)
    create = sdk({"changes": [], "questions": ["請指定部件"]})
    _, draft = core.run_quote_agent("1", current_draft)
    create.assert_not_called()
    assert draft["selections"][path]["code"] == "C001"
    assert path not in draft["allowed_options"]
    events = [json.loads(record.message) for record in caplog.records if record.name == "ai_quote"]
    applied = next(e["result"] for e in events if e["action"] == "quote_proposal_applied")
    assert path in applied["added_paths"]
    assert applied["question_count"] == 0


@pytest.mark.parametrize("text", ["817胡桃木", "2. 817胡桃木", "2 817胡桃木",
    r"817胡桃木（代碼：C001，路徑：CMT1\B001\S005\S001）"])
@pytest.mark.parametrize("llm", [True, False])
def test_material_then_shape_advances_and_keeps_both_selections(sdk, current_draft, cmt1_db, text, llm):
    from utils.helpers import quote_selections_list
    current_draft["selections"].pop(r"CMT1\B001\S005\S008")
    shape = r"CMT1\A001\S010"
    current_draft["selections"].pop(shape)
    check_missing_fields(current_draft)
    parent = r"CMT1\B001\S005"
    material = parent + r"\S001"
    assert next(iter(current_draft["allowed_options"])) == parent
    create = sdk({"changes": [], "questions": ["請指定部件"]})
    agent = core.LLMAgent() if llm else core.RuleBasedAgent()
    message, draft = agent.run(text, current_draft, [])
    assert draft["selections"][material]["code"] == "C001"
    assert parent not in draft["allowed_options"]
    assert next(iter(draft["allowed_options"])) == shape
    assert "CMT1\\" not in message
    assert "817胡桃木" in message
    rows = quote_selections_list(draft)
    assert any(r["codsc"] == "817胡桃木" and "木腳" in r["optdesc"] for r in rows)
    message, draft = agent.run("平直", draft, [])
    create.assert_not_called()
    assert draft["selections"][shape]["code"] == "C001"
    assert draft["selections"][material]["code"] == "C001"
    assert shape not in draft["allowed_options"]
    assert "CMT1\\" not in message
    assert_no_writes(cmt1_db)


@pytest.mark.parametrize("text", ["0", "999", "2. 平直", "1. 平彎",
    r"平直（代碼：C999，路徑：CMT1\A001\S010）"])
def test_inconsistent_choice_cannot_be_guessed_by_model(sdk, current_draft, text):
    current_draft["selections"].pop(r"CMT1\A001\S010")
    check_missing_fields(current_draft)
    before = deepcopy(current_draft["selections"])
    create = sdk({"changes": [], "questions": []})
    message, draft = core.run_quote_agent(text, current_draft)
    create.assert_not_called()
    assert draft["selections"] == before
    assert "未能唯一對應" in message
    assert draft["status"] == QuoteStatus.WAITING_FOR_INPUT


def test_shortcut_keeps_unrelated_questions_and_blocks_preview(sdk, current_draft):
    current_draft["selections"].pop(r"CMT1\A001\S010")
    current_draft["questions"] = ["請確認圓孔總數還是每張用量"]
    check_missing_fields(current_draft)
    create = sdk({"changes": [], "questions": []})
    _, draft = core.run_quote_agent("平直", current_draft)
    create.assert_not_called()
    assert draft["questions"] == ["請確認圓孔總數還是每張用量"]
    assert not draft.get("preview")


def test_model_context_contains_current_question_without_prices(sdk, current_draft):
    current_draft["selections"].pop(r"CMT1\A001\S010")
    check_missing_fields(current_draft)
    create = sdk({"changes": [], "questions": ["請確認外型"]})
    message, draft = core.run_quote_agent("外型還有哪些選擇", current_draft)
    context = json.loads(create.call_args.kwargs["messages"][1]["content"])["current_draft"]
    assert context["focused_question"]["path"] == r"CMT1\A001\S010"
    assert context["focused_question"]["options"][0]["codsc"] == "平直"
    assert context["allowed_options"]
    assert "compri" not in json.dumps(context)
    assert "需求仍有歧義" not in message
    assert "CMT1\\" not in message


def test_duplicate_candidate_names_require_number_and_display_round_trips(sdk, current_draft):
    from agent.rule_parser import select_candidate
    from utils.option_labels import candidate_label
    candidates = [
        {"path": r"CMT1\A001\S005\S001", "code": "C001", "codsc": "817胡桃木", "display_path": "桌面／美耐皿"},
        {"path": r"CMT1\B001\S005\S001", "code": "C001", "codsc": "817胡桃木", "display_path": "木腳／美耐皿"},
    ]
    assert select_candidate("817胡桃木", candidates) is None
    assert select_candidate("C001", candidates) is None
    assert select_candidate("2. 817胡桃木", candidates) == candidates[1]
    assert select_candidate(candidate_label(candidates[1]), candidates) == candidates[1]
    current_draft["pending_options"] = candidates
    create = sdk({"changes": [], "questions": []})
    before = deepcopy(current_draft["selections"])
    message, draft = core.run_quote_agent("817胡桃木", current_draft)
    create.assert_not_called()
    assert draft["selections"] == before
    assert len(draft["pending_options"]) == 2
    assert "未能唯一對應" in message


def test_displayed_material_option_can_be_copied_and_structural_branch_is_valid(sdk, current_draft):
    from utils.option_labels import candidate_label
    current_draft["selections"].pop(r"CMT1\B001\S005\S008")
    check_missing_fields(current_draft)
    parent, options = next(iter(current_draft["allowed_options"].items()))
    assert options[0]["code"] == ""  # 無規格代碼的中間材料方案亦可選擇。
    create = sdk({"changes": [], "questions": []})
    _, draft = core.run_quote_agent("1. " + candidate_label(options[0]), current_draft)
    create.assert_not_called()
    assert options[0]["path"] in draft["selections"]
    assert parent not in draft["allowed_options"]


@pytest.mark.parametrize("llm", [True, False])
def test_pending_candidate_keeps_previously_specified_per_product_quantity(sdk, current_draft, llm):
    path = r"CMT1\A001\S019\S020"
    current_draft["qty"] = 3
    current_draft["pending_options"] = [
        dict(o, path=path, line_qty=2) for o in core.repo.get_options_by_path(path)[:2]
    ]
    current_draft["questions"] = ["請選擇明確的規格與路徑。"]
    create = sdk({"changes": [], "questions": []})
    agent = core.LLMAgent() if llm else core.RuleBasedAgent()
    _, draft = agent.run("1", current_draft, [])
    create.assert_not_called()
    assert draft["selections"][path]["line_qty"] == 2
    assert draft["qty"] == 3
    assert not draft.get("pending_options")
    assert not draft["questions"]


def test_complete_choices_review_old_questions_once_then_preview(sdk, cmt1_db, caplog):
    """重現 3 個舊問題一路帶到缺項為零的案例；最後複核後進入預覽。"""
    questions = ["桌面材料要哪一種？", "木腳顏色要哪一種？", "桌面外型要哪一種？"]
    proposal = {"prodkind": "CMT1", "qty": 1, "changes": [
        {"op": "set", "path": r"CMT1\A001", "code": "C005"},
        {"op": "set", "path": r"CMT1\B001", "code": "C001"},
        {"op": "set", "path": r"CMT1\A001\S019\S020", "code": "C001"},
    ], "questions": questions}
    create = sdk(proposal)
    message, draft = core.run_quote_agent("我要一張60*180環式會議桌", new_quote_draft())
    assert create.call_count == 1
    assert "60×180" in message
    sdk({"changes": [], "questions": []})
    for answer in ["817胡桃木", "817胡桃木", "2", "1", "1", "1"]:
        message, draft = core.run_quote_agent(answer, draft)
        assert create.call_count == 1  # 逐項選擇不重新詢問 LLM。
        assert draft["questions"] == questions
        assert "還缺少以下資訊" not in message
        assert "／" not in message
    message, draft = core.run_quote_agent("1", draft)
    assert create.call_count == 2
    assert not draft["questions"]
    assert not draft["missing_fields"]
    assert len(draft["selections"]) == 25
    assert draft["status"] == QuoteStatus.PREVIEW
    assert "試算完成" in message
    assert "確定建立" in message
    assert len(message) < 180
    events = [json.loads(r.message) for r in caplog.records if r.name == "ai_quote"]
    review = next(e for e in events if e["action"] == "quote_questions_review_finished")
    assert review["result"]["removed_questions"] == questions
    assert_no_writes(cmt1_db)


def test_review_retains_real_quantity_question_and_displays_it(sdk, current_draft):
    current_draft["selections"].pop(r"CMT1\A001\S010")
    question = "請問兩個圓孔是每張用量，還是整筆總數？"
    current_draft["questions"] = ["桌面外型要哪一種？", question]
    check_missing_fields(current_draft)
    create = sdk({"changes": [], "questions": []})  # 即使模型誤清用量問題也必須保留。
    message, draft = core.run_quote_agent("平直", current_draft)
    create.assert_called_once()
    assert draft["questions"] == [question]
    assert question in message
    assert not draft.get("preview")
    context = json.loads(create.call_args.kwargs["messages"][1]["content"])
    assert "compri" not in json.dumps(context)
    assert context["protected_questions"] == [question]


def test_review_only_keeps_unresolved_original_question(sdk, current_draft):
    current_draft["selections"].pop(r"CMT1\A001\S010")
    question = "請問是否需要額外的走線孔？"
    current_draft["questions"] = ["桌面外型要哪一種？", question]
    check_missing_fields(current_draft)
    create = sdk({"changes": [], "questions": [question]})
    message, draft = core.run_quote_agent("平直", current_draft)
    create.assert_called_once()
    assert draft["questions"] == [question]
    assert question in message
    assert "桌面外型要哪一種？" not in message
    assert not draft.get("preview")


@pytest.mark.parametrize("kind", ["api_error", "malformed", "no_tool", "wrong_tool", "changes", "qty", "new_question"])
def test_review_failure_keeps_selected_spec_and_questions(sdk, current_draft, cmt1_db, kind):
    path = r"CMT1\A001\S010"
    current_draft["selections"].pop(path)
    current_draft["questions"] = ["桌面外型要哪一種？"]
    check_missing_fields(current_draft)
    proposal = {"changes": [], "questions": []}
    if kind == "changes":
        proposal["changes"] = [{"op": "remove", "path": r"CMT1\B001"}]
    if kind == "qty":
        proposal["qty"] = 99
    if kind == "new_question":
        proposal["questions"] = ["新的問題"]
    create = sdk(proposal, calls=kind != "no_tool",
                 raw="{bad" if kind == "malformed" else None,
                 tool_name="create_quote" if kind == "wrong_tool" else "propose_quote_changes")
    if kind == "api_error":
        create.side_effect = RuntimeError("離線")
    message, draft = core.run_quote_agent("平直", current_draft)
    assert draft["selections"][path]["code"] == "C001"
    assert r"CMT1\B001" in draft["selections"]
    assert draft["qty"] == 1
    assert draft["questions"] == ["桌面外型要哪一種？"]
    assert "重新檢查" in message
    assert not draft.get("preview")
    assert_no_writes(cmt1_db)


def test_existing_stuck_draft_can_retry_review_without_new_selection(sdk, current_draft):
    current_draft["questions"] = ["桌面外型要哪一種？"]
    create = sdk({"changes": [], "questions": []})
    message, draft = core.run_quote_agent("重新檢查", current_draft)
    create.assert_called_once()
    assert draft["status"] == QuoteStatus.PREVIEW
    assert not draft["questions"]
    assert "試算完成" in message


def test_user_answer_can_clear_last_question_without_selection_changes(sdk, current_draft):
    current_draft["questions"] = ["請問一個圓孔是每張用量嗎？"]
    create = sdk({"changes": [], "questions": []})
    _, draft = core.run_quote_agent("每張一個，不是整筆總數", current_draft)
    create.assert_called_once()
    assert not draft["questions"]
    assert draft["status"] == QuoteStatus.PREVIEW


def test_import_warning_is_not_removed_by_selection_review(sdk, current_draft):
    current_draft["selections"].pop(r"CMT1\A001\S010")
    current_draft["imported_quote"] = {"warnings": ["來源名稱不同"]}
    current_draft["questions"] = ["請確認來源規格差異後再重報"]
    check_missing_fields(current_draft)
    create = sdk({"changes": [], "questions": []})
    message, draft = core.run_quote_agent("平直", current_draft)
    create.assert_not_called()
    assert draft["questions"]
    assert not draft.get("preview")
