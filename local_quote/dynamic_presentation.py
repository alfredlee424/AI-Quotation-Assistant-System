"""純顯示／下載投影；不查主檔、不計價、不建立單號或讀取時鐘。"""
from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import io
import json
import re
import unicodedata


FIELDS = ("workgroup", "ref_no", "path", "qty", "spc_code", "spdsc",
          "stdqty", "stdpar", "compri", "abndat", "abntim", "usrno", "prgno")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


def safe_cell(value):
    """普通數字／路徑／日期原樣保留；僅危險文字加單引號，不改快照。

    csv quoting 保護 tab/newline 欄界；公式前綴連同前置空白、控制／格式
    字元及全形變體一併檢查。普通負數不是公式。
    """
    text = "" if value is None else str(value)
    if _NUMBER.fullmatch(text):
        return text
    probe = unicodedata.normalize("NFKC", text)
    probe = "".join(c for c in probe if not c.isspace() and unicodedata.category(c) not in {"Cc", "Cf"})
    return "'" + text if probe.startswith(("=", "+", "-", "@")) else text


def tsv(rows):
    """UTF-8、Tab 分隔、固定 13 欄表頭；不加第十四欄或假單號。"""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
    writer.writerow(FIELDS)
    writer.writerows([safe_cell(row.get(key)) for key in FIELDS] for row in rows)
    return stream.getvalue()


def _quantity(item):
    # 核心遇到缺價會保留 qty=None；已知數量仍可獨立顯示，不補成本。
    if item.get("qty") is not None:
        return item["qty"]
    if not item.get("selected") or item.get("line_qty") is None:
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = 80
            quantity = Decimal(str(item["line_qty"]))
            if item["quantity_basis"] == "per_product":
                quantity *= Decimal(str(item["product_qty"]))
            return str(quantity) if quantity.is_finite() and quantity > 0 else None
    except (KeyError, InvalidOperation, ValueError):
        return None


def report_rows(child, catalog, *, receipt=None):
    """草稿無配號／保存時間；正式一律使用已持久化 receipt 的單張與稽核。"""
    ref, date, clock, actor = "", "", "", ""
    if receipt is not None:
        document = receipt["document"]
        child, catalog = document["child"], document["context"]["catalog_snapshot"]
        ref = receipt["ref_no"]
        instant = datetime.fromisoformat(receipt["confirmed_at"])
        date, clock = instant.strftime("%Y.%m.%d"), instant.strftime("%H:%M:%S")
        actor = document["frozen_audit"]["actor"]
    return [dict(zip(FIELDS, (catalog["workgroup"], ref, item["path"], _quantity(item),
                              item.get("code"), item.get("codsc"), item.get("stdqty"),
                              item.get("stdpar"), item.get("compri"), date, clock, actor, "DYNAMIC1")))
            for item in child["items"]]


def screen_rows(child, catalog, *, receipt=None):
    if receipt is not None:
        child = receipt["document"]["child"]
        catalog = receipt["document"]["context"]["catalog_snapshot"]
    rows = report_rows(child, catalog, receipt=receipt)
    for row, item in zip(rows, child["items"]):
        node = catalog["nodes"][item["path"]]
        gaps = [g["message"] for g in child["issues"]
                if item["path"] in g["id"] or g["id"] in item.get("requirements", [])]
        row.update({"階層名稱": "　" * max(0, item["depth"] - 1) + item["label"],
                    "父路徑": item["parent_path"], "必選": node.get("must_chose"),
                    "缺項": "；".join(gaps), "成品數": child["product_qty"],
                    "數量基準": item["quantity_basis"], "每件量／本張總量輸入": item["line_qty"],
                    "來源": json.dumps(item["sources"], ensure_ascii=False),
                    "人工": json.dumps(item["audit"], ensure_ascii=False),
                    "臨時件": bool(item.get("custom")), "明細成本": item["part_cost"]})
    # 畫面的 dataframe 也可被使用者匯出，必須和明確下載採同樣防護。
    return [{key: safe_cell(value) for key, value in row.items()} for row in rows]


_TERMS = {
    "shared": "共用", "targeted": "特定", "local": "特定／局部", "product": "品項",
    "chair": "主席", "attendee": "列席", "corner": "彎角", "straight": "平直",
    "curved": "平彎", "double_curved": "平直雙彎", "chamfered": "四邊稍導角", "quarter_round": "1/4圓",
    "tabletop": "桌面", "tabletop_accessory": "桌面配件", "tabletop_hole": "桌面孔",
    "legs": "桌腳", "leg_hole": "桌腳走線孔", "under_table": "桌下", "shelf": "置物板",
    "divider": "隔板", "divider_hole": "隔板孔", "unassigned": "未指派", "explicit_path": "明示路徑",
    "dimension": "尺寸", "shape": "外型", "edge": "邊加工", "box": "線盒", "hole": "挖孔",
    "constraint": "限制（非付費數量）", "routing": "走線", "assembly": "組裝工序",
    "material": "面材", "coating": "塗裝", "position": "位置", "vertical_position": "上下位置",
    "external_confirmation": "外部確認", "configuration": "主檔配置", "role": "產品角色",
    "legacy": "明示規格", "unknown": "未知", "round_hole": "圓孔", "rectangular_hole": "矩形孔",
    "holes_allowed": "是否允許挖孔", "legs_allowed": "是否允許木腳", "drawing": "圖面",
    "parsed": "已解析", "matched": "已匹配", "pending_master": "待主檔／候選確認",
    "pending_units": "待單位", "pending_external": "待圖面／訂單核對", "unrecognized": "未知",
    "overridden": "已被覆寫", "not_applicable": "不適用", "duplicate": "重複（不重計）", "conflict": "衝突",
    "per_product": "每件", "total": "本張總量", "forbidden": "禁止",
    "cut": "削角", "cable_routing": "桌下走線", "single_sided": "單面置物板",
    "foot_lock": "腳粒鎖好", "remove_protective_film": "撕保護膜", "817_walnut": "817胡桃",
    "340_red_walnut": "紅胡340", "808_white_oak": "808白橡", "white_oak_paper": "白橡紙",
    "walnut": "胡桃", "red_wood": "紅木", "white_oak": "白橡", "center": "置中",
    "below_routing": "走線下方", "master": "依主檔", "order": "訂單", "round": "圓形",
    "rectangle": "矩形", "single_flip_aluminum": "單掀鋁", "silver": "銀色",
    "axes": "尺寸軸", "unit": "單位", "confirmation_required": "需確認", "reference": "參照",
    "type": "型式", "dimensions": "尺寸", "color": "顏色", "brush": "毛刷", "diameter": "直徑",
    "cm": "公分", "mm": "毫米", "catalog_label": "主檔標示",
}


def semantic_text(value):
    """僅翻譯已知詞彙；未知值不隱藏、不修改底層語意。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, dict):
        return "；".join(f"{semantic_text(k)}：{semantic_text(value[k])}" for k in sorted(value))
    if isinstance(value, list):
        return "、".join(semantic_text(v) for v in value)
    return _TERMS.get(str(value), str(value))


def semantic_rows(child):
    """新版語意獨立投影；不更動13欄契約，不將禁挖片數當付費孔數。"""
    rows = []
    for req in child.get("normalized_requirements", []):
        origin = req.get("origin") or {}
        quantity = req.get("quantity")
        countable = (req.get("effective") and not req.get("conflict")
                     and req.get("kind") not in {"constraint", "external_confirmation"}
                     and req.get("status") != "pending_units")
        total = _quantity(dict(selected=bool(countable), line_qty=quantity,
                               quantity_basis=req.get("basis"), product_qty=child["product_qty"]))
        row = {
            "範圍": semantic_text(origin.get("scope")), "群組": child.get("group"),
            "群組內適用品項": semantic_text(req.get("targets", [])), "部位": semantic_text(req.get("part")),
            "需求類型": semantic_text(req.get("kind")), "屬性": semantic_text(req.get("attribute")),
            "需求": semantic_text(req.get("value")), "每件量": quantity if req.get("basis") == "per_product" else None,
            "數量基準": semantic_text(req.get("basis")), "可算本張總量": total,
            "數量說明": "限制片數／非付費孔數" if req.get("kind") == "constraint" else "需求量，非成本／完備保證",
            "來源行": origin.get("line"), "來源原文": origin.get("raw"), "解析片語": origin.get("text"),
            "優先級": req.get("priority"), "是否適用": "是" if req.get("effective") else "否",
            "狀態": semantic_text(req.get("status")), "覆寫需求": semantic_text(req.get("override", [])),
            "衝突需求": semantic_text(req.get("conflict", [])),
            "候選": json.dumps(req.get("candidates", []), ensure_ascii=False, sort_keys=True),
            "可掛接部位": semantic_text(req.get("custom_paths", [])),
            "已選規格": json.dumps(req.get("selected"), ensure_ascii=False, sort_keys=True),
            "需求識別": req.get("id"), "關聯需求": semantic_text(req.get("requirement_ids", [])),
        }
        rows.append({key: safe_cell(value) for key, value in row.items()})
    return rows


def hierarchy_rows(catalog, child):
    """包括未啟用選配，不能把 active items 誤稱完整 ordstr。"""
    active = set(child["active_paths"])
    return [{"完整路徑": safe_cell(path), "父路徑": safe_cell(node["pathf"]),
             "階層名稱": safe_cell(catalog["labels"][node["optnoc"]]),
             "必選": safe_cell(node.get("must_chose")), "啟用": path in active,
             "已有規格": path in child["selections"]}
            for path, node in catalog["nodes"].items()]


def legacy_report_rows(history):
    """舊 BatchStore 的固定資料；舊版未保存異動日／時／人員，留空不冒用。

    confirmed_at 是舊版 adddate 的依據，不冒充未保存的 abndat/abntim。
    """
    result = []
    for doc in history["children"]:
        for item in doc["child"]["calc"]["items"]:
            row = {key: item.get(key) for key in FIELDS}
            row.update(workgroup=doc["workgroup"], ref_no=doc["ref_no"], prgno="LOCAL4",
                       abndat=doc.get("abndat"), abntim=doc.get("abntim"), usrno=doc.get("usrno"))
            result.append(row)
    return result
