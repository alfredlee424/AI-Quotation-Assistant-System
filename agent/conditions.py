"""人工條件帳本：明確範圍、例外與否定保護；不授予工程核准或計價權限。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from agent.requirement_review import ReviewEvidence, _evidence, _requirements_digest, configuration_digest


SUBJECTS = {
    "surface_hole": "桌面開孔", "leg_panel_hole": "腳板走線孔",
    "underdesk_wiring": "桌下走線", "hole_position": "桌面孔位",
    "partition_position": "隔板位置",
}
STATES = {"UNSPECIFIED": "未指定", "REQUIRED": "明確需要",
          "FORBIDDEN": "明確不要", "NOT_APPLICABLE": "不適用"}
SCOPES = {"order": "全單（目前活動明細）", "group": "人工角色群組", "line": "單筆產品"}
_RANK = {"order": 0, "group": 1, "line": 2}


@dataclass(frozen=True)
class ConditionBinding:
    line_id: str
    path: str
    prodkind: str


@dataclass(frozen=True)
class ConditionRecord:
    condition_id: str
    subject: str
    state: str
    scope: str
    group_label: str
    targets: tuple[str, ...]
    value: str
    requirement_ids: tuple[str, ...]
    bindings: tuple[ConditionBinding, ...]
    overrides: tuple[str, ...]
    evidence: tuple[ReviewEvidence, ...]
    requirement_digest: str
    parent_digest: str
    status: str = "RECORDED"
    rules_version: str = "explicit-scope-v1"


def _parent_digest(conditions, keys):
    records = {c.condition_id: c for c in conditions}
    content = [asdict(records[key]) if key in records else {"missing": key} for key in sorted(keys)]
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def refresh_conditions(draft):
    """失效狀態不自行恢復；配置改回或明細恢復後仍須重新核對。"""
    active = {line.line_id for line in draft.lines}
    requirements = {r.requirement_id: r for r in draft.requirements}
    records = list(draft.conditions)
    # 父條件的失效須傳播到例外，不依新增／編輯順序決定結果。
    for _ in range(len(records) + 1):
        changed = False
        for index, record in enumerate(records):
            if record.status != "RECORDED":
                continue
            valid = set(record.targets) <= active
            valid = valid and (record.scope != "order" or set(record.targets) == active)
            valid = valid and record.evidence[-1].configuration_digest == configuration_digest(draft, record.targets)
            valid = valid and all(key in requirements for key in record.requirement_ids)
            valid = valid and record.requirement_digest == _requirements_digest(requirements, record.requirement_ids)
            parents = {c.condition_id: c for c in records}
            valid = valid and all(key in parents and parents[key].status == "RECORDED" for key in record.overrides)
            valid = valid and record.parent_digest == _parent_digest(records, record.overrides)
            if not valid:
                records[index] = replace(record, status="STALE")
                changed = True
        if not changed:
            break
    return replace(draft, conditions=tuple(records))


def _ids(value, label, *, empty=False):
    if (not isinstance(value, list) or (not value and not empty)
            or any(not isinstance(key, str) for key in value) or len(set(value)) != len(value)):
        raise ValueError(f"{label}須為不重複的識別清單。")
    return tuple(value)


def apply_condition_command(draft, command, *, draft_id, expected_revision, actor, reason, source_batch=None):
    from agent.multi_quote import DraftChange, _identity
    _identity(draft, draft_id, expected_revision, actor, reason, source_batch)
    fields = {
        "record": {"op", "condition_id", "subject", "state", "scope", "group_label", "targets", "value",
                   "requirement_ids", "bindings", "overrides", "reference", "explanation"},
        "withdraw": {"op", "condition_id"},
    }
    if (not isinstance(command, dict) or not isinstance(command.get("op"), str)
            or command["op"] not in fields or set(command) != fields[command["op"]]):
        raise ValueError("條件操作欄位不正確；不得指定價格或解除來源阻擋。")
    updated = refresh_conditions(deepcopy(draft))
    key = command["condition_id"]
    if not isinstance(key, str):
        raise ValueError("條件識別必須為文字。")
    old = next((c for c in updated.conditions if c.condition_id == key), None)
    if key and (old is None or old.status == "WITHDRAWN"):
        raise ValueError("找不到本草稿的活動條件。")
    if command["op"] == "withdraw":
        if old is None:
            raise ValueError("請指定要撤回的條件。")
        revised = replace(old, status="WITHDRAWN")
    else:
        subject, state, scope = (command[name] for name in ("subject", "state", "scope"))
        if not all(isinstance(v, str) for v in (subject, state, scope)) or subject not in SUBJECTS or state not in STATES or scope not in SCOPES:
            raise ValueError("條件類型、狀態或作用範圍不正確。")
        targets = _ids(command["targets"], "作用明細")
        active = {line.line_id: line for line in updated.lines}
        if not set(targets) <= set(active):
            raise ValueError("條件包含封存或其他草稿的明細。")
        if (scope == "order" and set(targets) != set(active)) or (scope == "line" and len(targets) != 1):
            raise ValueError("全單须明確包含目前所有明細；單筆範圍只能包含一筆。")
        group = command["group_label"]
        if not isinstance(group, str) or len(group) > 100 or (scope == "group" and not group.strip()) or (scope != "group" and group):
            raise ValueError("群組須提供角色名稱；其他範圍不得填群組名稱。")
        value = command["value"]
        if not isinstance(value, str) or len(value) > 1000:
            raise ValueError("条件內容須為至多 1000 字的文字。")
        keys = _ids(command["requirement_ids"], "來源需求")
        requirements = {r.requirement_id: r for r in updated.requirements}
        if not set(keys) <= set(requirements):
            raise ValueError("來源需求不存在或已拆分，請重新引用本草稿原文。")
        parents = _ids(command["overrides"], "例外引用", empty=True)
        by_id = {c.condition_id: c for c in updated.conditions}
        for parent_id in parents:
            parent = by_id.get(parent_id)
            if (parent is None or parent_id == key or parent.status != "RECORDED" or parent.subject != subject
                    or _RANK[parent.scope] >= _RANK[scope] or not set(targets) <= set(parent.targets)):
                raise ValueError("例外須引用有效、同類型且較廣範圍的條件，目標不得越界。")
        bindings = command["bindings"]
        if not isinstance(bindings, list):
            raise ValueError("部位對照須為清單。")
        bound, seen, catalogs = [], set(), {}
        from engine.configuration import load_catalog
        for binding in bindings:
            if not isinstance(binding, dict) or set(binding) != {"line_id", "path"}:
                raise ValueError("條件部位只接受明細與完整路徑。")
            target, path = binding["line_id"], binding["path"]
            if not isinstance(target, str) or not isinstance(path, str) or target not in targets or target in seen:
                raise ValueError("每筆條件明細只能指定一個部位，且不得越界或重複。")
            product = active[target].configuration.get("prodkind")
            if not product:
                raise ValueError("綁定正式部位前須先指定產品。")
            if product not in catalogs:
                catalogs[product] = load_catalog(product, updated.workgroup)
            if path not in catalogs[product]["nodes"]:
                raise ValueError("條件部位不在目前產品合法選單。")
            seen.add(target)
            bound.append(ConditionBinding(target, path, product))
        evidence = _evidence(updated, targets, actor, command["reference"], command["explanation"])
        revised = ConditionRecord(key or uuid4().hex, subject, state, scope, group.strip(), targets, value.strip(),
                                  keys, tuple(bound), parents, (*(old.evidence if old else ()), evidence),
                                  _requirements_digest(requirements, keys), _parent_digest(updated.conditions, parents))
    records = tuple(revised if c.condition_id == key else c for c in updated.conditions)
    if old is None:
        if len(records) >= 2000:
            raise ValueError("條件數已達本階段上限。")
        records = (*records, revised)
    event = DraftChange(draft.revision + 1, "condition_review", revised.targets, actor.strip(), reason.strip(),
                        datetime.now(timezone.utc).isoformat(), deepcopy(command))
    from agent.requirement_review import refresh_review_evidence
    return refresh_review_evidence(replace(updated, conditions=records, revision=draft.revision + 1,
                                      changes=(*updated.changes, event)))


def _effective(records):
    by_id = {c.condition_id: c for c in records}
    suppressed = set()

    def ancestors(record):
        # 範圍嚴格递減，最多全單→群組→單筆，不存在合法循環。
        for key in record.overrides:
            parent = by_id.get(key)
            if parent is not None:
                suppressed.add(key)
                ancestors(parent)

    for record in records:
        if record.status == "RECORDED":
            ancestors(record)
    return [c for c in records if c.condition_id not in suppressed], sorted(suppressed)


def condition_report(draft):
    current = refresh_conditions(draft)
    rows, issues = [], []
    live = [c for c in current.conditions if c.status != "WITHDRAWN"]
    for record in live:
        if record.status == "STALE":
            issues.append({"kind": "stale", "condition_ids": [record.condition_id], "line_id": "",
                           "message": "條件的配置、來源、作用範圍或例外依據已變更，須重新核對。"})
    for line in current.lines:
        for subject in SUBJECTS:
            inherited = [c for c in live if line.line_id in c.targets and c.subject == subject]
            effective, suppressed = _effective(inherited)
            signatures = {(c.state, c.value) for c in effective}
            ids = [c.condition_id for c in effective]
            rows.append({"line_id": line.line_id, "subject": subject,
                         "state": effective[0].state if len(signatures) == 1 else "CONFLICT" if effective else "UNSPECIFIED",
                         "condition_ids": ids, "inherited_ids": [c.condition_id for c in inherited],
                         "overridden_ids": suppressed})
            def issue(kind, message, keys=ids):
                issues.append({"kind": kind, "line_id": line.line_id, "condition_ids": keys, "message": message})
            if len(signatures) > 1:
                issue("conflict", f"{SUBJECTS[subject]}有未明確覆蓋的相反或不同內容條件。")
            for record in effective:
                binding = next((b for b in record.bindings if b.line_id == line.line_id), None)
                if record.state == "UNSPECIFIED":
                    issue("unspecified", f"{SUBJECTS[subject]}仍未指定。", [record.condition_id])
                elif binding is None:
                    issue("unbound", f"{SUBJECTS[subject]}尚未綁定本明細正式部位，不能驗證配置。", [record.condition_id])
                else:
                    if binding.prodkind != line.configuration.get("prodkind"):
                        issue("product_changed", "產品已變更，條件部位須重新綁定。", [record.condition_id])
                    present = any(p == binding.path or p.startswith(binding.path + "\\") for p in line.configuration.get("selections", {}))
                    if present and record.state in ("FORBIDDEN", "NOT_APPLICABLE"):
                        issue("forbidden_selection", f"{SUBJECTS[subject]}禁止／不適用部位仍在配置：{binding.path}", [record.condition_id])
                    if not present and record.state == "REQUIRED":
                        issue("missing_selection", f"{SUBJECTS[subject]}所需部位尚未選配：{binding.path}", [record.condition_id])
                if subject in ("hole_position", "partition_position") and record.state == "REQUIRED":
                    issue("position_review", "位置依據僅為人工文字紀錄，尚未驗證圖面、基準與幾何。", [record.condition_id])
        states = {row["subject"]: row["state"] for row in rows if row["line_id"] == line.line_id}
        if states["hole_position"] == "REQUIRED" and states["surface_hole"] != "REQUIRED":
            issues.append({"kind": "position_dependency", "line_id": line.line_id, "condition_ids": [],
                           "message": "桌面孔位需要桌面開孔條件；目前未指定、禁止、不適用或存在衝突。"})
    return {"rows": rows, "issues": issues, "can_quote": False, "rules_version": "explicit-scope-v1"}


def validate_condition_configuration(draft, line_id, configuration, active_paths):
    """在單品解析自動必選之後攔截，例外不能讓另一種條件的禁止失效。"""
    current = refresh_conditions(draft)
    live = [c for c in current.conditions if c.status != "WITHDRAWN" and line_id in c.targets]
    for subject in SUBJECTS:
        effective, _ = _effective([c for c in live if c.subject == subject])
        if len({(c.state, c.value) for c in effective}) > 1:
            raise ValueError(f"{SUBJECTS[subject]}條件衝突，請先核對局部例外。")
        for record in effective:
            for binding in record.bindings:
                if binding.line_id != line_id:
                    continue
                if binding.prodkind != configuration.get("prodkind"):
                    raise ValueError("條件部位所屬產品已變更，請先撤回並重新綁定條件。")
                if record.state in ("FORBIDDEN", "NOT_APPLICABLE") and any(
                    p == binding.path or p.startswith(binding.path + "\\") for p in active_paths
                ):
                    raise ValueError(f"{SUBJECTS[subject]}否定條件禁止此部位（含自動必選）：{binding.path}")
