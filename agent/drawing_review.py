"""最小圖面依賴帳本：只記錄人工核對，不讀附件、不授予工程核准。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from agent.requirement_review import configuration_digest, _requirements_digest


DRAWING_VERSION = "drawing-reference-v1"


@dataclass(frozen=True)
class DrawingEvidence:
    reference: str
    version: str
    actor: str
    summary: str
    recorded_at: str
    context_digest: str


@dataclass(frozen=True)
class DrawingDependency:
    dependency_id: str
    targets: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    purpose: str
    status: str = "MISSING"
    evidence: tuple[DrawingEvidence, ...] = ()
    rules_version: str = DRAWING_VERSION


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label}須為 1 至 {limit} 字。")
    return value.strip()


def _ids(value, label, *, empty=False):
    if (not isinstance(value, list) or (not value and not empty)
            or any(not isinstance(key, str) or not key for key in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"{label}須為不重複的識別清單。")
    return tuple(value)


def _context(draft, record):
    requirements = {r.requirement_id: r for r in draft.requirements}
    content = {
        "draft_id": draft.draft_id, "source": draft.source,
        "targets": sorted(record.targets), "purpose": record.purpose,
        "configuration": configuration_digest(draft, record.targets),
        "requirements": _requirements_digest(requirements, record.requirement_ids),
        "requirement_ids": sorted(record.requirement_ids),
        "conditions": [asdict(c) for c in draft.conditions if set(c.targets) & set(record.targets)],
        "rules_version": DRAWING_VERSION,
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def refresh_drawings(draft):
    """失效只前進，不因配置改回或明細恢復而自動沿用舊核對。"""
    active = {line.line_id for line in draft.lines}
    requirements = {r.requirement_id for r in draft.requirements}
    records = []
    for record in draft.drawings:
        if record.status == "RECORDED":
            valid = bool(record.evidence) and set(record.targets) <= active
            valid = valid and set(record.requirement_ids) <= requirements
            valid = valid and record.rules_version == DRAWING_VERSION
            valid = valid and record.evidence[-1].context_digest == _context(draft, record)
            if not valid:
                record = replace(record, status="STALE")
        records.append(record)
    return replace(draft, drawings=tuple(records))


def apply_drawing_command(draft, command, *, draft_id, expected_revision, actor, reason, source_batch=None):
    """獨立人工命令，不提供刪除依賴、成本或正式核准欄位。"""
    from agent.multi_quote import DraftChange, _identity
    from agent.requirement_review import refresh_review_evidence

    _identity(draft, draft_id, expected_revision, actor, reason, source_batch)
    fields = {
        "declare": {"op", "targets", "requirement_ids", "purpose"},
        "record": {"op", "dependency_id", "reference", "version", "summary"},
        "mark_missing": {"op", "dependency_id"},
    }
    if (not isinstance(command, dict) or not isinstance(command.get("op"), str)
            or command["op"] not in fields or set(command) != fields[command["op"]]):
        raise ValueError("圖面操作欄位不正確；不得解除需求、工程核准或價格阻擋。")
    updated = refresh_review_evidence(deepcopy(draft))
    records = list(updated.drawings)
    if command["op"] == "declare":
        targets = _ids(command["targets"], "適用明細")
        if not set(targets) <= {line.line_id for line in updated.lines}:
            raise ValueError("圖面依賴不得引用封存或其他草稿明細。")
        keys = _ids(command["requirement_ids"], "來源需求", empty=updated.source.get("kind") != "work_order")
        requirements = {r.requirement_id: r for r in updated.requirements}
        if not set(keys) <= set(requirements):
            raise ValueError("圖面來源需求不存在或已拆分，須引用本草稿活動需求。")
        for key in keys:
            if requirements[key].targets and not set(targets) <= set(requirements[key].targets):
                raise ValueError("圖面適用明細超出引用需求的作用範圍。")
        purpose = _text(command["purpose"], "圖面用途／待確認內容", 1000)
        if any(set(r.targets) == set(targets) and set(r.requirement_ids) == set(keys) and r.purpose == purpose for r in records):
            raise ValueError("相同圖面依賴已存在，請更新原紀錄。")
        if len(records) >= 2000:
            raise ValueError("圖面依賴數已達本階段上限。")
        revised = DrawingDependency(uuid4().hex, targets, keys, purpose)
        records.append(revised)
    else:
        key = command["dependency_id"]
        if not isinstance(key, str):
            raise ValueError("圖面依賴識別必須為文字。")
        old = next((r for r in records if r.dependency_id == key), None)
        if old is None:
            raise ValueError("找不到本草稿的圖面依賴。")
        if command["op"] == "mark_missing":
            if old.status == "MISSING":
                raise ValueError("圖面依賴已是缺圖待補狀態。")
            revised = replace(old, status="MISSING")
        else:
            if not set(old.targets) <= {line.line_id for line in updated.lines}:
                raise ValueError("適用明細已封存，請先恢復後核對。")
            requirements = {r.requirement_id: r for r in updated.requirements}
            if not set(old.requirement_ids) <= set(requirements):
                raise ValueError("來源需求已拆分，舊依賴不能直接核對；須重新核對來源草稿。")
            if any(requirements[k].targets and not set(old.targets) <= set(requirements[k].targets) for k in old.requirement_ids):
                raise ValueError("圖面適用明細超出引用需求的作用範圍。")
            evidence = DrawingEvidence(
                _text(command["reference"], "圖面依據識別", 500),
                _text(command["version"], "圖面版本", 100), actor.strip(),
                _text(command["summary"], "人工確認內容", 2000),
                datetime.now(timezone.utc).isoformat(), _context(updated, old),
            )
            revised = replace(old, status="RECORDED", evidence=(*old.evidence, evidence))
            # 同一圖號在同草稿不允許默默混用不同版本；其他依賴必須另行核對。
            records = [replace(r, status="STALE") if r.dependency_id != key and r.status == "RECORDED"
                       and r.evidence[-1].reference == evidence.reference
                       and r.evidence[-1].version != evidence.version else r for r in records]
        records = [revised if r.dependency_id == key else r for r in records]
    event = DraftChange(draft.revision + 1, "drawing_review", revised.targets, actor.strip(), reason.strip(),
                        datetime.now(timezone.utc).isoformat(), deepcopy(command))
    return replace(updated, drawings=tuple(records), revision=draft.revision + 1, changes=(*updated.changes, event))


def drawing_report(draft):
    from agent.requirement_review import refresh_review_evidence

    current = refresh_review_evidence(draft)
    active = {line.line_id for line in current.lines}
    issues = []
    for record in current.drawings:
        if record.status == "RECORDED":
            continue
        targets = set(record.targets) & active
        for target in sorted(targets) or [""]:
            issues.append({"dependency_id": record.dependency_id, "line_id": target,
                           "status": record.status,
                           "message": f"圖面依賴尚未完成核對：{record.dependency_id}｜{record.status}"})
    return {"records": [asdict(r) for r in current.drawings], "issues": issues,
            "rules_version": DRAWING_VERSION, "can_quote": False,
            "warning": "人工圖面紀錄不是工程核准；未驗證附件、幾何、成本或來源語意完整性。"}
