"""T45 工作階段核對摘要；只投影固定分類／計數，不是稽核或核准服務。"""
from copy import deepcopy

from agent.quote_batch import build_quote_batch
from agent.requirement_review import refresh_review_evidence, source_rows
from agent.conditions import condition_report
from agent.drawing_review import drawing_report


DOCUMENT_TYPE = "work_order_review_summary_nonformal_only"
VERSION = "work-order-review-summary-v1"
WARNING = "去識別化開發核對摘要：僅統計目前帳本，不證明語意完整、主檔有效、工程核准或可正式報價。"
ERROR = "無法產生核對摘要；請核對目前來源、完整品項、帳本版本與操作人，不沿用舊結果。"
REQUIREMENT_STATES = ("UNREVIEWED", "RECORDED", "NEEDS_ENGINEERING", "STALE")
CONDITION_STATES = ("RECORDED", "STALE", "WITHDRAWN")
DRAWING_STATES = ("MISSING", "RECORDED", "STALE")
QUESTION_STATES = ("OPEN", "RESOLVED")
CONDITION_ISSUES = (
    "stale", "conflict", "unspecified", "unbound", "product_changed",
    "forbidden_selection", "missing_selection", "position_review", "position_dependency",
)
# 固定 v1 摘要契約；不匯入試算／配號／保存服務來取得正式能力。
FORMAL_GATES = (
    "business", "engineering", "product_lines", "included_price", "authorization",
    "dba", "formal_preview_numbering", "atomic_save",
)
REVIEW_OPERATIONS = ("requirement_review", "condition_review", "drawing_review")


def _counts(values, allowed):
    counts = dict.fromkeys(allowed, 0)
    for value in values:
        if not isinstance(value, str) or value not in counts:
            raise ValueError(ERROR)
        counts[value] += 1
    return counts


def _assemble(draft, source_batch, actor):
    # 先走既有完整來源／品項／座標驗證；不接受 caller 傳入的舊報告。
    expansion = build_quote_batch(draft, actor=actor, source_batch=source_batch)
    current = refresh_review_evidence(deepcopy(draft))
    requirements = expansion["requirements"]
    active = {line.line_id for line in current.lines}
    requirement_ids = {r.requirement_id for r in current.requirements}
    if any(r.status in ("RECORDED", "NEEDS_ENGINEERING") and not r.evidence
           for r in current.requirements):
        raise ValueError(ERROR)
    for records, field in ((current.conditions, "condition_id"),
                           (current.drawings, "dependency_id"),
                           (current.questions, "question_id")):
        ids = [getattr(record, field) for record in records]
        if len(set(ids)) != len(ids):
            raise ValueError(ERROR)
    for drawing in current.drawings:
        if (not drawing.targets or not set(drawing.targets) <= active
                or not set(drawing.requirement_ids) <= requirement_ids):
            raise ValueError(ERROR)
    if any(q.target_id not in active | {current.draft_id} for q in current.questions):
        raise ValueError(ERROR)
    conditions = condition_report(current)
    drawings = drawing_report(current)
    changes = dict.fromkeys((*REVIEW_OPERATIONS, "other"), 0)
    for change in current.changes:
        # 不輸出 operation 原值、details、理由、操作者或時間；未知操作只算 other。
        changes[change.operation if change.operation in REVIEW_OPERATIONS else "other"] += 1
    return {
        "document_type": DOCUMENT_TYPE, "document_version": VERSION,
        "status": "NON_FORMAL_REVIEW_ONLY", "warning": WARNING,
        "scope": "current_in_memory_source_ledger_only",
        "source": {
            "item_count": len(current.lines), "nonempty_line_count": len(source_rows(current)),
            "item_coverage_complete": expansion["source_item_coverage_complete"],
            "ledger_coverage_complete": requirements["source_coverage_complete"],
            "uncovered_line_count": len(requirements["uncovered_source_line_ids"]),
        },
        "requirements": {
            "record_count": len(current.requirements),
            "states": _counts((r.status for r in current.requirements), REQUIREMENT_STATES),
            "pending_count": len(requirements["pending_requirement_ids"]),
            "all_records_reviewed": requirements["all_records_reviewed"],
            "binding_count": len(requirements["bindings"]),
            "unmapped_selection_count": len(requirements["unmapped_selections"]),
            "archived_record_count": len(current.archived_requirements),
        },
        "conditions": {
            "record_count": len(current.conditions),
            "states": _counts((c.status for c in current.conditions), CONDITION_STATES),
            "issue_count": len(conditions["issues"]),
            "issue_kinds": _counts((i["kind"] for i in conditions["issues"]), CONDITION_ISSUES),
        },
        "drawings": {
            "record_count": len(current.drawings),
            "states": _counts((d.status for d in current.drawings), DRAWING_STATES),
            "issue_projection_count": len(drawings["issues"]),
        },
        "questions": {
            "record_count": len(current.questions),
            "states": _counts((q.status for q in current.questions), QUESTION_STATES),
            "configuration_question_projection_count": sum(
                len(line.configuration.get("questions", [])) for line in current.lines),
        },
        "expansion": {
            "review_complete": expansion["expansion_review_complete"],
            "issue_projection_count": len(expansion["issues"]),
            "children_with_issues": sum(bool(c["blockers"]) for c in expansion["children"]),
            "children_with_pending_quantity": sum(
                c["quantity_status"] == "PENDING" for c in expansion["children"]),
            "draft_blocker_entry_count": len(current.blockers),
        },
        "recorded_changes": {
            "draft_event_count": len(current.changes), "categories": changes,
            "source_classification_count": len(current.source["classification_history"]),
        },
        "formal_gates": {gate: "PENDING" for gate in FORMAL_GATES},
        "pricing_checked": False, "semantic_coverage_certified": False,
        "actor_is_authenticated": False, "can_quote": False,
        "can_confirm": False, "can_save": False,
    }


def build_work_order_review_summary(draft, *, source_batch, actor):
    """每次重建目前摘要，不快取、不修改草稿、不回傳來源或可連結身份的摘要。"""
    try:
        return _assemble(draft, source_batch, actor)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
        # 下層診斷可能帶任意識別／路徑；對外只回固定錯誤。
        raise ValueError(ERROR) from None
