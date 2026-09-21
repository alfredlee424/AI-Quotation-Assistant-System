"""由已解析配置計價；不接受 LLM 提供的單價或材料用量。"""
from __future__ import annotations

from config import WORKGROUP
from database import repository as repo
from engine.configuration import number, resolve_configuration
from utils.logger import log_action


# 明確按件計價的類別。立水、上／下板及貼面材料必須有 BOM 規則。
# 僅於完全沒有該路徑的用量資料時適用，不能掩蓋缺尺寸規則。
PER_ITEM_OPTIONS = {"S010", "S015", "S020", "S022", "S025", "S030", "S035",
                    "S040", "S049", "S050", "S055", "S061",
                    "W010", "W020", "W030", "W040", "W050", "W060", "W070"}


def calculate_configuration(draft: dict):
    from engine.calculator import QuoteItem, calculate_quote, _quo_rate_to_markup

    quantity = number(draft.get("qty"), "產品數量", positive=True)
    resolved = resolve_configuration(draft)
    if not resolved["valid"]:
        raise ValueError("；".join(resolved["errors"] + resolved["missing"]))
    if draft.get("questions") or draft.get("pending_options"):
        raise ValueError("尚有未確認的需求或候選規格")
    wg = draft.get("workgroup", WORKGROUP)
    category = repo.get_product_category(draft["prodkind"], workgroup=wg)
    if not category or category.get("quo_rate") is None:
        raise ValueError("產品未設定有效報價係數")
    rate = number(category["quo_rate"], "報價係數", positive=True)
    selections = resolved["selections"]
    items = []
    evidence = {}
    for path, selection in selections.items():
        line_qty = number(selection.get("line_qty", 1), f"{path} 項目數量", positive=True)
        cost = number(selection.get("compri"), f"{path} 採購單價")
        # 無規格的容器也保存，但不除零，不憑葉／父節點判斷是否計費。
        numerator, denominator = 1.0, 1.0
        source, driver_path, driver_code = "zero_cost", "", ""
        if cost > 0:
            rules = repo.get_quantity_rules(path, workgroup=wg)
            if rules:
                drivers = {r.get("part_path") for r in rules}
                if len(drivers) != 1 or not next(iter(drivers)):
                    raise ValueError(f"{path} 的驅動部位定義不唯一或為空")
                driver_path = next(iter(drivers))
                driver_code = selections.get(driver_path, {}).get("code", "")
                matches = [r for r in rules if r["code"] == driver_code]
                if len(matches) != 1:
                    raise ValueError(f"{path} 缺少或重複尺寸用量規則：{driver_path} / {driver_code}")
                numerator = number(matches[0].get("stdqty"), f"{path} 用料量")
                denominator = number(matches[0].get("stdpar"), f"{path} 裁切量", positive=True)
                source = "ordqty"
            elif draft["prodkind"] == "CMT1" and selection["optno"] in PER_ITEM_OPTIONS:
                source = "per_item_policy"
            else:
                raise ValueError(f"{path} 缺少用量規則，不能假設為 1／1")
        evidence[path] = {"source": source, "driver_path": driver_path, "driver_code": driver_code,
                          "line_qty": line_qty, "product_qty": quantity}
        items.append(QuoteItem(
            part_code=selection["code"], part_desc=selection["codsc"], path=path,
            spc_code=selection["code"], spdsc=selection["codsc"], qty=quantity * line_qty,
            stdqty=numerator, stdpar=denominator, compri=cost,
            optno=selection["optno"], optdesc=selection["optdesc"],
        ))
    result = calculate_quote(items, discount_rate=draft.get("discount_rate", 0),
                             markup_rate=_quo_rate_to_markup(rate))
    for item in result.items:
        item.update(evidence[item["path"]])
    log_action("quote_pricing_sources", params={
        "revision": draft.get("revision", 0), "prodkind": draft.get("prodkind"),
    }, result={
        "total_cost": result.total_cost,
        "items": [{key: item.get(key) for key in (
            "path", "spc_code", "spdsc", "source", "driver_path", "driver_code",
            "product_qty", "line_qty", "stdqty", "stdpar", "compri", "part_cost",
        )} for item in result.items],
    })
    return result, resolved
