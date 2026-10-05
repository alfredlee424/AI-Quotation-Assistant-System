"""非正式批次展開的明確操作與唯讀下載；不查主檔或保存報價。"""
import json

from agent.quote_batch import WARNING, build_quote_batch, checked_quote_batch


STATE_KEY = "quote_batch_expansions"


def clear_quote_batch(st, draft_id):
    stored = dict(st.session_state.get(STATE_KEY, {}))
    stored.pop(draft_id, None)
    st.session_state[STATE_KEY] = stored


def render_quote_batch(st, draft, batch, actor):
    st.warning(WARNING)
    st.caption("一來源工單 → 一批次 → 按品項多張獨立子報價；不是單張多產品，也不按件數拆單。舊整單試算不是新批次金額。")
    stored = dict(st.session_state.get(STATE_KEY, {}))
    document = stored.get(draft.draft_id)
    if document is not None:
        try:
            document = checked_quote_batch(draft, document, actor=actor, source_batch=batch)
        except Exception:
            clear_quote_batch(st, draft.draft_id)
            document = None
            st.info("草稿、來源、操作人、內容或契約版本變更，舊展開已清除。")
    if draft.source.get("kind") != "work_order":
        st.info("本區只接受同一工單轉接草稿，不接受手動或單品轉接。")
        return
    if st.button("產生批次逐張展開核對（不配號、不計價）", key=f"quote_batch_run_{draft.draft_id}", disabled=not actor.strip()):
        clear_quote_batch(st, draft.draft_id)
        document = None
        try:
            document = build_quote_batch(draft, actor=actor, source_batch=batch)
        except (ValueError, TypeError) as exc:
            st.error(str(exc))
        except Exception:
            st.error("展開核對失敗，舊結果已清除；本次未修改草稿或保存報價。")
        else:
            stored = dict(st.session_state.get(STATE_KEY, {}))
            stored[draft.draft_id] = document
            st.session_state[STATE_KEY] = stored
    if document is None:
        return
    st.text(f"批次識別 {document['quote_batch_id']}｜來源單號 {document['source_ref_raw']}｜共 {len(document['children'])} 張（未配號）")
    if not document["expansion_review_complete"]:
        st.warning("展開核對仍有待確認項目；不得當成完整正式報價。")
    for issue in document["issues"]:
        st.warning(issue["message"])
    for child in document["children"]:
        st.markdown(f"#### 第 {child['display_order']} 張：{child['label']}")
        st.text(f"子報價識別 {child['child_quote_id']}｜來源品項 {child['source_item_id']}")
        st.text(f"來源單號 {child['source_ref_raw']}｜來源行 {child['source_line']['number']}｜成品數 {child['product_qty'] if child['product_qty'] is not None else '待確認'}")
        st.caption(f"未來三位尾碼位置 {child['future_suffix_position']}（未配號）；成本列五位序號屬各張獨立範圍，本區不產生成本列。")
        st.text(child["source_line"]["raw"])
        st.caption("以下完整來源行供逐張追溯；同一行內片段的適用範圍仍依核對依據分開，不代表整行加工均適用。")
        for source in child["description_sources"]:
            st.text(source["raw"])
        for description in child["descriptions"]:
            st.caption(f"來源行 {description['source']['number']}｜字元 [{description['start']}, {description['end']})｜範圍 {description['scope_status']}")
        with st.expander(f"第 {child['display_order']} 張來源座標、範圍及完整核對依據"):
            st.json({key: child[key] for key in ("descriptions", "conditions", "effective_conditions", "drawings", "blockers")})
        st.download_button(f"下載第 {child['display_order']} 張描述（非正式）",
                           json.dumps(child, ensure_ascii=False, indent=2, allow_nan=False),
                           file_name=f"child-description-{child['child_quote_id']}.json", mime="application/json",
                           key=f"child_download_{child['child_quote_id']}")
    st.caption("下載含來源原文及自填核對依據，請依內部資料權限保管；不可匯入為正式報價。")
    st.download_button("下載整批展開核對（非正式、無金額）",
                       json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False),
                       file_name=f"quote-batch-review-{document['quote_batch_id']}.json", mime="application/json")
