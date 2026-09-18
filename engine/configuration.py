"""完整路徑配置與變更驗證。LLM 只能提案，不能指定價格或 BOM 用量。"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math

from config import PRODUCT_PREFIX, WORKGROUP
from database import repository as repo


def number(value, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必須是有效數字")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{label}必須是有效數字") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"{label}必須是{'大於零' if positive else '非負'}有限數字")
    converted = float(result)
    if not math.isfinite(converted):
        raise ValueError(f"{label}超過支援的數值範圍")
    return converted


def normalize_selections(selections: dict) -> dict:
    """接受舊鍵格式，但不接受同一路徑的衝突選擇。"""
    normalized = {}
    for value in selections.values():
        if not isinstance(value, dict) or not value.get("path"):
            raise ValueError("每個選項都必須提供完整路徑")
        item = deepcopy(value)
        path = str(item["path"]).strip()
        item.update(path=path, code=str(item.get("code", "")).strip())
        item["line_qty"] = number(item.get("line_qty", 1), f"{path} 每件產品的項目數量", positive=True)
        if path in normalized and (
            normalized[path]["code"], normalized[path]["line_qty"]
        ) != (item["code"], item["line_qty"]):
            raise ValueError(f"同一路徑有衝突選擇：{path}")
        normalized[path] = item
    return normalized


def invalidate_preview(draft: dict) -> None:
    draft.pop("preview", None)
    draft["calc_result"] = None
    draft["status"] = "CHECKING"


def load_catalog(prodkind: str, workgroup: str = WORKGROUP) -> dict:
    tree = repo.expand_ordstr_tree(prodkind, workgroup=workgroup)
    if not tree:
        raise ValueError(f"產品 {prodkind} 尚未設定選單結構，不能報價")
    nodes = {n["pathc"]: n for n in tree}
    for path, node in nodes.items():
        if not path.startswith(prodkind + "\\") or path.rsplit("\\", 1)[0] != node["pathf"]:
            raise ValueError(f"選單父子路徑不一致：{path}")
        if node["pathf"] != prodkind and node["pathf"] not in nodes:
            raise ValueError(f"選單缺少父節點：{node['pathf']}")
    options = {p: repo.get_options_by_path(p, workgroup=workgroup) for p in nodes}
    labels = {c["optno"]: c.get("optdesc", c["optno"])
              for c in repo.get_all_option_categories(workgroup=workgroup)}
    return {"prodkind": prodkind, "nodes": nodes, "options": options, "labels": labels}


def _entry(path: str, option: dict, catalog: dict, line_qty=1, *, automatic=False) -> dict:
    optno = path.rsplit("\\", 1)[-1]
    return {"path": path, "optno": optno, "optdesc": catalog["labels"].get(optno, optno),
            "code": option.get("code", ""), "codsc": option.get("codsc", ""),
            "compri": option.get("compri"), "line_qty": line_qty, "automatic": automatic}


def _choice_groups(catalog: dict) -> dict[str, list[str]]:
    # CMT1 材料方案政策：S000/S001/S008 替代，S007 下板不是替代項。
    # 不將所有兄弟節點視為互斥；其他產品必須另行設定政策。
    if catalog["prodkind"] != "CMT1":
        return {}
    nodes = catalog["nodes"]
    return {p: [p + "\\" + s for s in ("S000", "S001", "S008") if p + "\\" + s in nodes]
            for p in nodes if p.endswith("\\S005")}


def resolve_configuration(draft: dict, catalog: dict | None = None) -> dict:
    """產出同一份供缺項提示、計價與快照使用的已啟用樹，不修改草稿。"""
    prodkind = str(draft.get("prodkind") or PRODUCT_PREFIX)
    catalog = catalog or load_catalog(prodkind, draft.get("workgroup", WORKGROUP))
    nodes, options = catalog["nodes"], catalog["options"]
    selected = normalize_selections(draft.get("selections", {}))
    errors, missing, allowed = [], [], {}
    for path, item in selected.items():
        if path not in nodes:
            errors.append(f"路徑不在目前產品選單：{path}")
            continue
        matches = [o for o in options[path] if o.get("code") == item["code"]]
        if not item["code"] and not options[path]:
            selected[path] = _entry(path, {"compri": 0}, catalog, item["line_qty"])
        elif len(matches) != 1:
            errors.append(f"規格不存在或不唯一：{path} / {item['code']}")
        else:
            selected[path] = _entry(path, matches[0], catalog, item["line_qty"],
                                    automatic=item.get("automatic", False))
    active = {prodkind}
    for path in selected:
        current = path
        while current in nodes:
            active.add(current)
            current = nodes[current]["pathf"]
    # 必選邊只在父分支啟用後生效，包含沒有規格的中間節點。
    for _ in range(len(nodes) + 1):
        before = len(active)
        for path, node in nodes.items():
            if node["pathf"] in active and (node.get("must_chose") or "").upper() == "Y":
                active.add(path)
        if len(active) == before:
            break
    for parent, children in _choice_groups(catalog).items():
        chosen = [p for p in children if p in active]
        if len(chosen) > 1:
            errors.append(f"材料方案互斥，請移除多餘分支：{parent}")
        elif parent in active and not chosen:
            missing.append(f"請選擇材料方案：{parent}")
            allowed[parent] = []
            for child in children:
                if options[child]:
                    allowed[parent].extend(dict(o, path=child) for o in options[child])
                else:
                    allowed[parent].append({"path": child, "code": "", "codsc": catalog["labels"].get(child.rsplit('\\', 1)[-1], child)})
    for path in nodes:  # 保留選單順序
        if path not in active:
            continue
        if path in selected:
            continue
        candidates = options[path]
        if not candidates:
            # 空選項葉不能當作免費；中間節點才可自動生成。
            if not any(n["pathf"] == path for n in nodes.values()):
                errors.append(f"已啟用項目沒有規格主檔：{path}")
            else:
                selected[path] = _entry(path, {"compri": 0}, catalog, automatic=True)
            continue
        target = candidates[0] if len(candidates) == 1 else None
        if "\\W001\\" in path and len(candidates) > 1:
            root = "\\".join(path.split("\\")[:2])
            material = root + "\\S005"
            kind = ("美耐板" if material + "\\S000" in active else
                    "美耐皿" if material + "\\S001" in active else
                    "色紙" if material + "\\S008" in active else None)
            matches = [o for o in candidates if kind and kind in o.get("codsc", "")]
            target = matches[0] if len(matches) == 1 else None
        if target:
            selected[path] = _entry(path, target, catalog, automatic=True)
        else:
            missing.append(f"請選擇規格：{path}")
            allowed[path] = [dict(o, path=path) for o in candidates]
    return {"valid": not errors and not missing, "errors": errors, "missing": missing,
            "allowed_options": allowed, "selections": {p: selected[p] for p in nodes if p in selected},
            "active_paths": active, "catalog": catalog}


def apply_proposal(draft: dict, proposal: dict) -> dict:
    """交易式套用白名單變更；錯誤提案不留下半套修改，不接受模型提供的成本。"""
    if draft.get("status") in ("SNAPSHOT_CREATED", "COMPLETED"):
        raise ValueError("已建立的報價不可修改，請建立新草稿")
    if set(proposal) - {"prodkind", "qty", "discount_rate", "changes", "questions"}:
        raise ValueError("變更提案包含不允許的欄位")
    updated = deepcopy(draft)
    product = proposal.get("prodkind") or draft.get("prodkind")
    categories = repo.get_product_categories(workgroup=draft.get("workgroup", WORKGROUP))
    category = next((c for c in categories if c["prodkind"] == product), None)
    if category is None:
        raise ValueError("請先選擇資料庫中可報價的產品類別")
    if product != draft.get("prodkind"):
        updated["selections"] = {}
        updated.pop("imported_quote", None)
    updated.update(prodkind=product, product_name=category.get("codsc") or product)
    if proposal.get("qty") is not None:
        updated["qty"] = number(proposal["qty"], "產品數量", positive=True)
    if proposal.get("discount_rate") is not None:
        from config import MAX_DISCOUNT_RATE
        discount = number(proposal["discount_rate"], "折扣率")
        if discount > min(MAX_DISCOUNT_RATE, 1):
            raise ValueError("折扣超過允許範圍")
        updated["discount_rate"] = discount
    selections = normalize_selections(updated.get("selections", {}))
    # 自動帶入的值依新分支重建，不能沿用前一種材料的工費。
    selections = {p: s for p, s in selections.items() if not s.get("automatic")}
    catalog = load_catalog(product, draft.get("workgroup", WORKGROUP))
    changes = proposal.get("changes", [])
    if not isinstance(changes, list):
        raise ValueError("changes 必須為清單")
    for change in changes:
        if not isinstance(change, dict) or set(change) - {"op", "path", "code", "line_qty"}:
            raise ValueError("選項變更只允許操作、完整路徑、規格代碼及項目數量")
        path, op = change.get("path", ""), change.get("op")
        if op == "remove" and path in selections:
            # 舊單重報可能保有已退休路徑；允許明確移除，但不可新增到舊路徑。
            selections = {p: s for p, s in selections.items() if p != path and not p.startswith(path + "\\")}
            continue
        if path not in catalog["nodes"]:
            raise ValueError(f"路徑不在產品選單：{path}")
        if op == "remove":
            selections = {p: s for p, s in selections.items() if p != path and not p.startswith(path + "\\")}
        elif op == "set":
            options = catalog["options"][path]
            code = change.get("code", "")
            matches = [o for o in options if o.get("code") == code]
            if not code and not options:
                matches = [{"compri": 0}]
            if len(matches) != 1:
                raise ValueError(f"無效或不唯一規格：{path} / {code}")
            quantity = number(change.get("line_qty", selections.get(path, {}).get("line_qty", 1)),
                              f"{path} 項目數量", positive=True)
            selections[path] = _entry(path, matches[0], catalog, quantity)
        else:
            raise ValueError("變更操作只能是 set 或 remove")
    updated["selections"] = selections
    resolved = resolve_configuration(updated, catalog)
    if resolved["errors"]:
        raise ValueError("；".join(resolved["errors"]))
    updated["selections"] = resolved["selections"]
    updated["missing_fields"] = resolved["missing"]
    updated["allowed_options"] = resolved["allowed_options"]
    updated["revision"] = int(draft.get("revision", 0)) + 1
    invalidate_preview(updated)
    updated.pop("pending_options", None)
    updated["questions"] = proposal.get("questions") or []
    draft.clear()
    draft.update(updated)
    return resolved
