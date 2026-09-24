"""來源片段、選配對照與人工依據紀錄；不允許解除正式報價關卡。"""
from agent.requirement_review import apply_review_command, requirement_report, source_rows


DISPOSITION_LABELS = {
    "specification": "對應正式選配", "included_process": "標準含價工序（需依據）",
    "production_note": "生產備註（不等於免費）", "metadata": "來源識別資訊",
    "engineering_review": "非標／待工程核定",
}


def render_requirement_review(st, draft, batch, actor):
    """回傳有效的新版本或 None；呼叫端統一更新工作階段。"""
    report = requirement_report(draft)
    st.markdown("**來源需求帳本與人工核對**")
    st.caption("已有人工處置紀錄不代表正式工程核准；不會解除整單計價／保存的未完成關卡。")
    if not report["source_available"]:
        st.info("此草稿沒有可逐句追溯的工單原文；不能視為需求已完整。單品轉接的來源問題仍保留。")
        return None

    def apply(command):
        try:
            return apply_review_command(draft, command, draft_id=draft.draft_id,
                                         expected_revision=draft.revision, actor=actor,
                                         reason="人工需求處置／問題核對", source_batch=batch)
        except ValueError as exc:
            st.error(str(exc))
        except Exception:
            st.error("來源或主檔驗證失敗，需求紀錄未修改。")
        return None

    if not draft.requirements:
        if st.button("建立來源需求帳本", key=f"requirements_init_{draft.draft_id}"):
            return apply({"op": "initialize"})
        return None
    rows = {row["line_id"]: row for row in source_rows(draft)}
    names = {line.line_id: line.label for line in (*draft.lines, *draft.archived_lines)}
    st.caption(f"需求片段 {len(draft.requirements)}｜待處置／重新核對 {len(report['pending_requirement_ids'])}｜"
               f"來源片段覆蓋：{'完整' if report['source_coverage_complete'] else '不完整'}（不代表語意完整）")
    st.dataframe([{"來源行": rows.get(record.source_line_id, {}).get("number", "未知"),
                   "原文片段": record.text, "狀態": record.status,
                   "處置": DISPOSITION_LABELS.get(record.disposition, "待核對"),
                   "目標明細": "、".join(names.get(key, "已移除") for key in record.targets)}
                  for record in draft.requirements], hide_index=True, use_container_width=True)
    ids = [record.requirement_id for record in draft.requirements]
    selected = st.selectbox("待處置需求片段", ids,
                            format_func=lambda key: next(f"行 {rows.get(r.source_line_id, {}).get('number', '?')}｜{r.text[:80]}"
                                                         for r in draft.requirements if r.requirement_id == key),
                            key=f"requirement_pick_{draft.draft_id}_{draft.revision}")
    record = next(record for record in draft.requirements if record.requirement_id == selected)
    st.text(record.text)
    if record.evidence:
        with st.expander("此片段的人工依據歷史"):
            st.dataframe([{"操作人": e.actor, "依據／版本": e.reference, "說明": e.explanation,
                           "時間": e.recorded_at} for e in record.evidence], hide_index=True)
    with st.form(f"requirement_record_{draft.draft_id}_{draft.revision}_{selected}"):
        disposition = st.selectbox("需求處置", list(DISPOSITION_LABELS), format_func=DISPOSITION_LABELS.get)
        targets = st.multiselect("需求適用明細", [line.line_id for line in draft.lines],
                                 default=[key for key in record.targets if key in {line.line_id for line in draft.lines}],
                                 format_func=names.get)
        options = [(line.line_id, path) for line in draft.lines for path, value in line.configuration.get("selections", {}).items() if value.get("code")]
        bindings = st.multiselect("已選部位對照（須包含每個規格目標）", options,
                                  format_func=lambda value: f"{names[value[0]]}｜{value[1]}")
        reference = st.text_input("依據識別與版本（不會自動讀取文件）", max_chars=500)
        explanation = st.text_area("處置理由／核對說明", max_chars=2000)
        if st.form_submit_button("保存人工處置紀錄（非正式核准）"):
            return apply({"op": "record", "requirement_id": selected, "disposition": disposition,
                          "targets": targets, "bindings": [{"line_id": key, "path": path} for key, path in bindings],
                          "reference": reference, "explanation": explanation})
    if len(record.text) > 1:
        with st.form(f"requirement_split_{draft.draft_id}_{draft.revision}_{selected}"):
            offset = st.number_input("從第幾個字元後拆成兩段（含空白及標點）", min_value=1, max_value=len(record.text)-1, value=1)
            st.caption("拆分後兩段重新核對，原處置留在封存紀錄；不自動判定語意。")
            if st.form_submit_button("拆分需求片段"):
                return apply({"op": "split", "requirement_id": selected, "offset": offset})
    if record.status != "UNREVIEWED" and st.button("將此需求重新列為待核對", key=f"requirement_reopen_{selected}_{draft.revision}"):
        return apply({"op": "reopen", "requirement_id": selected})
    with st.expander("選配對應哪些來源需求"):
        st.dataframe(report["bindings"], hide_index=True, use_container_width=True)
        st.caption("尚無有效來源對照的已選規格（含自動補齊項目；不代表可以省略核對）")
        st.dataframe(report["unmapped_selections"], hide_index=True, use_container_width=True)
    questions = [question for question in draft.questions if question.status == "OPEN"]
    if questions:
        st.markdown("**有依據的 AI 問題結案（不解除來源／報價關卡）**")
        question_id = st.selectbox("待結案 AI 問題", [q.question_id for q in questions],
                                    format_func=lambda key: next(f"{names.get(q.target_id, '整單')}｜{q.text}" for q in questions if q.question_id == key),
                                    key=f"question_resolve_{draft.draft_id}_{draft.revision}")
        with st.form(f"question_resolution_{draft.draft_id}_{draft.revision}_{question_id}"):
            reviewed = [r.requirement_id for r in draft.requirements if r.status == "RECORDED"]
            links = st.multiselect("引用已處置需求", reviewed,
                                   format_func=lambda key: next(r.text for r in draft.requirements if r.requirement_id == key))
            reference = st.text_input("結案依據識別與版本", max_chars=500)
            answer = st.text_area("具體回答與核對理由", max_chars=2000)
            if st.form_submit_button("記錄問題結案（非工程核准）"):
                return apply({"op": "resolve_question", "question_id": question_id, "requirement_ids": links,
                              "reference": reference, "answer": answer})
    return None
