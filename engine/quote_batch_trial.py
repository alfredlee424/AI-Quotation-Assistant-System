"""逐張獨立內部試算；只讀主檔，不配號、確認、寫庫或沿用舊整單金額。"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import hmac
import secrets
from uuid import uuid4

from agent import quote_batch
from agent.multi_quote import export_multi_draft
from agent.requirement_review import refresh_review_evidence, requirement_report
from agent.conditions import condition_report, validate_condition_configuration
from agent.drawing_review import drawing_report
from config import MAX_DISCOUNT_RATE, TAX_RATE
from engine.calculator import calculate_quote
from engine.configuration import number, resolve_configuration
from engine.pricing import _configuration_cost_inputs, PER_ITEM_OPTIONS
from engine.multi_trial import _line_blockers


DOCUMENT_TYPE = "independent_quote_batch_internal_trial_only"
CHILD_TYPE = "independent_child_quote_internal_trial_only"
TRIAL_VERSION = "independent-batch-trial-v1"
CHILD_VERSION = "independent-child-trial-v1"
FORMULA_VERSION = "usage-divided-by-yield-v1"
POLICY_VERSION = "rounded-children-decimal-sum-v1"
MAX_TRIAL_CHILDREN = 200
WARNING = "非正式批次逐張獨立試算：不配號、不可向客戶確認、不保存正式報價；來源、主檔、含價與工程核准均未解除。"
MONEY_FIELDS = ("total_cost", "subtotal", "discount_amount", "after_discount", "tax_amount", "total_price")
ITEM_MONEY_FIELDS = ("part_cost", "unit_cost", "unit_price", "amount")
# 程序內完整性封印，不是操作人認證／工程簽章；重啟後須重新試算，不提供檔案匯入。
_SEAL_KEY = secrets.token_bytes(32)


def policy_context():
    return {"policy_version": POLICY_VERSION, "formula_version": FORMULA_VERSION,
            "trial_version": TRIAL_VERSION, "child_version": CHILD_VERSION,
            "expansion_version": quote_batch.DOCUMENT_VERSION,
            "description_version": quote_batch.CHILD_VERSION,
            "tax_rate": str(TAX_RATE), "max_discount_rate": str(MAX_DISCOUNT_RATE),
            "max_children": quote_batch.MAX_CHILDREN,
            "max_trial_children": MAX_TRIAL_CHILDREN,
            "per_item_options": sorted(PER_ITEM_OPTIONS), "per_item_product": "CMT1",
            "single_rounding": "legacy ROUND_HALF_UP; unrounded within each child; each total rounded independently",
            "batch_sum": "exact decimal sum of rounded child amounts; no further discount or tax",
            "money_serialization": "fixed-point decimal strings; never float batch arithmetic"}


def _money_string(value):
    value = Decimal(str(value))
    if not value.is_finite() or value != value.quantize(Decimal("0.01")):
        raise ValueError("單品核心須回傳有限且已捨入至兩位的金額。")
    return format(value, ".2f")


def _sum(values):
    # 不受外部 Decimal context 精度影響；單品核心仍維持其既有計算政策。
    values = [Decimal(str(v)) for v in values]
    if any(not v.is_finite() for v in values):
        raise ValueError("不得加總非有限金額。")
    with localcontext() as context:
        context.prec = max([28, *(len(v.as_tuple().digits) + abs(v.as_tuple().exponent) + 10 for v in values)])
        return format(sum(values, Decimal("0.00")), ".2f")


def _difference(left, right):
    return _sum((left, str(Decimal(right).copy_negate())))


def _seal(document):
    document = deepcopy(document)
    document.pop("content_digest", None)
    document.pop("process_seal", None)
    document["content_digest"] = quote_batch._digest(document)
    document["process_seal"] = hmac.new(_SEAL_KEY, quote_batch._json(document).encode(), hashlib.sha256).hexdigest()
    return document


def _check_seal(document):
    if not isinstance(document, dict):
        raise ValueError("非本版非正式試算文件。")
    content = {k: v for k, v in document.items() if k not in ("content_digest", "process_seal")}
    digest = quote_batch._digest(content)
    content["content_digest"] = digest
    expected = hmac.new(_SEAL_KEY, quote_batch._json(content).encode(), hashlib.sha256).hexdigest()
    if document.get("content_digest") != digest or not hmac.compare_digest(str(document.get("process_seal", "")), expected):
        raise ValueError("試算內容被修改或程序已重啟，請重新試算；不接受下載檔重新匯入。")


def _calculation(config, resolved, line_id, child_id, discount, tax):
    items, evidence, rate = _configuration_cost_inputs(config, resolved)
    result = calculate_quote(items, discount_rate=discount, markup_rate=rate - 1, tax_rate=tax)
    calc = asdict(result)
    for key in MONEY_FIELDS:
        calc[key] = _money_string(calc[key])
    for item in calc["items"]:
        item.update(evidence[item["path"]], line_id=line_id, child_quote_id=child_id)
        for key in ITEM_MONEY_FIELDS:
            item[key] = _money_string(item[key])
    calc.update(quote_rate=str(rate), discount_rate=str(result.discount_rate), tax_rate=str(result.tax_rate),
                markup_rate=str(result.markup_rate), formula_version=FORMULA_VERSION,
                selections=deepcopy(resolved["selections"]),
                cost_inputs=[asdict(item) for item in items])
    calc["rounding_differences"] = {
        "display_cost_rows_minus_total_cost": _difference(_sum(i["part_cost"] for i in calc["items"]), calc["total_cost"]),
        "display_amount_rows_minus_subtotal": _difference(_sum(i["amount"] for i in calc["items"]), calc["subtotal"]),
        "after_discount_plus_tax_minus_total_price": _difference(_sum((calc["after_discount"], calc["tax_amount"])), calc["total_price"]),
        "explanation": "各欄依既有單品未捨入中間值各自捨入；差額如實揭露，不調整成本列、稅額或含稅額湊數。",
    }
    return calc


def build_batch_trial(draft, *, actor, source_batch=None):
    """結構先整批驗證；未完成逐張保留，只有全數試算才加總。"""
    expansion = quote_batch.build_quote_batch(draft, actor=actor, source_batch=source_batch)
    if len(expansion["children"]) > MAX_TRIAL_CHILDREN:
        raise ValueError("內部金額試算最多 200 張；來源展開上限仍為 999。超量整批拒絕，不截斷、不另拆批。")
    discount = number(draft.discount_rate, "批次共同折扣率")
    if discount > min(MAX_DISCOUNT_RATE, 1):
        raise ValueError("批次共同折扣超過允許範圍。")
    tax = number(TAX_RATE, "稅率")
    policy = policy_context()
    current = refresh_review_evidence(deepcopy(draft))
    requirements, conditions, drawings = requirement_report(current), condition_report(current), drawing_report(current)
    trial_id, created_at = uuid4().hex, datetime.now(timezone.utc).isoformat()
    context = {"trial_id": trial_id, "created_at": created_at, "policy": policy,
               "policy_fingerprint": quote_batch._digest(policy), "expansion_digest": expansion["content_digest"],
               "draft_snapshot": export_multi_draft(draft), "source": deepcopy(draft.source),
               "source_batch_digest": expansion["source_batch_digest"],
               "order": expansion["coverage"], "requirements": requirements, "all_conditions": conditions,
               "all_drawings": drawings, "condition_records": [asdict(c) for c in current.conditions],
               "drawing_records": [asdict(d) for d in current.drawings],
               "discount_rate": str(discount), "tax_rate": str(tax)}
    children = []
    for line, description in zip(current.lines, expansion["children"]):
        child = deepcopy(description)
        child.pop("content_digest")
        child.update(document_type=CHILD_TYPE, document_version=CHILD_VERSION, trial_version=TRIAL_VERSION,
                     warning=WARNING, trial_context={k: deepcopy(v) for k, v in context.items()
                         if k not in ("draft_snapshot", "source")}, trial_id=trial_id,
                     status="BLOCKED", calculation=None)
        child["formal_blockers"] = [WARNING, *[b for b in child["formal_blockers"] if b != quote_batch.WARNING]]
        child["blockers"] = list(dict.fromkeys([*child["blockers"],
            *_line_blockers(current, line, requirements, conditions, drawings)]))
        if not child["blockers"]:
            try:
                config = deepcopy(line.configuration)
                if not config.get("prodkind"):
                    raise ValueError("尚未明確指定產品，不使用預設產品試算。")
                if number(config.get("discount_rate", 0), "明細折扣率") != 0:
                    raise ValueError("僅接受批次共同折扣，不能沿用逐張不同折扣。")
                resolved = resolve_configuration(config)
                if not resolved["valid"]:
                    raise ValueError("；".join(resolved["errors"] + resolved["missing"]))
                validate_condition_configuration(current, line.line_id, config, resolved["active_paths"])
                child["calculation"] = _calculation(config, resolved, line.line_id, child["child_quote_id"], discount, tax)
                child["status"] = "CALCULATED"
            except (ValueError, InvalidOperation, OverflowError) as exc:
                child["blockers"].append(str(exc) if isinstance(exc, ValueError) else "試算數值超過單品核心支援範圍。")
            except Exception:
                child["blockers"].append("主檔讀取或配置試算失敗；本張未試算，不沿用舊價格。")
        children.append(_seal(child))
    count = sum(c["status"] == "CALCULATED" for c in children)
    totals = None
    if count == len(children):
        totals = {key: _sum(c["calculation"][key] for c in children) for key in MONEY_FIELDS}
        totals["product_qty"] = sum(int(c["product_qty"]) for c in children)
        totals["after_discount_plus_tax_minus_total_price"] = _difference(
            _sum((totals["after_discount"], totals["tax_amount"])), totals["total_price"])
    return _seal({"document_type": DOCUMENT_TYPE, "document_version": TRIAL_VERSION, "schema_version": 1,
        "trial_version": TRIAL_VERSION, "trial_id": trial_id, "created_at": created_at,
        "quote_batch_id": expansion["quote_batch_id"], "draft_id": draft.draft_id,
        "draft_revision": draft.revision, "actor": actor.strip(), "warning": WARNING,
        "trial_context": context, "expansion": expansion, "children": children,
        "calculated_count": count, "child_count": len(children), "totals": totals,
        "status": "ALL_CHILDREN_CALCULATED" if totals is not None else "PARTIAL_OR_BLOCKED",
        "formal_blockers": [WARNING, *[b for b in expansion["formal_blockers"] if b != quote_batch.WARNING]],
        "ref_no": None, "formal_suffix": None,
        "can_quote": False, "can_confirm": False, "can_save": False,
        "cross_query_snapshot_guaranteed": False})


def checked_batch_trial(draft, document, *, actor, source_batch=None):
    """當前來源雙向覆蓋及程序內內容驗證；不查價、不執行單品公式。"""
    _check_seal(document)
    if (document.get("document_type") != DOCUMENT_TYPE or document.get("trial_version") != TRIAL_VERSION
            or document.get("actor") != actor.strip()
            or document["trial_context"]["policy"] != policy_context()):
        raise ValueError("非正式試算版本、操作人或金額政策已變更，請重新試算。")
    quote_batch.checked_quote_batch(draft, document["expansion"], actor=actor, source_batch=source_batch)
    return deepcopy(document)


def checked_child_trial(draft, document, child_id, *, actor, source_batch=None):
    """逐張下載仍須由完整當前批次驗證，不接受孤立子文件繞過來源。"""
    checked = checked_batch_trial(draft, document, actor=actor, source_batch=source_batch)
    for child in checked["children"]:
        if child["child_quote_id"] == child_id:
            _check_seal(child)
            return child
    raise ValueError("子報價不屬於本批次。")
