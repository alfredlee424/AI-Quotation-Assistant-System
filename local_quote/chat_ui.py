"""對話式報價 UI（app.py 入口）：左側對話、右側報價單。

規則全部來自 local_quote.chat.ChatSession → dynamic.analyze（與 four_quote_acceptance.py
--local-db 相同的本機規則，非 LLM）。主檔唯讀；只有按下「保存到資料庫」並勾選確認後，
才以 DynamicStore 寫入 ordqdt_ai（每個項目各自一個 DQ 單號，同 ERP 一項一單）。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import html
from pathlib import Path
import re

import pandas as pd
import streamlit as st

from local_quote.chat import ChatSession
from local_quote.dynamic_presentation import safe_cell, tsv
from local_quote.storage import StorageError

RED = "#d32f2f"
LOG_DIR = Path(__file__).resolve().parents[1] / "logs"  # 每次對話一個 chat_quote_日期_時間.log


@st.cache_resource
def services():
    """(唯讀主檔來源, 逐項保存)；連線設定只取 .env 的 DB_CONN_STR／WORKGROUP（本機 MySQL／MSSQL）。"""
    from local_quote.backend import LocalDatabase, load_settings
    from local_quote.business import MasterSource
    from local_quote.dynamic_storage import DynamicStore
    db = LocalDatabase(load_settings(Path(__file__).resolve().parents[1] / ".env"))
    db.inspect_schema()
    return MasterSource(db, allow_code_variants=True), DynamicStore(db)


def money(value):
    """千分位；金額保留兩位小數，缺值顯示破折號。"""
    if value is None:
        return "—"
    try:
        return f"{Decimal(str(value)):,.2f}"
    except InvalidOperation:
        return str(value)


def number(value):
    """數量／用料：去掉多餘的尾零（1.0 → 1、4.50 → 4.5）。"""
    if value is None:
        return ""
    try:
        return f"{Decimal(str(value)).normalize():f}"
    except InvalidOperation:
        return str(value)


def escape_markdown(text):
    """對話內容原樣顯示：60*150、路徑反斜線等不被當成 Markdown。"""
    return re.sub(r"([\\`*_{}\[\]<>#$~|])", r"\\\1", text).replace("\n", "  \n")


def red(lines):
    """紅字提醒；用 HTML 區塊避免規格內的 * 被當成 Markdown。"""
    body = "<br>".join(html.escape(line) for line in lines)
    st.markdown(f'<div style="color:{RED};font-weight:600">{body}</div>', unsafe_allow_html=True)


def _red_rows(frame, column):
    return frame.style.apply(
        lambda row: [f"color: {RED}; font-weight: 600" if row[column] else ""] * len(row), axis=1)


def summary_frame(view):
    return pd.DataFrame([{"項目": safe_cell(item["name"]), "內容": safe_cell(item["description"]),
                          "數量": item["qty"], "單價（未稅）": money(item["unit_price"]),
                          "小計（未稅）": money(item["amount"]),
                          "報價單號": safe_cell(item["ref_no"] or ""),
                          "狀態": "" if item["ready"] else f"缺 {len(item['gaps'])} 項"}
                         for item in view["items"]])


def detail_frame(item):
    return pd.DataFrame([{"部位": "　" * max(0, row["depth"] - 1) + row["label"],
                          "代碼": safe_cell(row["code"]), "規格": safe_cell(row["codsc"]),
                          "數量": number(row["qty"]), "用料量": number(row["stdqty"]),
                          "裁切量": number(row["stdpar"]), "單價": number(row["compri"]),
                          "金額": "" if row["amount"] is None else money(row["amount"]),
                          "缺漏": row["missing"] or ""} for row in item["rows"]])


def _save(chat, store, view):
    """逐項保存到 ordqdt_ai（每個項目各自一個單號）；需完整、填操作人並勾選確認。保存後鎖定。"""
    complete, refs = view["totals"]["complete"], [i for i in view["items"] if i["ref_no"]]
    if view["saved"]:
        st.success("已保存到資料庫（ordqdt_ai），每個項目各一個單號：" + "、".join(f"{i['name']} {i['ref_no']}" for i in refs)
                   + "。要建立新報價請按「重新開始」。")
        return
    if refs:
        red(["只保存了部分項目：" + "、".join(f"{i['name']} {i['ref_no']}" for i in refs)
             + "。請用同一操作人再按一次保存，補存其餘項目（不會重複建單）。"])
    cols = st.columns([1, 2])
    actor = cols[0].text_input("操作人（1 至 8 字元）", key="chat_actor", max_chars=8)
    confirmed = cols[1].checkbox("我已核對所有項目的明細、數量與金額，確認保存", key="chat_confirm",
                                 disabled=not complete)
    if st.button("保存到資料庫（ordqdt_ai，每項一個單號）", key="chat_save", type="primary",
                 disabled=not (complete and confirmed and actor.strip())):
        try:
            chat.save(store, actor=actor)
        except (ValueError, StorageError) as exc:
            st.session_state.chat_save_error = f"保存未完成：{exc}。內容與操作人不變再按一次即為重試，不會重複建單。"
        except Exception:
            st.session_state.chat_save_error = "保存結果未知；請勿重新輸入，直接再按一次保存以同一筆確認重試。"
        else:
            st.session_state.pop("chat_confirm", None)
        st.rerun()
    if st.session_state.get("chat_save_error"):
        st.error(st.session_state.pop("chat_save_error"))
    if not complete:
        st.caption("報價完整（沒有紅字）後才能保存。")


def _quote(chat, store):
    view = chat.quote()
    if view is None:
        st.info("請先在左側對話決定產品類別，再輸入品項；報價單會顯示在這裡。")
        return
    st.subheader(f"報價單｜{view['category']['prodkind']} {view['category']['codsc']}")
    if not view["items"]:
        st.info("尚未有品項。請在左側輸入品項與數量，或貼上整份工單。")
        return
    st.dataframe(_red_rows(summary_frame(view), "狀態"), hide_index=True, width="stretch")
    ids = [item["id"] for item in view["items"]]
    names = {item["id"]: item["name"] + ("" if item["ready"] else " ⚠️") for item in view["items"]}
    # 助理換問另一個項目時，明細跟著切換；其餘時間保留使用者自己的選擇。
    asking = chat.pending["child_id"] if chat.pending else None
    if st.session_state.get("chat_asking_shown") != asking and asking in ids:
        st.session_state.chat_item = asking
    st.session_state.chat_asking_shown = asking
    if st.session_state.get("chat_item") not in ids:
        st.session_state.chat_item = ids[0]
    selected = st.radio("查看項目", ids, format_func=names.get, horizontal=True, key="chat_item")
    item = next(i for i in view["items"] if i["id"] == selected)
    st.caption(f"{item['name']}｜{item['description']}｜數量 {item['qty']}")
    if item["gaps"]:
        red(["缺漏提醒："] + ["・" + gap for gap in item["gaps"]])
    st.dataframe(_red_rows(detail_frame(item), "缺漏"), hide_index=True, width="stretch", height=420)
    totals = view["totals"]
    if not totals["complete"]:
        red(["尚有缺漏：以下金額只含已確定的明細，不是完整報價。"])
    cols = st.columns(3)
    cols[0].metric("未稅金額", money(totals["untaxed"]))
    cols[1].metric("稅率", f"{Decimal(totals['tax_rate']) * 100:.0f}%", delta=f"稅額 {money(totals['tax'])}",
                   delta_color="off")
    cols[2].metric("總額（含稅）", money(totals["total"]))
    saved = view["saved"]
    st.download_button("下載明細（13 欄 TSV，" + ("已保存" if saved else "部分保存" if view["locked"] else "草稿") + "）", tsv(chat.detail_rows()),
                       file_name="chat-quote-saved.tsv" if saved else "chat-quote-draft.tsv",
                       mime="text/tab-separated-values", key="chat_download")
    _save(chat, store, view)
    with st.expander("目前工單文字（可複製，下次一次貼上）"):
        st.code(f"({view['category']['prodkind']})\n" + "\n".join(line for line in chat.lines if line), language=None)
        answers = sum(len(v) for v in chat.choices.values()) + sum(len(v) for v in chat.overrides.values())
        if answers:
            st.caption(f"另有 {answers} 個以問答選定的規格／數量不在文字內；重貼時助理會再詢問。")


def main():
    st.set_page_config(page_title="AI 報價助理", layout="wide")
    st.markdown("### AI 報價助理")
    if "chat" not in st.session_state:
        try:
            master, _ = services()
            st.session_state.chat = ChatSession(master.categories(), master.catalog, log_dir=LOG_DIR)
        except Exception:
            st.error("無法讀取主檔：請確認 .env 的本機資料庫連線（DB_CONN_STR、WORKGROUP）與既有資料表。")
            return
    chat = st.session_state.chat

    # 以回呼處理輸入：整頁每次都完整重繪，操作人等欄位不會因中途重跑而被清空。
    def act(action):
        action()
        st.session_state.pop("chat_confirm", None)  # 內容變動後須重新勾選確認

    left, right = st.columns([2, 3], gap="large")
    with left:
        box = st.container(height=560)
        for message in chat.messages:
            box.chat_message(message["role"]).markdown(escape_markdown(message["content"]))
        st.chat_input("輸入需求，或貼上整份工單", key="chat_prompt",
                      on_submit=lambda: act(lambda: chat.send(st.session_state.chat_prompt)))
        buttons = st.columns(2)
        buttons[0].button("復原上一步", key="chat_undo", width="stretch", on_click=act, args=(chat.undo,))
        buttons[1].button("重新開始", key="chat_reset", width="stretch", on_click=act, args=(chat.reset,))
    with right:
        _quote(chat, services()[1])


if __name__ == "__main__":
    main()
