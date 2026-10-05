"""共用條件與局部例外的人工核對介面；不推論正式部位或工程核准。"""
from dataclasses import asdict

from agent.conditions import SUBJECTS, STATES, SCOPES, apply_condition_command, condition_report, refresh_conditions


def render_conditions(st, draft, batch, actor):
    current = refresh_conditions(draft)
    report = condition_report(current)
    st.caption("人工範圍紀錄不是工程核准。全單／群組固定目前明細；新增明細不會默默繼承。未指定與明確不要不同。")
    labels = {line.line_id: f"{index + 1}. {line.label}｜{line.line_id[:8]}" for index, line in enumerate(current.lines)}
    if current.conditions:
        st.dataframe([{"條件識別": c.condition_id, "類型": SUBJECTS[c.subject], "狀態": STATES[c.state],
                       "範圍": SCOPES[c.scope], "角色": c.group_label, "核對狀態": c.status,
                       "內容": c.value, "依據": c.evidence[-1].reference}
                      for c in current.conditions], hide_index=True, use_container_width=True)
        with st.expander("條件來源、正式部位與歷次核對依據"):
            requirements = {r.requirement_id: r for r in (*current.requirements, *current.archived_requirements)}
            for condition in current.conditions:
                st.json({"condition": asdict(condition), "source_requirements": [asdict(requirements[key])
                         for key in condition.requirement_ids if key in requirements]})
        with st.expander("逐筆條件繼承與例外對照"):
            st.dataframe([{"明細": labels[row["line_id"]], "類型": SUBJECTS[row["subject"]],
                           "狀態": STATES.get(row["state"], "衝突"),
                           "有效條件": "、".join(row["condition_ids"]),
                           "原始繼承": "、".join(row["inherited_ids"]),
                           "已覆蓋條件": "、".join(row["overridden_ids"])} for row in report["rows"]],
                         hide_index=True, use_container_width=True)
        for issue in report["issues"]:
            st.warning(f"{labels.get(issue['line_id'], '整單')}｜{issue['message']}")
    if not current.lines or not current.requirements:
        st.info("條件核對須有活動明細與工單來源需求帳本；來源不足不代表沒有條件。")
        return None
    if not st.checkbox("編輯共用條件與局部例外", key=f"conditions_open_{draft.draft_id}"):
        return None
    base = f"conditions_{draft.draft_id}_{draft.revision}"
    live = {c.condition_id: c for c in current.conditions if c.status != "WITHDRAWN"}
    key = st.selectbox("條件紀錄", ["", *live], key=base + "_record",
                       format_func=lambda k: f"{SUBJECTS[live[k].subject]}｜{live[k].condition_id[:8]}｜{live[k].status}" if k else "新增條件")
    old = live.get(key)
    prefix = base + key

    def apply(command, reason):
        try:
            return apply_condition_command(draft, command, draft_id=draft.draft_id, expected_revision=draft.revision,
                                           actor=actor, reason=reason, source_batch=batch)
        except (ValueError, TypeError) as exc:
            st.error(str(exc))
        except Exception:
            st.error("條件核對或主檔查詢失敗，本次操作未保存。")
        return None

    subject = st.selectbox("條件類型（部位語意分離）", list(SUBJECTS), format_func=SUBJECTS.get,
                           index=list(SUBJECTS).index(old.subject) if old else 0, key=prefix + "_subject")
    state = st.selectbox("要求狀態", list(STATES), format_func=STATES.get,
                         index=list(STATES).index(old.state) if old else 0, key=prefix + "_state")
    scope = st.selectbox("條件作用範圍", list(SCOPES), format_func=SCOPES.get,
                         index=list(SCOPES).index(old.scope) if old else 0, key=prefix + "_scope")
    group = st.text_input("角色群組名稱", value=old.group_label if old and scope == "group" else "",
                          max_chars=100, key=prefix + "_group_" + scope) if scope == "group" else ""
    if scope == "order":
        targets = list(labels)
        st.text("全單範圍：" + "、".join(labels.values()))
    else:
        defaults = [target for target in old.targets if target in labels] if old and old.scope == scope else []
        targets = st.multiselect("條件適用明細", list(labels), default=defaults, format_func=labels.get,
                                 key=prefix + "_targets_" + scope)
    reqs = {r.requirement_id: r for r in current.requirements}
    requirements = st.multiselect("條件來源需求", list(reqs),
                                  default=[k for k in old.requirement_ids if k in reqs] if old else [],
                                  format_func=lambda k: f"{reqs[k].text}｜{k[:8]}", key=prefix + "_requirements")
    parents = [k for k in live if k != key]
    overrides = st.multiselect("明確覆蓋的較廣範圍條件（不自動依先後決定）", parents,
                               default=[k for k in old.overrides if k in parents] if old else [],
                               format_func=lambda k: f"{SUBJECTS[live[k].subject]}｜{SCOPES[live[k].scope]}｜{k[:8]}",
                               key=prefix + "_overrides")
    value = st.text_area("條件內容／位置基準（不推論圖面）", value=old.value if old else "", max_chars=1000, key=prefix + "_value")
    bindings = [{"line_id": b.line_id, "path": b.path} for b in old.bindings if b.line_id in targets] if old else []
    st.caption("正式部位可未選配，但必須人工核對語意。綁定路徑涵蓋其子樹；不要用過廣部位代替桌面孔或腳板孔。")
    if st.checkbox("查詢並設定條件正式部位（唯讀主檔）", key=prefix + "_catalog"):
        from engine.configuration import load_catalog
        bindings = []
        try:
            for target in targets:
                line = next(line for line in current.lines if line.line_id == target)
                if not line.configuration.get("prodkind"):
                    st.info(f"{labels[target]}尚未指定產品；此筆保留未綁定。")
                    continue
                catalog = load_catalog(line.configuration["prodkind"], current.workgroup)
                paths = ["", *catalog["nodes"]]
                previous = next((b.path for b in old.bindings if b.line_id == target), "") if old else ""
                path = st.selectbox(f"條件正式部位｜{labels[target]}", paths,
                                    index=paths.index(previous) if previous in paths else 0,
                                    format_func=lambda p: p or "未綁定（保留待核對）", key=prefix + "_path_" + target)
                if path:
                    bindings.append({"line_id": target, "path": path})
        except Exception:
            st.error("無法讀取條件部位主檔，本次不可保存；原草稿未變更。")
            return None
    elif bindings:
        st.text("保留既有部位：" + "、".join(b["path"] for b in bindings))
    reference = st.text_input("條件依據識別／版本", max_chars=500, key=prefix + "_reference")
    explanation = st.text_area("條件核對理由（重新核對須重新填寫）", max_chars=1000, key=prefix + "_explanation")
    if st.button("保存條件核對紀錄（非工程核准）", key=prefix + "_save"):
        return apply({"op": "record", "condition_id": key, "subject": subject, "state": state, "scope": scope,
                      "group_label": group, "targets": targets, "value": value, "requirement_ids": requirements,
                      "bindings": bindings, "overrides": overrides, "reference": reference, "explanation": explanation}, explanation)
    if old and st.button("撤回此條件（保留歷史及來源阻擋）", key=prefix + "_withdraw"):
        return apply({"op": "withdraw", "condition_id": key}, explanation)
    return None
