"""
app.py - AI 報價助理系統主程式入口

Streamlit 雙區介面：
  左側：對話互動區（Chat UI）
  右側：報價試算卡片（Preview + 確認建立）

啟動：
  streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

from agent.core import run_quote_agent
from agent.state import QuoteStatus, new_quote_draft, STATUS_LABEL
from agent.tools import create_quote, preview_quote
from agent.quotation_importer import import_pasted_quote, quotation_to_natural_language
from database.seed_data import seed
from utils.helpers import (
    calc_items_to_df,
    quote_draft_summary,
    quote_selections_list,
    get_status_badge,
    fmt_money,
)
from utils.logger import log_user_input, log_quote_created
from config import USE_LLM, OPENAI_MODEL, IS_SQLITE, print_config


# ============================================================
# 頁面設定（必須是第一個 Streamlit 指令）
# ============================================================

st.set_page_config(
    page_title="AI 報價助理",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ============================================================
# 應用啟動初始化（只執行一次）
# ============================================================

@st.cache_resource
def _init_app():
    """初始化資料庫（建立資料表 + 灌入範例資料）"""
    seed()
    return True


_init_app()


# ============================================================
# Session State 初始化
# ============================================================

if "messages" not in st.session_state:
    st.session_state.messages: list[dict] = []

if "current_quote" not in st.session_state:
    st.session_state.current_quote: dict = new_quote_draft()

if "quote_confirmed" not in st.session_state:
    st.session_state.quote_confirmed: bool = False


# ============================================================
# 全域標題
# ============================================================

st.markdown(
    """
    <div style='text-align:center; padding: 0.5rem 0 0.2rem;'>
        <h2 style='margin:0'>🤖 AI 產品報價助理</h2>
        <p style='color:#888; font-size:0.85rem; margin:0'>
            自然語言報價系統 &nbsp;|&nbsp;
            資料庫：{db_mode} &nbsp;|&nbsp;
            AI 模式：{ai_mode}
        </p>
    </div>
    """.format(
        db_mode="SQLite（開發）" if IS_SQLITE else "MSSQL（正式）",
        ai_mode=f"LLM（{OPENAI_MODEL}）" if USE_LLM else "規則式",
    ),
    unsafe_allow_html=True,
)
st.divider()


# ============================================================
# 雙區佈局
# ============================================================

left_col, right_col = st.columns([1, 1], gap="large")


# ============================================================
# 左側：對話互動區
# ============================================================

with left_col:
    st.subheader("💬 需求對話")

    # ── 完整報價單貼上匯入 ─────────────────────────────────
    with st.expander("📥 貼上完整報價單（13 欄 TSV）", expanded=False):
        st.caption(
            "欄位順序：事業別、報價單號、path、狀態、代碼、規格、"
            "數量、標準用量、來源成本、日期、時間、使用者、來源。"
            "匯入成本僅作參考，正式試算會重新查詢資料庫。"
        )
        pasted_quote = st.text_area(
            "貼上報價明細",
            height=220,
            key="pasted_quote_text",
            placeholder="103\tQU26824010-01001\tCMT1\\A001\t1\tC005\t60*180\t1.0\t1.0\t0.0\t2026.08.24\t16:07:34\thou\torda60",
        )
        if st.button("匯入並重新試算", use_container_width=True):
            try:
                imported = import_pasted_quote(pasted_quote)
                draft = imported.draft
                preview = preview_quote(draft)
                draft["calc_result"] = preview["calc"]
                draft["imported_quote"]["source_total_rows"] = len(imported.rows)
                st.session_state.current_quote = draft
                st.session_state.quote_confirmed = False
                natural_request = quotation_to_natural_language(imported.rows)
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": f"已將報價單轉成自然語句：\n\n{natural_request}\n\n"
                    f"已匯入 {len(imported.rows)} 筆，並使用目前資料庫重新試算。",
                })
                st.success(f"已匯入 {len(imported.rows)} 筆明細並完成試算。")
                for warning in draft["imported_quote"].get("warnings", []):
                    st.warning(warning)
                st.rerun()
            except Exception as exc:
                st.error(f"匯入失敗：{exc}")

    # 渲染歷史訊息
    chat_container = st.container(height=480, border=True)
    with chat_container:
        if not st.session_state.messages:
            st.markdown(
                "👋 **歡迎使用 AI 報價助理！**\n\n"
                "請直接描述您的需求，例如：\n\n"
                "> 我要 20 張 60*120 的桌子，美耐板白色，木腳。\n\n"
                "系統將自動解析並為您試算報價。"
            )

        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    # 使用者輸入
    if prompt := st.chat_input("請輸入需求，例如：20張 60*120 美耐板白色木腳桌子…"):
        # 記錄輸入
        log_user_input(prompt)

        # 新增到歷史
        st.session_state.messages.append({"role": "user", "content": prompt})

        # 呼叫 Agent
        with st.spinner("🔍 AI 分析需求與查詢規格中…"):
            response, updated_quote = run_quote_agent(
                user_input=prompt,
                quote_draft=st.session_state.current_quote,
                messages=st.session_state.messages,
            )

        # 更新狀態
        st.session_state.current_quote = updated_quote
        st.session_state.messages.append({"role": "assistant", "content": response})

        # 重置確認狀態（若 draft 已更新）
        if updated_quote.get("status") != QuoteStatus.SNAPSHOT_CREATED:
            st.session_state.quote_confirmed = False

        st.rerun()

    # 快捷按鈕
    st.markdown("**快捷輸入：**")
    btn_cols = st.columns(3)
    with btn_cols[0]:
        if st.button("🔄 重新開始", use_container_width=True):
            st.session_state.current_quote = new_quote_draft()
            st.session_state.messages = []
            st.session_state.quote_confirmed = False
            st.rerun()
    with btn_cols[1]:
        if st.button("📐 標準規格", use_container_width=True):
            prompt = "使用標準規格"
            st.session_state.messages.append({"role": "user", "content": prompt})
            response, updated_quote = run_quote_agent(
                user_input=prompt,
                quote_draft=st.session_state.current_quote,
                messages=st.session_state.messages,
            )
            st.session_state.current_quote = updated_quote
            st.session_state.messages.append({"role": "assistant", "content": response})
            st.rerun()
    with btn_cols[2]:
        if st.button("📋 範例需求", use_container_width=True):
            prompt = "我要 20 張 60*120 的桌子，MDF，胡桃，木腳"
            st.session_state.messages.append({"role": "user", "content": prompt})
            response, updated_quote = run_quote_agent(
                user_input=prompt,
                quote_draft=st.session_state.current_quote,
                messages=st.session_state.messages,
            )
            st.session_state.current_quote = updated_quote
            st.session_state.messages.append({"role": "assistant", "content": response})
            st.rerun()


# ============================================================
# 右側：報價試算卡片
# ============================================================

with right_col:
    st.subheader("📋 報價試算")

    quote_data = st.session_state.current_quote
    status: QuoteStatus = quote_data.get("status", QuoteStatus.NEW)

    # 狀態標籤
    st.markdown(f"**狀態：** {get_status_badge(status)}")

    # 已建立報價單的特別顯示
    if status == QuoteStatus.SNAPSHOT_CREATED and quote_data.get("ref_no"):
        ref_no = quote_data["ref_no"]
        calc = quote_data.get("calc_result", {}) or {}
        st.success(
            f"🎉 **報價單已建立成功！**\n\n"
            f"報價單號：`{ref_no}`\n\n"
            f"報價金額：**{fmt_money(calc.get('total_price', 0))}**"
        )
        if st.button("🆕 建立新報價", type="primary", use_container_width=True):
            st.session_state.current_quote = new_quote_draft()
            st.session_state.messages = []
            st.session_state.quote_confirmed = False
            st.rerun()
        st.stop()

    # ── 規格摘要卡片（動態渲染 selections） ───────────────
    imported_info = quote_data.get("imported_quote")
    if imported_info:
        st.info(
            f"📥 來源報價單：{imported_info.get('source_ref_no', '--')}｜"
            f"已匯入 {imported_info.get('source_total_rows', len(imported_info.get('source_rows', [])))} 筆；"
            "下方金額為目前資料庫重新試算結果。"
        )
        source_warnings = imported_info.get("warnings", [])
        if source_warnings:
            with st.expander(f"⚠️ 匯入警告（{len(source_warnings)}）"):
                for warning in source_warnings:
                    st.write(f"- {warning}")

    with st.container(border=True):
        col1, col2 = st.columns(2)

        # 固定欄位
        with col1:
            st.markdown(f"**產品：** {quote_data.get('product_name', '辦公桌')}")
            qty_display = f"{quote_data.get('qty')} 張" if quote_data.get("qty") else "--"
            st.markdown(f"**數量：** {qty_display}")

        # 動態列出已選選項（依 selections 的 optdesc）
        sel_list = quote_selections_list(quote_data)
        half = max(len(sel_list) // 2, 1)
        left_sels = sel_list[:half]
        right_sels = sel_list[half:]

        with col1:
            for sel in left_sels:
                label = sel["optdesc"]
                codsc = sel["codsc"]
                compri = sel["compri"]
                if compri and compri > 0:
                    st.markdown(f"**{label}：** {codsc}（${compri:,.0f}）")
                else:
                    st.markdown(f"**{label}：** {codsc}")

        with col2:
            for sel in right_sels:
                label = sel["optdesc"]
                codsc = sel["codsc"]
                compri = sel["compri"]
                if compri and compri > 0:
                    st.markdown(f"**{label}：** {codsc}（${compri:,.0f}）")
                else:
                    st.markdown(f"**{label}：** {codsc}")

            # 建議售價顯示在右欄
            calc = quote_data.get("calc_result", {}) or {}
            if calc.get("total_price"):
                st.markdown(f"**建議售價：** {fmt_money(calc.get('total_price'))}")

    # ── 缺少欄位提示 ─────────────────────────────────────
    missing = quote_data.get("missing_fields", [])
    if missing:
        st.warning(
            "⚠️ 尚缺少以下必要資訊：\n"
            + "\n".join(f"  • {f}" for f in missing)
        )

    # ── 報價明細表格 ─────────────────────────────────────
    calc_result = quote_data.get("calc_result")
    if calc_result and calc_result.get("items"):
        st.markdown("**📊 報價明細**")
        df = calc_items_to_df(calc_result["items"])
        st.dataframe(df, use_container_width=True, hide_index=True)

        # 金額摘要
        st.markdown("---")
        metric_cols = st.columns(3)
        with metric_cols[0]:
            st.metric("材料成本", fmt_money(calc_result.get("total_cost", 0)))
        with metric_cols[1]:
            st.metric(
                "折扣",
                f"-{fmt_money(calc_result.get('discount_amount', 0))}",
                delta=f"{calc_result.get('discount_rate', 0):.0%}",
                delta_color="inverse",
            )
        with metric_cols[2]:
            st.metric(
                "報價總金額（含稅）",
                fmt_money(calc_result.get("total_price", 0)),
                delta=f"稅 {fmt_money(calc_result.get('tax_amount', 0))}",
                delta_color="off",
            )

    # ── 確認建立報價按鈕（只在 PREVIEW 狀態顯示）────────
    if status == QuoteStatus.PREVIEW:
        st.divider()

        if not st.session_state.quote_confirmed:
            confirm = st.button(
                "✅ 確認建立正式報價單（寫入 ordqdt_ai）",
                type="primary",
                use_container_width=True,
            )
            if confirm:
                st.session_state.quote_confirmed = True
                st.rerun()
        else:
            st.warning("⚠️ **確認後將寫入正式報價快照，此操作無法撤銷。**")
            confirm_cols = st.columns(2)
            with confirm_cols[0]:
                if st.button("✅ 確定建立", type="primary", use_container_width=True):
                    with st.spinner("⚙️ 建立報價快照中…"):
                        try:
                            result = create_quote(quote_data, user="SYS")
                            st.session_state.current_quote["status"] = QuoteStatus.SNAPSHOT_CREATED
                            st.session_state.current_quote["ref_no"] = result["ref_no"]
                            # 記錄到稽核日誌
                            log_quote_created(result["ref_no"], result.get("total_price", 0))
                            # 加入對話歷史
                            st.session_state.messages.append({
                                "role": "assistant",
                                "content": result["message"],
                            })
                            st.session_state.quote_confirmed = False
                        except Exception as e:
                            st.error(f"❌ 建立報價時發生錯誤：{e}")
                    st.rerun()
            with confirm_cols[1]:
                if st.button("❌ 取消", use_container_width=True):
                    st.session_state.quote_confirmed = False
                    st.rerun()

    # ── 空白狀態提示 ─────────────────────────────────────
    elif status in (QuoteStatus.NEW, QuoteStatus.ANALYZING, QuoteStatus.CHECKING):
        st.info("💡 請於左側輸入需求，試算結果將顯示於此處。")

    elif status == QuoteStatus.WAITING_FOR_INPUT:
        st.info("⏳ 請於左側補充缺少的規格資訊。")
