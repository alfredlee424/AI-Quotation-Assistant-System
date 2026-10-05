"""明確同意及按鈕才試算；重繪只驗證固定結果，無正式保存入口。"""
import json

from agent.multi_quote import apply_multi_command
from config import MAX_DISCOUNT_RATE
from engine.quote_batch_trial import WARNING, build_batch_trial, checked_batch_trial

STATE_KEY = "quote_batch_trials"


def clear_batch_trial(st, draft_id):
    from utils.quote_batch_review_ui import clear_batch_review
    clear_batch_review(st, draft_id)
    stored = dict(st.session_state.get(STATE_KEY, {}))
    stored.pop(draft_id, None)
    st.session_state[STATE_KEY] = stored


def render_batch_trial(st, draft, batch, actor):
    st.warning(WARNING)
    st.caption("每張独立折扣、稅及捨入，批次只加總已捨入金額；不沿用舊整單試算。最多 200 張內部計算；來源展開契約上限仍為 999，超量整批拒絕而不截斷。")
    document = st.session_state.get(STATE_KEY, {}).get(draft.draft_id)
    if document is not None:
        try:
            document = checked_batch_trial(draft, document, actor=actor, source_batch=batch)
        except Exception:
            clear_batch_trial(st, draft.draft_id)
            document = None
            st.info("來源、草稿、操作人、政策或內容已變更，舊批次金額已清除。")
    prefix = f"batch_trial_{draft.draft_id}_{draft.revision}"
    with st.form(prefix + "_discount"):
        discount = st.number_input("批次共同折扣率（0.1 表示每張九折）", min_value=0.0,
            max_value=float(min(MAX_DISCOUNT_RATE, 1)), value=float(draft.discount_rate))
        reason = st.text_input("批次共同折扣調整理由", max_chars=1000)
        if st.form_submit_button("設定批次共同折扣（不試算）"):
            try:
                updated = apply_multi_command(draft, {"op": "set_discount", "discount_rate": discount},
                    draft_id=draft.draft_id, expected_revision=draft.revision,
                    actor=actor, reason=reason, source_batch=batch)
            except (ValueError, TypeError) as exc:
                st.error(str(exc))
            else:
                clear_batch_trial(st, draft.draft_id)
                return updated
    consent = st.checkbox("了解批次逐張試算僅供非正式內部核對，不可向客戶確認", key=prefix + "_consent")
    if st.button("產生批次逐張獨立金額試算（唯讀主檔）", key=prefix + "_run", disabled=not consent or not actor.strip()):
        clear_batch_trial(st, draft.draft_id)
        document = None
        try:
            document = build_batch_trial(draft, actor=actor, source_batch=batch)
        except (ValueError, TypeError) as exc:
            st.error(str(exc))
        except Exception:
            st.error("批次逐張試算失敗，舊成功金額已清除；未保存正式報價。")
        else:
            stored = dict(st.session_state.get(STATE_KEY, {}))
            stored[draft.draft_id] = document
            st.session_state[STATE_KEY] = stored
    if document is None:
        st.caption("展開／重繪不查價；必須勾選非正式用途及按試算按鈕才讀主檔。")
        return None
    st.text(f"批次 {document['quote_batch_id']}｜固定試算 {document['trial_id']}｜已試算 {document['calculated_count']}／{document['child_count']} 張")
    st.warning("僅保留本次讀取主檔結果，不保證跨查詢一致性或對客戶鎖價；展示／下載不重算，程序重啟後須重新試算。")
    for child in document["children"]:
        st.markdown(f"**非正式子報價 {child['display_order']}｜{child['label']}｜成品數 {child['product_qty']}**")
        st.text(f"來源單號 {child['source_ref_raw']}｜來源品項 {child['source_item_id']}｜子報價 {child['child_quote_id']}")
        for source in child["description_sources"]:
            st.text(f"來源第 {source['number']} 行｜{source['raw']}")
        for description in child["descriptions"]:
            st.text(f"{description['kind']}｜{description['scope_status']}｜{description['text']}")
        for blocker in child["blockers"]:
            st.warning(blocker)
        calc = child["calculation"]
        if calc is not None:
            st.dataframe([{"成品數": child["product_qty"], "產品係數": calc["quote_rate"],
                "折扣率": calc["discount_rate"], "稅率": calc["tax_rate"], "成本": calc["total_cost"],
                "未稅": calc["after_discount"], "稅額": calc["tax_amount"], "含稅": calc["total_price"]}], hide_index=True)
            st.dataframe(calc["items"], hide_index=True)
            st.text("單張捨入差額（不湊數）：" + json.dumps(calc["rounding_differences"], ensure_ascii=False))
        with st.expander(f"完整非正式來源／配置／條件／圖面／公式｜{child['child_quote_id']}"):
            st.json(child)
        # 整份文件在本次渲染開始已驗證；下載與展示使用同一固定內容，不再查價。
        st.download_button("下載此張非正式金額核對（非報價單）", json.dumps(child, ensure_ascii=False, indent=2, allow_nan=False),
            file_name=f"nonformal-child-trial-{child['child_quote_id']}-{document['trial_id']}.json",
            mime="application/json", key=prefix + child["child_quote_id"])
    if document["totals"] is None:
        st.error("未完成整批試算：保留全部子報價及原因，不提供完整批次總計。")
    else:
        st.markdown("**非正式批次總計：僅十進位加總各張已捨入金額**")
        st.json(document["totals"])
        st.caption("批次不再次折扣、課稅或分配差額；總未稅＋總稅與總含稅可能依單品政策不相等，差額已列示。")
    with st.expander("仍保留的正式核准關卡"):
        for blocker in document["formal_blockers"]:
            st.warning(blocker)
    st.download_button("下載整批非正式金額核對（非報價單）", json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False),
        file_name=f"nonformal-batch-trial-{document['trial_id']}.json", mime="application/json", key=prefix + "_download")
    return None
