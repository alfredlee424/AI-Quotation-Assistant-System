"""固定內部試算的快照關聯演練，沒有資料庫保存按鈕。"""
import json

from engine.multi_snapshot import build_snapshot_layout, checked_snapshot_layout


def render_snapshot_layout(st, draft, trial, batch, actor):
    stored = dict(st.session_state.get("multi_snapshot_layouts", {}))
    layout = stored.get(draft.draft_id)
    if trial is None:
        if layout is not None:
            stored.pop(draft.draft_id, None)
            st.session_state["multi_snapshot_layouts"] = stored
        return
    if layout is not None:
        try:
            layout = checked_snapshot_layout(draft, trial, layout, actor=actor, source_batch=batch)
        except (ValueError, TypeError):
            stored.pop(draft.draft_id, None)
            st.session_state["multi_snapshot_layouts"] = stored
            layout = None
            st.info("草稿、試算或關聯契約已變更，舊關聯演練已清除。")
    st.caption("只從目前固定內部試算建立產品明細與快照列對照，不重新查價、不產生正式單號、不執行建表或保存。")
    if st.button("產生快照關聯演練（不查價、不保存）", key=f"snapshot_layout_{draft.draft_id}_{trial['trial_id']}"):
        stored.pop(draft.draft_id, None)
        st.session_state["multi_snapshot_layouts"] = stored
        layout = None
        try:
            layout = build_snapshot_layout(draft, trial, actor=actor, source_batch=batch)
        except (ValueError, TypeError) as exc:
            st.error(str(exc))
        except Exception:
            st.error("快照關聯演練失敗，未保留舊結果；請檢查固定試算與資料契約。")
        else:
            stored[draft.draft_id] = layout
            st.session_state["multi_snapshot_layouts"] = stored
    if layout is None:
        return
    st.warning(layout["warning"])
    st.caption(f"關聯列數 {layout['row_count']}／{layout['sequence_limit']}；全單唯一序號不取代穩定產品明細識別。")
    if layout["layout_complete"]:
        st.info("目前已試算明細的關聯與欄位容量檢查通過，但仍不可正式保存。")
    else:
        st.warning("關聯演練未完整：有未試算、封存明細或欄位容量問題，不得視為整單快照。")
    st.dataframe([{"產品明細識別": p["line_id"], "順序": p["display_order"], "產品": p["prodkind"] or "未指定",
                   "名稱": p["label"], "關聯列數": len(p["row_sequences"]), "試算狀態": p["trial_status"]}
                  for p in layout["products"]], hide_index=True, use_container_width=True)
    if layout["row_links"]:
        st.dataframe(layout["row_links"], hide_index=True, use_container_width=True)
    for issue in layout["storage_issues"]:
        st.error(f"{issue['line_id']}｜{issue['seq_no']}｜{issue['message']}")
    if layout["display_row_amount_difference"] is not None:
        st.caption(f"逐列展示金額加總與未捨入整單小計差額：{layout['display_row_amount_difference']:.2f}；不分攤整單折扣或稅，不重算成本。")
    st.caption("下載含固定成本、圖面摘要與核對紀錄；只供內部／DBA 審查，不可作正式報價或匯入保存。")
    st.download_button("下載快照關聯演練（不可保存為報價）", json.dumps(layout, ensure_ascii=False, indent=2, allow_nan=False),
                       file_name=f"snapshot-layout-{trial['trial_id']}.json", mime="application/json")
