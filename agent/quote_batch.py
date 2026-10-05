"""一來源工單、多張獨立子報價的非正式展開；不查主檔、不計價或配號。

文件只接受由目前記憶體草稿重建的核對結果，摘要不是核准簽章，沒有匯入服務。
穩定識別與排列分離；三位尾碼僅描述未來政策，本模組不保留任何單號。
"""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
from uuid import UUID, uuid5

from agent.multi_quote import MultiQuoteDraft, _identity, export_multi_draft, from_work_order
from agent.work_orders import WorkOrderBatch, _build_orders, _identifier
from agent.requirement_review import refresh_review_evidence, requirement_report, source_rows
from agent.conditions import condition_report


DOCUMENT_TYPE = "source_order_child_quote_expansion_only"
DOCUMENT_VERSION = "order-child-expansion-v1"
CHILD_VERSION = "independent-child-description-v1"
RELATION_VERSION = "batch-child-source-draft-v1"
MAX_CHILDREN = 999
WARNING = "非正式展開核對：不配號、不計價、不保存正式報價；文字展示不代表選配、收費或工程核准。"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _source(draft, batch):
    if not isinstance(batch, WorkOrderBatch) or draft.source.get("kind") != "work_order":
        raise ValueError("展開只接受同一來源工單轉接的配置草稿。")
    if (batch.schema_version != 1 or batch.parser_version != "work-order-boundaries-v1"
            or batch.status != "REVIEW_REQUIRED"):
        raise ValueError("來源文件版本或狀態不相容。")
    if hashlib.sha256(batch.raw_text.encode()).hexdigest() != batch.source_digest:
        raise ValueError("來源原文摘要不一致。")
    raw = batch.raw_text.splitlines()
    if len(raw) != len(batch.lines) or any(
        line.number != i or line.raw != text or line.line_id != _identifier(batch.batch_id, f"line:{i}")
        for i, (line, text) in enumerate(zip(batch.lines, raw), 1)
    ):
        raise ValueError("來源行內容、識別或座標不一致。")
    orders, unassigned = _build_orders(batch.batch_id, batch.lines)
    if orders != batch.orders or unassigned != batch.unassigned_line_ids:
        raise ValueError("來源品項與工單歸屬不一致。")
    # 不只檢查宣稱的摘要／版本，也逐欄比對目前來源的原文、候選、歸屬及歷史。
    expected = from_work_order(batch, draft.source["order_id"]).source
    if draft.source != expected:
        raise ValueError("草稿來源內容與目前工單不一致，請重新轉接。")
    return next(order for order in orders if order.order_id == draft.source["order_id"])


def _coverage(draft, order):
    candidates = {item.item_id: item for item in order.items}
    if not 1 <= len(order.items) <= MAX_CHILDREN or len(draft.lines) > MAX_CHILDREN:
        raise ValueError("每批須有 1 至 999 個來源品項；超量整批拒絕，不截斷或另拆批。")
    all_lines = (*draft.lines, *draft.archived_lines)
    ids = [line.line_id for line in all_lines]
    if len(set(ids)) != len(ids) or len(candidates) != len(order.items):
        raise ValueError("來源品項或配置識別重複，整批拒絕展開。")
    if draft.archived_lines:
        raise ValueError("存在封存品項，整批拒絕展開；請恢復並核對，不可靜默排除。")
    if set(ids) != set(candidates):
        raise ValueError("來源覆蓋缺漏或混入跨單／新增無來源品項，整批拒絕展開。")
    for line in all_lines:
        if line.source_line_ids != (candidates[line.line_id].source_line_id,):
            raise ValueError("配置來源座標與來源品項不一致，不能跨單或互換來源。")
        if line.configuration.get("workgroup") != draft.workgroup:
            raise ValueError("配置事業別與批次不一致。")
    return candidates


def _ledger(draft):
    rows = {row["line_id"]: row for row in source_rows(draft)}
    ids = [record.requirement_id for record in draft.requirements]
    active = {line.line_id for line in draft.lines}
    if len(ids) != len(set(ids)):
        raise ValueError("需求識別重複，不能展開。")
    for record in draft.requirements:
        row = rows.get(record.source_line_id)
        if (row is None or type(record.start) is not int or type(record.end) is not int
                or not 0 <= record.start < record.end <= len(row["raw"])
                or row["raw"][record.start:record.end] != record.text):
            raise ValueError("需求原文或來源座標不一致。")
        if len(set(record.targets)) != len(record.targets) or not set(record.targets) <= active:
            raise ValueError("需求作用範圍包含重複、封存或跨單品項。")
    for key in rows:
        intervals = sorted((r.start, r.end) for r in draft.requirements if r.source_line_id == key)
        if any(right[0] < left[1] for left, right in zip(intervals, intervals[1:])):
            raise ValueError("來源需求片段重疊，請先核對帳本，不能重複展示為獨立需求。")
    for condition in draft.conditions:
        if (not set(condition.targets) <= active or not set(condition.requirement_ids) <= set(ids)
                or (condition.status == "RECORDED" and not condition.evidence)):
            raise ValueError("條件作用範圍、需求引用或核對依據不完整。")
    return rows


def _descriptions(draft, line, rows, uncovered, item_sources):
    descriptions = []
    for record in draft.requirements:
        own = record.source_line_id in line.source_line_ids
        related = [c for c in draft.conditions if c.status != "WITHDRAWN"
                   and line.line_id in c.targets and record.requirement_id in c.requirement_ids]
        condition_scope = any(c.status == "RECORDED" and c.evidence for c in related)
        recorded = record.status in ("RECORDED", "NEEDS_ENGINEERING") and bool(record.evidence)
        metadata = recorded and record.disposition == "metadata" and not record.targets
        pending_scope = not condition_scope and (not recorded or (not record.targets and not metadata))
        # 未核對的上下文逐張顯示但不宣稱適用；其他品項專屬原文不任意共用。
        source_is_item = record.source_line_id in item_sources
        show = own or line.line_id in record.targets or related or metadata or (pending_scope and not source_is_item)
        if not show:
            continue
        descriptions.append({
            "kind": "dedicated" if own else "source_metadata" if metadata else "shared",
            "scope_status": "PENDING" if pending_scope else "RECORDED_METADATA" if metadata else "RECORDED_CONDITION_SCOPE" if condition_scope else "RECORDED_SCOPE",
            "source": deepcopy(rows[record.source_line_id]),
            "start": record.start, "end": record.end, "text": record.text,
            "requirement": asdict(record),
            "applies_to_child": True if condition_scope else line.line_id in record.targets if recorded else None,
            "inherited_condition_ids": [c.condition_id for c in related],
            "engineering_approved": False, "selects_option": False, "charges_fee": False,
        })
    # 被刪漏的需求不能讓文字隨帳本消失；以未核對原文補列並阻擋。
    for key in uncovered:
        if key in line.source_line_ids or key not in item_sources:
            row = rows[key]
            descriptions.append({"kind": "uncovered", "scope_status": "PENDING", "source": deepcopy(row),
                                 "start": 0, "end": len(row["raw"]), "text": row["raw"], "requirement": None,
                                 "engineering_approved": False, "selects_option": False, "charges_fee": False})
    return sorted(descriptions, key=lambda d: (d["source"]["number"], d["start"], d["kind"]))


def _assemble(draft, batch, actor):
    if not isinstance(draft, MultiQuoteDraft):
        raise ValueError("只接受配置草稿，不能使用舊試算、快照或展開文件。")
    _identity(draft, draft.draft_id, draft.revision, actor, "非正式批次展開核對", batch)
    order = _source(draft, batch)
    candidates = _coverage(draft, order)
    rows = _ledger(draft)
    current = refresh_review_evidence(deepcopy(draft))
    requirements = requirement_report(current)
    conditions = condition_report(current)
    batch_id = uuid5(UUID(draft.draft_id), "independent-quote-batch").hex
    item_sources = {line.source_line_ids[0] for line in current.lines}
    children, issues = [], []
    for index, line in enumerate(current.lines, 1):
        item = candidates[line.line_id]
        child_id = uuid5(UUID(batch_id), "child:" + item.item_id).hex
        qty = line.configuration.get("qty")
        quantity_valid = (type(qty) in (int, float) and math.isfinite(qty) and qty > 0 and int(qty) == qty)
        blockers = []
        if not quantity_valid:
            blockers.append("成品數未知或不是正整數；來源行尾候選不會自動採用。")
        descriptions = _descriptions(current, line, rows, requirements["uncovered_source_line_ids"], item_sources)
        description_sources = {d["source"]["line_id"]: d["source"] for d in descriptions}
        if any(d["scope_status"] == "PENDING" for d in descriptions):
            blockers.append("描述作用範圍仍待核對；展示不代表適用或工程核准。")
        if any(d["requirement"] and d["requirement"]["status"] != "RECORDED" for d in descriptions):
            blockers.append("來源需求仍未核對、已失效或待工程核定。")
        if any(line.line_id in d.targets and d.status != "RECORDED" for d in current.drawings):
            blockers.append("本品項圖面依賴缺少或已失效，須重新核對。")
        condition_issues = [i for i in conditions["issues"] if i["line_id"] in ("", line.line_id)]
        blockers.extend(i["message"] for i in condition_issues)
        children.append({
            "document_type": "independent_child_quote_description_only", "document_version": CHILD_VERSION,
            "warning": WARNING, "workgroup": draft.workgroup, "actor": actor.strip(),
            "draft_id": draft.draft_id, "draft_revision": draft.revision,
            "source_batch_id": batch.batch_id, "source_batch_revision": batch.revision,
            "source_digest": batch.source_digest,
            "child_quote_id": child_id, "quote_batch_id": batch_id, "source_item_id": item.item_id,
            "configuration_line_id": line.line_id, "display_order": index,
            "future_suffix_position": index, "formal_suffix": None, "ref_no": None,
            "source_order_id": order.order_id, "source_ref_raw": order.source_ref_raw,
            "source_item": asdict(item), "source_line": deepcopy(rows[item.source_line_id]),
            "label": line.label, "line_revision": line.revision,
            "product_qty": qty if quantity_valid else None, "quantity_status": "CONFIGURED" if quantity_valid else "PENDING",
            "configuration": deepcopy(line.configuration), "descriptions": descriptions,
            "description_sources": sorted(description_sources.values(), key=lambda row: row["number"]),
            "conditions": [asdict(c) for c in current.conditions if line.line_id in c.targets],
            "effective_conditions": [r for r in conditions["rows"] if r["line_id"] == line.line_id],
            "drawings": [asdict(d) for d in current.drawings if line.line_id in d.targets],
            "blockers": list(dict.fromkeys(blockers)), "formal_blockers": [WARNING, *current.blockers],
            "can_quote": False, "can_confirm": False, "can_save": False,
        })
        children[-1]["content_digest"] = _digest(children[-1])
        issues.extend({"child_quote_id": child_id, "message": message} for message in dict.fromkeys(blockers))
    if not requirements["source_coverage_complete"]:
        issues.append({"child_quote_id": "", "message": "來源文字帳本仍有缺漏，已保留未覆蓋原文。"})
    content = {
        "document_type": DOCUMENT_TYPE, "document_version": DOCUMENT_VERSION, "schema_version": 1,
        "relation_version": RELATION_VERSION, "status": "NON_FORMAL_REVIEW_ONLY", "warning": WARNING,
        "quote_batch_id": batch_id, "workgroup": draft.workgroup, "draft_id": draft.draft_id,
        "draft_revision": draft.revision, "actor": actor.strip(), "draft_digest": _digest(export_multi_draft(draft)),
        "source_batch_digest": _digest(asdict(batch)), "source_batch_id": batch.batch_id,
        "source_order_id": order.order_id, "source_ref_raw": order.source_ref_raw,
        "source": deepcopy(draft.source), "children": children,
        "coverage": [{"source_item_id": c["source_item_id"], "child_quote_id": c["child_quote_id"],
                      "configuration_line_id": c["configuration_line_id"]} for c in children],
        "source_item_coverage_complete": True, "expansion_review_complete": not issues,
        "issues": issues, "requirements": requirements,
        "formal_blockers": [WARNING, *current.blockers, "需求、條件、圖面、主檔及工程正式核准關卡均保留。"],
        "numbering_policy": {"max_children": MAX_CHILDREN, "future_suffix_width": 3,
                             "cost_row_seq_width": 5, "cost_row_scope": "each_child_quote",
                             "allocated": False},
        "can_quote": False, "can_confirm": False, "can_save": False,
    }
    content["content_digest"] = _digest(content)
    # 輸出為純 JSON 型別，使下載後比較也不會因 tuple/list 差異而誤判。
    return json.loads(_json(content))


def build_quote_batch(draft, *, actor, source_batch=None):
    """只讀取呼叫者傳入的來源與草稿；結構異常原子拒絕，待核對逐張揭露。"""
    try:
        return _assemble(draft, source_batch, actor)
    except (KeyError, TypeError, AttributeError, IndexError, OverflowError) as exc:
        raise ValueError("來源或草稿缺少展開必要欄位，請重新核對。") from exc


def checked_quote_batch(draft, document, *, actor, source_batch=None):
    """重建非正式文件逐欄比對；即使篡改者重算摘要也不接受變更內容。"""
    expected = build_quote_batch(draft, actor=actor, source_batch=source_batch)
    if not isinstance(document, dict) or _json(document) != _json(expected):
        raise ValueError("展開文件與目前操作人、草稿、來源、順序或版本不一致，請重新產生。")
    return deepcopy(expected)
