"""受驗證來源／固定文件的純顯示轉接；不查價、不計算、不授權、不改寫輸入。

呼叫端先使用 inspect_source / Workflow.retrieve 核驗來源。這裡的 dict
只供文字元件呈現，絕不可反向充當請求或用來決定 confirm/save 權限。
數值保留原字串（含精度與尾零）；None／不存在、空字串與零分開。
"""
from __future__ import annotations

import json


MISSING = "未提供／未驗證"
EMPTY = "（空白）"
NOT_CREATED = "尚未產生"
COST_COLUMNS = (
    ("成本序號", "seq_no"), ("部位名稱", "part_desc"), ("完整路徑", "path"),
    ("規格代碼", "spc_code"), ("規格說明", "spdsc"), ("單位", "unit"),
    ("每件項目數", "line_qty"), ("本張總項目數", "qty"),
    ("用料量", "stdqty"), ("裁切量", "stdpar"),
    ("採購單價", "compri"), ("明細成本", "part_cost"),
)
MONEY_COLUMNS = (
    ("核心成本合計", "total_cost"), ("折扣前小計", "subtotal"),
    ("折扣額", "discount_amount"), ("未稅金額", "after_discount"),
    ("稅額", "tax_amount"), ("含稅金額", "total_price"),
)
AMOUNT_COLUMNS = (
    MONEY_COLUMNS[0], ("報價係數", "quote_rate"), MONEY_COLUMNS[1],
    ("折扣率", "discount_rate"), *MONEY_COLUMNS[2:4],
    ("稅率", "tax_rate"), *MONEY_COLUMNS[4:],
    ("展示成本列合計－核心成本", "cost_display_difference"),
    ("含稅－未稅－稅額差額", "tax_rounding_difference"),
)
SUBJECTS = {"surface_hole": "桌面開孔", "underdesk_wiring": "桌下走線",
            "hole_position": "孔位", "partition_position": "格位"}
STATES = {"FORBIDDEN": "禁止", "REQUIRED": "需要", "NOT_APPLICABLE": "不適用"}
DISPOSITIONS = {"specification": "合成選配候選", "condition": "條件／待正式工程核對",
                "included_process": "合成含價工序", "metadata": "來源標頭"}


def display(value, *, missing=MISSING):
    if value is None:
        return missing
    if value == "":
        return EMPTY
    return str(value)


def _object(value):
    return {} if value is None else json.loads(value)


def mapped(value, columns):
    return {label: display(value.get(key)) for label, key in columns}


def cost_rows(child):
    # 不過濾零價／空規格、不依路徑合併；qty 已含成品數，不再相乘。
    return [mapped(row, COST_COLUMNS) for row in child["rows"]]


def amounts(child):
    return [{"金額項目": label, "固定值": display(child["amounts"].get(key))}
            for label, key in AMOUNT_COLUMNS]


def gap_rows(gaps):
    return [{"缺口": display(g.get("label")),
             "適用張次": "、".join(map(str, g.get("positions", []))) or MISSING,
             "部位代碼": "、".join(g.get("parts", [])) or "未指定部位／全張",
             "來源依據": "\n".join(f"第 {s['line_no']} 行：{s['text']}" for s in g.get("sources", [])),
             "缺少內容": display(g.get("missing")), "需由誰確認": display(g.get("required_approver")),
             "正式依據": display(g.get("formal_reference")), "正式版本": display(g.get("formal_version")),
             "正式核准狀態": "未核准" if g.get("formal_approved") is False else "未驗證（展示不授權）"}
            for g in gaps]


def _size(spec, unit):
    return f"深 {display(spec.get('depth'))} × 寬 {display(spec.get('width'))} {display(unit)}"


def source_summary(document):
    """尚未計價；cm 僅是本版明示的合成候選，不宣稱原文有單位。"""
    shared = "\n".join(document["raw_text"].split("\n")[n - 1] for n in document["shared_line_numbers"])
    result = []
    for index, child in enumerate(document["children"], 1):
        result.append({
            "header": {"張次": str(index), "驗收單號": "尚未配號", "來源單號": document["original_order_no"],
                       "品項": child["shape"] + child["corner"], "合成候選尺寸": _size(child, "cm（待人工接受）"),
                       "成品數": display(child["product_qty"]), "成本狀態": "尚未計算成本"},
            "description": child["raw"], "shared_description": shared,
            "exception": ("面無孔、保留桌下走線；孔位不適用，格位仍待工程核對。" if child["no_hole"]
                          else "每件一孔及一只單掀鋁盒；孔位與格位仍待工程核對。"),
            "requirements": [{"來源行": str(d["source_line_no"]), "完整片段": d["text"],
                              "處置": DISPOSITIONS.get(d["disposition"], MISSING),
                              "部位代碼": "、".join(d["parts"]) or "條件／來源資訊"}
                             for d in document["dispositions"] if child["source_item_id"] in d["targets"]
                             or d["disposition"] == "metadata"],
        })
    return result


def confirmation_label(view):
    return "單次測試確認" if view.get("confirmation_mode") == "single-test-save" else "舊兩次核對"


def phase_label(view):
    if view.get("confirmation_mode") == "single-test-save":
        if view["invalidated"]:
            return "已失效，請重新產生"
        return {"SAVED": "已完成測試報價", "SAVE_FAILED": "測試存檔未完成，可重試",
                "PREVIEW": "待確認並測試存檔", "RESERVING": "產生中",
                "RESERVATION_FAILED": "產生未完成，可重試"}.get(view["phase"], MISSING)
    if view["invalidated"]:
        return "已失效（僅可查閱，須明確建新版）"
    if view["phase"] == "SAVED":
        return "已保存至驗收庫（非正式）"
    if view["phase"] == "SAVE_FAILED":
        return "保存失敗待重試（原身份）"
    if view["phase"] in ("RESERVING", "RESERVATION_FAILED"):
        return "已計算／缺配號，待原身份重試"
    if view["phase"] == "PREVIEW":
        return {0: "已計算／待第 1 次核對", 1: "已計算／待第 2 次核對",
                2: "可保存至驗收資料庫（非正式）"}.get(view["stage"], MISSING)
    return MISSING


def fixed_summary(view):
    """只讀 Workflow.retrieve 已核驗的含號文件；保存後仍使用同一固定內容。"""
    document = view["document"]
    request = document["fixed_request"]
    children, overview = [], []
    def heading(value):
        return display(value, missing=NOT_CREATED)
    for numbered in document["children"]:
        child = numbered["fixed_child"]
        dimensions = _object(child.get("dimensions_json"))
        spec = dimensions.get("source", {})
        header = {"張次": heading(child.get("position")), "驗收單號": display(numbered.get("ref_no"), missing="尚未配號"),
                  "來源單號": heading(request["source"].get("original_order_no")), "品項": heading(child.get("label")),
                  "合成產品代碼": heading(child.get("prodkind")),
                  "尺寸": _size(spec, dimensions["unit"]) if spec and dimensions.get("unit") else NOT_CREATED,
                  "成品數": heading(child.get("product_qty")), "事業別": heading(request.get("workgroup")),
                  "版本": heading(view.get("version")), "操作人（自填）": heading(document["context"].get("actor")),
                  "固定內容建立時間": display(document.get("created_at"), missing=NOT_CREATED),
                  "快照保存時間": display((view.get("saved") or {}).get("created_at"), missing=NOT_CREATED),
                  "來源程式（固定來源標記）": heading(request.get("origin"))}
        condition_document = _object(child.get("conditions_json"))
        conditions = condition_document.get("records", [])
        drawings = _object(child.get("drawings_json"))
        formal = "未核准" if condition_document.get("formal_approved") is False else "未驗證（展示不授權）"
        geometry = "幾何未驗證" if drawings.get("geometry_verified") is False else "幾何狀態未驗證"
        children.append({"header": header, "description": child["description"],
                         "shared_description": child["shared_description"], "rows": cost_rows(child),
                         "amounts": amounts(child),
                         "conditions": [{"條件": SUBJECTS.get(c["subject"], MISSING),
                                         "合成處置": STATES.get(c["state"], MISSING),
                                         "固定說明": display(c.get("value")), "正式工程": formal + "／" + geometry}
                                        for c in conditions]})
        overview.append({key: header[key] for key in ("張次", "驗收單號", "品項", "尺寸", "成品數")} |
                        mapped(child["amounts"], MONEY_COLUMNS) | {"目前狀態": phase_label(view)})
    return {"children": children, "overview": overview,
            "totals": mapped(_object(request["totals_json"]), MONEY_COLUMNS),
            "gaps": gap_rows(_object(request["source"]["history_json"])["formal_gaps"])}


def creation_todos(*, inspected, inspection_exists, actor, source_confirmed, assumptions, accepted):
    """鏡映 UI 既有條件的說明，不是 workflow 的安全驗證或授權。"""
    result = []
    if not actor.strip():
        result.append("請填寫操作人（自填紀錄，非核准簽章）。")
    if len(actor) > 80:
        result.append("操作人超過 80 字，請縮短；不會自動截斷。")
    if not inspected:
        result.append("來源已修改，請重新解析。" if inspection_exists else "來源尚未解析，請先貼上完整含單號標頭的來源並解析。")
    for assumption in assumptions:
        if assumption["id"] not in accepted:
            result.append("尚未接受合成假設：" + assumption["label"])
    if not source_confirmed:
        result.append("尚未核對來源：請核對四張順序、尺寸、成品數及完整共用要求後明確勾選。")
    return result


def action_todos(view):
    """只說明既有按鈕狀態；按鈕及服務仍使用原始 workflow 欄位判斷。"""
    common = []
    if view["invalidated"]:
        common.append("本版已永久失效，請重新解析目前來源並明確建立新版本。")
    if view["saved"] is not None:
        common.append("本版已保存；不可重複核對或另存，請直接查閱。修改內容須建新版。")
    if view["document"] is None:
        common.append("尚缺配號／完整含號預覽，請明確重試原身份配號／核驗。")
    result = {"confirm_1": list(common), "confirm_2": list(common), "save": list(common),
              "resume": [s for s in common if "缺配號" not in s]}
    for stage in (1, 2):
        if view["next_stage"] != stage and not common:
            result[f"confirm_{stage}"].append(f"第 {stage} 次核對已完成。" if view["stage"] >= stage
                                               else "請先完成第 1 次核對，再分開進行第 2 次核對。")
    if view["stage"] != 2:
        result["save"].append("尚待第 1 次及第 2 次核對。" if view["stage"] == 0 else "尚待第 2 次核對。")
    return result
