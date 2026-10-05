"""多明細內部選配試算。文件不可確認、不可保存正式快照、不解除整單關卡。"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from uuid import uuid4

from agent.multi_quote import _GATE, _identity, export_multi_draft
from agent.requirement_review import refresh_review_evidence, requirement_report
from agent.conditions import condition_report, validate_condition_configuration
from agent.drawing_review import drawing_report
from config import MAX_DISCOUNT_RATE, TAX_RATE
from engine.calculator import calculate_quote
from engine.configuration import number, resolve_configuration
from engine.pricing import _configuration_cost_inputs


TRIAL_VERSION = "multi-internal-trial-v2"
_WARNING = "僅供目前標準選配的內部核對；非完整工單報價，不可向客戶確認或保存正式快照。"


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def trial_context(draft):
    return _digest({"draft": export_multi_draft(draft), "trial_version": TRIAL_VERSION,
                    "tax_rate": TAX_RATE, "max_discount_rate": MAX_DISCOUNT_RATE})


def _money(value):
    number(value, "試算金額")
    try:
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        raise ValueError("試算金額超過目前支援範圍。") from None


def _line_blockers(draft, line, requirements, conditions, drawings):
    blockers = [q for q in line.configuration.get("questions", []) if q != _GATE]
    if line.configuration.get("pending_options"):
        blockers.append("仍有未確認規格候選。")
    for question in draft.questions:
        if question.status != "RESOLVED" and question.target_id in (draft.draft_id, line.line_id):
            blockers.append(question.text)
    for record in draft.requirements:
        if record.status != "RECORDED" and (not record.targets or line.line_id in record.targets):
            blockers.append(f"來源需求尚未完成核對：{record.requirement_id}｜{record.status}")
    if requirements["invalid_requirement_ids"] or requirements["uncovered_source_line_ids"]:
        blockers.append("來源帳本有無效對照或未涵蓋片段。")
    for issue in conditions["issues"]:
        if issue["line_id"] in ("", line.line_id):
            blockers.append(issue["message"])
    for issue in drawings["issues"]:
        if issue["line_id"] in ("", line.line_id):
            blockers.append(issue["message"])
    return list(dict.fromkeys(blockers))


def build_internal_trial(draft, *, actor, source_batch=None):
    """每筆保留成功／失敗；任一活動明細未試算即不提供整單合計。"""
    _identity(draft, draft.draft_id, draft.revision, actor, "產生內部選配試算", source_batch)
    if not 1 <= len(draft.lines) <= 200:
        raise ValueError("內部試算須有 1 至 200 筆活動明細，請先核對範圍。")
    ids = [line.line_id for line in (*draft.lines, *draft.archived_lines)]
    if len(set(ids)) != len(ids):
        raise ValueError("明細識別重複或同時存在活動與封存範圍，不能試算。")
    context = trial_context(draft)
    discount = number(draft.discount_rate, "整單折扣率")
    if discount > min(MAX_DISCOUNT_RATE, 1):
        raise ValueError("整單折扣超過目前允許範圍。")
    tax = number(TAX_RATE, "稅率")
    current = refresh_review_evidence(deepcopy(draft))
    requirements, conditions = requirement_report(current), condition_report(current)
    drawings = drawing_report(current)
    rows = []
    for line in current.lines:
        row = {"line_id": line.line_id, "line_revision": line.revision, "label": line.label,
               "prodkind": line.configuration.get("prodkind"), "qty": line.configuration.get("qty"),
               "status": "BLOCKED", "issues": _line_blockers(current, line, requirements, conditions, drawings),
               "calculation": None}
        rows.append(row)
        if row["issues"]:
            continue
        try:
            config = deepcopy(line.configuration)
            if not config.get("prodkind"):
                raise ValueError("尚未明確指定產品，不使用預設產品試算。")
            if config.get("workgroup") != draft.workgroup:
                raise ValueError("明細事業別與整單不一致。")
            if number(config.get("discount_rate", 0), "明細折扣率") != 0:
                raise ValueError("本版只支援整單折扣，不能沿用逐筆折扣。")
            number(config.get("qty"), "產品數量", positive=True)
            resolved = resolve_configuration(config)
            if not resolved["valid"]:
                raise ValueError("；".join(resolved["errors"] + resolved["missing"]))
            validate_condition_configuration(current, line.line_id, config, resolved["active_paths"])
            # 保留原配置所有問題；只透過唯讀共用成本輸入作內部數學試算，未呼叫正式預覽。
            items, evidence, rate = _configuration_cost_inputs(config, resolved)
            # 單筆只加成，不折扣、不課稅；同部位以明細識別隔離。
            result = calculate_quote(items, discount_rate=0, markup_rate=rate - 1, tax_rate=0)
            raw_cost = sum(item.part_cost for item in items)
            raw_subtotal = raw_cost * (1 + (rate - 1))
            _money(raw_cost)
            _money(raw_subtotal)
            for item in result.items:
                item.update(evidence[item["path"]], line_id=line.line_id)
            row.update(status="CALCULATED", calculation={
                "quote_rate": rate, "total_cost": result.total_cost, "subtotal": result.subtotal,
                "raw_total_cost": repr(raw_cost), "raw_subtotal": repr(raw_subtotal),
                "items": result.items, "selections": deepcopy(resolved["selections"]),
            })
        except (ValueError, InvalidOperation, OverflowError) as exc:
            row["issues"].append(str(exc) if isinstance(exc, ValueError) else "試算數值超過支援範圍。")
        except Exception:
            row["issues"].append("主檔讀取或配置試算失敗，未沿用舊價格；請重新檢查資料來源。")
    calculated = [row for row in rows if row["status"] == "CALCULATED"]
    totals = None
    if len(calculated) == len(rows) and not current.archived_lines:
        # 沿用既有單品的未逐列捨入浮點中間值；不加總已捨入的小計。
        cost = sum(float(row["calculation"]["raw_total_cost"]) for row in rows)
        subtotal = sum(float(row["calculation"]["raw_subtotal"]) for row in rows)
        discount_amount = subtotal * discount
        after_discount = subtotal - discount_amount
        tax_amount = after_discount * tax
        amounts = {"total_cost": cost, "subtotal": subtotal, "discount_amount": discount_amount,
                   "after_discount": after_discount, "tax_amount": tax_amount, "total_price": after_discount + tax_amount}
        totals = {key: _money(value) for key, value in amounts.items()}
        displayed_subtotals = sum(Decimal(str(row["calculation"]["subtotal"])) for row in rows)
        totals.update(discount_rate=discount, tax_rate=tax,
                      display_subtotal_difference=float(displayed_subtotals - Decimal(str(totals["subtotal"]))),
                      raw_amounts={key: repr(value) for key, value in amounts.items()})
    document = {
        "document_type": "multi_quote_internal_trial_only", "schema_version": 1, "trial_version": TRIAL_VERSION,
        "trial_id": uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(), "actor": actor.strip(),
        "draft_id": draft.draft_id, "draft_revision": draft.revision, "draft_digest": context,
        "warning": _WARNING, "can_quote": False, "can_confirm": False,
        "status": "ALL_ACTIVE_CONFIGURATIONS_CALCULATED" if totals is not None else "PARTIAL_OR_BLOCKED",
        "lines": rows, "totals": totals, "calculated_count": len(calculated), "active_count": len(rows),
        "archived_line_ids": [line.line_id for line in current.archived_lines],
        "order_blockers": [*current.blockers, *(["存在封存明細：只顯示活動明細內部試算，不提供整單合計。"]
                                               if current.archived_lines else [])],
        "requirements": requirements, "conditions": conditions, "drawings": drawings,
        "rounding": "ROUND_HALF_UP; legacy unrounded line amounts summed before order discount and tax",
        "cross_query_snapshot_guaranteed": False,
    }
    document["content_digest"] = _digest(document)
    return document


def checked_internal_trial(draft, document, *, actor, source_batch=None):
    """僅供核對及下載既有內部結果，不重查主檔、不轉成正式預覽。"""
    _identity(draft, draft.draft_id, draft.revision, actor, "核對內部試算", source_batch)
    if (not isinstance(document, dict) or document.get("document_type") != "multi_quote_internal_trial_only"
            or document.get("trial_version") != TRIAL_VERSION or document.get("can_quote") is not False
            or document.get("can_confirm") is not False or document.get("draft_id") != draft.draft_id
            or document.get("draft_revision") != draft.revision or document.get("draft_digest") != trial_context(draft)
            or document.get("actor") != actor.strip()):
        raise ValueError("內部試算與目前草稿、操作人或規則版本不一致，請重新試算。")
    content = {key: value for key, value in document.items() if key != "content_digest"}
    if document.get("content_digest") != _digest(content):
        raise ValueError("內部試算內容摘要不一致，請重新試算。")
    return deepcopy(document)
