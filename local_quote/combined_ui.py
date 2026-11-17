"""本機工單合併報價 UI（不拆單）：整份工單一張報價、一個單號。

沿用逐張 UI 的規格選取／臨時件／重述／成本輸入元件（以品項為編輯單位），
另依頂層部位（CMT1\\A001、CMT1\\B001…）分區塊顯示，每個區塊各自檢查
子項必填、規格與成本；全部區塊完整才可固定並輸出到 ordqdt_ai。
"""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from local_quote import dynamic_ui as base
from local_quote.combined import blocks as split_blocks, product_issues
from local_quote.combined_storage import CombinedStore
from local_quote.combined_workflow import CombinedWorkspace
from local_quote.dynamic_presentation import compact_rows, report_rows, safe_cell, semantic_rows, tsv
from local_quote.storage import StorageError


@st.cache_resource
def services():
    from local_quote.backend import LocalDatabase, load_settings
    from local_quote.business import MasterSource
    db = LocalDatabase(load_settings(Path(__file__).resolve().parents[1] / ".env"))
    db.inspect_schema()
    return MasterSource(db, allow_code_variants=True), CombinedStore(db)


def combined_rows(children, catalog, *, receipt=None):
    """13 欄：全部品項依序串接；正式單號／保存時間取自 receipt。"""
    rows = []
    for child in children:
        rows.extend(report_rows(child, catalog))
    if receipt is not None:
        from datetime import datetime
        instant = datetime.fromisoformat(receipt["confirmed_at"])
        actor = receipt["document"]["frozen_audit"]["actor"]
        for row in rows:
            row.update(ref_no=receipt["ref_no"], abndat=instant.strftime("%Y.%m.%d"),
                       abntim=instant.strftime("%H:%M:%S"), usrno=actor)
    for row in rows:
        row["prgno"] = "COMBINE1"
    return rows


def block_table(analysis):
    rows = []
    for b in split_blocks(analysis):
        rows.append({"區塊": f"{b['root']} 第{b['number']}組", "部位": b["label"],
                     "品項": f"第{b['ordinal']}項（群組 {b['group']}）", "成品數": b["product_qty"],
                     "子項數": len(b["items"]), "必填／成本檢查": "完整" if b["ready"] else f"待處理 {len(b['issues']) or '明細'}",
                     "原文": b["description"]})
    return [{k: safe_cell(v) for k, v in row.items()} for row in rows]


def _block(block, catalog):
    title = f"{block['root']} 第{block['number']}組 — {block['label'] or ''}（第{block['ordinal']}項，{block['product_qty']}件）"
    st.markdown(("✅ " if block["ready"] else "⚠️ ") + title)
    st.dataframe(compact_rows(dict(items=block["items"]), catalog), hide_index=True, width="stretch",
                 column_config={"path": None})
    for issue in block["issues"]:
        st.text("待處理：" + issue["message"])
    if not block["ready"] and not block["issues"]:
        st.text("待處理：區塊內仍有未選規格或未計算成本的子項")


def _product(ws, child, analysis, blocks, disabled):
    catalog = ws.catalog
    st.text(child["description"])
    st.text(f"來源行 {child['source_line']}／群組 {child['group']}／成品數 {child['product_qty']}")
    with st.expander("語意解析／適用部位"):
        st.dataframe(semantic_rows(child), hide_index=True, width="stretch")
    for block in blocks:
        _block(block, catalog)
    for issue in product_issues(analysis, child):
        st.text("品項層級待處理：" + issue["message"])
    base._candidate_tables(ws, child, disabled)
    with st.expander("進階：合法規格候選／本次臨時件"):
        for req in child["requirements"]:
            if req["kind"] in {"constraint", "units", "external_confirmation"}:
                continue
            base._selector(ws, child, req, disabled)
            if not req.get("candidates") or req["id"] in base._mapping(ws, child["id"], "customs"):
                base._custom(ws, child, req, disabled)
    with st.expander("進階：需求重述／人工成本輸入"):
        base._resolutions(ws, child, disabled)
        base._cost_editor(ws, child, disabled)
    blocked = st.session_state.get("dq_blocked", {}).get(child["id"])
    if blocked:
        st.error(blocked + "；請重新套用該欄位後才能固定／保存。")


def _quote(ws, store):
    analysis, catalog = ws.analysis, ws.catalog
    saved = ws.combined_saved
    disabled = saved is not None or not base.editable_audit()
    all_blocks = split_blocks(analysis)
    st.subheader("區塊檢查（同一路徑可有多組，各自檢查子項必填）")
    st.dataframe(block_table(analysis), hide_index=True, width="stretch")
    children = analysis["children"]
    if children:
        tabs = st.tabs([f"第 {c['ordinal']} 項（{c['product_qty']}件）" + ("" if c["ready"] else " ⚠️") for c in children])
        for tab, child in zip(tabs, children):
            with tab:
                _product(ws, child, analysis, [b for b in all_blocks if b["child_id"] == child["id"]], disabled)
    st.subheader("合併報價（單一單號）")
    calc = ws.summary()
    if calc["complete"]:
        cols = st.columns(3)
        cols[0].metric("整單完整成本", calc["total_cost"])
        cols[1].metric("未稅小計", calc["subtotal"])
        cols[2].metric("整單報價（含稅）", calc["total_price"])
    else:
        st.metric("已知成本小計（非完整合計）", calc["known_cost"])
        pending = [f"{b['root']} 第{b['number']}組" for b in all_blocks if not b["ready"]]
        st.warning("尚有區塊未完整，不可固定或保存" + ("：" + "、".join(pending) if pending else "（品項層級待處理）"))
    rows = combined_rows(children, catalog, receipt=saved)
    st.download_button("下載13欄 TSV（" + ("正式已保存" if saved else "草稿未保存") + "）", tsv(rows),
                       file_name="saved-combined-quote.tsv" if saved else "draft-combined-quote.tsv",
                       mime="text/tab-separated-values", key="dc_download")
    if saved:
        st.success("已保存合併報價：" + saved["ref_no"] + "；修改與再次保存已停用。")
        return
    if ws.combined_revoked:
        st.info("先前合併固定已撤銷：" + ws.combined_revoked)
    blocked = bool(st.session_state.get("dq_blocked"))
    frozen = ws.combined_frozen
    if st.button("固定整單預覽", key="dc_freeze", disabled=disabled or not calc["complete"] or frozen is not None or blocked):
        try:
            stamp = base.audit()
            reason = json.dumps({"reason": stamp["reason"], "source": st.session_state.get("dq_source_audit"),
                                 "ui_events": st.session_state.get("dq_events", {})}, ensure_ascii=False)
            ws.freeze_combined(actor=stamp["actor"], reason=reason, time=stamp["time"])
            st.rerun()
        except (ValueError, StorageError) as exc:
            st.error(str(exc))
    confirmed = False
    if frozen:
        st.text("固定 UUID：" + frozen.confirmation_id)
        confirmed = st.checkbox("我已核對整單所有區塊明細、數量與金額，確認保存", key="dc_confirm:" + frozen.confirmation_id)
    if st.button("輸出至資料庫報價單（ordqdt_ai，單一單號）", key="dc_save", type="primary",
                 disabled=not frozen or not confirmed or blocked):
        try:
            ws.confirm_combined(store, confirmed=True)
            st.rerun()
        except (ValueError, StorageError) as exc:
            st.error(str(exc))
            st.warning("保存未完成；保留同一固定 UUID，使用本按鈕重試，勿重新建立工單。")
        except Exception:
            st.error("保存結果未知；保留同一固定 UUID 重試或查歷史，不另建確認。")


def _history(store):
    with st.expander("合併報價歷史"):
        if st.button("載入合併報價歷史", key="dc_history:list"):
            st.session_state.pop("dc_history:document", None)
            try:
                st.session_state["dc_history:rows"] = store.list_saved()
            except Exception:
                st.error("歷史查詢失敗或完整性不符；不會修復或重新查價。")
        rows = st.session_state.get("dc_history:rows", [])
        labels = {r["confirmation_id"]: r["ref_no"] for r in rows}
        selected = st.selectbox("選擇單號", list(labels), index=None, format_func=labels.get, key="dc_history:selected")
        if st.button("查回", key="dc_history:read", disabled=selected is None):
            try:
                st.session_state["dc_history:document"] = store.read(selected)
            except Exception:
                st.error("固定歷史不完整，拒絕顯示為有效報價。")
        result = st.session_state.get("dc_history:document")
        if result:
            doc = result["document"]
            st.text("正式單號：" + result["ref_no"] + "／保存時間：" + result["confirmed_at"])
            st.metric("整單報價（含稅）", doc["calc"]["total_price"])
            history_rows = combined_rows(doc["children"], doc["context"]["catalog_snapshot"], receipt=result)
            st.dataframe([{k: safe_cell(v) for k, v in r.items()} for r in history_rows], hide_index=True)
            st.download_button("下載13欄 TSV（正式已保存）", tsv(history_rows), file_name="saved-combined-quote.tsv",
                               mime="text/tab-separated-values", key="dc_history:tsv")


def _new_source():
    st.session_state.dq_workspace = CombinedWorkspace()
    for key in ("dq_category", "dq_source", "dq_events", "dq_source_audit", "dq_blocked"):
        st.session_state.pop(key, None)


def main():
    st.set_page_config(page_title="本機工單合併報價", layout="wide")
    st.title("本機工單合併報價（不拆單）")
    st.caption("整份工單所有品項併為一張報價、一個單號。每個 CMT1\\A001、CMT1\\B001… 區塊各自檢查子項必填；"
               "全部完整才可固定。人工確認前不配號、不寫庫。")
    try:
        master, store = services()
    except Exception:
        st.error("無法載入本機服務；請檢查連線及既有七表，不會自動建表。")
        return
    if not isinstance(st.session_state.get("dq_workspace"), CombinedWorkspace):
        st.session_state.dq_workspace = CombinedWorkspace()
    ws = st.session_state.dq_workspace
    try:
        if "dq_categories" not in st.session_state:
            st.session_state.dq_categories = master.categories()
        names = {c["prodkind"]: c["codsc"] + "（" + c["prodkind"] + "）" for c in st.session_state.dq_categories}
    except Exception:
        st.error("產品類別唯讀查詢失敗；仍可查閱已保存歷史。")
        names = {}
    locked = ws.combined_saved is not None
    st.button("開始新工單（不更改已保存歷史）", key="dq_new", on_click=_new_source)
    category = st.selectbox("產品類別", list(names), index=None, format_func=names.get, key="dq_category", disabled=locked)
    source = st.text_area("貼上工單需求（所有品項併為一張）", height=180, key="dq_source",
                          disabled=category is None or locked,
                          placeholder="平直70X120-----2\n平直70X150-----3\n共用：\n面無孔\n817胡／噴胡")
    if not locked:
        if category is None and ws.category is not None:
            ws = CombinedWorkspace()
            st.session_state.dq_workspace = ws
            st.session_state.dq_events = {}
            st.session_state.dq_blocked = {}
        elif category is not None and ws.set_input(source, category):
            st.session_state.dq_events = {}
            st.session_state.dq_blocked = {}
    st.text_input("操作人 actor（1至8字元，非登入憑證）", key="dq_actor", max_chars=8)
    st.text_input("本次人工操作原因", key="dq_reason")
    valid_audit = base.editable_audit()
    if not valid_audit:
        st.info("請先填合法操作人及原因；人工編輯、分析及固定尚未開放。")
    if st.button("讀取主檔並分析工單", key="dq_analyze", type="primary",
                 disabled=category is None or not source.strip() or not valid_audit or locked):
        try:
            stamp = base.audit()
            ws.load_catalog(master)
            st.session_state.dq_source_audit = dict(stamp, source=ws.source, category=category)
        except (ValueError, StorageError) as exc:
            st.error(str(exc))
        except Exception:
            st.error("唯讀主檔查詢未完成；不顯示為已完成報價，請重試。")
    if st.session_state.get("dq_notice"):
        st.info(st.session_state.pop("dq_notice"))
    if ws.analysis:
        for issue in ws.analysis["global_issues"]:
            st.text(issue["message"])
        _quote(ws, store)
    _history(store)


if __name__ == "__main__":
    main()
