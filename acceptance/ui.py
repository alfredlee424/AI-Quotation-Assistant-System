"""真正的隔離四張驗收 UI；請由獨立入口啟動，不提供正式開立。"""
from __future__ import annotations

import os

from acceptance import bootstrap

# 所有業務匯入與存取都在此隔離之後；每次 Streamlit rerun 再核驗。
try:
    DIRECTORY = bootstrap.isolate_process(os.environ["FOUR_QUOTE_DIRECTORY"])
except Exception:
    import streamlit as st
    st.error("拒絕啟動：請使用獨立驗收入口與已識別的私有目錄。")
    st.stop()

import json
import sqlite3
from contextlib import closing

import streamlit as st

from acceptance import business, presentation
from acceptance.workflow import Workflow, WorkflowOperationError


# 只呈現已知且不含使用者原文的規格訊息，未知例外不輸出 repr/traceback。
SPEC_ERRORS = frozenset({
    "來源必須恰有一張工單及四個不同品項，不接受少項、重複或拆批",
    "來源分類可省略；若提供僅接受單一環式會議桌分類標題", "分類標題含未知內容",
    "品項含未知需求或衝突：只支援本案明確外型、尺寸及孔/走線條件",
    "品項順序、外型、尺寸或數量不符；重複或不同尺寸必須重新核定",
    "第一張必須面無孔有桌下走線，其餘三張每件1孔13.5X40單掀鋁",
    "來源標題須為日期式單號及短純客戶識別；未知/混合需求不可當來源識別",
    "來源有未知行，不得忽略", "共用來源含未知或相反要求",
    "來源共用需求重複，不自動合併", "共用來源有未處置尾段",
    "共用需求少項，不能以預設需求補齊", "共同折扣只允許0至10%",
    "折扣精度超出既有單品核心，拒絕以不同實際折扣計算",
    "合成主檔缺規格/成本/用量、遭修改或版本不符；拒絕計價",
    "此前版已有不同後繼；請從最新版本明確建新版",
    "同內容舊版已失效；不可回切，須明確 new_version_of",
})


def _error(exc):
    if isinstance(exc, ValueError) and str(exc) == "來源必須恰有一張工單及四個不同品項，不接受少項、重複或拆批":
        return "請貼上完整標頭行（日期式單號＋客戶識別）、四筆品項及全部共用要求；不會自動補單號。"
    if isinstance(exc, ValueError) and str(exc) in SPEC_ERRORS:
        return str(exc)
    return "操作未完成，請再按同一按鈕重試；若持續失敗，請保留測試資料庫。"


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def _expanded(value):
    if isinstance(value, dict):
        return {key: _expanded(json.loads(item) if key.endswith("_json") else item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_expanded(item) for item in value]
    return value


def _objects(name):
    # 只查結構，不讀成本；不得以「存在」假冒主檔內容已驗證。
    bootstrap.verify_directory(DIRECTORY)
    with closing(sqlite3.connect((DIRECTORY / name).as_uri() + "?mode=ro", uri=True)) as db:
        return {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


def _context():
    from dataclasses import replace
    old = st.session_state.get("history_context")
    if old and old.business_version != business.TEST_VERSION:
        return replace(old, raw_text=st.session_state.raw, discount_rate=st.session_state.discount_rate)
    return Workflow.test_context(st.session_state.raw, st.session_state.discount_rate)


def _sync(workflow):
    active = st.session_state.get("active_batch")
    if active:
        workflow.synchronize_context(active, _context())


def _edited():
    """widget callback 先於切歷史／載樣本執行，避免覆蓋輸入後漏掉失效。"""
    if st.session_state.get("active_batch"):
        try:
            workflow = Workflow(DIRECTORY)
            try:
                _sync(workflow)
            finally:
                workflow.close()
        except Exception as exc:
            st.session_state.callback_error = _error(exc)


def _sample():
    st.session_state.raw = business.SAMPLE
    _edited()


def _restore_history():
    try:
        workflow = Workflow(DIRECTORY)
        try:
            _sync(workflow)
            view = workflow.retrieve(st.session_state.history_batch)
        finally:
            workflow.close()
        context = view["context"]
        st.session_state.active_batch = view["id"]
        st.session_state.raw = context.raw_text
        st.session_state.discount_rate = context.discount_rate
        st.session_state.history_context = context
    except Exception as exc:
        st.session_state.callback_error = _error(exc)


def _table(rows, *, key=None):
    """所有欄位強制純文字；不使用 Markdown/HTML/圖片欄位或浮點格式化。"""
    if not rows:
        st.text("未提供／未驗證")
        return
    # 包含欄名及完整單行內容；中文保守留 16px，不用省略字串湊進窄欄。
    columns = {name: st.column_config.TextColumn(name, width=max(
        130, max(len(name), *(len(line) for row in rows for line in str(row[name]).split("\n"))) * 16 + 32))
        for name in rows[0]}
    st.dataframe(rows, hide_index=True, width="stretch", column_config=columns,
                 height="content", key=key)


def _header(header):
    # 純文字不截斷單頭及使用者標籤，亦不解析原文中的 Markdown 圖片。
    for label, value in header.items():
        st.text(label + "：" + value)


def _gaps(rows):
    st.subheader("真實缺口：合成驗收通過也不解除正式核准")
    for index, row in enumerate(rows, 1):
        st.text(f"缺口 {index}｜" + row["缺口"])
        _header({key: value for key, value in row.items() if key != "缺口"})


def _preview(workflow, view):
    if view["document"] is None:
        st.info("成本已計算，配號尚未完成；請再按「產生四張成本明細」。")
        return
    st.subheader("四張成本明細")
    document = view["document"]
    summary = presentation.fixed_summary(view)
    st.subheader("四張批次總覽")
    _table(summary["overview"], key="fixed_overview")
    st.text("批次固定總額（各張固定結果的原總額；不是將下方展示成本列重新計算）")
    _table([summary["totals"]], key="fixed_totals")
    for index, (tab, child) in enumerate(zip(st.tabs([f"第 {n} 張完整成本表" for n in range(1, 5)]), summary["children"]), 1):
        with tab:
            st.text("規格／成本明細（合成價格；保留零價結構與含價工序）")
            st.caption("本張總項目數已包含成品數，不可再乘；用料量／裁切量為固定成本輸入。空白、未提供與零值分開；可水平捲動檢視完整路徑與所有欄位。")
            _table(child["rows"], key=f"fixed_cost_{index}")
            _table(child["amounts"], key=f"fixed_amounts_{index}")
            st.caption("以上直接顯示單張核心固定金額；差額如實保留、不湊數。折扣率／稅率使用原小數值（例如 0.05 表示 5%）。")
    if view["saved"] is not None:
        st.success("已完成測試報價，共4張")
    with st.expander("進階｜固定文件、設定、缺口與下載", expanded=False):
        st.text(presentation.confirmation_label(view) + " · " + presentation.phase_label(view))
        st.text(business.WARNING)
        _gaps(summary["gaps"])
        st.text(f"批次：{view['id']} · 前版：{view['predecessor']} · 預覽摘要：{view['preview_digest']}")
        st.json(_expanded(document))
        st.json(view["confirmations"])
        if view["saved"] is not None:
            st.text("保存摘要：" + view["saved"]["final_digest"])
        for child in workflow.child_documents(view["id"]):
            st.text(child["text"])
            st.download_button("下載完整文字 · " + child["ref_no"], child["text"],
                               "nonformal-" + child["ref_no"] + ".txt", "text/plain",
                               key="text_" + child["ref_no"])
            st.download_button("下載完整 JSON · " + child["ref_no"], _json(child["structured"]),
                               "nonformal-" + child["ref_no"] + ".json", "application/json",
                               key="json_" + child["ref_no"])


def _run():
    st.set_page_config(page_title="四張測試報價", layout="wide")
    st.title("四張成本明細與測試報價")
    st.caption("測試模式／合成成本，僅測試存檔")
    for key, value in dict(raw="", discount_rate="0", active_batch=None, history_context=None).items():
        st.session_state.setdefault(key, value)

    workflow = Workflow(DIRECTORY) if _objects("workflow.sqlite") else None
    try:
        st.text_area("貼上來源（含工單標頭、四筆品項及完整共用要求）", key="raw", height=260,
                     placeholder=business.SAMPLE, help="目前只支援樣本四種規格與數量2/1/6/2；可從進階區複製完整樣本。",
                     on_change=_edited)
        with st.expander("進階｜折扣、樣本與歷史", expanded=False):
            st.text_input("共同折扣率（0 至 0.1，例如 0.05）", key="discount_rate", on_change=_edited)
            st.code(business.SAMPLE, language=None)
            st.button("載入完整合成樣本", key="load_sample", on_click=_sample)
            if workflow:
                batches = workflow.list_batches()
                if batches:
                    by_id = {batch["id"]: batch for batch in batches}
                    st.selectbox("選擇測試歷史", list(by_id), key="history_batch",
                                 format_func=lambda key: by_id[key]["created_at"] + " · "
                                 + presentation.confirmation_label(by_id[key]) + " · "
                                 + presentation.phase_label(by_id[key]))
                    st.button("載入歷史（不重新計價）", key="load_history", on_click=_restore_history)
            st.text("採預設合成設定與本機測試操作標記；未逐項人工核准。")

        if workflow:
            _sync(workflow)
        # 同一產生鍵亦可重試同步或配號，不增加另一個核驗操作。
        generate = st.button("產生四張成本明細", key="create", disabled=not st.session_state.raw.strip())
        if generate:
            if workflow is None:
                workflow = Workflow.prepare(DIRECTORY)
            else:
                # 部分準備完成時，只建立仍空白的主檔，不覆寫既存資料。
                workflow.close()
                workflow = Workflow.prepare(DIRECTORY)
            view = workflow.generate(st.session_state.raw, discount_rate=st.session_state.discount_rate,
                                     active_batch=st.session_state.active_batch)
            st.session_state.active_batch = view["id"]
            st.session_state.history_context = view["context"]
            st.session_state.pop("callback_error", None)
            st.rerun()
        if st.session_state.get("callback_error"):
            st.error(st.session_state.callback_error)
            return
        if workflow and st.session_state.active_batch:
            view = workflow.retrieve(st.session_state.active_batch)
            if view["invalidated"]:
                st.info("來源或折扣已修改，請重新按「產生四張成本明細」。")
                with st.expander("進階｜已失效歷史（不可完成）", expanded=False):
                    _preview(workflow, view)
                return
            _preview(workflow, view)
            if view["confirmation_mode"] != business.TEST_CONFIRMATION:
                st.info("這是舊兩次核對歷史；可查閱下載，按「產生四張成本明細」建立目前測試流程。")
                return
            if view["document"] is not None and view["saved"] is None:
                if st.button("確認並完成報價（測試存檔）", key="save"):
                    token = view["confirmations"][0]["token"] if view["confirmations"] else view["token"]
                    try:
                        workflow.complete_test(view["id"], token, _context())
                    except WorkflowOperationError:
                        st.error("測試存檔未完成，請再按「確認並完成報價（測試存檔）」重試。")
                    else:
                        st.rerun()
    finally:
        if workflow:
            workflow.close()


try:
    _run()
except WorkflowOperationError as exc:
    st.session_state.active_batch = exc.batch_id
    st.session_state.history_context = Workflow.test_context(st.session_state.raw, st.session_state.discount_rate)
    st.error("產生尚未完成，請再按「產生四張成本明細」重試；已計算內容會保留。")
except Exception as exc:
    st.error(_error(exc))
