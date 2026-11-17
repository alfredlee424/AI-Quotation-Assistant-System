"""不拆單：整份工單所有品項合併為一張報價（純核心，無 DB／UI）。

沿用 dynamic.analyze 的逐品項解析與必選展開；每個品項的每個頂層部位
（例：CMT1\\A001 桌面、CMT1\\B001 木腳）是一個「區塊」。同一頂層路徑可
出現多組區塊（多個品項），各自獨立檢查子項必填（must_chose）、規格及成本，
不跨區塊借用選擇或數量。整張報價只有在所有區塊與品項層級都完整時才可固定。

blocks(analysis) -> 區塊清單（root、number 為同 root 第幾組、issues、ready）
summary(analysis) -> 合併計算（ready、known_cost、total_cost…）
freeze(analysis, actor=..., reason=..., time=...) -> detached 合併文件
validate_document(document, current=None, trusted_catalog_digest=None) -> children
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_HALF_UP, localcontext
import re

from . import dynamic
from .dynamic_v1 import _audit, _digest, _money, _plain

VERSION = "combined-1"
DOCUMENT_TYPE = "local_dynamic_combined"
_DOCUMENT_FIELDS = {"document_type", "version", "children", "calc", "context",
                    "frozen_audit", "frozen_digest"}


def block_root(prodkind, path):
    """CMT1\\A001\\S004 -> CMT1\\A001；不屬於本產品類別的路徑回傳 None。"""
    parts = str(path or "").split("\\")
    if len(parts) < 2 or parts[0] != prodkind or not parts[1]:
        return None
    return parts[0] + "\\" + parts[1]


def _issue_roots(prodkind, issue):
    """問題 id 內含路徑者歸該區塊；否則依候選路徑；仍無法判定者屬品項層級。"""
    found = set(re.findall(re.escape(prodkind) + r"\\([A-Za-z0-9_]+)", str(issue.get("id", ""))))
    roots = {prodkind + "\\" + segment for segment in found}
    if not roots:
        roots = {block_root(prodkind, c.get("path")) for c in issue.get("candidates") or []} - {None}
    if not roots:
        roots = {block_root(prodkind, p) for p in issue.get("custom_paths") or []} - {None}
    return roots


def blocks(analysis):
    cat = analysis["context"]["catalog_snapshot"]
    prodkind = cat["prodkind"]
    counters, result = {}, []
    for child in analysis["children"]:
        roots = list(dict.fromkeys(r for r in (block_root(prodkind, i["path"]) for i in child["items"]) if r))
        issue_roots = [(issue, _issue_roots(prodkind, issue)) for issue in child["issues"]]
        # 有問題但尚未啟用任何明細的頂層部位（例如必選頂層缺規格）也要列為區塊。
        for _, found in issue_roots:
            roots.extend(r for r in sorted(found) if r not in roots)
        for root in roots:
            counters[root] = counters.get(root, 0) + 1
            items = [i for i in child["items"] if i["path"] == root or i["path"].startswith(root + "\\")]
            issues = [issue for issue, found in issue_roots if root in found]
            node = cat["nodes"].get(root)
            label = cat["labels"].get(node["optnoc"]) if node else None
            pending = [i for i in items if not i["selected"] or i.get("part_cost") is None]
            result.append(dict(block_id=child["id"] + ":" + root, root=root, label=label,
                               number=counters[root], child_id=child["id"], ordinal=child["ordinal"],
                               group=child["group"], group_ordinal=child["group_ordinal"],
                               description=child["description"], product_qty=child["product_qty"],
                               items=deepcopy(items), issues=deepcopy(issues),
                               ready=not issues and not pending and bool(items)))
    return result


def product_issues(analysis, child):
    """不屬於任何區塊的品項層級問題（例如原文範圍、係數、過期輸入）。"""
    prodkind = analysis["context"]["catalog_snapshot"]["prodkind"]
    return [i for i in child["issues"] if not _issue_roots(prodkind, i)]


def summary(analysis):
    children = analysis["children"]
    ready = (bool(children) and not analysis["global_issues"] and all(c["ready"] for c in children))
    with localcontext(Context(prec=80, rounding=ROUND_HALF_UP)):
        known = sum((Decimal(i["raw_cost"]) for c in children for i in c["items"] if i.get("raw_cost") is not None),
                    Decimal(0))
        rates = {c["calc"].get("quote_rate") for c in children}
        calc = dict(known_cost=_money(known), complete=ready, quote_rate=None, tax_rate="0.05",
                    product_count=len(children), block_count=len(blocks(analysis)),
                    rounding="Decimal precision=80; ROUND_HALF_UP; round totals after summation",
                    pricing_policy="所有區塊成本加總×invdoc.quo_rate；折扣0%；外加5%稅")
        if len(rates) == 1 and None not in rates:
            calc["quote_rate"] = next(iter(rates))
        if ready and calc["quote_rate"] is not None:
            subtotal = known * Decimal(calc["quote_rate"])
            calc.update(total_cost=_money(known), subtotal=_money(subtotal),
                        tax_amount=_money(subtotal * Decimal("0.05")),
                        total_price=_money(subtotal * Decimal("1.05")))
        else:
            calc["complete"] = False
    return calc


def _rebuilt(context):
    if not isinstance(context, dict) or context.get("version") != dynamic.VERSION:
        raise ValueError("合併報價只支援 " + dynamic.VERSION + " 分析")
    return dynamic._rebuild(context)


def freeze(analysis, *, actor, reason, time=None):
    """整份工單一次固定；任一區塊或品項未完整即拒絕。不寫檔、不配號。"""
    if analysis.get("version") != dynamic.VERSION:
        raise ValueError("合併報價只支援 " + dynamic.VERSION + " 分析")
    audit = _audit(dict(actor=actor, reason=reason, time=time or datetime.now(timezone.utc).isoformat()))
    rebuilt = _rebuilt(analysis["context"])
    if _plain(analysis["children"]) != rebuilt["children"]:
        raise ValueError("合併內容、結構或金額與輸入不一致")
    calc = summary(rebuilt)
    if not calc["complete"]:
        pending = [f"{b['root']} 第{b['number']}組" for b in blocks(rebuilt) if not b["ready"]]
        raise ValueError("仍有區塊／品項未完整，不可固定合併報價" + ("：" + "、".join(pending) if pending else ""))
    document = dict(document_type=DOCUMENT_TYPE, version=VERSION, children=rebuilt["children"], calc=calc,
                    context=deepcopy(analysis["context"]), frozen_audit=audit)
    document["frozen_digest"] = _digest(dict(children=document["children"], calc=calc))
    return deepcopy(document)


def validate_document(document, *, current=None, trusted_catalog_digest=None):
    """重建整份工單並比對；current 為保存端的最新可信分析。回傳 children。"""
    if (not isinstance(document, dict) or set(document) != _DOCUMENT_FIELDS
            or document["document_type"] != DOCUMENT_TYPE or document["version"] != VERSION):
        raise ValueError("不是支援的合併報價文件")
    _audit(document["frozen_audit"])
    rebuilt = _rebuilt(document["context"])
    if document["children"] != rebuilt["children"] or document["calc"] != summary(rebuilt):
        raise ValueError("合併內容、結構或金額與輸入不一致")
    if not document["calc"]["complete"]:
        raise ValueError("合併報價仍有未完整區塊，不可保存")
    if document["frozen_digest"] != _digest(dict(children=document["children"], calc=document["calc"])):
        raise ValueError("凍結內容已變更")
    if trusted_catalog_digest is not None and rebuilt["catalog_digest"] != trusted_catalog_digest:
        raise ValueError("快照不符可信主檔版本；需重新讀取主檔並分析")
    if current is not None:
        latest = _rebuilt(current["context"])
        if latest["input_digest"] != rebuilt["input_digest"] or latest["children"] != rebuilt["children"]:
            raise ValueError("工單來源或人工輸入已變更，合併固定已撤銷")
    return deepcopy(rebuilt["children"])
