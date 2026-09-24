"""AI 提案的明確範圍、人工核對與套用；不直接計價或保存報價。"""
import config
from agent.multi_quote_agent import commit_multi_proposal, prepare_multi_proposal


def render_multi_quote_ai(st, draft, batch, actor):
    """回傳已核對的新草稿或 None；所有暫存依草稿識別分開保存。"""
    st.markdown("**AI 跨明細配置提案（先核對、後套用）**")
    st.caption("只傳本次選取明細的名稱、配置、待確認問題及合法選單，不傳來源快照的成本與用量欄位。請勿在需求文字貼入不必要的客戶資料或價格。")
    st.caption("AI 只能新增追問，不能結案；有依據的人工結案須在需求帳本操作，亦不代表工程核准。")
    pending_key = f"multi_ai_pending_{draft.draft_id}"
    if not config.USE_LLM:
        st.session_state.pop(pending_key, None)
        st.info("目前未啟用 LLM，可繼續使用下方手動選配。")
        return None
    labels = {line.line_id: f"第 {index} 筆｜{line.label}｜{line.line_id[:8]}" for index, line in enumerate(draft.lines, 1)}
    scope_key = f"multi_ai_scope_{draft.draft_id}"
    if scope_key in st.session_state:
        st.session_state[scope_key] = [key for key in st.session_state[scope_key] if key in labels]
    scope = tuple(st.multiselect("允許 AI 修改的明細（不代表全部套同規格）", list(labels),
                                 format_func=labels.get, key=scope_key))
    request = st.text_area("本次跨明細需求", max_chars=4000, key=f"multi_ai_request_{draft.draft_id}",
                          placeholder="例如：第1筆改成3件，第2筆每件增加2個6公分圓孔；其餘不變。")
    prepared = st.session_state.get(pending_key)
    if prepared is not None and (prepared.request != request or prepared.allowed_line_ids != scope or
                                 prepared.actor != actor or prepared.proposal["revision"] != draft.revision):
        st.session_state.pop(pending_key, None)
        prepared = None
        st.info("需求、範圍、操作人或草稿版本已變更，舊提案已清除。")
    if st.button("產生 AI 配置提案（不修改草稿）", key=f"multi_ai_prepare_{draft.draft_id}"):
        st.session_state.pop(pending_key, None)
        prepared = None
        try:
            with st.spinner("理解指定明細需求並驗證整批配置…"):
                prepared = prepare_multi_proposal(draft, request, allowed_line_ids=scope, actor=actor, source_batch=batch)
        except ValueError as exc:
            st.error(str(exc))
        except Exception:
            st.error("AI 或主檔服務暫時失敗，草稿未變更，舊提案已清除。")
        else:
            st.session_state[pending_key] = prepared
    if prepared is None:
        return None
    st.warning("以下只是配置變更核對，不是報價預覽。請核對每筆目標、數量、分支移除及追問。")
    before = {line["line_id"]: line for line in prepared.before["lines"]}
    for line in prepared.after["lines"]:
        if before[line["line_id"]] != line:
            with st.expander(f"變更：{labels.get(line['line_id'], line['line_id'])}", expanded=True):
                st.write("變更前")
                st.json(before[line["line_id"]])
                st.write("變更後（未核准）")
                st.json(line)
    if prepared.proposal["questions"]:
        st.write("本次新增追問（套用後保存）")
        st.json(prepared.proposal["questions"])
    if st.button("套用已驗證配置提案（非報價）", key=f"multi_ai_commit_{draft.draft_id}"):
        st.session_state.pop(pending_key, None)
        try:
            return commit_multi_proposal(draft, prepared, source_batch=batch)
        except ValueError as exc:
            st.error(str(exc))
        except Exception:
            st.error("重新驗證失敗，整批配置未修改，請重新產生提案。")
    return None
