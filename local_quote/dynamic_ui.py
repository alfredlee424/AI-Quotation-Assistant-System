"""本機工單逐張報價 UI；匯入不建 DB、不讀 config，不取代舊四張 UI。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import streamlit as st

from local_quote.dynamic_presentation import (hierarchy_rows, legacy_report_rows,
                                              report_rows, safe_cell, screen_rows,
                                              semantic_rows, semantic_text, tsv)
from local_quote.dynamic_storage import DynamicStore, validate_actor
from local_quote.dynamic_workflow import DynamicWorkspace
from local_quote.catalog import option_identity
from local_quote.storage import BatchStore, StorageError


@st.cache_resource
def services():
    # 僅 main 明確取得服務時才載入設定；不可使用一般 database.connection。
    from local_quote.backend import LocalDatabase, load_settings
    from local_quote.business import MasterSource
    db = LocalDatabase(load_settings(Path(__file__).resolve().parents[1] / ".env"))
    db.inspect_schema()
    return MasterSource(db, allow_code_variants=True), DynamicStore(db), BatchStore(db)


def audit():
    actor = validate_actor(st.session_state.get("dq_actor", ""))
    reason = st.session_state.get("dq_reason", "")
    if not reason.strip():
        raise ValueError("所有人工更動都需要填寫原因")
    return dict(actor=actor, reason=reason, time=datetime.now(timezone.utc).isoformat())


def editable_audit():
    try:
        audit()
        return True
    except (ValueError, StorageError):
        return False


def _mapping(ws, cid, name):
    analysis = ws.analysis
    if analysis is None or not any(c["id"] == cid for c in analysis["children"]):
        raise ValueError("分析已失效，請重新分析目前工單")
    return deepcopy(analysis["context"]["inputs"][name].get(cid, {}))


def _edit(ws, cid, stamp, **changes):
    before = {name: _mapping(ws, cid, name) for name in changes}
    ws.edit(cid, **changes)
    events = st.session_state.setdefault("dq_events", {})
    events.setdefault(cid, []).append(dict(stamp, before=before, after=deepcopy(changes)))
    st.session_state.setdefault("dq_blocked", {}).pop(cid, None)
    st.session_state["dq_notice"] = "本張已重新分析；如有舊固定預覽已撤銷，其他張不受影響。"


def _callback(action, ws, cid, *args, origin=None):
    # Streamlit 在 main() 同步來源前執行回呼，也可能送達已移除元件的舊事件。
    # 不以空 mapping 補救：那會將舊候選／成本寫入另一份草稿。
    current = st.session_state.get("dq_workspace")
    analysis = current.analysis if current is not None else None
    if (current is not ws or analysis is None
            or st.session_state.get("dq_source") != current.source["text"]
            or st.session_state.get("dq_category") != current.category
            or origin != (analysis["source_digest"], analysis["catalog_digest"])
            or not any(c["id"] == cid for c in analysis["children"])
            or cid in current.saved):
        st.session_state["dq_notice"] = "舊畫面的輸入事件已失效，未套用；請以目前工單的分析結果繼續操作。"
        return
    try:
        action(current, cid, *args)
    except (ValueError, StorageError) as exc:
        # 即使事件因稽核無效被拒絕，也不能保存畫面尚未套用的舊固定內容。
        st.session_state.setdefault("dq_blocked", {})[cid] = str(exc)


def _choose(ws, cid, requirement, widget, options):
    stamp = audit()
    index = st.session_state[widget]
    if type(index) is not int or not 0 <= index < len(options):
        raise ValueError("請明確選擇合法候選；不提供清除必選規格捷徑")
    candidate = options[index]
    choices, customs = _mapping(ws, cid, "choices"), _mapping(ws, cid, "customs")
    choices[requirement] = option_identity(ws.catalog, candidate)
    customs.pop(requirement, None)
    _edit(ws, cid, stamp, choices=choices, customs=customs)


def _cost(ws, cid, path, field, widget):
    stamp = audit()
    overrides = _mapping(ws, cid, "overrides")
    values = overrides.get(path, {}).get("values", {}).copy()
    values[field] = st.session_state[widget]
    overrides[path] = dict(stamp, values=values)
    _edit(ws, cid, stamp, overrides=overrides)


def _selector(ws, child, requirement, disabled):
    options = requirement.get("candidates", [])
    if not options:
        return
    cid, rid = child["id"], requirement["id"]
    widget = f"dq_choice:{cid}:{rid}"
    chosen = _mapping(ws, cid, "choices").get(rid, requirement.get("selected"))
    catalog = ws.catalog
    index = next((n for n, o in enumerate(options) if chosen == option_identity(catalog, o)), None)
    st.session_state[widget] = index
    st.selectbox("合法候選 — " + requirement["text"], list(range(len(options))), index=index,
                 format_func=lambda n: options[n]["path"] + " / " + options[n]["code"] + " — " + options[n]["codsc"],
                 key=widget, disabled=disabled, placeholder="請人工選擇，不預選第一筆",
                 on_change=_callback, args=(_choose, ws, cid, rid, widget, options),
                 kwargs={"origin": (child["source_digest"], child["catalog_digest"])})


def _custom(ws, child, requirement, disabled):
    if requirement["kind"] in {"constraint", "units", "external_confirmation"}:
        return  # 這些不是缺少客製零件；不得提供核心不會消耗的 no-op 表單。
    cid, rid = child["id"], requirement["id"]
    old = _mapping(ws, cid, "customs").get(rid, {})
    paths = requirement.get("custom_paths")
    if paths is None:
        paths = list(ws.catalog["nodes"])
    paths = [p for p in paths if p in ws.catalog["nodes"]]
    if not paths:
        st.warning("沒有可掛接的既有部位；需由受控主檔程序修正結構。")
        return
    with st.expander("需要建立客製化新件 — " + requirement["text"]):
        st.caption("只建立本次臨時規格、不寫主檔、不新增結構邊；仍需通過必選、成本與數量驗證。表單提交才套用。")
        prefix = f"dq_custom:{cid}:{rid}"
        with st.form(prefix):
            path = st.selectbox("掛接既有部位", paths, key=prefix + ":path",
                                index=paths.index(old["path"]) if old.get("path") in paths else None,
                                disabled=disabled)
            code = st.text_input("臨時代碼（TMP: 開頭、至多10字元）", value=old.get("code", "TMP:"),
                                 max_chars=10, key=prefix + ":code", disabled=disabled)
            description = st.text_input("臨時規格說明", value=old.get("codsc", ""), max_chars=40,
                                        key=prefix + ":description", disabled=disabled)
            price = st.text_input("臨時採購單價（未知留空，零成本填0）", value=str(old.get("compri") or "")
                                  if old.get("compri") != 0 else "0", key=prefix + ":price", disabled=disabled)
            submit = st.form_submit_button("套用本次臨時規格", disabled=disabled)
        if submit:
            try:
                stamp = audit()
                if path is None or not code.startswith("TMP:") or not code[4:].strip() or not description.strip():
                    raise ValueError("請填完整既有部位、TMP: 代碼與規格說明")
                customs, choices = _mapping(ws, cid, "customs"), _mapping(ws, cid, "choices")
                customs[rid] = dict(stamp, path=path, code=code, codsc=description, compri=price or None)
                choices.pop(rid, None)
                _edit(ws, cid, stamp, customs=customs, choices=choices)
                st.rerun()
            except (ValueError, StorageError) as exc:
                st.error(str(exc))


def _resolutions(ws, child, disabled):
    # 每個片段只畫一次；共用片段明確逐張確認，不隱式套用全部。
    fragments = {}
    external = set()
    for req in child["requirements"]:
        source = req.get("source") or {}
        if source.get("fragment_id"):
            fragments[source["fragment_id"]] = source
            if req["kind"] == "external_confirmation":
                external.add(source["fragment_id"])
    for rid, source in fragments.items():
        if rid in external:
            st.info(f"原文第 {source['line']} 行待圖面／訂單核對：{source['original']}。"
                    "目前保留待核對；不提供客製或外部確認重述捷徑，不可為通過而忽略需求。")
            continue
        old = _mapping(ws, child["id"], "resolutions").get(rid, {})
        prefix = f"dq_resolution:{child['id']}:{rid}"
        with st.expander(f"明確重述與範圍確認 — 原文第 {source['line']} 行"):
            st.text(source["original"])
            st.text(f"原範圍：{semantic_text(source['scope'])}；本次重述只套用第 {child['ordinal']} 張（群組 {child['group']}）。")
            st.caption("重述仍經同一規則解析器；不可用『忽略』『備註』或刪除文字代替需求。其他張需分別確認。")
            st.caption("待單位需先人工核對，再以含明確單位的完整片段重述，保留外型、部位、孔數等所有需求；不自動換算台尺。")
            with st.form(prefix):
                text = st.text_area("精確重述完整片段", value=old.get("text", ""), key=prefix + ":text", disabled=disabled)
                confirmed = st.checkbox("確認完整保留需求，且只作用於本張", key=prefix + ":scope", disabled=disabled)
                submit = st.form_submit_button("套用重述及作用範圍", disabled=disabled)
            if submit:
                try:
                    stamp = audit()
                    if not confirmed or not text.strip():
                        raise ValueError("需要非空重述及本張作用範圍的人工確認")
                    resolutions = _mapping(ws, child["id"], "resolutions")
                    resolutions[rid] = dict(stamp, text=text)
                    _edit(ws, child["id"], stamp, resolutions=resolutions)
                    st.rerun()
                except (ValueError, StorageError) as exc:
                    st.error(str(exc))


def _cost_editor(ws, child, disabled):
    for item in child["items"]:
        if not item["selected"]:
            continue
        path, cid = item["path"], child["id"]
        with st.expander("編輯成本輸入 — " + item["label"] + " / " + path):
            st.text("本張總量直接輸入，不再乘成品數" if item["quantity_basis"] == "total"
                    else f"每件量 × 成品數 {child['product_qty']} = 本張總量；不猜共用總量平均")
            override = _mapping(ws, cid, "overrides").get(path, {}).get("values", {})
            for field, label in (("compri", "採購單價 compri"), ("stdqty", "用料量 stdqty"),
                                 ("stdpar", "裁切量 stdpar"),
                                 ("line_qty", "本張總量 total qty" if item["quantity_basis"] == "total" else "每件量 line_qty")):
                widget = f"dq_cost:{cid}:{path}:{field}"
                value = override.get(field, item.get(field))
                st.session_state[widget] = "" if value is None else str(value)
                st.text_input(label, key=widget, disabled=disabled, on_change=_callback,
                              args=(_cost, ws, cid, path, field, widget),
                              kwargs={"origin": (child["source_digest"], child["catalog_digest"])})
            st.caption("輸入失焦／Enter 即套用並撤銷本張舊固定；不提供直接編輯總額。空白不等於0。")


def _download(child, catalog, *, receipt=None, key):
    status = "正式已保存" if receipt else "草稿未保存"
    st.download_button("下載13欄 TSV（" + status + "）", tsv(report_rows(child, catalog, receipt=receipt)),
                       file_name=f"{'saved' if receipt else 'draft'}-quote-{child['ordinal']}.tsv",
                       mime="text/tab-separated-values", key=key)


def _show(child, catalog, *, receipt=None, key):
    if receipt is not None:
        child = receipt["document"]["child"]
        catalog = receipt["document"]["context"]["catalog_snapshot"]
    st.text(child["description"])
    st.text(f"來源行 {child['source_line']}／群組 {child['group']}／成品數 {child['product_qty']}")
    if "normalized_requirements" in child:
        st.text(f"產品角色：{semantic_text(child.get('role')) or '未指定'}／外型：{semantic_text(child.get('shape')) or '未指定'}")
        with st.expander("語意解析／適用部位", expanded=True):
            st.caption("適用品項為同群組內序號，不是件數。共用為預設，特定／品項優先；覆寫來源仍保留。"
                       "已匹配不等於成本完整；限制片數不計為付費孔數，未適用／衝突／待單位不推算總量。")
            st.dataframe(semantic_rows(child), hide_index=True, width="stretch")
        with st.expander("語意解析原始欄位（含来源、候選及關聯需求）"):
            st.json(child["normalized_requirements"])
    else:
        st.caption("舊版歷史未記錄語意解析／適用部位；依原固定明細顯示，不以新版解析器補算。")
    st.dataframe(screen_rows(child, catalog, receipt=receipt), hide_index=True, width="stretch")
    calc = child["calc"]
    if child["ready"]:
        cols = st.columns(3)
        cols[0].metric("本張完整成本", calc["total_cost"])
        cols[1].metric("未稅小計", calc["subtotal"])
        cols[2].metric("本張報價（含稅）", calc["total_price"])
    else:
        st.metric("已知成本小計（非完整合計）", calc["known_cost"])
        st.warning("尚有需求／結構／成本缺口，不可固定或保存。")
    for issue in child["issues"]:
        st.text("待處理：" + issue["message"])
    st.text("正式單號：" + (receipt["ref_no"] if receipt else "尚未配號"))
    _download(child, catalog, receipt=receipt, key=key)


def _child(ws, child, store):
    cid, catalog = child["id"], ws.catalog
    saved = ws.saved.get(cid)
    disabled = saved is not None or not editable_audit()
    _show(child, catalog, receipt=saved, key="dq_download:" + cid)
    with st.expander("完整主檔階層（含未啟用選配）／來源及人工紀錄"):
        st.dataframe(hierarchy_rows(catalog, child), hide_index=True)
        st.json(child["source_audit"])
        st.json(st.session_state.get("dq_events", {}).get(cid, []))
    if saved:
        st.success("本張已保存，修改與再次保存已停用；歷史不重查價。")
    with st.expander("合法規格候選／本次臨時件", expanded=not child["ready"]):
        for req in child["requirements"]:
            if req["kind"] in {"constraint", "units", "external_confirmation"}:
                continue
            _selector(ws, child, req, disabled)
            if not req.get("candidates") or req["id"] in _mapping(ws, cid, "customs"):
                _custom(ws, child, req, disabled)
    _resolutions(ws, child, disabled)
    _cost_editor(ws, child, disabled)
    blocked = st.session_state.get("dq_blocked", {}).get(cid)
    if blocked:
        st.error(blocked + "；請重新套用該欄位後才能固定／保存。")
    frozen = ws.frozen.get(cid)
    if st.button("固定本張預覽", key="dq_freeze:" + cid,
                 disabled=disabled or not child["ready"] or frozen is not None or bool(blocked)):
        try:
            stamp = audit()
            # choices 僅含 path/code，重複代碼再帶 codsc；完整事件鏈放在 frozen_audit.reason，
            # 不向核心塞入未支援的 choices 欄位；歷史下載可重讀所有人員／原因／時間。
            reason = json.dumps({"reason": stamp["reason"], "source": st.session_state.get("dq_source_audit"),
                                 "ui_events": st.session_state.get("dq_events", {}).get(cid, [])}, ensure_ascii=False)
            ws.freeze(cid, actor=stamp["actor"], reason=reason, time=stamp["time"])
            st.rerun()
        except (ValueError, StorageError) as exc:
            st.error(str(exc))
    frozen = ws.frozen.get(cid)
    if frozen:
        document = json.loads(frozen.payload)
        st.text("固定 UUID：" + frozen.confirmation_id)
        with st.expander("固定預覽（尚未保存者無正式單號）", expanded=not saved):
            st.dataframe(screen_rows(document["child"], document["context"]["catalog_snapshot"], receipt=saved), hide_index=True)
            st.json(document["frozen_audit"])
        confirmed = st.checkbox("我已核對本張固定明細、數量與金額，確認保存", key="dq_confirm:" + frozen.confirmation_id,
                                disabled=saved is not None or bool(blocked))
        if st.button("確認保存本張", key="dq_save:" + cid, disabled=saved is not None or not confirmed or bool(blocked)):
            try:
                ws.confirm(cid, store, confirmed=True)
                st.rerun()
            except (ValueError, StorageError) as exc:
                st.error(str(exc))
                st.warning("保存未完成；保留同一固定 UUID，使用本按鈕重試，勿重新建立工單。")
            except Exception:
                st.error("保存結果未知；保留同一固定 UUID 重試或查歷史，不另建確認。")


def _history(store, legacy):
    for kind, service, title, identity in (("new", store, "新版逐張歷史", "confirmation_id"),
                                           ("old", legacy, "舊四張歷史（唯讀）", "batch_id")):
        with st.expander(title):
            prefix = "dq_history:" + kind
            if st.button("載入" + title, key=prefix + ":list"):
                st.session_state.pop(prefix + ":document", None)
                st.session_state.pop(prefix + ":rows", None)
                try:
                    st.session_state[prefix + ":rows"] = service.list_saved()
                except Exception:
                    st.error("歷史查詢失敗或完整性不符；不會修復或重新查價。")
            rows = st.session_state.get(prefix + ":rows", [])
            labels = {r[identity]: r["ref_no"] if kind == "new" else "、".join(r["ref_nos"]) for r in rows}
            selected = st.selectbox(title + "選擇", list(labels), index=None, format_func=labels.get, key=prefix + ":selected",
                                    on_change=lambda p=prefix: st.session_state.pop(p + ":document", None))
            if st.button("查回" + title, key=prefix + ":read", disabled=selected is None):
                st.session_state.pop(prefix + ":document", None)
                try:
                    st.session_state[prefix + ":document"] = service.read(selected)
                    if st.session_state[prefix + ":document"] is None:
                        st.warning("找不到該歷史；不顯示舊的查詢結果。")
                except Exception:
                    st.error("固定歷史不完整，拒絕顯示為有效報價。")
            result = st.session_state.get(prefix + ":document")
            if not result:
                continue
            if kind == "new":
                doc = result["document"]
                _show(doc["child"], doc["context"]["catalog_snapshot"], receipt=result, key=prefix + ":tsv")
            else:
                st.caption("舊版未記錄的異動日／時／人員留空；不以目前操作人或現在時間補寫。")
                for doc in result["children"]:
                    st.text("舊正式單號：" + doc["ref_no"])
                    st.text("保存時間：" + doc["confirmed_at"])
                    st.metric("舊本張報價（含稅）", str(doc["child"]["calc"]["total_price"]))
                old_rows = legacy_report_rows(result)
                st.dataframe([{k: safe_cell(v) for k, v in row.items()} for row in old_rows], hide_index=True)
                st.download_button("下載舊四張13欄 TSV（正式已保存）", tsv(old_rows), file_name="saved-legacy-quotes.tsv",
                                   mime="text/tab-separated-values", key=prefix + ":tsv")
            st.download_button("下載持久化歷史 JSON", json.dumps(result, ensure_ascii=False, indent=2),
                               file_name="saved-quote-history.json", mime="application/json", key=prefix + ":json")
            with st.expander("持久化內容及稽核"):
                st.json(result)


def _new_source():
    st.session_state.dq_workspace = DynamicWorkspace()
    for key in ("dq_category", "dq_source", "dq_events", "dq_source_audit", "dq_blocked"):
        st.session_state.pop(key, None)


def main():
    st.set_page_config(page_title="本機工單逐張報價", layout="wide")
    st.title("本機工單逐張報價")
    st.caption("本機規則＋合法候選，不是 LLM。任意品項分張；人工確認前不配號、不寫庫、不代表工程或業務核准。")
    st.warning("容量限制：每張保留完整主檔與來源，必要時無損壓縮；完整快照上限 8 MiB，儲存封套上限 TEXT 65535 bytes。超量阻擋固定／保存，不裁剪、不改表。")
    try:
        master, store, legacy = services()
    except Exception:
        st.error("無法載入本機服務；請檢查連線及既有七表，不會自動建表。")
        return
    if "dq_workspace" not in st.session_state:
        st.session_state.dq_workspace = DynamicWorkspace()
    ws = st.session_state.dq_workspace
    try:
        if "dq_categories" not in st.session_state:
            st.session_state.dq_categories = master.categories()
        names = {c["prodkind"]: c["codsc"] + "（" + c["prodkind"] + "）" for c in st.session_state.dq_categories}
    except Exception:
        st.error("產品類別唯讀查詢失敗；仍可嘗試查閱已保存歷史。")
        names = {}
    locked = bool(ws.saved)
    st.button("開始新工單（不更改已保存歷史）", key="dq_new", on_click=_new_source)
    category = st.selectbox("產品類別", list(names), index=None, format_func=names.get, key="dq_category", disabled=locked)
    source = st.text_area("貼上工單需求（任意品項）", height=180, key="dq_source", disabled=category is None or locked,
                          placeholder="平直70X120-----2\n平直70X150-----3\n共用：\n面無孔\n817胡／噴胡",
                          help="可單獨一行『共用：』開啟多行區塊，也可每行『共用：條件』。區塊至新品項、新工單或明示範圍結束；空行不結束。")
    st.caption("共用區塊不必每行重複標頭；條件套用同工單群組的適用品項。"
               "『主席及彎角桌…』『列席桌…』可置於品項之間；『第N項：』『局部：』『配件：』明示特定範圍。")
    if not locked:
        if category is None and ws.category is not None:
            ws = DynamicWorkspace()
            st.session_state.dq_workspace = ws
            st.session_state.dq_events = {}
            st.session_state.dq_blocked = {}
        elif category is not None and ws.set_input(source, category):
            st.session_state.dq_events = {}
            st.session_state.dq_blocked = {}
    st.text_input("操作人 actor（1至8字元，非登入憑證）", key="dq_actor", max_chars=8)
    st.text_input("本次人工操作原因", key="dq_reason")
    valid_audit = editable_audit()
    if not valid_audit:
        st.info("請先填合法操作人及原因；人工編輯、分析及固定尚未開放。")
    if st.button("讀取主檔並分析工單", key="dq_analyze", type="primary",
                 disabled=category is None or not source.strip() or not valid_audit or locked):
        try:
            stamp = audit()
            ws.load_catalog(master)
            st.session_state.dq_source_audit = dict(stamp, source=ws.source, category=category)
        except (ValueError, StorageError) as exc:
            st.error(str(exc))
        except Exception:
            st.error("唯讀主檔查詢未完成；不顯示為已完成報價，請重試。")
    if st.session_state.get("dq_notice"):
        st.info(st.session_state.pop("dq_notice"))
    if ws.analysis:
        analysis = ws.analysis
        for issue in analysis["global_issues"]:
            st.text(issue["message"])
        if analysis["global_issues"]:
            st.warning("原文行未確認作用範圍時，請以獨立『共用：』標頭開啟多行區塊，或每行『共用：條件』；"
                       "特定條件可用『第N項：』『配件：』。區塊至新品項、新工單或明示範圍結束，空行不結束；不提供忽略。")
        st.caption("成本：依每件量或本張總量 × 用料量 ÷ 裁切量 × 採購單價；乘主檔係數、折扣0%、外加5%稅。")
        children = analysis["children"]
        if children:
            for tab, child in zip(st.tabs([f"第 {c['ordinal']} 張（{c['product_qty']}件）" for c in children]), children):
                with tab:
                    _child(ws, child, store)
    _history(store, legacy)
    st.caption("未保存草稿只在目前工作階段；失去工作階段且提交狀態未知，先查歷史，勿直接重建保存。下載不支援匯入。")


if __name__ == "__main__":
    main()
