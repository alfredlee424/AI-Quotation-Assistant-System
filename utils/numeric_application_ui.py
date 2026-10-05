"""工單數值候選的人工作用範圍核對、固定差異與套用。"""
from dataclasses import asdict

from agent.numeric_application import (checked_numeric_application, commit_numeric_application,
                                       dimension_options, numeric_candidates, prepare_numeric_application)


def render_numeric_application(st, draft, batch, actor):
    if draft.source.get("kind") != "work_order" or batch is None or not draft.lines:
        st.info("受控數值套用僅提供有直接來源的工單轉接明細；請先完成來源切分及指定產品。")
        return None
    st.warning("候選需人工確認成品／桌面用途。此操作只改配置，不核准來源、圖面、用料或工程規則。")
    st.caption("先產生固定差異，再人工套用；成品數與尺寸分次核對。台尺、分片、孔數、配件與假厚不能藉此直接套用。")
    prefix = f"numeric_{draft.draft_id}_{draft.revision}"
    stored = dict(st.session_state.get("numeric_application_proposals", {}))
    pending = stored.get(draft.draft_id)
    if pending is not None:
        try:
            pending = checked_numeric_application(draft, pending, actor=actor, source_batch=batch)
        except (ValueError, TypeError):
            stored.pop(draft.draft_id, None)
            st.session_state["numeric_application_proposals"] = stored
            pending = None
            st.info("草稿、來源或操作人已變更，舊數值差異已清除。")

    line_id = st.selectbox("數值套用目標明細", [line.line_id for line in draft.lines],
                           format_func=lambda key: next(line.label for line in draft.lines if line.line_id == key),
                           key=prefix + "_line")
    try:
        report = numeric_candidates(draft, batch, line_id)
    except ValueError as exc:
        st.warning(str(exc))
        report = {"findings": [], "issues": []}
    findings = report["findings"]
    if findings:
        st.dataframe([{"原文": f["raw"], "候選類型": f["kind"], "原始單位": "、".join(f["units"]),
                       "公分候選": "×".join(f["centimeters"]) if f["centimeters"] else "未確認",
                       "來源行": f["line_number"]} for f in findings], hide_index=True, use_container_width=True)
        for issue in report["issues"]:
            st.warning(issue["message"])
        mode = st.radio("數值套用類型", ["product_quantity", "table_dimensions"],
                        format_func=lambda value: "行尾候選確認為成品數量" if value == "product_quantity" else "兩軸候選確認為桌面尺寸",
                        key=prefix + "_mode")
        kind = "row_quantity_candidate" if mode == "product_quantity" else "table_dimensions_candidate"
        candidates = [f for f in findings if f["kind"] == kind]
        if candidates:
            finding_id = st.selectbox("要核對的數值來源", [f["finding_id"] for f in candidates],
                                     format_func=lambda key: next(f["raw"] for f in candidates if f["finding_id"] == key),
                                     key=prefix + "_finding_" + mode)
            code = source_unit = catalog_unit = axis_order = ""
            ready = True
            if mode == "table_dimensions":
                st.caption("主檔未寫單位時也須人工確認。明確公分／毫米可用原文單位；不明時只能依實際依據補充，不按大小猜測。")
                source_unit = st.selectbox("來源尺寸單位確認", ["", "cm", "mm"],
                    format_func=lambda value: {"": "使用原文明確單位；未知則阻擋", "cm": "人工確認為公分", "mm": "人工確認為毫米"}[value], key=prefix + "_source_unit")
                catalog_unit = st.selectbox("主檔尺寸單位確認", ["", "cm", "mm"],
                    format_func=lambda value: {"": "使用主檔明確單位；未知則阻擋", "cm": "人工確認為公分", "mm": "人工確認為毫米"}[value], key=prefix + "_catalog_unit")
                axis_order = st.selectbox("來源兩軸對應主檔順序", ["", "as_written", "swapped"],
                    format_func=lambda value: {"": "請明確選擇", "as_written": "依原文順序", "swapped": "人工確認交換兩軸"}[value], key=prefix + "_axis")
                ready = st.checkbox("查詢目標明細合法桌面尺寸（唯讀主檔）", key=prefix + "_catalog")
                if ready:
                    try:
                        options = dimension_options(draft, line_id)
                    except ValueError as exc:
                        st.error(str(exc))
                        options = []
                    except Exception:
                        st.error("尺寸主檔讀取失敗，未產生數值提案。")
                        options = []
                    code = st.selectbox("要核對的合法桌面尺寸", [""] + [o["code"] for o in options],
                        format_func=lambda key: next((f"{o['description']}｜{o['code']}" for o in options if o["code"] == key), "請明確選擇"),
                        key=prefix + "_code")
            reference = st.text_input("數值語意／單位確認依據識別與版本", max_chars=500, key=prefix + "_ref")
            explanation = st.text_area("成品數或桌面用途、單位及軸順序的確認內容", max_chars=1000, key=prefix + "_explanation")
            acknowledged = st.checkbox("已人工確認上述數值屬於此明細的成品數或桌面尺寸，不是配件／分片用量", key=prefix + "_ack")
            if st.button("產生數值套用差異（不修改草稿）", disabled=not (ready and acknowledged), key=prefix + "_prepare"):
                stored.pop(draft.draft_id, None)
                st.session_state["numeric_application_proposals"] = stored
                pending = None
                try:
                    pending = prepare_numeric_application(draft, {"finding_id": finding_id, "line_id": line_id, "mode": mode,
                        "source_unit": source_unit, "catalog_unit": catalog_unit, "axis_order": axis_order, "code": code,
                        "reference": reference, "explanation": explanation}, actor=actor, source_batch=batch)
                except (ValueError, TypeError) as exc:
                    st.error(str(exc))
                except Exception:
                    st.error("數值差異或主檔核對失敗，本次未修改草稿。")
                else:
                    stored[draft.draft_id] = pending
                    st.session_state["numeric_application_proposals"] = stored
        else:
            st.info("此來源沒有本類型可供套用的候選，不能改用孔數或分片數代替。")
    else:
        st.info("目前沒有可核對的數值候選。")
    if pending is not None:
        st.markdown("**待套用的固定數值差異（以上輸入修改不會改寫此提案）**")
        st.caption("請核對下列提案實際目標與依據；改輸入後須重新產生差異，套用仍以此固定提案為準。")
        st.json({"目標明細": pending.request["line_id"], "來源": pending.finding,
                 "依據": pending.request["reference"], "確認內容": pending.request["explanation"],
                 "正規化核對": pending.normalization, "套用前": pending.before, "套用後": pending.after})
        if st.button("套用已核對數值差異（非工程核准）", key=prefix + "_commit"):
            stored.pop(draft.draft_id, None)
            st.session_state["numeric_application_proposals"] = stored
            try:
                return commit_numeric_application(draft, pending, actor=actor, source_batch=batch)
            except (ValueError, TypeError) as exc:
                st.error(str(exc))
            except Exception:
                st.error("套用前的來源／主檔驗證失敗，未修改草稿。")
    history = [asdict(change) for change in draft.changes if change.operation == "numeric_application"]
    if history:
        st.json({"數值套用歷史（不是目前工程核准）": history}, expanded=False)
    return None
