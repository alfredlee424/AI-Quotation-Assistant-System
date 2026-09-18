"""報價需求入口：理解／提案 → 程式驗證 → 固定預覽 → UI 確認。

LLM 只有一個提案工具；不執行 SQL、不決定價格，也不能建立正式報價。
所有選項均以完整 path 為鍵，與共用配置解析器一致。
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Optional

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
    select_candidate, is_confirm, is_cancel,
)


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

SYSTEM_PROMPT = r"""你是繁體中文家具需求理解助理，只能呼叫 propose_quote_changes 一次。
本回合 current_draft 是目前狀態；歷史訊息僅供語意參考，不可還原舊選擇。
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


def _allowed_proposal(text: str, draft: dict) -> Optional[dict]:
    """編號只對第一組提示生效，分支候選使用自身 path 而非容器 path。"""
    match = re.fullmatch(r"(?:第\s*)?(\d+)(?:\s*[個項號])?", text.strip())
    allowed = draft.get("allowed_options") or {}
    if not match or not allowed or draft.get("pending_options"):
        return None
    parent, options = next(iter(allowed.items()))
    index = int(match.group(1)) - 1
    if not 0 <= index < len(options):
        return None
    option = options[index]
    return {"changes": [{"op": "set", "path": option.get("path") or parent,
                         "code": option.get("code", "")}], "questions": []}


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


def _finish_turn(draft: dict) -> tuple[str, dict]:
    """成功提案後一律重新解析，只有程式試算成功才會有 PREVIEW。"""
    missing = check_missing_fields(draft)
    if draft.get("pending_options"):
        return _waiting(draft, format_candidate_confirmation(draft["pending_options"]))
    if draft.get("questions"):
        # 不把模型自由文字（可能含捏造價格）直接呈現給使用者。
        prompt = "需求仍有歧義，請補充欲修改的部件、完整規格及每件產品的配件數量。"
        if missing:
            prompt += "\n\n" + format_missing_prompt(missing, draft, draft.get("allowed_options"))
        return _waiting(draft, prompt)
    if missing:
        return _waiting(draft, format_missing_prompt(missing, draft, draft.get("allowed_options")))
    try:
        preview = preview_quote(draft)
    except ValueError as exc:
        return _waiting(draft, f"目前無法產生報價預覽：{exc}")
    # 固定預覽與狀態由 preview_quote 建立，不能自行偽造 PREVIEW。
    return preview["summary"] + "\n\n請確認右側預覽後按下「確定建立」，或繼續修改規格。", draft


def _submit_proposal(draft: dict, proposal: dict) -> tuple[str, dict]:
    _validate_proposal(proposal)
    if not proposal.get("changes") and not any(k in proposal for k in ("prodkind", "qty", "discount_rate")):
        if not proposal.get("questions"):
            return _waiting(draft, "尚未取得明確的需求變更，請指定欲修改的規格或數量。")
    if not (proposal.get("prodkind") or draft.get("prodkind")):
        return _waiting(draft, _product_prompt(draft.get("product_options", [])))
    apply_proposal(draft, proposal)
    return _finish_turn(draft)


def _product_prompt(categories: list[dict]) -> str:
    names = "、".join(f"{c.get('codsc') or c.get('prodkind')}（{c.get('prodkind')}）" for c in categories)
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
        return {
            "current_draft": {"prodkind": draft.get("prodkind"), "qty": draft.get("qty"),
                              "discount_rate": draft.get("discount_rate", 0),
                              "revision": draft.get("revision", 0),
                              "selections": {path: {k: item.get(k) for k in ("path", "code", "codsc", "line_qty")}
                                             for path, item in selections.items()}},
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
            if shortcut is not None:
                return _submit_proposal(quote_draft, shortcut)
            context = self._turn_context(quote_draft)
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
            if len(calls) != 1 or calls[0].function.name != "propose_quote_changes":
                return _waiting(quote_draft, "尚未取得唯一且合法的需求提案，請補充欲修改的規格或數量。")
            proposal = json.loads(calls[0].function.arguments)
            return _submit_proposal(quote_draft, proposal)
        except Exception as exc:
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
