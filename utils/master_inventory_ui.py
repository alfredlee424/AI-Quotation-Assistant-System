"""明確操作才查主檔的盤點工作區，不修改任何報價／工單草稿。"""
import json

from config import WORKGROUP
from engine.master_inventory import REPORT_VERSION, inventory_products


def render_master_inventory(st):
    st.subheader("產品線主檔缺口盤點（唯讀，不報價）")
    st.warning("請先確認目前連線為允許查詢的受控副本或環境；此處不驗證存取權限、不初始化或修補資料庫。")
    st.caption("盤點所有已讀取的規格與用量，不代表所有分支能組合成產品。零價不代表核准含價，資料齊全也不代表工程核准。")
    products_text = st.text_input("盤點產品代碼（逗號分隔，最多三個）", value="CMT1,MT,DT1", key="inventory_products", max_chars=80)
    source = st.text_input("資料來源／受控副本版本（人工說明）", key="inventory_source", max_chars=200)
    consent = st.checkbox("已確認允許唯讀查詢目前設定的資料庫", key="inventory_consent")
    context = (products_text, source, consent, WORKGROUP, REPORT_VERSION)
    stored = st.session_state.get("master_inventory_result")
    if stored and stored["context"] != context:
        st.session_state.pop("master_inventory_result", None)
        stored = None
    if st.button("執行唯讀主檔盤點", key="inventory_run", disabled=not consent):
        # 先清除，任何輸入／查詢錯誤均不可沿用上次成功結果。
        st.session_state.pop("master_inventory_result", None)
        stored = None
        products = tuple(part.strip() for part in products_text.split(","))
        try:
            document = inventory_products(products, source_label=source)
        except ValueError as exc:
            st.error(str(exc))
        except Exception:
            st.error("主檔讀取失敗，本次未產生盤點結果。請由管理人員檢查連線、權限及資料表；不顯示敏感連線資訊。")
        else:
            stored = {"context": context, "document": document}
            st.session_state["master_inventory_result"] = stored
    if not stored:
        st.info("填寫來源並按執行才會查詢，不會自動載入主檔或範例資料。")
        return
    document = stored["document"]
    st.caption(f"事業別 {document['workgroup']}｜讀取完成 {document['read_finished_at']}｜來源摘要 {document['source_digest'][:16]}")
    st.warning("以下是本次讀取結果，不會自動隨主檔更新；跨查詢一致性快照未獲保證，正式驗收須使用受控靜態副本。")
    st.dataframe([{"產品代碼": p["prodkind"], "產品名稱": p["product_name"], "報價係數": p["quote_rate"],
                   "結構筆數": p["source_counts"]["edges"], "規格筆數": p["source_counts"]["options"],
                   "用量筆數": p["source_counts"]["quantities"], "資料問題": p["data_error_count"],
                   "待人工核對": p["review_count"]} for p in document["products"]], hide_index=True, use_container_width=True)
    for product in document["products"]:
        with st.expander(f"{product['prodkind']}｜缺口及部位盤點", expanded=True):
            st.text(f"來源摘要：{product['source_digest']}")
            if not product["data_error_count"]:
                st.info("本次未發現已支援檢查項目的資料錯誤；仍須核准規則、實際配置與需求，不能正式報價。")
            st.dataframe([{"類型": i["kind"], "分類": "資料問題" if i["level"] == "data_error" else "待人工核對",
                           "部位": i["path"], "代碼": i["code"], "說明": i["message"], "問題識別": i["issue_id"]}
                          for i in product["issues"]], hide_index=True, use_container_width=True)
            st.dataframe([{"部位": p["path"], "名稱": p["label"], "根部可達": p["reachable"],
                           "規格數": p["option_count"], "正價筆數": p["positive_cost_count"], "零價筆數": p["zero_cost_count"],
                           "無效單價筆數": p["invalid_cost_count"], "用量來源判定": p["quantity_mode"],
                           "驅動部位": p["driver_path"], "缺少驅動代碼": "、".join(p["missing_driver_codes"]),
                           "工費候選（未核准）": p["labor_candidate"]} for p in product["paths"]],
                         hide_index=True, use_container_width=True)
    st.caption("下載含內部產品名稱、路徑、代碼與資料問題，不包含採購單價數值；請依公司資料政策保管。")
    st.download_button("下載主檔缺口報告（非報價單）", json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False),
                       file_name=f"master-inventory-{document['source_digest'][:16]}.json", mime="application/json")
