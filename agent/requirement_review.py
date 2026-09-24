"""原文需求帳本及人工證據紀錄；不授予正式工程核准或計價權限。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

if TYPE_CHECKING:
    from agent.multi_quote import MultiQuoteDraft


DISPOSITIONS = ("specification", "included_process", "production_note", "metadata", "engineering_review")


@dataclass(frozen=True)
class SpecBinding:
    line_id: str
    path: str
    code: str
    line_qty: float


@dataclass(frozen=True)
class ReviewEvidence:
    actor: str
    reference: str
    explanation: str
    recorded_at: str
    configuration_digest: str


@dataclass(frozen=True)
class RequirementRecord:
    requirement_id: str
    source_line_id: str
    start: int
    end: int
    text: str
    targets: tuple[str, ...] = ()
    bindings: tuple[SpecBinding, ...] = ()
    disposition: str = ""
    status: str = "UNREVIEWED"
    evidence: tuple[ReviewEvidence, ...] = ()
    parent_id: str | None = None


def source_rows(draft: MultiQuoteDraft) -> tuple[dict, ...]:
    if draft.source.get("kind") != "work_order":
        return ()
    by_id = {}
    for row in (*draft.source.get("lines", []), *draft.source.get("unassigned_lines", [])):
        if row["raw"].strip():
            if row["line_id"] in by_id and row != by_id[row["line_id"]]:
                raise ValueError("來源行識別衝突，不能建立需求帳本。")
            by_id[row["line_id"]] = deepcopy(row)
    return tuple(sorted(by_id.values(), key=lambda row: row["number"]))


def _record_id(draft: MultiQuoteDraft, source_id: str, start: int, end: int) -> str:
    return uuid5(UUID(draft.draft_id), f"requirement:{source_id}:{start}:{end}").hex


def initialize_requirements(draft: MultiQuoteDraft) -> MultiQuoteDraft:
    """只有全新帳本可初始化；按行保留待核對需求，不猜片段或處置。"""
    if draft.requirements or draft.archived_requirements:
        raise ValueError("需求帳本已存在，不能覆寫原紀錄。")
    records = []
    for row in source_rows(draft):
        targets = tuple(line.line_id for line in draft.lines if row["line_id"] in line.source_line_ids)
        records.append(RequirementRecord(_record_id(draft, row["line_id"], 0, len(row["raw"])),
                                         row["line_id"], 0, len(row["raw"]), row["raw"], targets))
    if not records:
        raise ValueError("沒有可追溯的工單原文；空白或單品轉接不代表需求已完整。")
    return replace(deepcopy(draft), requirements=tuple(records))


def configuration_digest(draft: MultiQuoteDraft, targets: tuple[str, ...], *, whole_order=False) -> str:
    active = {line.line_id: line for line in draft.lines}
    keys = tuple(sorted(active)) if whole_order else tuple(sorted(targets))
    content = []
    for key in keys:
        line = active.get(key)
        if line is None:
            content.append({"line_id": key, "missing": True})
        else:
            config = line.configuration
            content.append({"line_id": key, "prodkind": config.get("prodkind"), "qty": config.get("qty"),
                            "selections": {path: {k: item.get(k) for k in ("code", "codsc", "line_qty")}
                                           for path, item in config.get("selections", {}).items()}})
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _evidence(draft, targets, actor, reference, explanation, *, whole_order=False):
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 500:
        raise ValueError("請填寫 1 至 500 字的依據識別／版本。")
    if not isinstance(explanation, str) or not explanation.strip() or len(explanation) > 2000:
        raise ValueError("請填寫 1 至 2000 字的處置理由或回答。")
    return ReviewEvidence(actor.strip(), reference.strip(), explanation.strip(), datetime.now(timezone.utc).isoformat(),
                          configuration_digest(draft, targets, whole_order=whole_order))


def _requirements_digest(records, keys):
    content = [asdict(records[key]) for key in sorted(keys) if key in records]
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _validated_bindings(draft, targets, bindings):
    if not isinstance(bindings, list):
        raise ValueError("選配對照必須是清單。")
    by_id = {line.line_id: line for line in draft.lines}
    result, seen = [], set()
    catalogs = {}
    from engine.configuration import load_catalog
    for value in bindings:
        if not isinstance(value, dict) or set(value) != {"line_id", "path"}:
            raise ValueError("選配對照只接受明細識別與完整部位路徑。")
        key, path = value["line_id"], value["path"]
        if not isinstance(key, str) or not isinstance(path, str) or key not in targets or (key, path) in seen:
            raise ValueError("選配對照包含越界、錯誤或重複部位。")
        seen.add((key, path))
        config = by_id[key].configuration
        item = config.get("selections", {}).get(path)
        if not item or not item.get("code"):
            raise ValueError("對照部位必須已選定正式規格，不能引用空結構或未選項目。")
        product = config.get("prodkind")
        if product not in catalogs:
            catalogs[product] = load_catalog(product, draft.workgroup)
        matches = [option for option in catalogs[product]["options"].get(path, []) if option.get("code") == item["code"]]
        if len(matches) != 1:
            raise ValueError("對照規格已失效或不唯一，請先修正配置。")
        result.append(SpecBinding(key, path, item["code"], item.get("line_qty", 1)))
    return tuple(result)


def refresh_review_evidence(draft: MultiQuoteDraft, previous: MultiQuoteDraft | None = None) -> MultiQuoteDraft:
    """配置變更只使證據失效，不自動核准；已結案問題恢復為待核對。"""
    records = []
    for record in draft.requirements:
        if record.status in ("RECORDED", "NEEDS_ENGINEERING") and record.evidence:
            if record.evidence[-1].configuration_digest != configuration_digest(draft, record.targets):
                record = replace(record, status="STALE")
        records.append(record)
    by_requirement = {record.requirement_id: record for record in records}
    questions, lines, blockers = [], list(deepcopy(draft.lines)), list(draft.blockers)
    for question in draft.questions:
        if question.status == "RESOLVED":
            whole = question.target_id == draft.draft_id
            targets = () if whole else (question.target_id,)
            evidence = question.resolution
            valid = evidence is not None and evidence.configuration_digest == configuration_digest(draft, targets, whole_order=whole)
            valid = valid and all(key in by_requirement and by_requirement[key].status == "RECORDED"
                                  for key in question.requirement_ids)
            valid = valid and question.requirement_digest == _requirements_digest(by_requirement, question.requirement_ids)
            if not whole:
                valid = valid and all(question.target_id in by_requirement[key].targets
                                      for key in question.requirement_ids if key in by_requirement)
            if not valid:
                question = replace(question, status="OPEN")
        if question.status == "OPEN":
            if question.target_id == draft.draft_id:
                if question.text not in blockers:
                    blockers.append(question.text)
            else:
                for index, line in enumerate(lines):
                    if line.line_id == question.target_id and question.text not in line.configuration["questions"]:
                        config = deepcopy(line.configuration)
                        config["questions"].append(question.text)
                        lines[index] = replace(line, configuration=config)
        questions.append(question)
    result = replace(draft, requirements=tuple(records), questions=tuple(questions), lines=tuple(lines), blockers=tuple(blockers))
    if previous is not None:
        old = {line.line_id: line for line in previous.lines}
        revised = []
        for line in result.lines:
            if line.line_id in old and line.configuration != old[line.line_id].configuration:
                config = deepcopy(line.configuration)
                config["revision"] = max(config.get("revision", 0), old[line.line_id].configuration.get("revision", 0) + 1)
                line = replace(line, configuration=config, revision=max(line.revision, old[line.line_id].revision + 1))
            revised.append(line)
        result = replace(result, lines=tuple(revised))
    return result


def apply_review_command(draft: MultiQuoteDraft, command: dict, *, draft_id: str, expected_revision: int,
                         actor: str, reason: str, source_batch=None) -> MultiQuoteDraft:
    from agent.multi_quote import DraftChange, _identity
    _identity(draft, draft_id, expected_revision, actor, reason, source_batch)
    fields = {
        "initialize": {"op"}, "split": {"op", "requirement_id", "offset"},
        "record": {"op", "requirement_id", "disposition", "targets", "bindings", "reference", "explanation"},
        "reopen": {"op", "requirement_id"},
        "resolve_question": {"op", "question_id", "requirement_ids", "reference", "answer"},
    }
    if not isinstance(command, dict) or not isinstance(command.get("op"), str) or command["op"] not in fields or set(command) != fields[command["op"]]:
        raise ValueError("需求核對操作欄位不正確。")
    updated = refresh_review_evidence(deepcopy(draft))
    operation = command["op"]
    if operation == "initialize":
        updated = initialize_requirements(updated)
    elif operation == "resolve_question":
        question = next((q for q in updated.questions if q.question_id == command["question_id"]), None)
        if question is None or question.status != "OPEN":
            raise ValueError("找不到待結案的 AI 問題；來源阻擋不能用此操作解除。")
        active = {line.line_id for line in updated.lines}
        if question.target_id != updated.draft_id and question.target_id not in active:
            raise ValueError("目標明細已封存，請恢復後重新核對。")
        keys = command["requirement_ids"]
        if not isinstance(keys, list) or not keys or any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("問題結案須引用不重複的已處置需求。")
        by_id = {record.requirement_id: record for record in updated.requirements}
        for key in keys:
            record = by_id.get(key)
            if record is None or record.status != "RECORDED":
                raise ValueError("引用需求尚未處置、需工程核定或依據已失效。")
            if question.target_id != updated.draft_id and question.target_id not in record.targets:
                raise ValueError("引用需求不屬於問題的目標明細。")
        whole = question.target_id == updated.draft_id
        evidence = _evidence(updated, () if whole else (question.target_id,), actor, command["reference"], command["answer"], whole_order=whole)
        resolved = replace(question, status="RESOLVED", resolution=evidence, requirement_ids=tuple(keys),
                           resolution_history=(*question.resolution_history, evidence),
                           requirement_digest=_requirements_digest(by_id, keys))
        questions = tuple(resolved if q.question_id == question.question_id else q for q in updated.questions)
        lines, blockers = list(updated.lines), list(updated.blockers)
        # 只移除由本 AI 問題新增的投影，不刪除碰巧同文字的原始來源問題。
        if question.owns_projection:
            if whole:
                blockers = [text for text in blockers if text != question.text]
            else:
                for index, line in enumerate(lines):
                    if line.line_id == question.target_id:
                        config = deepcopy(line.configuration)
                        config["questions"] = [text for text in config["questions"] if text != question.text]
                        lines[index] = replace(line, configuration=config)
        updated = replace(updated, questions=questions, lines=tuple(lines), blockers=tuple(blockers))
    else:
        record = next((record for record in updated.requirements if record.requirement_id == command["requirement_id"]), None)
        if record is None:
            raise ValueError("找不到本草稿的活動需求，不能修改其他或已拆分的需求。")
        if operation == "split":
            offset = command["offset"]
            if type(offset) is not int or not 0 < offset < len(record.text) or not record.text[:offset].strip() or not record.text[offset:].strip():
                raise ValueError("拆分位置須位於文字內，且兩段都須有內容。")
            middle = record.start + offset
            children = tuple(RequirementRecord(_record_id(updated, record.source_line_id, start, end),
                                               record.source_line_id, start, end, text, record.targets,
                                               parent_id=record.requirement_id)
                             for start, end, text in ((record.start, middle, record.text[:offset]), (middle, record.end, record.text[offset:])))
            records = tuple(child for current in updated.requirements for child in (children if current.requirement_id == record.requirement_id else (current,)))
            updated = replace(updated, requirements=records, archived_requirements=(*updated.archived_requirements, replace(record, status="SPLIT")))
        elif operation == "reopen":
            if record.status == "UNREVIEWED":
                raise ValueError("需求已是待核對狀態。")
            revised = replace(record, status="UNREVIEWED")
            updated = replace(updated, requirements=tuple(revised if r.requirement_id == record.requirement_id else r for r in updated.requirements))
        else:
            disposition, targets = command["disposition"], command["targets"]
            if disposition not in DISPOSITIONS or not isinstance(targets, list) or any(not isinstance(key, str) for key in targets):
                raise ValueError("處置類型或目標明細格式不正確。")
            if len(set(targets)) != len(targets) or not set(targets) <= {line.line_id for line in updated.lines}:
                raise ValueError("需求目標包含重複、封存或其他草稿的明細。")
            if not targets and disposition != "metadata":
                raise ValueError("非來源識別資訊必須指定適用明細。")
            bindings = _validated_bindings(updated, tuple(targets), command["bindings"])
            if disposition == "specification" and (not bindings or {binding.line_id for binding in bindings} != set(targets)):
                raise ValueError("規格處置須對應每個目標明細的已選合法規格。")
            evidence = _evidence(updated, tuple(targets), actor, command["reference"], command["explanation"])
            revised = replace(record, targets=tuple(targets), bindings=bindings, disposition=disposition,
                              status="NEEDS_ENGINEERING" if disposition == "engineering_review" else "RECORDED",
                              evidence=(*record.evidence, evidence))
            updated = replace(updated, requirements=tuple(revised if r.requirement_id == record.requirement_id else r for r in updated.requirements))
    updated = refresh_review_evidence(updated, previous=draft)
    event = DraftChange(draft.revision + 1, "requirement_review", (), actor.strip(), reason.strip(),
                        datetime.now(timezone.utc).isoformat(), deepcopy(command))
    return replace(updated, revision=draft.revision + 1, changes=(*deepcopy(draft.changes), event))


def requirement_report(draft: MultiQuoteDraft) -> dict:
    """覆蓋僅表示來源片段已建立帳本，不能推論語意或成本完整。"""
    current = refresh_review_evidence(draft)
    rows = source_rows(current)
    uncovered, invalid = [], []
    by_source = {row["line_id"]: row for row in rows}
    for record in current.requirements:
        row = by_source.get(record.source_line_id)
        if row is None or not 0 <= record.start < record.end <= len(row["raw"]) or row["raw"][record.start:record.end] != record.text:
            invalid.append(record.requirement_id)
    for row in rows:
        covered = set()
        for record in current.requirements:
            if record.source_line_id == row["line_id"] and record.requirement_id not in invalid:
                covered.update(range(record.start, record.end))
        if any(index not in covered for index, char in enumerate(row["raw"]) if not char.isspace()):
            uncovered.append(row["line_id"])
    pending = [r.requirement_id for r in current.requirements if r.status != "RECORDED"]
    links = [{"requirement_id": r.requirement_id, "line_id": b.line_id, "path": b.path, "code": b.code,
              "status": r.status} for r in current.requirements for b in r.bindings]
    mapped = {(binding.line_id, binding.path, binding.code) for record in current.requirements
              if record.status == "RECORDED" for binding in record.bindings}
    unmapped = [{"line_id": line.line_id, "path": path, "code": item.get("code"),
                "automatic": item.get("automatic", False)} for line in current.lines
               for path, item in line.configuration.get("selections", {}).items()
               if item.get("code") and (line.line_id, path, item["code"]) not in mapped]
    return {"source_available": bool(rows), "source_coverage_complete": bool(rows) and not uncovered and not invalid,
            "uncovered_source_line_ids": uncovered, "invalid_requirement_ids": invalid,
            "pending_requirement_ids": pending, "all_records_reviewed": bool(rows) and bool(current.requirements) and not pending and not uncovered and not invalid,
            "bindings": links, "unmapped_selections": unmapped, "can_quote": False}
