"""工單唯讀數值診斷，不確認單位、不修改來源或報價草稿。"""
import json

from agent.work_order_diagnostics import diagnose_work_order


KIND_LABELS = {
    "table_dimensions_candidate": "桌面尺寸候選", "accessory_dimensions_candidate": "五金尺寸候選",
    "panel_dimensions_candidate": "分片尺寸候選", "three_axis_dimensions": "三軸尺寸待確認",
    "panel_count_candidate": "每件分片數候選", "false_thickness": "假厚（非基板實厚）",
    "board_thickness": "板材實厚候選", "hole_diameter_candidate": "孔徑候選", "diameter": "直徑",
    "circle_diameter_candidate": "圓形直徑候選", "compatible_table_size": "適用桌面尺數",
    "accessory_dimension_candidate": "鋁件／配件尺寸候選", "length": "單一長度",
    "circle_segment": "圓形分片比例", "hole_count": "孔數", "component_count": "部件數量",
    "item_count": "項目數量", "row_quantity_candidate": "來源明細數量候選", "geometry_formula": "幾何算式待確認",
}


def render_work_order_diagnostics(st, batch):
    with st.expander("尺寸、數量與簡寫診斷（唯讀，不套用配置）"):
        result = diagnose_work_order(batch)
        st.caption("只換算明確的公分／毫米。無單位、台尺慣例與不明用途保留待確認；正規化成功不等於可報價。")
        st.dataframe([{"來源行": item["line_number"], "原文": item["raw"],
                       "用途": KIND_LABELS.get(item["kind"], item["kind"]),
                       "原始數值": " × ".join(item["values"]), "單位": "／".join(item["units"]),
                       "正規化公分": " × ".join(item["centimeters"]) if item["centimeters"] is not None else "待確認／不適用",
                       "數量範圍": item["scope"], "說明": item["note"]} for item in result["findings"]],
                     hide_index=True, use_container_width=True)
        if result["issues"]:
            st.warning(f"有 {len(result['issues'])} 個待確認提示；不會自動改正尺寸、補單位或計算報價。")
            st.dataframe([{"來源行": issue["line_number"], "原文": issue["raw"], "提示": issue["message"]}
                          for issue in result["issues"]], hide_index=True, use_container_width=True)
        else:
            st.info("未命中本階段診斷規則，不代表需求完整或已獲核准。")
        st.download_button("下載尺寸數量診斷（含來源片段，非報價）",
                           json.dumps(result, ensure_ascii=False, indent=2),
                           file_name=f"work-order-diagnostics-{batch.batch_id}.json", mime="application/json")
