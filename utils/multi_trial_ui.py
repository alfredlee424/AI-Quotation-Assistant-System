"""多明細內部試算介面：不提供確認／正式保存，只保存獨立核對結果。"""
import json

from agent.multi_quote import apply_multi_command
from config import MAX_DISCOUNT_RATE
from engine.multi_trial import build_internal_trial, checked_internal_trial


def render_multi_trial(st, draft, batch, actor):
    st.warning("僅供已配置標準規格的內部成本核對，不是完整工單報價。來源、含價及工程核准關卡仍保留。")
    stored = dict(st.session_state.get("multi_internal_trials", {}))
    document = stored.get(draft.draft_id)
    if document is not None:
        try:
            document = checked_internal_trial(draft, document, actor=actor, source_batch=batch)
        except (ValueError, TypeError):
            stored.pop(draft.draft_id, None)
            st.session_state["multi_internal_trials"] = stored
            document = None
            st.info("草稿、操作人或規則已變更，舊內部試算已清除。")
    prefix = f"multi_trial_{draft.draft_id}_{draft.revision}"
    with st.form(prefix + "_discount"):
        discount = st.number_input("整單折扣率（0.1 表示九折）", min_value=0.0,
                                   max_value=float(min(MAX_DISCOUNT_RATE, 1)), value=float(draft.discount_rate))
        reason = st.text_input("整單折扣調整理由", max_chars=1000)
        if st.form_submit_button("保存整單折扣（不試算）"):
            try:
                return apply_multi_command(draft, {"op": "set_discount", "discount_rate": discount},
                                           draft_id=draft.draft_id, expected_revision=draft.revision,
                                           actor=actor, reason=reason, source_batch=batch)
            except (ValueError, TypeError) as exc:
                st.error(str(exc))
    consent = st.checkbox("了解僅供內部核對，不可作為客戶報價", key=prefix + "_consent")
    if st.button("產生多明細內部試算（唯讀主檔）", key=prefix + "_run", disabled=not consent):
        stored.pop(draft.draft_id, None)
        st.session_state["multi_internal_trials"] = stored
        document = None
        try:
            document = build_internal_trial(draft, actor=actor, source_batch=batch)
        except (ValueError, TypeError) as exc:
            st.error(str(exc))
        except Exception:
            st.error("內部試算失敗，未保留舊結果，請重新檢查來源與主檔。")
        else:
            stored[draft.draft_id] = document
            st.session_state["multi_internal_trials"] = stored
    from utils.multi_snapshot_ui import render_snapshot_layout
    if document is None:
        render_snapshot_layout(st, draft, None, batch, actor)
        st.caption("明確執行才讀主檔；本區不建立正式預覽、報價單號或資料庫快照。")
        return None
    st.caption(f"試算時間 {document['created_at']}｜草稿版本 {document['draft_revision']}｜{document['calculated_count']}／{document['active_count']} 筆已試算")
    st.warning("主檔不會自動重查，金額只是本次讀取結果，不保證跨查詢快照一致性。需要目前金額時須重新試算。")
    st.dataframe([{"明細識別": row["line_id"], "名稱": row["label"], "產品": row["prodkind"] or "未指定",
                   "數量": str(row["qty"]), "狀態": "內部已試算" if row["calculation"] else "未試算",
                   "成本": row["calculation"]["total_cost"] if row["calculation"] else None,
                   "產品報價係數": row["calculation"]["quote_rate"] if row["calculation"] else None,
                   "未折扣未稅小計": row["calculation"]["subtotal"] if row["calculation"] else None}
                  for row in document["lines"]], hide_index=True, use_container_width=True)
    for row in document["lines"]:
        with st.expander(f"內部試算明細｜{row['label']}｜{row['line_id'][:8]}"):
            for issue in row["issues"]:
                st.warning(issue)
            if row["calculation"]:
                st.dataframe(row["calculation"]["items"], hide_index=True, use_container_width=True)
    if document["totals"] is None:
        st.error("未完成整單試算：存在未試算或封存明細，不提供整單合計。")
    else:
        st.markdown("**全部活動選配的內部合計（非正式工單報價）**")
        total = document["totals"]
        st.dataframe([{"成本合計": total["total_cost"], "加成後小計": total["subtotal"],
                       "整單折扣率": total["discount_rate"], "折扣金額": total["discount_amount"],
                       "折扣後未稅": total["after_discount"], "稅率": total["tax_rate"],
                       "稅額": total["tax_amount"], "內部含稅合計": total["total_price"]}], hide_index=True)
        st.caption(f"逐筆展示小計加總與未捨入彙總後的小計差額：{total['display_subtotal_difference']:.2f}。折扣與稅僅於整單套用一次。")
    with st.expander("仍未解除的整單阻擋與需求核對"):
        for blocker in document["order_blockers"]:
            st.warning(blocker)
        st.json({"requirements": document["requirements"], "conditions": document["conditions"],
                 "archived_line_ids": document["archived_line_ids"]})
    st.caption("下載包含內部成本、用量與需求核對識別；不可交作正式報價，請依公司資料政策保存。")
    st.download_button("下載內部試算核對（非報價單）", json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False),
                       file_name=f"internal-trial-{document['trial_id']}.json", mime="application/json")
    with st.expander("多明細快照關聯演練（不可正式保存）", expanded=False):
        render_snapshot_layout(st, draft, document, batch, actor)
    return None
