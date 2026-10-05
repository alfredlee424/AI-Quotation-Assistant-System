"""人工圖面引用介面；不開啟引用位置或讀取附件。"""
from agent.drawing_review import apply_drawing_command, drawing_report


def render_drawing_review(st, draft, batch, actor):
    report = drawing_report(draft)
    st.warning(report["warning"])
    st.caption("先登記缺圖依賴，再保存圖號、版本及人工確認摘要。紀錄不會套用尺寸或解除其他來源／工程阻擋。")
    prefix = f"drawing_{draft.draft_id}_{draft.revision}"

    def apply(command, reason):
        try:
            return apply_drawing_command(draft, command, draft_id=draft.draft_id,
                                         expected_revision=draft.revision, actor=actor,
                                         reason=reason, source_batch=batch)
        except ValueError as exc:
            st.error(str(exc))
        return None

    for issue in report["issues"]:
        st.warning(issue["message"])
    if report["records"]:
        st.dataframe([{"依賴識別": r["dependency_id"], "用途": r["purpose"], "狀態": r["status"],
                       "適用明細": "、".join(r["targets"]),
                       "圖號": r["evidence"][-1]["reference"] if r["evidence"] else "",
                       "版本": r["evidence"][-1]["version"] if r["evidence"] else ""}
                      for r in report["records"]], hide_index=True, use_container_width=True)
        st.json({"圖面核對歷史": report["records"]}, expanded=False)

    if draft.lines:
        with st.form(prefix + "_declare"):
            targets = st.multiselect("圖面適用明細", [line.line_id for line in draft.lines],
                                    format_func=lambda key: next(line.label for line in draft.lines if line.line_id == key))
            requirements = st.multiselect("圖面來源需求", [r.requirement_id for r in draft.requirements],
                                         format_func=lambda key: next(r.text for r in draft.requirements if r.requirement_id == key))
            purpose = st.text_input("圖面用途／待確認內容", max_chars=1000)
            reason = st.text_input("圖面依賴登記理由", max_chars=1000)
            if st.form_submit_button("登記圖面依賴（缺圖待補）"):
                return apply({"op": "declare", "targets": targets, "requirement_ids": requirements,
                              "purpose": purpose}, reason)
    if not draft.drawings:
        st.info("尚無已登記圖面依賴，不代表已完成缺圖或圖面需求判定。")
        return None
    key = st.selectbox("待核對圖面依賴", [r.dependency_id for r in draft.drawings],
                       format_func=lambda key: next(r.purpose for r in draft.drawings if r.dependency_id == key),
                       key=prefix + "_selected")
    with st.form(prefix + "_record_" + key):
        reference = st.text_input("圖面依據識別（不自動開啟）", max_chars=500)
        version = st.text_input("圖面版本", max_chars=100)
        summary = st.text_area("人工確認內容（部位、尺寸或定位摘要）", max_chars=2000)
        reason = st.text_input("本次圖面核對理由", max_chars=1000)
        acknowledged = st.checkbox("已人工核對指定版本；了解此紀錄不是工程核准")
        if st.form_submit_button("保存圖面人工核對（非正式核准）"):
            if not acknowledged:
                st.error("請先人工核對指定版本並確認紀錄界線。")
                return None
            return apply({"op": "record", "dependency_id": key, "reference": reference,
                          "version": version, "summary": summary}, reason)
        if st.form_submit_button("標記缺圖／撤回本次核對"):
            return apply({"op": "mark_missing", "dependency_id": key}, reason)
    return None
