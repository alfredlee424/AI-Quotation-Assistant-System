"""
agent/core.py - AI Agent 主流程

run_quote_agent() 是對外唯一入口，由 app.py 呼叫。
依 config.USE_LLM 決定使用：
  - 規則式模式（RuleBasedAgent）：無需 API Key，關鍵字比對
  - LLM 模式（LLMAgent）：OpenAI Function Calling，語意理解

兩種模式共用：
  - agent/tools.py   : 相同的 Tool 函式與 schema
  - agent/state.py   : 相同的狀態機
  - agent/rule_parser.py : 缺項提示等輔助功能（LLM 模式可選用）

selections 格式（optno 驅動，對齊真實資料庫）：
    {
        "A001": {"optno": "A001", "optdesc": "桌面尺寸",
                 "path": "CMT1\\A001", "code": "C001", "codsc": "60*120", "compri": 0.0},
        "S002": {...},
        ...
    }

回傳格式（統一）：
    (response_text: str, updated_quote: dict)
"""

from __future__ import annotations

import json
import re
from typing import Optional

from config import USE_LLM, USE_AZURE, OPENAI_API_KEY, OPENAI_MODEL, PRODUCT_PREFIX, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_VERSION
from agent.state import (
    QuoteStatus,
    check_missing_fields,
    new_quote_draft,
    STATUS_LABEL,
    transition,
)
from agent.tools import dispatch_tool, preview_quote, create_quote, TOOLS_SCHEMA
from agent.rule_parser import (
    parse_and_update,
    format_missing_prompt,
    format_candidate_confirmation,
    select_candidate,
    is_confirm,
    is_cancel,
)
from agent.quotation_importer import restore_historical_matches
from utils.logger import log_action


# ============================================================
# 規則式 Agent
# ============================================================

class RuleBasedAgent:
    """
    規則式報價 Agent。
    不需要外部 API，使用關鍵字比對與正規表示式解析使用者輸入。
    """

    def run(
        self,
        user_input: str,
        quote_draft: dict,
        messages: list[dict],
    ) -> tuple[str, dict]:
        """
        處理一次使用者輸入，更新報價草稿並回傳回覆文字。

        Returns:
            (response_text, updated_quote_draft)
        """
        status: QuoteStatus = quote_draft.get("status", QuoteStatus.NEW)

        # ── 取消指令 ───────────────────────────────────────
        if is_cancel(user_input):
            quote_draft = new_quote_draft()
            return "已取消目前報價，請重新輸入需求。", quote_draft

        pending = quote_draft.get("pending_options", [])
        resolved_pending = False
        if pending:
            selected = select_candidate(user_input, pending)
            if selected:
                selection_key = selected["optno"]
                if quote_draft.get("imported_quote"):
                    selection_key = selected.get("path", "").removeprefix(
                        f"{PRODUCT_PREFIX}\\"
                    )
                quote_draft.setdefault("selections", {})[selection_key] = selected
                quote_draft.pop("pending_options", None)
                resolved_pending = True
            else:
                quote_draft.pop("pending_options", None)

        # ── 確認建立報價 ───────────────────────────────────
        if status == QuoteStatus.PREVIEW and is_confirm(user_input):
            return self._do_create_quote(quote_draft)

        # ── 標準規格快速填入 ───────────────────────────────
        if "標準規格" in user_input or "預設" in user_input:
            quote_draft = self._apply_defaults(quote_draft)

        # ── 解析輸入，更新草稿 ────────────────────────────
        quote_draft["status"] = QuoteStatus.ANALYZING
        if resolved_pending:
            found_fields = []
        else:
            quote_draft, found_fields = parse_and_update(user_input, quote_draft)
        restore_historical_matches(user_input, quote_draft)

        if quote_draft.get("pending_options"):
            quote_draft["status"] = QuoteStatus.WAITING_FOR_INPUT
            return format_candidate_confirmation(quote_draft["pending_options"]), quote_draft

        # ── 檢查必要欄位 ───────────────────────────────────
        quote_draft["status"] = QuoteStatus.CHECKING
        missing = check_missing_fields(quote_draft)
        quote_draft["missing_fields"] = missing

        if missing:
            quote_draft["status"] = QuoteStatus.WAITING_FOR_INPUT
            prompt = format_missing_prompt(missing, quote_draft)
            if found_fields:
                header = "✅ 已從您的描述中擷取：" + "、".join(found_fields) + "\n\n"
            else:
                header = "🔍 正在收集報價所需資訊。\n\n"
            return header + prompt, quote_draft

        # ── 資料完整，試算報價 ────────────────────────────
        quote_draft["status"] = QuoteStatus.PREVIEW
        preview = preview_quote(quote_draft)
        quote_draft["calc_result"] = preview["calc"]

        response = (
            preview["summary"]
            + "\n\n請回覆「確認建立」建立正式報價單，或繼續修改規格。"
        )
        return response, quote_draft

    def _do_create_quote(self, quote_draft: dict) -> tuple[str, dict]:
        """建立正式報價快照"""
        try:
            quote_draft["status"] = QuoteStatus.CREATING
            result = create_quote(quote_draft, user="SYS")
            quote_draft["status"] = QuoteStatus.SNAPSHOT_CREATED
            quote_draft["ref_no"] = result["ref_no"]
            log_action("create_quote", quote_draft, result)
            return result["message"] + "\n\n感謝您的訂購！如需修改，請輸入新的需求。", quote_draft
        except Exception as e:
            quote_draft["status"] = QuoteStatus.ERROR
            return f"❌ 建立報價時發生錯誤：{e}", quote_draft

    def _apply_defaults(self, quote_draft: dict) -> dict:
        """
        套用標準規格預設值（從真實資料庫查詢，以 optno 為 key）。

        預設值涵蓋根節點與子部件，確保計算引擎有足夠的有成本項目：
          根節點（compri=0，作為驅動代碼來源）：
            A001/C001 → CMT1\\A001  60*120
            B001/C001 → CMT1\\B001  45寬環式腳
          子部件（compri>0，實際計入成本）：
            CMT1\\A001\\S004    / C001 → 25mm MDF（板材）
            CMT1\\A001\\W001\\W010 / C001 → 裁切費
            CMT1\\A001\\W001\\W030 / C001 → 木工費
            CMT1\\B001\\S004    / C001 → MDF 4*8*18mm（腳板材）

        selections dict 的 key 規則：
          - 根節點：optno（如 "A001"、"B001"）
          - 子部件：path 去掉 PRODUCT_PREFIX 後的段落（如 "A001\\S004"）
            確保不同產品樹下同名子部件（如 A001\\S004 和 B001\\S004）不衝突
        """
        from database import repository as repo
        from config import PRODUCT_PREFIX

        quote_draft.setdefault("selections", {})

        # 取得 optdesc 對映
        try:
            cats = repo.get_all_option_categories()
            cat_map = {c["optno"]: c["optdesc"] for c in cats}
        except Exception:
            cat_map = {}

        # ── 格式：(full_path, code, sel_key, optno, optdesc_fallback) ──────
        # sel_key 為 selections dict 的 key，path 去前綴後作為唯一識別
        prefix = PRODUCT_PREFIX
        defaults = [
            # 根節點（compri=0，作為子部件 stdqty 的驅動代碼來源）
            (f"{prefix}\\A001",             "C001", "A001",          "A001", "桌面尺寸"),
            (f"{prefix}\\B001",             "C001", "B001",          "B001", "木腳"),
            # A001 樹子部件（有成本）
            (f"{prefix}\\A001\\S004",       "C001", "A001\\S004",   "S004", "板材"),
            (f"{prefix}\\A001\\W001\\W010", "C001", "A001\\W001\\W010", "W010", "裁切費"),
            (f"{prefix}\\A001\\W001\\W030", "C001", "A001\\W001\\W030", "W030", "木工費"),
            # B001 樹子部件（有成本）
            (f"{prefix}\\B001\\S004",       "C001", "B001\\S004",   "S004", "腳板材"),
        ]

        for full_path, code, sel_key, optno, optdesc_fb in defaults:
            if sel_key not in quote_draft["selections"]:
                options = repo.get_options_by_path(full_path)
                target = next((o for o in options if o["code"] == code), None)
                if target:
                    quote_draft["selections"][sel_key] = {
                        "optno": optno,
                        "optdesc": cat_map.get(optno, optdesc_fb),
                        "path": full_path,
                        "code": target["code"],
                        "codsc": target["codsc"],
                        "compri": target["compri"],
                    }

        if not quote_draft.get("qty"):
            quote_draft["qty"] = 1
        return quote_draft


# ============================================================
# LLM Agent（OpenAI Function Calling）
# ============================================================

class LLMAgent:
    """
    OpenAI Function Calling 報價 Agent。
    偵測到 OPENAI_API_KEY 時自動啟用。
    """

    def __init__(self) -> None:
        if USE_AZURE:
            from openai import AzureOpenAI
            self.client = AzureOpenAI(
                api_key=OPENAI_API_KEY,
                azure_endpoint=AZURE_OPENAI_ENDPOINT,
                api_version=AZURE_OPENAI_API_VERSION,
            )
        else:
            from openai import OpenAI
            self.client = OpenAI(api_key=OPENAI_API_KEY)

        self.system_prompt = (
            "你是一個專業的辦公家具報價助理。\n"
            "你的工作是協助業務人員從自然語言需求產生正式報價單。\n\n"
            "重要規則：\n"
            "1. 你不得自行猜測或計算任何金額，所有價格必須透過 calculate_quote 工具計算\n"
            "2. 你不得自行猜測產品代碼，必須透過 search_option 工具查詢正式代碼\n"
            "3. 程式提供 allowed_options 時，只能列出其中的選項，不得自行新增或修改\n"
            "4. 必須先從 invdoc 選定 prodkind 與 quo_rate，不能自行猜產品\n"
            "5. 資料不完整時，必須依 ordstr.must_chose 主動詢問使用者補充\n"
            "6. 所有回覆使用繁體中文\n"
            "7. 建立正式報價前，必須明確取得使用者確認\n\n"
            "7. ordspd 的 optno 是獨立選項類別，不是互斥產品型號。A001=桌面尺寸、"
            "B001=木腳、B002=鐵腳、C001=轉盤尺寸可以依資料庫規則同時存在；"
            "不可因為 path 分別是 CMT1\\A001 與 CMT1\\B001 就判定不能混搭。\n"
            "8. 每個選項必須使用其自身資料庫 path 的 code；不要把 B001 的代碼放到 A001，"
            "也不要自行產生如『2317胡桃美耐板』這類資料庫不存在的規格。"
            "注意：ordspd.optno（例如 C001）與 ordspe.code（例如各 path 下的 C001）是不同欄位，"
            "不可混為一談。\n\n"
            "產品路徑格式：{prodkind}\\{optno}，例如 CMT1\\A001\n"
            "你可以使用以下工具：get_product_categories、search_product、search_option、get_options_by_path、"
            "get_part_quantity、calculate_quote、preview_quote"
        )

    def run(
        self,
        user_input: str,
        quote_draft: dict,
        messages: list[dict],
    ) -> tuple[str, dict]:
        """
        呼叫 OpenAI API，處理 Function Calling 並更新報價草稿。
        """
        status: QuoteStatus = quote_draft.get("status", QuoteStatus.NEW)

        # 確認建立（UI 按鈕觸發時直接建立，不需再問 LLM）
        if status == QuoteStatus.PREVIEW and is_confirm(user_input):
            rule_agent = RuleBasedAgent()
            return rule_agent._do_create_quote(quote_draft)

        # 取消
        if is_cancel(user_input):
            return "已取消目前報價，請重新輸入需求。", new_quote_draft()

        product_error = self._prepare_product_context(user_input, quote_draft)
        if product_error:
            quote_draft["status"] = QuoteStatus.WAITING_FOR_INPUT
            return product_error, quote_draft

        # 模糊候選必須先由使用者確認，確認後才允許寫入草稿。
        pending = quote_draft.get("pending_options", [])
        resolved_pending = False
        if pending:
            selected = self._select_pending_option(user_input, pending)
            if selected:
                self._update_draft_from_option(selected, quote_draft)
                quote_draft.pop("pending_options", None)
                resolved_pending = True
            elif user_input.strip():
                # 使用者提供新描述時，放棄上一批候選並重新搜尋。
                quote_draft.pop("pending_options", None)

        # 標準規格由程式直接套用，避免 LLM 模式只把指令當成一般文字，
        # 導致數量與桌面尺寸仍被判定為缺少。沿用規則式 Agent 的資料庫
        # 查詢與預設值邏輯，且只補入尚未存在的選擇，不覆蓋使用者已選內容。
        used_standard_defaults = "標準規格" in user_input or "預設" in user_input
        if used_standard_defaults:
            quote_draft = RuleBasedAgent()._apply_defaults(quote_draft)

        # 先以程式解析並檢查缺漏，避免把資料庫規則交給 LLM 自行判斷。
        if not resolved_pending:
            quote_draft, _ = parse_and_update(user_input, quote_draft)
        restore_historical_matches(user_input, quote_draft)
        missing = check_missing_fields(quote_draft)
        missing_options = self._get_missing_options(quote_draft, missing)
        quote_draft["missing_fields"] = missing
        if quote_draft.get("prodkind"):
            quote_draft["status"] = QuoteStatus.STRUCTURE_EXPANDED

        # 標準規格已由程式完成所有必要條件時，直接使用正式報價引擎試算，
        # 不依賴 LLM 是否正確呼叫 preview_quote，確保按鈕可立即顯示報價。
        if used_standard_defaults and not missing:
            quote_draft["status"] = QuoteStatus.PREVIEW
            preview = preview_quote(quote_draft)
            quote_draft["calc_result"] = preview["calc"]
            return (
                preview["summary"]
                + "\n\n請回覆「確認建立」建立正式報價單，或繼續修改規格。",
                quote_draft,
            )

        # 加入使用者訊息
        chat_messages = [{"role": "system", "content": self.system_prompt}]
        if missing:
            chat_messages.append({
                "role": "system",
                "content": (
                    "程式已完成缺漏檢查。只能從下列 allowed_options 回覆，"
                    "不得自行新增或推測選項：\n"
                    + json.dumps(missing_options, ensure_ascii=False)
                ),
            })
        if quote_draft.get("imported_quote"):
            historical = [
                {
                    "path": row.get("path"),
                    "code": row.get("opt_code"),
                    "spec": row.get("spec_desc"),
                }
                for row in quote_draft["imported_quote"].get("source_rows", [])
                if row.get("opt_code") and row.get("spec_desc")
            ]
            chat_messages.append({
                "role": "system",
                "content": (
                    "這是一張已匯入的歷史報價。若使用者只是重述下列規格，"
                    "必須沿用相同 path/code，不要改用其他路徑的相近候選；"
                    "只有使用者明確說『改成』時才重新搜尋。歷史規格如下：\n"
                    + json.dumps(historical, ensure_ascii=False)
                ),
            })
        chat_messages.extend(messages[-10:])  # 保留最近 10 則對話（控制 Token）
        chat_messages.append({"role": "user", "content": user_input})

        quote_draft["status"] = QuoteStatus.ANALYZING

        # LLM 對話循環（處理 Function Calling）
        MAX_TOOL_CALLS = 8
        for _ in range(MAX_TOOL_CALLS):
            response = self.client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=chat_messages,
                tools=TOOLS_SCHEMA,
                tool_choice="auto",
            )
            msg = response.choices[0].message

            # 無 Tool Call → 直接回覆
            if not msg.tool_calls:
                reply = msg.content or ""
                # 再次檢查，確保 LLM 回覆不會繞過程式規則。
                if not resolved_pending:
                    quote_draft, _ = parse_and_update(user_input, quote_draft)
                restore_historical_matches(user_input, quote_draft)
                missing = check_missing_fields(quote_draft)
                missing_options = self._get_missing_options(quote_draft, missing)
                if not missing and quote_draft["status"] not in (
                    QuoteStatus.PREVIEW, QuoteStatus.SNAPSHOT_CREATED
                ):
                    quote_draft["status"] = QuoteStatus.PREVIEW
                elif missing:
                    quote_draft["status"] = QuoteStatus.WAITING_FOR_INPUT
                quote_draft["missing_fields"] = missing
                if missing:
                    if quote_draft.get("pending_options"):
                        return format_candidate_confirmation(
                            quote_draft["pending_options"]
                        ), quote_draft
                    return format_missing_prompt(missing, quote_draft, missing_options), quote_draft
                return reply, quote_draft

            # 處理 Tool Calls
            chat_messages.append(msg)
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments)

                # LLM 可能先呼叫全域模糊搜尋；匯入歷史報價時，
                # 在每次計價工具前重新套用原始 path/code，避免候選覆蓋歷史組合。
                restore_historical_matches(user_input, quote_draft)

                if fn_name == "create_quote":
                    tool_result = {
                        "blocked": True,
                        "message": "禁止由 LLM 直接建立正式報價；請先顯示 PREVIEW，等待使用者按下確認按鈕。",
                    }
                    chat_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    })
                    continue

                # 若 calculate_quote / preview_quote 傳入 quote_draft，注入目前草稿
                if fn_name in ("calculate_quote", "preview_quote"):
                    fn_args["quote_draft"] = quote_draft

                try:
                    tool_result = dispatch_tool(fn_name, fn_args)
                    log_action(fn_name, fn_args, tool_result)
                except Exception as e:
                    tool_result = {"error": str(e)}

                # 更新草稿（search_option 結果）— 以 optno 為 key
                if fn_name == "search_option" and tool_result.get("results"):
                    results = tool_result["results"]
                    if tool_result.get("requires_confirmation"):
                        quote_draft["pending_options"] = results
                    elif len(results) == 1:
                        self._update_draft_from_option(results[0], quote_draft)
                    else:
                        tool_result = {
                            "ambiguous": True,
                            "count": len(results),
                            "results": results,
                            "message": "找到多筆資料，請使用者確認後才能寫入報價草稿。",
                        }

                # preview 結果更新草稿
                if fn_name == "preview_quote" and tool_result.get("calc"):
                    quote_draft["calc_result"] = tool_result["calc"]
                    quote_draft["status"] = QuoteStatus.PREVIEW

                chat_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_result, ensure_ascii=False),
                })

        # 超過最大 Tool Call 次數
        return "抱歉，處理時發生問題，請重新描述需求。", quote_draft

    @staticmethod
    def _select_pending_option(user_input: str, candidates: list[dict]) -> dict | None:
        """依編號、代碼或明確確認選取候選；回傳值必須是資料庫候選原物件。"""
        text = user_input.strip()
        match = re.search(r"(?:第\s*)?(\d+)\s*(?:個|項|號)?", text)
        if match:
            index = int(match.group(1)) - 1
            if 0 <= index < len(candidates):
                return candidates[index]
        for candidate in candidates:
            if candidate.get("code") and candidate["code"].lower() in text.lower():
                return candidate
        if is_confirm(text) and candidates:
            return candidates[0]
        return None

    def _get_missing_options(self, quote_draft: dict, missing: list[str]) -> dict[str, list[dict]]:
        """依 ordstr 缺漏節點查詢資料庫選項，建立白名單。"""
        from config import REQUIRED_OPTNOS
        from database import repository as repo

        selections = quote_draft.get("selections", {})
        if not missing:
            return {}

        result: dict[str, list[dict]] = {}
        prodkind = str(quote_draft.get("prodkind", PRODUCT_PREFIX)).strip() or PRODUCT_PREFIX
        required_nodes = repo.get_required_nodes(prodkind)
        chosen_paths = {str(v.get("path", "")).strip() for v in selections.values()}
        if required_nodes:
            for node in required_nodes:
                path = str(node.get("pathc", "")).strip()
                if path and path not in chosen_paths:
                    result[path] = repo.get_options_by_path(path)
        else:
            for optno in REQUIRED_OPTNOS:
                if optno not in selections:
                    result[optno] = repo.get_options_by_path(repo.build_option_path(optno))
        quote_draft["allowed_options"] = result
        return result

    @staticmethod
    def _prepare_product_context(user_input: str, quote_draft: dict) -> str | None:
        """從 invdoc 選定產品類別並保存 prodkind/quo_rate。"""
        from database import repository as repo

        if quote_draft.get("prodkind"):
            return None
        try:
            categories = repo.get_product_categories()
        except Exception:
            categories = []
        if not categories:
            quote_draft["prodkind"] = PRODUCT_PREFIX
            return None
        text = user_input.strip().lower()
        matched = [
            c for c in categories
            if str(c.get("prodkind", "")).lower() in text
            or str(c.get("codsc", "")).lower() in text
        ]
        if len(categories) == 1 and not matched:
            matched = categories
        if len(matched) != 1:
            quote_draft["product_options"] = categories if not matched else matched
            names = "、".join(
                f"{c.get('codsc') or c.get('prodkind')}（{c.get('prodkind')}）"
                for c in quote_draft["product_options"]
            )
            return "請先選擇報價產品類別：" + names
        category = matched[0]
        quote_draft["prodkind"] = category["prodkind"]
        quote_draft["product_name"] = category.get("codsc") or category["prodkind"]
        quote_draft["quo_rate"] = category.get("quo_rate")
        return None

    def _update_draft_from_option(self, result: dict, quote_draft: dict) -> None:
        """
        根據搜尋結果更新報價草稿，以 optno 為 selections key。
        """
        from database import repository as repo

        path = result.get("path", "")
        code = result.get("code", "")
        codsc = result.get("codsc", "")
        compri = result.get("compri", 0.0)
        optno = result.get("optno") or repo.optno_from_path(path)

        if not optno:
            return

        # 只接受資料庫中確實存在的 path + code，拒絕 LLM 自行捏造的規格。
        db_option = next(
            (item for item in repo.get_options_by_path(path) if item["code"] == code),
            None,
        )
        if db_option is None:
            return
        codsc = db_option["codsc"]
        compri = db_option["compri"]

        # 取得 optdesc
        try:
            cats = repo.get_all_option_categories()
            cat_map = {c["optno"]: c["optdesc"] for c in cats}
            optdesc = cat_map.get(optno, optno)
        except Exception:
            optdesc = optno

        selection_key = path.removeprefix(f"{PRODUCT_PREFIX}\\")
        if not quote_draft.get("imported_quote"):
            selection_key = optno
        quote_draft.setdefault("selections", {})[selection_key] = {
            "optno": optno,
            "optdesc": optdesc,
            "path": path,
            "code": code,
            "codsc": codsc,
            "compri": compri,
        }


# ============================================================
# 公開入口
# ============================================================

def run_quote_agent(
    user_input: str,
    quote_draft: Optional[dict],
    messages: Optional[list[dict]] = None,
) -> tuple[str, dict]:
    """
    報價 Agent 主入口。由 app.py 呼叫。

    Args:
        user_input  : 使用者輸入的文字
        quote_draft : 目前報價草稿（None 時建立新草稿）
        messages    : 對話歷史（LLM 模式使用）

    Returns:
        (response_text, updated_quote_draft)
    """
    if quote_draft is None:
        quote_draft = new_quote_draft()
    if messages is None:
        messages = []

    try:
        if USE_LLM:
            agent = LLMAgent()
        else:
            agent = RuleBasedAgent()

        response, updated_draft = agent.run(
            user_input=user_input,
            quote_draft=quote_draft,
            messages=messages,
        )
        return response, updated_draft

    except Exception as exc:
        import traceback
        traceback.print_exc()
        quote_draft["status"] = QuoteStatus.ERROR
        return f"❌ 系統發生錯誤：{exc}\n\n請重新輸入需求。", quote_draft
