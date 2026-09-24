"""獨立生產工單核對介面，不存取 current_quote 或任何報價服務。"""
from dataclasses import asdict
import json

from agent.work_orders import FAMILIES, MAX_TEXT_LENGTH, parse_work_orders, reclassify_line, export_review


KIND_LABELS = {
    "section": "產品分類標題", "order": "工單起始標題", "item": "明細候選",
    "note": "補充條件（未核准）", "unclassified": "未分類", "blank": "空白行",
}


def render_work_order_review(st) -> None:
    st.subheader("生產工單核對（尚未接正式報價）")
    st.warning("本區只整理來源、工單邊界及明細候選；不查價、不寫入報價、不代表規格或數量已核准。")
    st.caption("核對草稿只保留在目前工作階段；下載檔含原文，請依公司資料政策保管。目前不支援下載檔還原。")
    text = st.text_area("貼上生產工單原文", height=240, max_chars=MAX_TEXT_LENGTH, key="work_order_input")
    if st.button("建立／取代核對草稿", key="work_order_parse"):
        try:
            existing = st.session_state.get("work_order_batch")
            if existing is None or existing.raw_text != text:
                st.session_state["work_order_batch"] = parse_work_orders(text)
            st.success("核對草稿已建立；相同原文保留既有修正，新原文會建立新批次。")
        except ValueError as exc:
            st.error(str(exc))
    batch = st.session_state.get("work_order_batch")
    if batch is None:
        return
    if text != batch.raw_text:
        st.warning("輸入已變更，目前保存的仍是上一份草稿。請按「建立／取代核對草稿」後再核對；舊修正不會套到新原文。")
        return
    st.caption(f"版本 {batch.revision}｜{len(batch.orders)} 張工單候選｜"
               f"{sum(len(order.items) for order in batch.orders)} 筆明細候選；筆數不是成品桌總數。")
    by_id = {line.line_id: line for line in batch.lines}
    with st.expander("完整來源與行分類", expanded=not batch.orders):
        st.dataframe([{"行號": line.number, "原文": line.raw, "分類": KIND_LABELS[line.kind],
                       "類別線索": line.family_hint} for line in batch.lines],
                     hide_index=True, use_container_width=True)
    outside = [by_id[key] for key in batch.unassigned_line_ids if by_id[key].kind not in ("blank", "section")]
    if outside:
        st.warning(f"有 {len(outside)} 行尚未歸屬工單；保留待核對，不會自動套用到所有工單。")
        st.dataframe([{"行號": line.number, "原文": line.raw} for line in outside], hide_index=True)
    if batch.orders:
        selected = st.selectbox("選擇工單候選", [order.order_id for order in batch.orders],
                                format_func=lambda key: next(
                                    f"第 {by_id[order.header_line_id].number} 行｜{by_id[order.header_line_id].raw}"
                                    for order in batch.orders if order.order_id == key),
                                key=f"work_order_selected_{batch.batch_id}_{batch.revision}")
        order = next(order for order in batch.orders if order.order_id == selected)
        st.text(f"產品類別線索：{order.family_hint or '未指定'}（尚未對應正式產品主檔）")
        for warning in order.warnings:
            st.warning(warning)
        st.dataframe([{"來源行": by_id[item.source_line_id].number,
                       "明細原文": by_id[item.source_line_id].raw,
                       "數量原文": item.quantity_raw or "未辨識",
                       "數量候選（未核准）": str(item.quantity_candidate) if item.quantity_candidate is not None else "待確認",
                       "明細識別": item.item_id} for item in order.items],
                     hide_index=True, use_container_width=True)
        st.caption("標題與補充內容仍可能含規格；不因列在下方就視為不計價備註。")
        for key in order.context_line_ids:
            line = by_id[key]
            if line.raw.strip():
                st.text(f"{line.number}. {line.raw}")
    else:
        st.info("未辨識到工單起始標題；可在下方將正確來源行改為工單標題。")

    st.markdown("**人工修正行分類／工單邊界**")
    st.caption("改成工單標題可拆單；將多餘工單標題改成補充條件可合入前單。分類標題會中斷前單，修正時請核對所有受影響來源行。")
    line_id = st.selectbox("待修正來源行", [line.line_id for line in batch.lines if line.raw.strip()],
                           format_func=lambda key: f"{by_id[key].number}. {by_id[key].raw[:100]}",
                           key=f"work_order_line_{batch.batch_id}")
    source = by_id[line_id]
    choices = [kind for kind in KIND_LABELS if kind != "blank"]
    with st.form(f"work_order_correction_{batch.batch_id}_{batch.revision}_{line_id}"):
        kind = st.selectbox("修正分類", choices, index=choices.index(source.kind), format_func=KIND_LABELS.get)
        family = st.selectbox("類別線索（只限分類／工單標題）", ("", *FAMILIES),
                              index=("", *FAMILIES).index(source.family_hint),
                              format_func=lambda value: value or "未指定／沿用前方分類標題")
        actor = st.text_input("修正人（自填紀錄，非登入認證）", max_chars=80)
        reason = st.text_input("修正理由（不代表工程核准）", max_chars=1000)
        if st.form_submit_button("套用分類修正"):
            try:
                st.session_state["work_order_batch"] = reclassify_line(
                    batch, line_id=line_id, kind=kind, family_hint=family,
                    expected_revision=batch.revision, actor=actor, reason=reason,
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.rerun()
    if batch.corrections:
        with st.expander("分類修正紀錄（不是規格核准）"):
            st.dataframe([asdict(correction) for correction in batch.corrections], hide_index=True)
    st.download_button("下載核對草稿（含原文，非報價單）",
                       json.dumps(export_review(batch), ensure_ascii=False, indent=2),
                       file_name=f"work-order-review-{batch.batch_id}.json", mime="application/json")
