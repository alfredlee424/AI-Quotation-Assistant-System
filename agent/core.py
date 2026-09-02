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
from typing import Optional

from config import USE_LLM, OPENAI_API_KEY, OPENAI_MODEL, PRODUCT_PREFIX
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
    is_confirm,
    is_cancel,
)
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

        # ── 確認建立報價 ───────────────────────────────────
        if status == QuoteStatus.PREVIEW and is_confirm(user_input):
            return self._do_create_quote(quote_draft)

        # ── 標準規格快速填入 ───────────────────────────────
        if "標準規格" in user_input or "預設" in user_input:
            quote_draft = self._apply_defaults(quote_draft)

        # ── 解析輸入，更新草稿 ────────────────────────────
        quote_draft["status"] = QuoteStatus.ANALYZING
        quote_draft, found_fields = parse_and_update(user_input, quote_draft)

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
        from openai import OpenAI
        self.client = OpenAI(api_key=OPENAI_API_KEY)
        self.system_prompt = (
            "你是一個專業的辦公家具報價助理。\n"
            "你的工作是協助業務人員從自然語言需求產生正式報價單。\n\n"
            "重要規則：\n"
            "1. 你不得自行猜測或計算任何金額，所有價格必須透過 calculate_quote 工具計算\n"
            "2. 你不得自行猜測產品代碼，必須透過 search_option 工具查詢正式代碼\n"
            "3. 資料不完整時，必須主動詢問使用者補充（數量、桌面尺寸等必填項）\n"
            "4. 所有回覆使用繁體中文\n"
            "5. 建立正式報價前，必須明確取得使用者確認\n\n"
            f"產品路徑格式：{{PRODUCT_PREFIX}}\\{{optno}}，例如 {PRODUCT_PREFIX}\\A001\n"
            "你可以使用以下工具：search_option、get_options_by_path、"
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

        # 加入使用者訊息
        chat_messages = [{"role": "system", "content": self.system_prompt}]
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
                # 嘗試從對話中更新草稿（規則式輔助）
                quote_draft, _ = parse_and_update(user_input, quote_draft)
                missing = check_missing_fields(quote_draft)
                if not missing and quote_draft["status"] not in (
                    QuoteStatus.PREVIEW, QuoteStatus.SNAPSHOT_CREATED
                ):
                    quote_draft["status"] = QuoteStatus.PREVIEW
                elif missing:
                    quote_draft["status"] = QuoteStatus.WAITING_FOR_INPUT
                quote_draft["missing_fields"] = missing
                return reply, quote_draft

            # 處理 Tool Calls
            chat_messages.append(msg)
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments)

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
                    r = tool_result["results"][0]
                    self._update_draft_from_option(r, quote_draft)

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

        # 取得 optdesc
        try:
            cats = repo.get_all_option_categories()
            cat_map = {c["optno"]: c["optdesc"] for c in cats}
            optdesc = cat_map.get(optno, optno)
        except Exception:
            optdesc = optno

        quote_draft.setdefault("selections", {})[optno] = {
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
