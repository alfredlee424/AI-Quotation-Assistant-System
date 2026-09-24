"""多明細配置管理；主檔讀取須由操作者開啟，不呼叫計價／保存服務。"""
import json

from agent.multi_quote import (
    apply_multi_command, check_source_current, export_multi_draft,
    from_single_quote, from_work_order, new_multi_quote,
)


def _store(st, draft):
    drafts = dict(st.session_state.get("multi_quote_drafts", {}))
    drafts[draft.draft_id] = draft
    st.session_state["multi_quote_drafts"] = drafts


def render_multi_quote_workspace(st) -> None:
    st.subheader("多明細配置草稿（不計價）")
    st.warning("各明細可分別選配，但尚未檢查整單需求覆蓋、成本及圖面；本區不能建立正式報價。")
    st.caption("草稿只存於工作階段，下載可保留紀錄但尚不支援重新匯入。轉接會新增草稿，不取代單品或工單來源。")

    def create(factory):
        try:
            draft = factory()
        except ValueError as exc:
            st.error(str(exc))
        else:
            _store(st, draft)
            st.session_state["multi_selected"] = draft.draft_id
            st.rerun()

    if st.button("新增空白多明細草稿", key="multi_new"):
        create(new_multi_quote)
    single = st.session_state.get("current_quote")
    if single is not None and st.button("複製目前單品為新配置草稿", key="multi_from_single"):
        create(lambda: from_single_quote(single))
    batch = st.session_state.get("work_order_batch")
    if batch is not None and batch.orders:
        source_order = st.selectbox(
            "來源工單", [order.order_id for order in batch.orders],
            format_func=lambda key: next(f"{order.source_ref_raw or '人工工單'}｜{order.family_hint or '未指定類別'}"
                                         for order in batch.orders if order.order_id == key),
            key=f"multi_source_{batch.batch_id}_{batch.revision}",
        )
        if st.button("轉接此工單為新配置草稿", key="multi_from_order"):
            create(lambda: from_work_order(batch, source_order))
    drafts = st.session_state.get("multi_quote_drafts", {})
    if not drafts:
        st.info("請新增空白草稿，或從目前單品／工單核對結果轉接。")
        return
    selected = st.selectbox("選擇配置草稿", list(drafts), key="multi_selected",
                            format_func=lambda key: f"{key[:8]}｜{drafts[key].source.get('source_ref_raw') or drafts[key].source['kind']}")
    draft = drafts[selected]
    st.caption(f"整單版本 {draft.revision}｜活動明細 {len(draft.lines)}｜封存明細 {len(draft.archived_lines)}")
    for blocker in draft.blockers:
        st.warning(blocker)
    with st.expander("來源追溯（含原文，歷史價格不可視為現價）"):
        st.json(draft.source)
    st.download_button("下載配置草稿（含來源，非報價單）",
                       json.dumps(export_multi_draft(draft), ensure_ascii=False, indent=2),
                       file_name=f"multi-configuration-{draft.draft_id}.json", mime="application/json")
    try:
        check_source_current(draft, batch)
    except ValueError as exc:
        st.error(str(exc))
        return

    st.dataframe([{"明細識別": line.line_id, "名稱": line.label, "明細版本": line.revision,
                   "產品": line.configuration.get("product_name") or "未指定",
                   "產品數量": str(line.configuration.get("qty")) if line.configuration.get("qty") is not None else "未確認",
                   "已選部位數": len(line.configuration.get("selections", {}))}
                  for line in draft.lines], hide_index=True, use_container_width=True)
    actor = st.text_input("操作人（自填紀錄，非核准簽章）", key=f"multi_actor_{selected}", max_chars=80)
    reason = st.text_input("本次操作理由", key=f"multi_reason_{selected}", max_chars=1000)

    with st.expander("逐句需求對照與有依據的人工核對", expanded=False):
        from utils.requirement_review_ui import render_requirement_review
        reviewed = render_requirement_review(st, draft, batch, actor)
        if reviewed is not None:
            _store(st, reviewed)
            st.rerun()

    if draft.questions:
        with st.expander("AI 問題紀錄（結案需依據，變更後重新核對）"):
            for question in draft.questions:
                target = next((line.label for line in (*draft.lines, *draft.archived_lines) if line.line_id == question.target_id), "整單")
                st.text(f"{target}｜{question.text}｜{question.status}")
    if draft.lines:
        from utils.multi_quote_ai_ui import render_multi_quote_ai
        ai_updated = render_multi_quote_ai(st, draft, batch, actor)
        if ai_updated is not None:
            _store(st, ai_updated)
            st.rerun()

    def apply(command):
        try:
            updated = apply_multi_command(draft, command, draft_id=draft.draft_id,
                                          expected_revision=draft.revision, actor=actor, reason=reason,
                                          source_batch=batch)
        except (ValueError, TypeError) as exc:
            st.error(str(exc))
        except Exception:
            st.error("主檔查詢或配置驗證失敗，本次操作未保存。")
        else:
            _store(st, updated)
            st.rerun()

    with st.form(f"multi_add_{selected}_{draft.revision}"):
        label = st.text_input("新增明細名稱", max_chars=200)
        if st.form_submit_button("新增明細"):
            apply({"op": "add", "label": label})
    if draft.archived_lines:
        archived_id = st.selectbox("封存明細", [line.line_id for line in draft.archived_lines],
                                    format_func=lambda key: next(line.label for line in draft.archived_lines if line.line_id == key),
                                    key=f"multi_archived_{selected}_{draft.revision}")
        if st.button("恢復封存明細", key=f"multi_restore_{selected}_{draft.revision}"):
            apply({"op": "restore", "line_id": archived_id})
    if not draft.lines:
        return
    ids = [line.line_id for line in draft.lines]
    focus_key = f"multi_line_{selected}"
    if st.session_state.get(focus_key) not in ids:
        st.session_state[focus_key] = ids[0]
    line_id = st.selectbox("編輯明細", ids,
                           format_func=lambda key: next(f"{line.label}｜{key[:8]}" for line in draft.lines if line.line_id == key),
                           key=focus_key)
    line = next(line for line in draft.lines if line.line_id == line_id)
    index = ids.index(line_id)
    for label, offset in (("明細上移", -1), ("明細下移", 1)):
        if st.button(label, key=f"{label}_{selected}_{draft.revision}", disabled=not 0 <= index + offset < len(ids)):
            moved = ids[:]
            moved[index], moved[index + offset] = moved[index + offset], moved[index]
            apply({"op": "reorder", "line_ids": moved})
    if st.button("移除並封存此明細", key=f"multi_remove_{selected}_{draft.revision}"):
        apply({"op": "remove", "line_id": line_id})
    if not st.checkbox("顯示合法選配（唯讀查詢目前產品主檔）", key=f"multi_catalog_{selected}_{line_id}"):
        return
    from database import repository as repo
    from engine.configuration import load_catalog
    try:
        categories = repo.get_product_categories(workgroup=draft.workgroup)
    except Exception:
        st.error("無法讀取產品主檔；草稿未變更。")
        return
    codes = ["", *[category["prodkind"] for category in categories]]
    names = {category["prodkind"]: category.get("codsc") or category["prodkind"] for category in categories}
    current = line.configuration.get("prodkind") or ""
    with st.form(f"multi_product_{selected}_{draft.revision}_{line_id}"):
        product = st.selectbox("產品類別", codes, index=codes.index(current) if current in codes else 0,
                               format_func=lambda code: f"{names[code]}（{code}）" if code else "請選擇")
        qty = st.number_input("產品數量（須人工核對，不沿用工單候選）", min_value=0.001,
                              value=float(line.configuration["qty"]) if line.configuration.get("qty") is not None else None)
        if st.form_submit_button("套用產品及數量"):
            if not product or qty is None:
                st.error("請明確選擇產品與輸入數量。")
            else:
                apply({"op": "configure", "line_id": line_id, "proposal": {"prodkind": product, "qty": qty}})
    if not current:
        return
    try:
        catalog = load_catalog(current, draft.workgroup)
    except Exception:
        st.error("產品結構目前不可用；請核對主檔或重新選擇產品。")
        return
    config = line.configuration
    st.dataframe([{"部位路徑": path, "規格": value.get("codsc") or value.get("code", ""),
                   "每件用量": value.get("line_qty", 1)} for path, value in config["selections"].items()],
                 hide_index=True, use_container_width=True)
    for question in [*config.get("missing_fields", []), *config.get("questions", [])]:
        st.warning(str(question))
    paths = list(dict.fromkeys([*catalog["nodes"], *config["selections"]]))
    if not paths:
        return
    path_key = f"multi_path_{selected}_{line_id}_{current}"
    if st.session_state.get(path_key) not in paths:
        st.session_state[path_key] = paths[0]
    path = st.selectbox("配置部位（完整路徑）", paths, key=path_key)
    options = catalog["options"].get(path, [])
    if path in catalog["nodes"]:
        choices = list(range(len(options))) if options else [-1]
        with st.form(f"multi_spec_{selected}_{line_id}_{draft.revision}_{path}"):
            choice = st.selectbox("規格", choices, format_func=lambda i: (
                f"{options[i].get('codsc', '')}（{options[i].get('code', '')}）" if i >= 0 else "結構節點（由程式驗證）"))
            amount = st.number_input("每件產品的項目數量", min_value=0.001,
                                     value=float(config["selections"].get(path, {}).get("line_qty", 1)))
            if st.form_submit_button("套用此部位規格"):
                apply({"op": "configure", "line_id": line_id, "proposal": {"changes": [{
                    "op": "set", "path": path, "code": options[choice].get("code", "") if choice >= 0 else "", "line_qty": amount,
                }]}})
    if path in config["selections"] and st.button("移除此部位分支", key=f"multi_path_remove_{selected}_{line_id}_{draft.revision}"):
        apply({"op": "configure", "line_id": line_id, "proposal": {"changes": [{"op": "remove", "path": path}]}})
