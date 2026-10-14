"""明確開啟才即時彙整；不在 session 保存可過期的成功摘要。"""
import json

from engine.work_order_review_summary import ERROR, WARNING, build_work_order_review_summary


def render_work_order_review_summary(st, draft, batch, actor):
    st.caption("僅統計目前帳本；開啟後每次重繪重新核對，不查主檔、不計價、不寫監測日誌。")
    enabled = st.checkbox("顯示工單核對去識別化摘要（非正式）",
                          key=f"work_order_summary_{draft.draft_id}", disabled=not actor.strip())
    if not enabled or not actor.strip():
        return
    try:
        summary = build_work_order_review_summary(draft, source_batch=batch, actor=actor)
        payload = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    except Exception:
        st.error(ERROR)
        return
    st.warning(WARNING)
    st.caption("問題投影可能跨明細重複；各類計數不能加總為獨立問題數。已記錄不等於已核准。")
    st.dataframe([
        {"項目": "來源品項", "筆數": summary["source"]["item_count"]},
        {"項目": "未覆蓋來源行", "筆數": summary["source"]["uncovered_line_count"]},
        {"項目": "待核對需求（含失效／待工程）", "筆數": summary["requirements"]["pending_count"]},
        {"項目": "未映射選配", "筆數": summary["requirements"]["unmapped_selection_count"]},
        {"項目": "條件問題投影", "筆數": summary["conditions"]["issue_count"]},
        {"項目": "缺圖紀錄", "筆數": summary["drawings"]["states"]["MISSING"]},
        {"項目": "失效圖面紀錄", "筆數": summary["drawings"]["states"]["STALE"]},
        {"項目": "未結案問題紀錄", "筆數": summary["questions"]["states"]["OPEN"]},
        {"項目": "草稿修改事件", "筆數": summary["recorded_changes"]["draft_event_count"]},
    ], hide_index=True, use_container_width=True)
    st.json(summary)
    st.download_button("下載工單核對去識別化摘要（非正式）", payload,
                       file_name="work-order-review-summary.json", mime="application/json",
                       key=f"work_order_summary_download_{draft.draft_id}")
