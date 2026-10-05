"""獨立非正式整批固定內容與兩步核對；不觸發試算、配號或保存。"""
import json

from engine.quote_batch_review import BatchReviewSession, WARNING

STATE_KEY = "quote_batch_reviews"
SESSION_KEY = "quote_batch_review_sessions"
CREATE = "建立整批固定核對（非正式）"
FIRST = "記錄第一次整批核對（非正式）"
SECOND = "記錄第二次整批核對（非正式、非開立）"
DOWNLOAD = "下載整批固定核對與歷史（非正式、非報價單）"


def clear_batch_review(st, draft_id):
    sessions = dict(st.session_state.get(SESSION_KEY, {}))
    session = sessions.pop(draft_id, None)
    if session is not None:
        session.invalidate()
    documents = dict(st.session_state.get(STATE_KEY, {}))
    documents.pop(draft_id, None)
    st.session_state[SESSION_KEY] = sessions
    st.session_state[STATE_KEY] = documents


def _store(st, draft_id, document, session):
    st.session_state[STATE_KEY] = {**st.session_state.get(STATE_KEY, {}), draft_id: document}
    st.session_state[SESSION_KEY] = {**st.session_state.get(SESSION_KEY, {}), draft_id: session}


def render_batch_review(st, draft, batch, actor):
    st.warning(WARNING)
    if st.session_state.get("batch_review_error") == draft.draft_id:
        st.session_state.pop("batch_review_error")
        st.error("核對操作失敗；舊成功文件、兩次核對狀態與下載已清除，未開立或保存。")
    st.caption("使用目前完整逐張試算，固定全部來源、成本、用量與金額。兩次均由同一自填操作人核對，不是兩位工程簽核；手填依據不能解除正式待辦。")
    trial = st.session_state.get("quote_batch_trials", {}).get(draft.draft_id)
    document = st.session_state.get(STATE_KEY, {}).get(draft.draft_id)
    session = st.session_state.get(SESSION_KEY, {}).get(draft.draft_id)
    if document is not None:
        try:
            document = session.checked(document, draft, trial, actor=actor, source_batch=batch)
        except Exception:
            clear_batch_review(st, draft.draft_id)
            document = None
            st.info("目前來源、草稿、試算、政策、操作人或文件已失效；舊固定核對及兩次 token 已清除，改回不復活。")
    if st.button(CREATE, key=f"batch_review_create_{draft.draft_id}", disabled=not actor.strip()):
        clear_batch_review(st, draft.draft_id)
        document = None
        if trial is None:
            st.error("沒有目前有效完整逐張試算，請返回批次逐張試算；本操作不會自動查價。")
            return
        try:
            session = BatchReviewSession()
            document = session.freeze(draft, trial, actor=actor, source_batch=batch)
            _store(st, draft.draft_id, document, session)
        except Exception:
            clear_batch_review(st, draft.draft_id)
            st.error("無法建立固定核對：請返回有效完整逐張試算，核對全部來源與阻擋；舊核對已清除。")
            return
    if document is None:
        st.caption("尚無固定核對文件；只有按建立才固定內容，不自動試算、不產生保留請求。")
        return
    # 展示與下載同次驗證當前上下文，不重建 UUID／時間、不重新計價。
    try:
        payload = session.download(document, draft, trial, actor=actor, source_batch=batch)
    except Exception:
        clear_batch_review(st, draft.draft_id)
        st.error("固定核對驗證失敗；舊內容、核對狀態及下載已清除。")
        return
    content, state = document["content"], document["review_state"]
    fixed = content["trial"]
    st.text(f"固定核對 {content['review_id']}｜目前試算 {fixed['trial_id']}｜內容摘要 {document['content_digest']}")
    st.text(f"核對狀態摘要 {document['state_digest']}｜已記錄 {state['stage']}／2 次非正式核對")
    for child in fixed["children"]:
        st.markdown(f"**固定非正式第 {child['display_order']} 張｜{child['label']}｜成品數 {child['product_qty']}**")
        st.text(f"來源 {child['source_ref_raw']}｜品項 {child['source_item_id']}｜子報價識別 {child['child_quote_id']}")
        for description in child["descriptions"]:
            st.text(f"固定描述｜{description['kind']}｜來源第 {description['source']['number']} 行｜{description['text']}")
        calc = child["calculation"]
        st.dataframe([{key: calc[key] for key in ("total_cost", "subtotal", "discount_amount", "after_discount", "tax_amount", "total_price")}], hide_index=True)
        st.dataframe(calc["items"], hide_index=True)
        st.text("固定逐張差額（不湊數）：" + json.dumps(calc["rounding_differences"], ensure_ascii=False))
        with st.expander(f"固定完整配置／成本用量／需求／條件／圖面｜{child['child_quote_id']}"):
            st.json(child)
    st.markdown("**固定非正式批次總計／差額：不再次折扣、課稅或重算**")
    st.json(fixed["totals"])
    with st.expander("固定整份來源、草稿及證據／政策版本"):
        st.json(content["source_batch"])
        st.json(fixed["trial_context"])
    st.markdown("**正式關卡仍全部待辦；內部全部算出不等於可報價**")
    for gate in content["gate_report"]["formal"]:
        st.warning(f"PENDING｜{gate['reason']}")
    with st.expander("完整可追溯關卡報告（含原始正式阻擋）"):
        st.json(content["gate_report"])
    st.json(state["history"])
    prefix = f"batch_review_{content['review_id']}_{state['stage']}"
    first = st.button(FIRST, key=prefix + "_first", disabled=state["stage"] != 0)
    second = st.button(SECOND, key=prefix + "_second", disabled=state["stage"] != 1)
    if first or second:
        try:
            document = session.record(document, draft, trial, step=1 if first else 2,
                                      token=state["next_token"], actor=actor, source_batch=batch)
            _store(st, draft.draft_id, document, session)
        except Exception:
            clear_batch_review(st, draft.draft_id)
            # 重繪清除本輪先前已展示的內容及成功狀態，錯誤在下一輪顯示。
            st.session_state["batch_review_error"] = draft.draft_id
        st.rerun()
    if state["stage"] == 2:
        st.info("兩次非正式內容核對已記錄；仍非客戶報價、非工程核准、非開立，不可正式確認或保存。")
    st.download_button(DOWNLOAD, payload,
        file_name=f"nonformal-batch-review-{content['review_id']}.json", mime="application/json", key=prefix + "_download")
