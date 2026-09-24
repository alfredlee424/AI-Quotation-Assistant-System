"""多明細配置草稿：單品配置驗證的協調層，尚不計價或保存正式報價。

所有操作返回深複製的新版本。來源、封存明細與阻擋條件不會因選配完成
而被移除；工單分類修正後須建立新草稿，不自動重新綁定舊配置。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from uuid import UUID, uuid4, uuid5

from agent.state import new_quote_draft
from agent.work_orders import WorkOrderBatch
from agent.requirement_review import RequirementRecord, ReviewEvidence, initialize_requirements, refresh_review_evidence
from config import MAX_DISCOUNT_RATE, WORKGROUP
from engine.configuration import apply_proposal, normalize_selections, number


@dataclass(frozen=True)
class QuoteLine:
    line_id: str
    label: str
    configuration: dict
    source_line_ids: tuple[str, ...] = ()
    revision: int = 0


@dataclass(frozen=True)
class DraftChange:
    revision: int
    operation: str
    line_ids: tuple[str, ...]
    actor: str
    reason: str
    recorded_at: str
    details: dict


@dataclass(frozen=True)
class ScopedQuestion:
    question_id: str
    target_id: str
    text: str
    status: str = "OPEN"
    owns_projection: bool = False
    resolution: ReviewEvidence | None = None
    requirement_ids: tuple[str, ...] = ()
    resolution_history: tuple[ReviewEvidence, ...] = ()
    requirement_digest: str = ""


@dataclass(frozen=True)
class MultiQuoteDraft:
    draft_id: str
    workgroup: str
    lines: tuple[QuoteLine, ...]
    source: dict
    blockers: tuple[str, ...]
    archived_lines: tuple[QuoteLine, ...] = ()
    changes: tuple[DraftChange, ...] = ()
    discount_rate: float = 0.0
    revision: int = 0
    schema_version: int = 1
    status: str = "CONFIGURATION_ONLY"
    questions: tuple[ScopedQuestion, ...] = ()
    requirements: tuple[RequirementRecord, ...] = ()
    archived_requirements: tuple[RequirementRecord, ...] = ()


_GATE = "多明細整單計價與正式快照尚未接通，不可建立正式報價。"


def _configuration() -> dict:
    config = new_quote_draft()
    config["status"] = "WAITING_FOR_INPUT"
    # 即使未來誤把內部單品配置送進舊預覽入口，也不得繞過整單關卡。
    config["questions"] = [_GATE]
    return config


def new_multi_quote() -> MultiQuoteDraft:
    return MultiQuoteDraft(uuid4().hex, WORKGROUP, (), {"kind": "manual"},
                           (_GATE, "尚未完成逐句需求覆蓋與工程條件核對。"))


def from_work_order(batch: WorkOrderBatch, order_id: str) -> MultiQuoteDraft:
    order = next((order for order in batch.orders if order.order_id == order_id), None)
    if order is None:
        raise ValueError("找不到本批次工單，不能轉接其他工單。")
    if not order.items:
        raise ValueError("此工單尚無明細候選，請先修正來源分類。")
    selected_lines = set(order.line_ids)
    relevant_history = selected_lines | set(batch.unassigned_line_ids)
    source = {
        "kind": "work_order", "batch_id": batch.batch_id, "batch_revision": batch.revision,
        "source_digest": batch.source_digest, "order_id": order_id,
        "source_ref_raw": order.source_ref_raw, "family_hint": order.family_hint,
        "lines": [asdict(line) for line in batch.lines if line.line_id in selected_lines],
        # 未歸屬內容也可能是共用條件，獨立保留，不擅自套用。
        "unassigned_lines": [asdict(line) for line in batch.lines if line.line_id in batch.unassigned_line_ids],
        "candidates": [asdict(item) for item in order.items],
        "classification_history": [asdict(change) for change in batch.corrections if change.line_id in relevant_history],
    }
    lines = tuple(QuoteLine(item.item_id, item.description_raw, _configuration(),
                           (item.source_line_id,)) for item in order.items)
    return initialize_requirements(MultiQuoteDraft(uuid4().hex, WORKGROUP, lines, source, (
        _GATE, "工單候選數量及角色尚未核准；桌面、腳座、轉盤等不可直接當成相同成品。",
        "來源標題、補充條件及未歸屬內容尚待逐句核對；配置齊全不代表需求完整。",
        *order.warnings,
    )))


def from_single_quote(single: dict) -> MultiQuoteDraft:
    """單品／既有 13 欄匯入結果轉為獨立新草稿；不重報或重現舊金額。"""
    if not isinstance(single, dict) or "lines" in single or "selections" not in single:
        raise ValueError("只接受既有單品草稿；不接受多明細文件或任意匯入文件。")
    if single.get("workgroup") != WORKGROUP:
        raise ValueError("來源事業別與目前環境不一致，不能轉接。")
    if not isinstance(single["selections"], dict):
        raise ValueError("來源選配必須是完整路徑對照。")
    selected = normalize_selections(single["selections"])
    rate = number(single.get("discount_rate", 0), "來源折扣率")
    if rate > min(MAX_DISCOUNT_RATE, 1):
        raise ValueError("來源折扣超過目前允許範圍，不能直接轉接。")
    quantity = single.get("qty")
    if quantity is not None:
        quantity = number(quantity, "來源產品數量", positive=True)
    questions = single.get("questions") or []
    if not isinstance(questions, list) or any(not isinstance(question, str) for question in questions):
        raise ValueError("來源待確認問題格式不正確。")
    config = _configuration()
    config.update(prodkind=single.get("prodkind"), product_name=single.get("product_name") or "",
                  qty=quantity, questions=[_GATE, *deepcopy(questions)])
    # 活動配置不帶歷史成本、用量或自動選配標記，重選後仍由現行主檔驗證。
    config["selections"] = {
        path: {key: deepcopy(item[key]) for key in ("path", "code", "line_qty", "codsc", "optno", "optdesc") if key in item}
        for path, item in selected.items()
    }
    preserved = {key: deepcopy(single[key]) for key in (
        "product_name", "prodkind", "qty", "selections", "discount_rate", "questions",
        "pending_options", "imported_quote", "revision", "ref_no",
    ) if key in single}
    blockers = [_GATE, "單品來源及歷史規格尚未重新核對，來源價格只供追溯，不參與計價。"]
    blockers.extend(questions)
    if single.get("pending_options"):
        blockers.append("來源仍有待選候選；轉接不代表候選已確認。")
    imported = single.get("imported_quote") or {}
    if not isinstance(imported, dict):
        raise ValueError("來源匯入紀錄格式不正確。")
    if imported.get("warnings"):
        blockers.append("來源匯入警告尚未釐清，詳見保存的來源紀錄。")
    line = QuoteLine(uuid4().hex, single.get("product_name") or "單品轉接明細", config)
    return MultiQuoteDraft(uuid4().hex, WORKGROUP, (line,),
                           {"kind": "single_quote", "snapshot": preserved}, tuple(blockers),
                           discount_rate=rate)


def check_source_current(draft: MultiQuoteDraft, batch: WorkOrderBatch | None) -> None:
    if draft.source.get("kind") != "work_order":
        return
    if batch is None or (
        batch.batch_id, batch.revision, batch.source_digest
    ) != (draft.source["batch_id"], draft.source["batch_revision"], draft.source["source_digest"]):
        raise ValueError("來源工單批次或分類版本已變更，請重新轉接；不可沿用舊配置歸屬。")
    if draft.source["order_id"] not in {order.order_id for order in batch.orders}:
        raise ValueError("來源工單已不存在，請重新核對。")


def _identity(draft: MultiQuoteDraft, draft_id: str, revision: int,
              actor: str, reason: str, source_batch: WorkOrderBatch | None) -> None:
    if draft_id != draft.draft_id or type(revision) is not int or revision != draft.revision:
        raise ValueError("草稿識別或版本不一致，請重新載入後操作。")
    if draft.workgroup != WORKGROUP or draft.status != "CONFIGURATION_ONLY" or draft.schema_version != 1:
        raise ValueError("目前環境或草稿狀態不允許修改。")
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 80:
        raise ValueError("請提供 1 至 80 字的操作人紀錄。")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError("請提供 1 至 1000 字的修改理由。")
    check_source_current(draft, source_batch)


def apply_multi_command(draft: MultiQuoteDraft, command: dict, *, draft_id: str,
                        expected_revision: int, actor: str, reason: str,
                        source_batch: WorkOrderBatch | None = None) -> MultiQuoteDraft:
    """單次管理／配置變更的原子界線；不接受价格、成本或解除阻擋指令。"""
    _identity(draft, draft_id, expected_revision, actor, reason, source_batch)
    if not isinstance(command, dict) or not isinstance(command.get("op"), str):
        raise ValueError("請提供明確的草稿操作。")
    fields = {
        "add": {"op", "label"}, "remove": {"op", "line_id"}, "restore": {"op", "line_id"},
        "reorder": {"op", "line_ids"}, "configure": {"op", "line_id", "proposal"},
    }
    operation = command["op"]
    if operation not in fields or set(command) != fields[operation]:
        raise ValueError("草稿操作包含缺少或不允許的欄位。")
    updated = deepcopy(draft)
    lines, archived = list(updated.lines), list(updated.archived_lines)
    affected = ()
    if operation == "add":
        label = command["label"]
        if not isinstance(label, str) or not label.strip() or len(label) > 200:
            raise ValueError("新增明細名稱須為 1 至 200 字。")
        if len(lines) + len(archived) >= 2000:
            raise ValueError("明細數已達本階段上限，請分批處理。")
        line = QuoteLine(uuid4().hex, label.strip(), _configuration())
        lines.append(line)
        affected = (line.line_id,)
    elif operation == "reorder":
        ids = command["line_ids"]
        if not isinstance(ids, list) or any(not isinstance(key, str) for key in ids):
            raise ValueError("排序必須提供完整的明細識別清單。")
        existing = [line.line_id for line in lines]
        if len(ids) != len(existing) or len(set(ids)) != len(ids) or set(ids) != set(existing):
            raise ValueError("排序不能省略、重複或加入其他草稿的明細。")
        if ids == existing:
            raise ValueError("排序未改變。")
        by_id = {line.line_id: line for line in lines}
        lines = [by_id[key] for key in ids]
        affected = tuple(ids)
    else:
        pool = archived if operation == "restore" else lines
        line = next((line for line in pool if line.line_id == command["line_id"]), None)
        if line is None:
            raise ValueError("找不到目標明細，不能修改其他草稿或已移除的明細。")
        affected = (line.line_id,)
        if operation == "remove":
            lines.remove(line)
            archived.append(replace(line, revision=line.revision + 1))
        elif operation == "restore":
            archived.remove(line)
            lines.append(replace(line, revision=line.revision + 1))
        else:
            proposal = command["proposal"]
            if not isinstance(proposal, dict) or set(proposal) - {"prodkind", "qty", "changes"}:
                raise ValueError("配置提案只允許產品、產品數量及合法選配變更；不得清除來源問題。")
            if not proposal or (not proposal.get("changes") and not any(key in proposal for key in ("qty", "prodkind"))):
                raise ValueError("沒有明確的配置變更。")
            # 重用同一白名單／型別驗證，不使用 LLM 或另寫一套規格合法性。
            from agent.core import _validate_proposal
            validated = deepcopy(proposal)
            validated["questions"] = deepcopy(line.configuration.get("questions") or [])
            _validate_proposal(validated)
            config = deepcopy(line.configuration)
            if config.get("workgroup") != draft.workgroup:
                raise ValueError("明細事業別與整單不一致。")
            apply_proposal(config, validated)
            config.update(status="WAITING_FOR_INPUT", preview=None, calc_result=None, ref_no=None)
            changed_line = replace(line, configuration=config, revision=line.revision + 1)
            lines[lines.index(line)] = changed_line
    event = DraftChange(draft.revision + 1, operation, affected, actor.strip(), reason.strip(),
                        datetime.now(timezone.utc).isoformat(), deepcopy(command))
    return refresh_review_evidence(replace(updated, lines=tuple(lines), archived_lines=tuple(archived),
                   changes=(*updated.changes, event), revision=draft.revision + 1), previous=draft)


def export_multi_draft(draft: MultiQuoteDraft) -> dict:
    return {"document_type": "multi_quote_configuration_only", **asdict(draft)}


def validate_target_scope(draft: MultiQuoteDraft, allowed_line_ids: tuple[str, ...]) -> None:
    if not isinstance(allowed_line_ids, tuple) or not 1 <= len(allowed_line_ids) <= 25:
        raise ValueError("請明確指定 1 至 25 筆允許修改的活動明細。")
    if any(not isinstance(key, str) for key in allowed_line_ids):
        raise ValueError("明細識別必須是文字。")
    if len(set(allowed_line_ids)) != len(allowed_line_ids) or not set(allowed_line_ids) <= {line.line_id for line in draft.lines}:
        raise ValueError("作用範圍包含重複、已封存或其他草稿的明細。")


def apply_multi_proposal(draft: MultiQuoteDraft, proposal: dict, *, allowed_line_ids: tuple[str, ...],
                         actor: str, reason: str, source_batch: WorkOrderBatch | None = None) -> MultiQuoteDraft:
    """多筆配置與追問全部通過才回傳新草稿；整批只有一個修訂版與稽核事件。"""
    if not isinstance(proposal, dict) or set(proposal) != {"draft_id", "revision", "updates", "questions"}:
        raise ValueError("多明細提案欄位不正確。")
    _identity(draft, proposal["draft_id"], proposal["revision"], actor, reason, source_batch)
    validate_target_scope(draft, allowed_line_ids)
    updates, questions = proposal["updates"], proposal["questions"]
    if not isinstance(updates, list) or len(updates) > 25 or not isinstance(questions, list) or len(questions) > 50:
        raise ValueError("提案的變更或追問清單格式／數量不正確。")
    if not updates and not questions:
        raise ValueError("提案沒有明確變更或追問。")
    seen = set()
    from agent.core import _validate_proposal
    for update in updates:
        if not isinstance(update, dict) or set(update) != {"line_id", "proposal"}:
            raise ValueError("每笔變更必須指定明細識別與配置提案。")
        key = update["line_id"]
        if not isinstance(key, str) or key not in allowed_line_ids or key in seen:
            raise ValueError("提案含越界或重複的目標明細。")
        seen.add(key)
        content = update["proposal"]
        if not isinstance(content, dict) or set(content) - {"prodkind", "qty", "changes"}:
            raise ValueError("配置提案不能修改成本、折扣、問題或來源。")
        _validate_proposal(content)
        changes = content.get("changes", [])
        if len(changes) > 200:
            raise ValueError("單筆選配變更過多，請分批處理。")
        set_paths = [change["path"] for change in changes if change["op"] == "set"]
        if len(set(set_paths)) != len(set_paths):
            raise ValueError("同一明細部位不能在同批提案設定多種規格。")
    for question in questions:
        if not isinstance(question, dict) or set(question) != {"target_id", "text"}:
            raise ValueError("追問必須有目標識別與文字。")
        if question["target_id"] not in (draft.draft_id, *allowed_line_ids):
            raise ValueError("追問目標不在本次工單或允許範圍。")
        if not isinstance(question["text"], str) or not question["text"].strip() or len(question["text"]) > 1000:
            raise ValueError("追問須為 1 至 1000 字。")
    staged = deepcopy(draft)
    for update in updates:
        staged = apply_multi_command(staged, {"op": "configure", **update}, draft_id=staged.draft_id,
                                     expected_revision=staged.revision, actor=actor, reason=reason,
                                     source_batch=source_batch)
    new_questions = list(staged.questions)
    blockers = list(staged.blockers)
    lines = list(staged.lines)
    affected = set(seen)
    for question in questions:
        target, text = question["target_id"], question["text"].strip()
        if any(existing.target_id == target and existing.text == text for existing in new_questions):
            continue
        question_id = uuid5(UUID(draft.draft_id), f"question:{target}:{text}").hex
        projection = blockers if target == draft.draft_id else next(line.configuration["questions"] for line in lines if line.line_id == target)
        new_questions.append(ScopedQuestion(question_id, target, text, owns_projection=text not in projection))
        if target == draft.draft_id:
            if text not in blockers:
                blockers.append(text)
        else:
            affected.add(target)
            for index, line in enumerate(lines):
                if line.line_id == target:
                    config = deepcopy(line.configuration)
                    if text not in config["questions"]:
                        config["questions"].append(text)
                    lines[index] = replace(line, configuration=config)
    if not updates and len(new_questions) == len(draft.questions):
        raise ValueError("追問已存在，沒有新變更。")
    originals = {line.line_id: line for line in draft.lines}
    for index, line in enumerate(lines):
        if line.line_id in affected:
            config = deepcopy(line.configuration)
            config.update(revision=originals[line.line_id].configuration.get("revision", 0) + 1,
                          status="WAITING_FOR_INPUT", preview=None, calc_result=None, ref_no=None)
            lines[index] = replace(line, configuration=config, revision=originals[line.line_id].revision + 1)
    event = DraftChange(draft.revision + 1, "multi_proposal", tuple(line.line_id for line in draft.lines if line.line_id in affected),
                        actor.strip(), reason.strip(), datetime.now(timezone.utc).isoformat(), deepcopy(proposal))
    return refresh_review_evidence(replace(staged, lines=tuple(lines), blockers=tuple(blockers), questions=tuple(new_questions),
                   revision=draft.revision + 1, changes=(*deepcopy(draft.changes), event)), previous=draft)
