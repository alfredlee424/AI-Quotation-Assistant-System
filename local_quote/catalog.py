"""五張主檔的純資料邊界；不連線、不載入環境或一般 repository。

build_catalog 也供離線測試使用；測試資料不得當成 runtime 主檔替代品。
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math


def number(value, label, *, positive=False):
    if isinstance(value, bool):
        raise ValueError(f"{label}：必須為有限數值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{label}：必須為有限數值") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"{label}：必須為{'正' if positive else '非負'}有限數值")
    converted = float(result)
    if (not math.isfinite(converted) or abs(result) > Decimal("3.4028234663852886e38")
            or (result != 0 and converted == 0)):
        raise ValueError(f"{label}：超出保存數值範圍")
    return converted


def build_catalog(workgroup, category, tables):
    """驗證同工作群、類別內完整直接父子樹；不推測缺少的主檔內容。"""
    category = deepcopy(category)
    code = category.get("prodkind")
    if (not isinstance(workgroup, str) or not workgroup or len(workgroup) > 3
            or not isinstance(code, str) or not code or "\\" in code
            or not isinstance(category.get("codsc"), str)):
        raise ValueError("未選擇有效主檔類別或工作群")
    if category.get("workgroup", workgroup) != workgroup:
        raise ValueError("類別工作群不一致")
    if category.get("ordkind", "1") != "1":
        raise ValueError("不是產品類別")
    category = {k: category[k] for k in ("prodkind", "codsc", "quo_rate")}
    prefix = code + "\\"

    def rows(table):
        return [deepcopy(dict(r)) for r in tables[table] if r.get("workgroup") == workgroup]

    nodes = {}
    for row in rows("ordstr"):
        path = row.get("pathc", "")
        if not path.startswith(prefix):
            continue
        parent, optno = path.rsplit("\\", 1)
        if (not optno or parent != row.get("pathf") or optno != row.get("optnoc")
                or path in nodes or len(path) > 100
                or str(row.get("must_chose") or "").upper() not in {"Y", "N", ""}):
            raise ValueError("產品主檔父子路徑、必選旗標或識別不一致")
        nodes[path] = row
    if not nodes:
        raise ValueError("所選類別沒有完整結構樹")
    if any(n["pathf"] != code and n["pathf"] not in nodes for n in nodes.values()):
        raise ValueError("產品結構樹缺少父節點")
    # 深度優先的穩定順序，不依 SQL 回傳順序或不同主檔 seq 型態。
    nodes = dict(sorted(nodes.items()))
    optnos = {n["optnoc"] for n in nodes.values()}
    definitions = {}
    for row in rows("ordspd"):
        optno = row.get("optno")
        if optno not in optnos:
            continue
        if optno in definitions or not isinstance(row.get("optdesc"), str) or not row["optdesc"].strip():
            raise ValueError("選項名稱主檔缺少或不唯一")
        definitions[optno] = row
    if set(definitions) != optnos:
        raise ValueError("結構樹缺少選項名稱主檔")
    options = {p: [] for p in nodes}
    for row in rows("ordspe"):
        path = row.get("path")
        if path not in nodes:
            continue
        if (not isinstance(row.get("code"), str) or not row["code"]
                or not isinstance(row.get("codsc"), str) or not row["codsc"].strip()
                or any(o["code"] == row["code"] for o in options[path])):
            raise ValueError("同一路徑的規格代碼或名稱缺少／不唯一")
        options[path].append(row)
    for values in options.values():
        values.sort(key=lambda r: r["code"])
    rules = {p: [] for p in nodes}
    for row in rows("ordqty"):
        if row.get("path") in nodes:
            rules[row["path"]].append(row)
    return {"workgroup": workgroup, "category": category, "prodkind": code,
            "nodes": nodes, "options": options, "rules": rules,
            "labels": {k: v["optdesc"] for k, v in definitions.items()}}


def path_label(catalog, path):
    return " / ".join(catalog["labels"][p] for p in path.split("\\")[1:])


def entries(catalog, path):
    """只有真正有子節點、沒有規格的結構容器才可生成零價結構列。"""
    options = catalog["options"][path]
    optno = catalog["nodes"][path]["optnoc"]
    if not options and any(n["pathf"] == path for n in catalog["nodes"].values()):
        options = [{"code": "", "codsc": catalog["labels"][optno], "compri": 0,
                    "structural": True}]
    return [dict(o, path=path, optno=optno, optdesc=catalog["labels"][optno]) for o in options]


def material_groups(catalog):
    # 僅複製既有 CMT1 明列互斥政策，不把任意兄弟節點當作替代方案。
    if catalog["prodkind"] != "CMT1":
        return {}
    return {p: [p + "\\" + s for s in ("S000", "S001", "S008")
                if p + "\\" + s in catalog["nodes"]]
            for p in catalog["nodes"] if p.endswith("\\S005")}
