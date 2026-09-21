"""報價需求入口：理解／提案 → 程式驗證 → 固定預覽 → UI 確認。

LLM 只有一個提案工具；不執行 SQL、不決定價格，也不能建立正式報價。
所有選項均以完整 path 為鍵，與共用配置解析器一致。
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Callable, Optional

from config import (
    USE_LLM, USE_AZURE, OPENAI_API_KEY, OPENAI_MODEL, WORKGROUP,
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_VERSION,
)
from database import repository as repo
from engine.configuration import (
    apply_proposal, invalidate_preview, load_catalog, normalize_selections,
    resolve_configuration,
)
from agent.state import QuoteStatus, check_missing_fields, new_quote_draft
from agent.tools import preview_quote
from agent.rule_parser import (
    parse_and_update, format_missing_prompt, format_candidate_confirmation,
    select_candidate, is_candidate_reply, is_confirm, is_cancel,
)
from utils.logger import log_action
from utils.option_labels import candidate_label, open_question


PROPOSAL_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_quote_changes",
        "description": "僅提出需求變更；所有路徑及代碼須取自本回合資料庫選單。歧義放入 questions。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "prodkind": {"type": "string", "description": "資料庫產品類別代碼"},
                "qty": {"type": "number", "exclusiveMinimum": 0, "description": "產品總數量，不是配件數量"},
                "discount_rate": {"type": "number", "minimum": 0, "maximum": 1},
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "op": {"type": "string", "enum": ["set", "remove"]},
                            "path": {"type": "string", "description": "包含產品前綴的完整路徑"},
                            "code": {"type": "string", "description": "該路徑的正式規格代碼"},
                            "line_qty": {"type": "number", "exclusiveMinimum": 0,
                                         "description": "每件產品所需的此項目數量"},
                        },
                        "required": ["op", "path"],
                    },
                },
                "questions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["changes", "questions"],
        },
    },
}
# 僅保留提案 schema；不得使用 agent.tools 的執行型工具清單。
TOOLS_SCHEMA = [PROPOSAL_TOOL]

# 問題複核仍用同一提案介面，但不准改規格、數量或價格。
QUESTION_REVIEW_TOOL = deepcopy(PROPOSAL_TOOL)
QUESTION_REVIEW_TOOL["function"]["description"] = "只複核舊問題；changes 必須為空，questions 原文保留尚未解決的問題。"
QUESTION_REVIEW_TOOL["function"]["parameters"]["properties"] = {
    "changes": {"type": "array", "items": {"type": "object"}, "maxItems": 0},
    "questions": {"type": "array", "items": {"type": "string"}},
}

QUESTION_REVIEW_PROMPT = """你只負責複核家具需求的舊問題，不是重新選配。
根據已驗證的目前選擇與使用者回答，逐一判斷原問題是否已解決。
changes 必須為空；questions 只保留尚未解決的原問題（逐字保留），不能新增或改寫。
只有具體規格已回答原問題才可移除。必選齊全本身不代表所有需求都已釐清。
protected_questions 必須保留：規格選擇不代表確認配件用量或匯入警告。
不可用預設 line_qty=1 推論使用者已確認用量；不明確就保留原問題。
不得修改產品、規格、數量、價格或建立報價。只呼叫 propose_quote_changes 一次。
"""

SYSTEM_PROMPT = r"""你是繁體中文家具需求理解助理，只能呼叫 propose_quote_changes 一次。
本回合 current_draft 是目前狀態；歷史訊息僅供語意參考，不可還原舊選擇。
focused_question 是目前使用者正在回答的候選群組，不要把短回答套到其他部件。
allowed_options / pending_options 是尚待選擇的規格；已由使用者回答的問題不可重複提出。
questions 只保留尚未釐清的需求；程式會詢問結構缺項，不必用通用歧義問題取代具體提案。
從 categories 選產品；set 只可使用 catalogs 的完整 path 及該 path 的 code。
remove 可使用 current_draft 已存在的完整 path（包含已退休的歷史路徑）。
不得猜測產品、規格、數量、預設值，不得產生價格、成本、SQL 或呼叫建立報價。
只提本回合明確要求的變更，沒有要求的欄位請省略。無法唯一對應時填 questions，
不要代選第一筆，不要把同名規格套到整棵樹；請用桌面、腳架、下板等路徑上下文。
qty 是產品總量；2個圓孔不是2張桌子。line_qty 是每件產品的配件數量，
若配件總量與每件用量無法區分，請追問，不要改產品數量。
60*180 原樣以公分匹配，不可把180除10；1200x600或明確mm才考慮毫米換算。
同一句可能同時指定桌面尺寸與線盒尺寸，應分別對應合法路徑。
CMT1 每個 S005 下 S000/S001/S008 是互斥材料方案，S007 下板獨立。
更換方案必須先明確 remove 舊方案的完整子樹路徑，再 set 新方案；
移除上板不要順便移除下板或其他未要求的部件。結構與唯一必選值由程式補齊。
確認或建立報價只由 UI 按鈕處理。你不輸出報價敘述，價格由程式產生。
"""


def _waiting(draft: dict, message: str) -> tuple[str, dict]:
    invalidate_preview(draft)
    draft["status"] = QuoteStatus.WAITING_FOR_INPUT
    return message, draft


def _control_input(text: str, draft: dict) -> Optional[tuple[str, dict]]:
    """只有整句控制指令可以略過理解；確認不能改動固定預覽。"""
    if draft.get("ref_no") or draft.get("status") in (
        QuoteStatus.SNAPSHOT_CREATED, QuoteStatus.COMPLETED, QuoteStatus.CREATING,
    ):
        return "此報價已建立或正在建立，不能修改；請使用介面建立新草稿。", draft
    if is_cancel(text):
        fresh = new_quote_draft()
        fresh["workgroup"] = draft.get("workgroup", WORKGROUP)
        return "已取消目前報價，請重新輸入需求。", fresh
    if is_confirm(text):
        if draft.get("status") == QuoteStatus.PREVIEW and draft.get("preview"):
            return "目前仍是報價預覽；請確認右側內容後，按下「確定建立」才會寫入資料庫。", draft
        return _waiting(draft, "目前沒有可確認的固定預覽，請先補齊或釐清報價需求。")
    return None


def _current_candidates(draft: dict) -> tuple[Optional[str], list[dict]]:
    """與畫面相同：歧義候選優先，否則只取第一組缺項。"""
    allowed = draft.get("allowed_options") or {}
    parent, options = next(iter(allowed.items()), (None, []))
    if draft.get("pending_options"):
        parent, options = None, draft["pending_options"]
    labels = draft.get("option_labels", {})
    return parent, [dict(o, path=o.get("path") or parent,
                         display_path=labels.get(o.get("path") or parent) or o.get("display_path", ""))
                    for o in options]


def _allowed_proposal(text: str, draft: dict) -> Optional[dict]:
    """目前候選唯一命中後直接提出已驗證路徑，不依賴模型重判。"""
    parent, options = _current_candidates(draft)
    option = select_candidate(text, options)
    log_action("quote_selection_route", params={
        "revision": draft.get("revision", 0), "input_length": len(text),
        "first_group": parent, "candidate_count": len(options),
        "pending_count": len(draft.get("pending_options") or []),
    }, result={
        "shortcut_eligible": option is not None,
        "selected_path": option.get("path") if option else None,
        "selected_code": option.get("code") if option else None,
    })
    if option is None:
        return None
    # 選擇規格不代表確認其他配件數量或匯入警告；不得清空無關問題。
    questions = [q for q in draft.get("questions", [])
                 if not (draft.get("pending_options") and q == "請選擇明確的規格與路徑。")]
    change = {"op": "set", "path": option.get("path") or parent, "code": option.get("code", "")}
    if "line_qty" in option:
        change["line_qty"] = option["line_qty"]
    return {"changes": [change], "questions": questions}


def _unmatched_choice(text: str, draft: dict) -> Optional[tuple[str, dict]]:
    _, options = _current_candidates(draft)
    if not is_candidate_reply(text, options):
        return None
    prompt = format_candidate_confirmation(options) if draft.get("pending_options") else format_missing_prompt(
        draft.get("missing_fields", []), draft, draft.get("allowed_options"))
    return _waiting(draft, "未能唯一對應目前選項；請確認編號與名稱一致，同名規格請使用編號。\n\n" + prompt)


def _apply_allowed_option(user_input: str, quote_draft: dict) -> bool:
    proposal = _allowed_proposal(user_input, quote_draft)
    if proposal is None:
        return False
    invalidate_preview(quote_draft)
    apply_proposal(quote_draft, proposal)
    return True


def _auto_select_single_options(quote_draft: dict, missing_options: dict) -> bool:
    """相容入口；只讓共用解析器決定哪些必選／結構節點可以自動補齊。"""
    before = normalize_selections(quote_draft.get("selections", {}))
    resolved = resolve_configuration(quote_draft)
    if resolved["errors"]:
        raise ValueError("；".join(resolved["errors"]))
    if before == resolved["selections"]:
        return False
    apply_proposal(quote_draft, {"changes": [], "questions": quote_draft.get("questions", [])})
    return True


def _resolve_single_option_chain(quote_draft: dict, missing_options: dict) -> tuple[list[str], dict]:
    _auto_select_single_options(quote_draft, missing_options)
    missing = check_missing_fields(quote_draft)
    return missing, quote_draft.get("allowed_options", {})


def _validate_proposal(proposal: dict) -> None:
    """工具 schema 不是安全邊界；再次拒絕額外欄位、錯誤型別及非有限數值。"""
    if not isinstance(proposal, dict) or set(proposal) - {
        "prodkind", "qty", "discount_rate", "changes", "questions",
    }:
        raise ValueError("需求提案包含不允許的欄位")
    if "prodkind" in proposal and (not isinstance(proposal["prodkind"], str) or not proposal["prodkind"].strip()):
        raise ValueError("產品類別必須是正式代碼")
    for field in ("qty", "discount_rate"):
        if field in proposal and (isinstance(proposal[field], bool) or not isinstance(proposal[field], (int, float))):
            raise ValueError("數量與折扣必須為數值")
    questions = proposal.get("questions", [])
    if not isinstance(questions, list) or any(not isinstance(q, str) or not q.strip() for q in questions):
        raise ValueError("待釐清問題必須是文字清單")
    changes = proposal.get("changes", [])
    if not isinstance(changes, list):
        raise ValueError("需求變更必須是清單")
    for change in changes:
        if not isinstance(change, dict) or set(change) - {"op", "path", "code", "line_qty"}:
            raise ValueError("選項變更包含不允許的欄位")
        if change.get("op") not in ("set", "remove") or not isinstance(change.get("path"), str):
            raise ValueError("請提供合法操作與完整路徑")
        if change["op"] == "set" and not isinstance(change.get("code"), str):
            raise ValueError("設定規格必須提供正式代碼")
        if "line_qty" in change and (isinstance(change["line_qty"], bool) or not isinstance(change["line_qty"], (int, float))):
            raise ValueError("每件產品的項目數量必須為數值")


def _finish_turn(draft: dict, review_questions: Optional[Callable[[dict], bool]] = None) -> tuple[str, dict]:
    """成功提案後一律重新解析，只有程式試算成功才會有 PREVIEW。"""
    missing = check_missing_fields(draft)
    log_action("quote_turn_resolution", result={
        "revision": draft.get("revision", 0),
        "selection_count": len(draft.get("selections", {})),
        "question_count": len(draft.get("questions") or []),
        "questions": draft.get("questions", []),
        "pending_count": len(draft.get("pending_options") or []),
        "missing_fields": missing,
        "allowed_groups": list(draft.get("allowed_options") or {}),
    })
    if draft.get("pending_options"):
        return _waiting(draft, format_candidate_confirmation(_current_candidates(draft)[1]))
    if missing:
        # 已知結構缺項優先具體追問，不再每輪重複通用的歧義警告。
        return _waiting(draft, format_missing_prompt(missing, draft, draft.get("allowed_options")))
    if draft.get("questions"):
        if review_questions is not None and not review_questions(draft):
            return _waiting(draft, "規格已保存，問題檢查暫時失敗；請稍後輸入「重新檢查」。")
        if draft.get("questions"):
            return _waiting(draft, open_question(draft["questions"][0], draft.get("option_labels", {})))
    try:
        preview = preview_quote(draft)
    except ValueError as exc:
        return _waiting(draft, f"目前無法產生報價預覽：{exc}")
    # 固定預覽與狀態由 preview_quote 建立，不能自行偽造 PREVIEW。
    calc = preview["calc"]
    return (f"試算完成。成本：{calc['total_cost']:,.2f}；含稅報價：{calc['total_price']:,.2f}。\n\n"
            "請查看右側明細，確認後按「確定建立」。"), draft


def _submit_proposal(draft: dict, proposal: dict,
                     review_questions: Optional[Callable[[dict], bool]] = None) -> tuple[str, dict]:
    _validate_proposal(proposal)
    log_action("quote_proposal_validated", params={
        "revision": draft.get("revision", 0),
        "changes": proposal.get("changes", []),
        "question_count": len(proposal.get("questions") or []),
        "questions": proposal.get("questions", []),
    })
    if not proposal.get("changes") and not any(k in proposal for k in ("prodkind", "qty", "discount_rate")):
        if not proposal.get("questions") and not draft.get("questions"):
            return _waiting(draft, "尚未取得明確的需求變更，請指定欲修改的規格或數量。")
    if not (proposal.get("prodkind") or draft.get("prodkind")):
        return _waiting(draft, _product_prompt(draft.get("product_options", [])))
    before = normalize_selections(draft.get("selections", {}))
    apply_proposal(draft, proposal)
    after = normalize_selections(draft.get("selections", {}))
    log_action("quote_proposal_applied", result={
        "revision": draft.get("revision", 0),
        "added_paths": [p for p in after if p not in before],
        "removed_paths": [p for p in before if p not in after],
        "changed_paths": [p for p in after if p in before and
                          (after[p]["code"], after[p]["line_qty"]) !=
                          (before[p]["code"], before[p]["line_qty"])],
        "selection_count": len(after),
        "question_count": len(draft.get("questions") or []),
    })
    message, draft = _finish_turn(draft, review_questions)
    confirmed = [candidate_label(dict(after[c["path"]],
                                     display_path=draft.get("option_labels", {}).get(c["path"], "")), compact=True)
                 for c in proposal.get("changes", []) if c["op"] == "set" and c["path"] in after]
    if confirmed:
        message = "已選：" + "、".join(confirmed[:3]) + (f"等 {len(confirmed)} 項" if len(confirmed) > 3 else "") + "。\n\n" + message
    return message, draft


def _product_prompt(categories: list[dict]) -> str:
    names = "、".join(c.get("codsc") or "未命名產品" for c in categories)
    return "請先選擇報價產品類別：" + (names or "目前沒有可報價的產品類別，請確認產品主檔。")


class RuleBasedAgent:
    """安全規則解析只作用於副本，再以差異提案經共用驗證套用。"""

    def run(self, user_input: str, quote_draft: dict, messages: list[dict]) -> tuple[str, dict]:
        control = _control_input(user_input, quote_draft)
        if control is not None:
            return control
        shortcut = _allowed_proposal(user_input, quote_draft)
        invalidate_preview(quote_draft)
        try:
            if shortcut is not None:
                return _submit_proposal(quote_draft, shortcut)
            unmatched = _unmatched_choice(user_input, quote_draft)
            if unmatched is not None:
                return unmatched
            working = deepcopy(quote_draft)
            product_error = LLMAgent._prepare_product_context(user_input, working)
            if product_error:
                quote_draft["product_options"] = working.get("product_options", [])
                return _waiting(quote_draft, product_error)
            before = (normalize_selections(quote_draft.get("selections", {}))
                      if working.get("prodkind") == quote_draft.get("prodkind") else {})
            pending = working.pop("pending_options", [])
            selected = select_candidate(user_input, pending) if pending else None
            working["questions"] = [q for q in working.get("questions", [])
                                    if q != "請選擇明確的規格與路徑。"] if selected else []
            if selected is not None:
                working.setdefault("selections", {})[selected["path"]] = selected
            else:
                working, _ = parse_and_update(user_input, working)
            after = normalize_selections(working.get("selections", {}))
            changes = [{"op": "remove", "path": p} for p in before if p not in after]
            for path, item in after.items():
                old = before.get(path, {})
                if (old.get("code"), old.get("line_qty", 1)) != (item.get("code"), item.get("line_qty", 1)):
                    changes.append({"op": "set", "path": path, "code": item.get("code", ""),
                                    "line_qty": item.get("line_qty", 1)})
            proposal = {"changes": changes, "questions": working.get("questions", [])}
            for key in ("prodkind", "qty", "discount_rate"):
                if working.get(key) != quote_draft.get(key) and working.get(key) is not None:
                    proposal[key] = working[key]
            candidates = working.get("pending_options", [])
            if candidates:
                proposal["questions"] = proposal["questions"] or ["請選擇明確的規格與路徑。"]
            _validate_proposal(proposal)
            if not changes and not any(k in proposal for k in ("prodkind", "qty", "discount_rate")) and not proposal["questions"]:
                return _waiting(quote_draft, "未能唯一辨識需求，請提供完整規格、路徑或產品數量。")
            apply_proposal(quote_draft, proposal)
            if candidates:
                quote_draft["pending_options"] = candidates
            return _finish_turn(quote_draft)
        except Exception as exc:
            return _waiting(quote_draft, f"無法套用需求，請修正或補充：{exc}")


class LLMAgent:
    def __init__(self) -> None:
        if USE_AZURE:
            from openai import AzureOpenAI
            self.client = AzureOpenAI(api_key=OPENAI_API_KEY, azure_endpoint=AZURE_OPENAI_ENDPOINT,
                                      api_version=AZURE_OPENAI_API_VERSION)
        else:
            from openai import OpenAI
            self.client = OpenAI(api_key=OPENAI_API_KEY)
        self.system_prompt = SYSTEM_PROMPT

    def _review_questions(self, draft: dict, messages: list[dict], user_input: str) -> bool:
        """只複核舊問題，不呼叫 apply_proposal；失敗保留已選規格及全部問題。"""
        questions = list(draft.get("questions") or [])
        # 用量與匯入疑義必須由使用者另行回答，不能只因結構齊全而刪除。
        protected = [q for q in questions if re.search(
            r"數量|用量|個數|總數|總量|每件|每張|幾個|幾件|幾張|匯入|來源|警告", q)]
        if draft.get("imported_quote", {}).get("warnings"):
            protected = questions[:]
        log_action("quote_questions_review_started", params={
            "revision": draft.get("revision", 0), "questions": questions,
            "protected_questions": protected,
        })
        if len(protected) == len(questions):
            log_action("quote_questions_review_finished", result={
                "removed_questions": [], "remaining_questions": questions, "source": "protected",
            })
            return True
        selections = normalize_selections(draft.get("selections", {}))
        context = {
            "questions": questions, "protected_questions": protected,
            "current_draft": {"prodkind": draft.get("prodkind"), "qty": draft.get("qty"),
                              "selections": {p: {k: s.get(k) for k in
                                             ("code", "codsc", "line_qty", "automatic")}
                                             for p, s in selections.items()}},
            "user_answers": [m["content"] for m in (messages or [])
                             if m.get("role") == "user" and isinstance(m.get("content"), str)] + [user_input],
        }
        try:
            response = self.client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "system", "content": QUESTION_REVIEW_PROMPT},
                          {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                tools=[QUESTION_REVIEW_TOOL],
                tool_choice={"type": "function", "function": {"name": "propose_quote_changes"}},
            )
            calls = response.choices[0].message.tool_calls or []
            if len(calls) != 1 or calls[0].function.name != "propose_quote_changes":
                raise ValueError("問題複核未回傳唯一合法提案")
            proposal = json.loads(calls[0].function.arguments)
            _validate_proposal(proposal)
            if set(proposal) != {"changes", "questions"} or proposal["changes"]:
                raise ValueError("問題複核不得修改規格或數量")
            if any(q not in questions for q in proposal["questions"]):
                raise ValueError("問題複核只能保留原問題")
            remaining = [q for q in questions if q in proposal["questions"] or q in protected]
            draft["questions"] = remaining
            log_action("quote_questions_review_finished", result={
                "revision": draft.get("revision", 0),
                "removed_questions": [q for q in questions if q not in remaining],
                "remaining_questions": remaining, "source": "llm",
            })
            return True
        except Exception as exc:
            log_action("quote_questions_review_failed", result={
                "error_type": type(exc).__name__, "error": str(exc), "questions": questions,
            })
            return False

    @staticmethod
    def _turn_context(draft: dict) -> dict:
        workgroup = draft.get("workgroup", WORKGROUP)
        categories = repo.get_product_categories(workgroup=workgroup)
        safe_categories = [{"prodkind": c["prodkind"], "codsc": c.get("codsc", "")} for c in categories]
        draft["product_options"] = safe_categories
        catalogs = {}
        # 包括其他產品的合法選單，才能在同一回合提出產品更換，而非先用規則選產品。
        for category in safe_categories:
            product = category["prodkind"]
            try:
                catalog = load_catalog(product, workgroup)
            except ValueError:
                catalogs[product] = {"unavailable": True}
                continue
            catalogs[product] = {
                "nodes": [{"path": path, "parent": node.get("pathf"),
                           "label": catalog["labels"].get(path.rsplit("\\", 1)[-1], ""),
                           "required": node.get("must_chose")}
                          for path, node in catalog["nodes"].items()],
                "options": {path: [{"code": o.get("code", ""), "codsc": o.get("codsc", "")}
                                    for o in options] for path, options in catalog["options"].items()},
            }
        selections = normalize_selections(draft.get("selections", {}))
        def safe_options(options):
            return [{k: o[k] for k in ("path", "code", "codsc", "display_path", "line_qty") if k in o}
                    for o in options]
        focused_path, focused_options = _current_candidates(draft)
        return {
            "current_draft": {"prodkind": draft.get("prodkind"), "qty": draft.get("qty"),
                              "discount_rate": draft.get("discount_rate", 0),
                              "revision": draft.get("revision", 0),
                              "selections": {path: {k: item.get(k) for k in ("path", "code", "codsc", "line_qty")}
                                              for path, item in selections.items()},
                              "questions": draft.get("questions", []),
                              "missing_fields": draft.get("missing_fields", []),
                              "allowed_options": {p: safe_options(options) for p, options in
                                                  (draft.get("allowed_options") or {}).items()},
                              "pending_options": safe_options(draft.get("pending_options") or []),
                              "focused_question": {"path": focused_path,
                                                   "options": safe_options(focused_options)}},
            "categories": safe_categories, "catalogs": catalogs,
        }

    def run(self, user_input: str, quote_draft: dict, messages: list[dict]) -> tuple[str, dict]:
        control = _control_input(user_input, quote_draft)
        if control is not None:
            return control
        shortcut = _allowed_proposal(user_input, quote_draft)
        # 不論工具格式錯誤、無工具、API 錯誤或變更被拒，舊預覽都不能再確認。
        invalidate_preview(quote_draft)
        try:
            reviewer = lambda draft: self._review_questions(draft, messages, user_input)
            if shortcut is not None:
                return _submit_proposal(quote_draft, shortcut, review_questions=reviewer)
            if user_input.strip() == "重新檢查" and quote_draft.get("questions"):
                return _finish_turn(quote_draft, review_questions=reviewer)
            unmatched = _unmatched_choice(user_input, quote_draft)
            if unmatched is not None:
                return unmatched
            context = self._turn_context(quote_draft)
            log_action("quote_llm_context", params={
                "revision": quote_draft.get("revision", 0),
                "current_draft_fields": list(context["current_draft"]),
                "allowed_group_count": len(quote_draft.get("allowed_options") or {}),
                "selection_count": len(context["current_draft"]["selections"]),
            })
            history = [{"role": m["role"], "content": m["content"]} for m in (messages or [])[-10:]
                       if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
            # UI 可能已加入本回合輸入，不重複傳送。
            if history and history[-1] == {"role": "user", "content": user_input}:
                history.pop()
            chat = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "system", "content": json.dumps(context, ensure_ascii=False)}]
            chat.extend(history)
            chat.append({"role": "user", "content": user_input})
            quote_draft["status"] = QuoteStatus.ANALYZING
            response = self.client.chat.completions.create(
                model=OPENAI_MODEL, messages=chat, tools=TOOLS_SCHEMA,
                tool_choice={"type": "function", "function": {"name": "propose_quote_changes"}},
            )
            calls = response.choices[0].message.tool_calls or []
            log_action("quote_llm_response", result={
                "tool_call_count": len(calls),
                "tool_names": [call.function.name for call in calls],
            })
            if len(calls) != 1 or calls[0].function.name != "propose_quote_changes":
                return _waiting(quote_draft, "尚未取得唯一且合法的需求提案，請補充欲修改的規格或數量。")
            proposal = json.loads(calls[0].function.arguments)
            return _submit_proposal(quote_draft, proposal)
        except Exception as exc:
            log_action("quote_llm_turn_failed", result={
                "revision": quote_draft.get("revision", 0),
                "error_type": type(exc).__name__, "error": str(exc),
            })
            return _waiting(quote_draft, f"無法套用需求，請修正或補充：{exc}")

    @staticmethod
    def _prepare_product_context(user_input: str, quote_draft: dict) -> Optional[str]:
        """保留規則模式 API；絕不以固定產品或第一筆作為預設。"""
        categories = repo.get_product_categories(workgroup=quote_draft.get("workgroup", WORKGROUP))
        text = user_input.strip().casefold()
        matched = [c for c in categories if (
            str(c.get("prodkind", "")).strip() and
            re.search(r"(?<![a-z0-9])" + re.escape(str(c["prodkind"]).casefold()) + r"(?![a-z0-9])", text)
        ) or (str(c.get("codsc") or "").strip() and str(c["codsc"]).strip().casefold() in text)]
        if not matched and quote_draft.get("prodkind"):
            return None
        if len(matched) != 1:
            quote_draft["product_options"] = matched or categories
            return _product_prompt(quote_draft["product_options"])
        if matched[0]["prodkind"] != quote_draft.get("prodkind"):
            quote_draft["selections"] = {}
            quote_draft.pop("pending_options", None)
        quote_draft["prodkind"] = matched[0]["prodkind"]
        return None

    def _get_missing_options(self, quote_draft: dict, missing: list[str]) -> dict:
        check_missing_fields(quote_draft)
        return quote_draft.get("allowed_options", {})

    _select_pending_option = staticmethod(select_candidate)

    def _update_draft_from_option(self, result: dict, quote_draft: dict) -> None:
        invalidate_preview(quote_draft)
        apply_proposal(quote_draft, {"changes": [{"op": "set", "path": result.get("path", ""),
                                                  "code": result.get("code", "")}], "questions": []})


def run_quote_agent(user_input: str, quote_draft: Optional[dict],
                    messages: Optional[list[dict]] = None) -> tuple[str, dict]:
    if quote_draft is None:
        quote_draft = new_quote_draft()
    control = _control_input(user_input, quote_draft)
    if control is not None:
        return control
    try:
        agent = LLMAgent() if USE_LLM else RuleBasedAgent()
        return agent.run(user_input, quote_draft, messages or [])
    except Exception as exc:
        return _waiting(quote_draft, f"系統無法處理需求：{exc}\n\n請重新輸入需求。")
