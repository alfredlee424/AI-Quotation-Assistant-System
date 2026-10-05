"""獨立 Streamlit 開發查閱；不匯入正式連線，也不接受路徑或 URL。"""
from dataclasses import fields
import json

from engine.development_snapshot_view import (
    DevelopmentSnapshotIdentity, SCHEMA_VERSION, validate_identity,
    DevelopmentSnapshotReader, WARNING, CONFIDENTIAL, FRESHNESS, RESERVATION_WARNING,
    batch_view, child_view, export_snapshot,
)


STATE_KEY = "development_snapshot_readonly_result"
INPUT_PREFIX = "development_snapshot_identity_"
SELECT_KEY = "development_snapshot_selected_child"
LOAD = "唯讀查閱開發快照"
LABELS = {
    "workgroup": "事業別（3 位數字）", "batch_id": "批次識別（32 位小寫十六進位）",
    "request_id": "請求識別（32 位小寫十六進位）", "preview_identity": "預覽識別（32 位小寫十六進位）",
    "save_id": "保存識別（32 位小寫十六進位）", "schema_version": "開發 schema 版本",
}


def _clear(st):
    st.session_state.pop(STATE_KEY, None)
    st.session_state.pop(SELECT_KEY, None)


def render(st, reader=None):
    """reader 由可信開發 caller 注入；未配置、輸入／上下文變更、失敗均不留舊內容。"""
    st.title("開發快照唯讀查閱")
    st.warning(WARNING)
    st.text(CONFIDENTIAL)
    st.text(FRESHNESS)
    st.text("完整身份不是權限 token；沒有登入授權，僅能在隔離開發環境使用。")
    if type(reader) is not DevelopmentSnapshotReader:
        _clear(st)
        st.info("功能未啟用：尚未配置受控開發唯讀 reader；不連線、不建庫或產生示範資料。")
        return

    values = {f.name: st.text_input(LABELS[f.name], key=INPUT_PREFIX + f.name,
                                    value=SCHEMA_VERSION if f.name == "schema_version" else "")
              for f in fields(DevelopmentSnapshotIdentity)}
    current = tuple(values.items())
    stored = st.session_state.get(STATE_KEY)
    if stored is not None and (stored["reader"] is not reader
                               or stored["context"] is not reader.source_context
                               or stored["inputs"] != current):
        _clear(st)
        stored = None
    if st.button(LOAD):
        _clear(st)
        stored = None
        try:
            identity = DevelopmentSnapshotIdentity(**values)
            validate_identity(identity)  # 不合法 schema／身份在 SQL 前拒絕。
            snapshot = reader.read(identity)
            if snapshot is None:
                raise ValueError("not found")
            batch_view(reader, snapshot)  # 確認 handle 來自此 reader；不接受字典。
            stored = {"reader": reader, "context": reader.source_context,
                      "inputs": current, "snapshot": snapshot}
            st.session_state[STATE_KEY] = stored
        except Exception:
            _clear(st)
            st.error("查閱失敗：完整身份、版本或整批內容無法驗證。舊結果及下載已清除，未顯示半批；請核對後明確重試。")
            return
    if stored is None:
        st.info("請輸入完整身份，按唯讀查閱才讀取；不提供全庫搜尋或列舉。")
        return

    # 先完成封印檢查及全部輸出建構，再顯示任何批次數字，失敗不洩漏半批。
    try:
        snapshot = stored["snapshot"]
        if snapshot.identity != DevelopmentSnapshotIdentity(**values):
            raise ValueError("固定 handle 與目前完整身份不一致")
        batch = batch_view(reader, snapshot)
        child_ids = [c["child_quote_id"] for c in batch["content"]["summary"]["children"]]
        selected = st.session_state.get(SELECT_KEY, child_ids[0])
        if selected not in child_ids:
            selected = child_ids[0]
            st.session_state[SELECT_KEY] = selected
        child = child_view(reader, snapshot, selected)
        exports = [(label, export_snapshot(reader, snapshot, child_quote_id=child_id, format=fmt))
                   for label, child_id, fmt in (
                       ("下載整批開發 JSON", None, "json"), ("下載整批開發純文字", None, "txt"),
                       ("下載本張開發 JSON", selected, "json"), ("下載本張開發純文字", selected, "txt"))]
    except Exception:
        _clear(st)
        st.error("固定查閱內容已失效；舊結果及下載已清除，請明確重新查閱。")
        return

    summary = batch["content"]["summary"]
    st.text(f"已核驗整批：{summary['child_count']} 張／{summary['product_qty']} 件")
    st.text("唯讀文件版本：" + batch["document_version"] + "｜內容摘要：" + batch["content_digest"])
    st.text(summary["batch_total_meaning"])
    st.text(summary["batch_totals_json"])
    for item in summary["children"]:
        st.text(f"第 {item['position']} 張｜{item['label']}｜成品數 {item['product_qty']}｜"
                f"{RESERVATION_WARNING}：{item['development_reserved_ref_no']}")
        st.text("本張固定精確金額／差額：" + json.dumps(item["amounts"], ensure_ascii=False))
    st.selectbox("查閱已核驗批次中的子張", child_ids, key=SELECT_KEY)
    fixed = child["content"]["child"]["fixed_child"]
    st.text("完整專屬描述：\n" + fixed["description"])
    st.text("完整共用描述：\n" + fixed["shared_description"])
    with st.expander("本張完整來源、配置、成本列、政策及條件圖面", expanded=True):
        st.text(json.dumps(child, ensure_ascii=False, indent=2))
    with st.expander("整批完整保存原值（含來源及持久保留）"):
        st.text(json.dumps(batch, ensure_ascii=False, indent=2))
    for label, output in exports:
        st.download_button(label, output.data, file_name=output.filename, mime=output.mime,
                           key=label, on_click="ignore")
