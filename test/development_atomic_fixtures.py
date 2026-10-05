"""純合成 T29 typed fixture；刻意不從 trial/review 文件轉型。"""
from dataclasses import replace
from decimal import Decimal

from engine.development_atomic_contract import (
    MONEY_FIELDS, DevelopmentAmounts, DevelopmentAtomicChild, DevelopmentAtomicRequest,
    DevelopmentCostRow, DevelopmentSelection, DevelopmentSource, DevelopmentSourceLine, canonical,
)


def ident(value):
    return f"{value:032x}"


SHARED = "單面置物版\n孔靠單/格靠單\n合成色號/合成塗裝\n腳粒鎖好/撕保護膜"
EVIDENCE = canonical({"development_evidence": "synthetic, not approval", "version": "dev-1"})


def request(count=4):
    items = tuple(ident(10000 + i) for i in range(count))
    lines = [DevelopmentSourceLine(1, "合成來源工單（非客戶資料）", "context", None, ())]
    children = []
    for i, item in enumerate(items):
        qty = (2, 1, 6, 2)[i % 4]
        description = ("彎角面無孔有桌下走線" if i == 0 else f"合成品項 {i}") + f"；成品數 {qty}"
        lines.append(DevelopmentSourceLine(i + 2, description, "item", item, (item,)))
        selections, rows = [], []
        for j, (path, price, source) in enumerate((
                ("TEST", "0", "structural"), ("TEST\\PAID", "0.10", "ordqty"),
                ("TEST\\PAID\\CHILD", "0.235", "per_item_policy"), ("TEST\\ZERO", "0", "zero_cost")), 1):
            configuration = canonical({"code": f"C{j}", "development_basis": "synthetic", "enabled": True})
            selections.append(DevelopmentSelection(path, f"C{j}", "1", configuration))
            rows.append(DevelopmentCostRow(
                f"{j:05d}", path, f"C{j}", f"合成節點 {j}", "TEST", "合成配置", f"C{j}", "合成規格", "PCS",
                qty, "1", str(qty), "1.000000000000000001", "1", "1", price, price,
                price, price, price, source, EVIDENCE, EVIDENCE, configuration))
        money = dict(total_cost="0.34", subtotal="0.34", discount_amount="0.00",
                     after_discount="0.34", tax_amount="0.02", total_price="0.35")
        raw = dict(total_cost="0.335", subtotal="0.335", discount_amount="0",
                   after_discount="0.335", tax_amount="0.01675", total_price="0.35175")
        amounts = DevelopmentAmounts(**money, quote_rate="1", discount_rate="0", tax_rate="0.05",
                                     raw_amounts_json=canonical(raw), cost_display_difference="-0.005",
                                     tax_rounding_difference="-0.01")
        children.append(DevelopmentAtomicChild(
            ident(20000 + i), item, ident(30000 + i), i + 2, i + 1, qty, 0, "TEST", f"合成 {i}",
            description, SHARED, tuple(selections), tuple(rows), amounts,
            EVIDENCE, EVIDENCE, EVIDENCE, EVIDENCE))
    for shared in SHARED.split("\n"):
        lines.append(DevelopmentSourceLine(len(lines) + 1, shared, "shared", None, items))
    source = DevelopmentSource(ident(5), ident(6), "SYNTHETIC-ORDER", "synthetic-development",
                               "dev-parser-v1", "\n".join(line.text for line in lines), tuple(lines), EVIDENCE)
    totals = {key: format(sum((Decimal(getattr(c.amounts, key)) for c in children), Decimal(0)), ".2f") for key in MONEY_FIELDS}
    policy = dict(formula_version="usage-divided-by-yield-v1", rounding_version="independent-half-up-v1",
                  pricing_version="synthetic-dev-v1", currency="TWD", discount_rate="0", tax_rate="0.05")
    return DevelopmentAtomicRequest("103", ident(1), ident(2), ident(3), ident(4), 0,
                                    "isolated-development-test", source, tuple(children), canonical(totals), canonical(policy))


def change_child(req, **values):
    return replace(req, children=(replace(req.children[0], **values), *req.children[1:]))


def change_row(req, **values):
    child = req.children[0]
    return change_child(req, rows=(replace(child.rows[0], **values), *child.rows[1:]))
