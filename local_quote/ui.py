"""本機主檔四張報價。只有確認按鈕會進入寫入服務。"""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from local_quote.backend import LocalDatabase, load_settings
from local_quote.business import MasterSource
from local_quote.storage import BatchStore
from local_quote.workflow import Workspace


COLUMNS = (("成本序號", None), ("部位名稱", "optdesc"), ("完整路徑", "path"),
           ("規格代碼", "spc_code"), ("規格說明", "spdsc"),
           ("單位", "unit"), ("每件項目數", "line_qty"), ("本張總項目數", "qty"),
           ("用料量", "stdqty"), ("裁切量", "stdpar"), ("採購單價", "compri"), ("明細成本", "part_cost"))


@st.cache_resource
def services():
    db = LocalDatabase(load_settings(Path(__file__).resolve().parents[1] / ".env"))
    db.inspect_schema()
    return MasterSource(db), BatchStore(db)


def table_rows(items):
    return [{label: (f"{i:05d}" if key is None else item.get(key, "未提供" if key == "unit" else "缺資料"))
             for label, key in COLUMNS} for i, item in enumerate(items, 1)]


def show_children(children, refs=None):
    tabs = st.tabs([f"第 {i} 張" for i in range(1, 5)])
    for i, (tab, child) in enumerate(zip(tabs, children)):
        with tab:
            st.subheader("規格／成本明細")
            calc = child.get("calc")
            if calc:
                st.dataframe(table_rows(calc["items"]), hide_index=True, width="stretch")
                cols = st.columns(3)
                cols[0].metric("本張成本", f'{float(calc["total_cost"]):,.2f}')
                cols[1].metric("未稅小計", f'{float(calc["subtotal"]):,.2f}')
                cols[2].metric("本張報價（含稅）", f'{float(calc["total_price"]):,.2f}')
            else:
                st.warning(child.get("error") or "尚未產生成本；缺資料不以 0 代替。")
            st.caption(f"正式單號：{refs[i] if refs else '待確認後配號（未保留）'}")


def main():
    st.set_page_config(page_title="本機主檔四張報價", layout="wide")
    st.title("本機主檔成本")
    st.caption("依本機 MySQL 主檔計價，不是合成成本；不代表業務／工程核准。只有最後一次確認才寫入。")
    try:
        master, store = services()
        categories = master.categories()
    except Exception:
        st.error("無法唯讀載入本機資料庫。請檢查連線及既有表結構；不會自動建表或修復。")
        return
    if "local_workspace" not in st.session_state:
        st.session_state.local_workspace = Workspace()
    ws = st.session_state.local_workspace
    names = {c["prodkind"]: f'{c["codsc"]}（{c["prodkind"]}）' for c in categories}
    category = st.selectbox("產品類別（整批適用）", list(names), index=None,
                            placeholder="請先選擇資料庫產品類別", format_func=names.get,
                            key="local_category")
    source = st.text_area("貼上工單需求（四品項）", height=230, key="local_source",
                          disabled=category is None,
                          placeholder="每行：平直70X120-----2 面無孔\n共用要求放在四個品項之後；完整保留原文。")
    ws.set_input(category, source)
    if st.button("產生四張成本明細", type="primary", disabled=category is None or not source.strip()):
        try:
            ws.generate(master)
        except ValueError as exc:
            st.error(str(exc))
        except Exception:
            st.error("唯讀主檔查詢未完成；未計價或保存，請重試。")
    if ws.analysis:
        # callback 發生在畫面重繪前；選擇只重新分析，不重算成本。
        def changed(key, widget):
            try:
                ws.choose(key, st.session_state[widget])
            except ValueError:
                ws.result = ws.saved = None
                st.session_state.local_choice_error = "候選已失效，請重新產生成本明細。"

        gaps = {g["key"]: g for g in ws.analysis["gaps"]}
        questions = {q["key"]: q for q in ws.analysis["questions"]}
        pending = [g for g in gaps.values() if g["candidates"]]
        hard = [g for g in gaps.values() if not g["candidates"]]
        if hard:
            st.error("尚有主檔或語意缺項，不能以近似規格替代、手填價格或略過後保存。")
            for g in hard:
                st.write("• " + g["label"])
        if pending:
            st.subheader("只補必要主檔選擇")

        def selector(q):
            labels = {c["value"]: c["label"] for c in q["candidates"]}
            widget = f"local_choice:{ws.generation}:{q['key']}"
            # Streamlit 會刪除未渲染的元件狀態；從已驗證選擇恢復，而非預選第一筆。
            st.session_state[widget] = ws.choices.get(q["key"])
            ordinal = q["key"].split(":")[1]
            st.selectbox(f"第 {ordinal} 張 — {q['label']}", list(labels), index=None,
                         format_func=labels.get, placeholder="請選合法規格", key=widget,
                         on_change=changed, args=(q["key"], widget))

        for q in pending:
            selector(q)
        answered = [questions[k] for k in ws.choices if k in questions and k not in gaps]
        if answered:
            with st.expander("已補選規格（修改即撤銷舊成本）"):
                for q in answered:
                    selector(q)
        if st.session_state.pop("local_choice_error", None):
            st.warning("候選已失效，請重新產生成本明細。")
        if not gaps and not ws.result:
            st.info("必要選擇已補齊，請按「產生四張成本明細」計算。")
        children = ws.result["children"] if ws.result else ws.analysis["children"]
        st.subheader("批次總覽")
        st.dataframe([{"張次": c["ordinal"], "需求品項": c["description"], "成品數": c["product_qty"],
                       "狀態": "成本已固定" if ws.result and ws.result.get("frozen") else "未完成整批成本"}
                      for c in children], hide_index=True, width="stretch")
        st.caption("公式：成品數 × 每件項目數 × 用料量 ÷ 裁切量 × 採購單價。主檔報價係數；折扣 0%，外加稅 5%。")
        show_children(children, ws.saved["ref_nos"] if ws.saved else None)
        if ws.result and ws.result.get("error"):
            st.error(ws.result["error"])
        if ws.result and ws.result.get("frozen"):
            document = json.loads(ws.result["frozen"].payload)
            st.metric("四張合計（含稅）", f'{float(document["batch_total"]):,.2f}')
            if not ws.saved and st.button("確認並完成報價", type="primary"):
                try:
                    ws.confirm(store)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
                except Exception:
                    st.error("保存未完成，請保留本固定預覽重試；不另建批次，避免重複報價。")
            if ws.saved:
                st.success("已一次保存四張報價：" + "、".join(ws.saved["ref_nos"]))
            with st.expander("固定內容／下載"):
                content = ws.saved or document
                st.download_button("下載本批 JSON", json.dumps(content, ensure_ascii=False, indent=2),
                                   file_name="local-quote.json", mime="application/json")
                st.json(content)
    with st.expander("歷史報價（只讀固定內容，不查價）"):
        if st.button("載入已保存批次"):
            try:
                st.session_state.local_history = store.list_saved()
            except Exception:
                st.error("歷史查詢失敗或完整性不符；不會修復或重算。")
        history = st.session_state.get("local_history", [])
        if history:
            def clear_history_view():
                st.session_state.pop("local_history_document", None)

            selected = st.selectbox("已保存批次", history, index=None,
                                    format_func=lambda h: "、".join(h["ref_nos"]), on_change=clear_history_view)
            if selected and st.button("查回四張"):
                clear_history_view()
                try:
                    st.session_state.local_history_document = store.read(selected["batch_id"])
                except Exception:
                    st.error("固定歷史不完整，拒絕顯示為有效報價。")
            saved = st.session_state.get("local_history_document")
            if saved:
                show_children([d["child"] for d in saved["children"]], saved["ref_nos"])
                st.download_button("下載歷史 JSON", json.dumps(saved, ensure_ascii=False, indent=2),
                                   file_name="local-quote-history.json", mime="application/json")
    with st.expander("操作範圍與資料安全"):
        st.write("本頁只支援四個品項。未知尺寸／加工／定位會阻擋；不呼叫模型，也不將來源寫入一般日誌。")
        st.write("預覽只在目前瀏覽工作階段，重新整理或服務重啟可能失去尚未保存的內容。保存失敗先重試同一預覽；若失去工作階段，先查歷史確認是否已成立，勿直接新建。")


if __name__ == "__main__":
    main()
