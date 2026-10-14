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
