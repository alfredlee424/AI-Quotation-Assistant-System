"""生產工單核對入口：純文字切分與不可變候選草稿，不查價、不存報價。

只辨識明確的分類標題、日期式工單號與破折號數量列。沒有辨識到的
原文仍保留；分類與數量都是候選，不能當作已核准的報價規格。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import re
from uuid import UUID, uuid4, uuid5


MAX_TEXT_LENGTH = 100_000
MAX_LINES = 2_000
FAMILIES = ("環式會議桌", "會議桌", "餐桌")
LINE_KINDS = ("section", "order", "item", "note", "unclassified", "blank")
_HEADER = re.compile(r"^\s*(?:[（(]\s*\d+\s*[）)]\s*)?(?P<ref>\d{8,9}-\d+)(?P<tail>.*)$")
_SECTION = re.compile(
    r"^\s*(?:(?:Trello|Terllo)\s*內\s*)?(?P<family>環式會議桌|會議桌|餐桌)"
    r"\s*(?:[｛{]?\s*生產[工]?單.*)?$", re.IGNORECASE,
)
_DELIMITER = re.compile(r"[\-─—－]{3,}")
_QUANTITY = re.compile(r"\s*(?P<qty>\d+(?:\.\d+)?)(?P<suffix>.*)$")


def _item_parts(raw: str) -> dict[str, str] | None:
    # 先找連續分隔符，再解析其後數量；避免「長串破折號但無數量」的
    # 輸入觸發混合懶惰／貪婪正規式的平方級回溯。
    delimiter = _DELIMITER.search(raw)
    if delimiter is None or not raw[:delimiter.start()].strip():
        return None
    tail = _QUANTITY.fullmatch(raw[delimiter.end():])
    if tail is None:
        return None
    return {"description": raw[:delimiter.start()], "qty": tail["qty"], "suffix": tail["suffix"]}


@dataclass(frozen=True)
class SourceLine:
    line_id: str
    number: int
    raw: str
    kind: str
    family_hint: str = ""


@dataclass(frozen=True)
class ItemCandidate:
    item_id: str
    source_line_id: str
    description_raw: str
    quantity_raw: str | None
    quantity_candidate: int | None
    suffix_raw: str
    status: str = "UNREVIEWED"


@dataclass(frozen=True)
class OrderCandidate:
    order_id: str
    header_line_id: str
    source_ref_raw: str
    family_hint: str
    line_ids: tuple[str, ...]
    items: tuple[ItemCandidate, ...]
    context_line_ids: tuple[str, ...]
    warnings: tuple[str, ...]
    status: str = "REVIEW_REQUIRED"


@dataclass(frozen=True)
class Correction:
    revision: int
    line_id: str
    before_kind: str
    after_kind: str
    before_family: str
    after_family: str
    actor: str
    reason: str
    recorded_at: str


@dataclass(frozen=True)
class WorkOrderBatch:
    batch_id: str
    raw_text: str
    source_digest: str
    lines: tuple[SourceLine, ...]
    orders: tuple[OrderCandidate, ...]
    unassigned_line_ids: tuple[str, ...]
    corrections: tuple[Correction, ...] = ()
    revision: int = 0
    schema_version: int = 1
    parser_version: str = "work-order-boundaries-v1"
    status: str = "REVIEW_REQUIRED"


def _identifier(batch_id: str, label: str) -> str:
    return uuid5(UUID(batch_id), label).hex


def _candidate(batch_id: str, line: SourceLine) -> ItemCandidate:
    match = _item_parts(line.raw.strip())
    quantity_raw = match["qty"] if match else None
    # 數量只呈現候選，不換算單位，不把 0、小數或異常符號視為合法件數。
    quantity = (int(quantity_raw) if quantity_raw and len(quantity_raw) <= 9
                and quantity_raw.isascii() and quantity_raw.isdigit() else None)
    if quantity is not None and quantity <= 0:
        quantity = None
    if match and match["suffix"].lstrip().startswith((".", "+", "-", "e", "E", "/")):
        quantity = None
    return ItemCandidate(
        item_id=_identifier(batch_id, "item:" + line.line_id), source_line_id=line.line_id,
        description_raw=match["description"].strip() if match else line.raw,
        quantity_raw=quantity_raw, quantity_candidate=quantity,
        suffix_raw=match["suffix"] if match else "",
    )


def _build_orders(batch_id: str, lines: tuple[SourceLine, ...]) -> tuple[tuple[OrderCandidate, ...], tuple[str, ...]]:
    orders, unassigned = [], []
    section_family = ""
    active: list[SourceLine] = []
    active_family = ""

    def finish() -> None:
        if not active:
            return
        header = active[0]
        match = _HEADER.fullmatch(header.raw)
        source_ref = match["ref"] if match else ""
        warnings = ["僅為需求核對草稿；未核准規格、數量、適用範圍及成本，不可建立正式報價。"]
        if not active_family:
            warnings.append("產品類別線索未明；請修正分類標題或工單分類，不能預設產品。")
        if source_ref:
            try:
                date_text = source_ref.split("-", 1)[0]
                if len(date_text) != 8:
                    raise ValueError
                datetime.strptime(date_text, "%Y%m%d")
            except ValueError:
                warnings.append("來源單號的日期格式異常，已保留原文，請人工確認。")
        else:
            warnings.append("此工單邊界由人工指定，未辨識標準來源單號；標題原文仍保留。")
        items = tuple(_candidate(batch_id, line) for line in active if line.kind == "item")
        if not items:
            warnings.append("尚無明細候選；請核對行分類，不代表這張工單沒有產品。")
        if any(item.quantity_candidate is None for item in items):
            warnings.append("部分明細未取得正整數數量候選，須人工釐清。")
        if any(line.kind == "unclassified" for line in active):
            warnings.append("有未分類內容；不可略過，標題或補充行也可能包含規格。")
        orders.append(OrderCandidate(
            order_id=_identifier(batch_id, "order:" + header.line_id),
            header_line_id=header.line_id, source_ref_raw=source_ref,
            family_hint=active_family, line_ids=tuple(line.line_id for line in active),
            items=items,
            context_line_ids=tuple(line.line_id for line in active if line.kind != "item"),
            warnings=tuple(warnings),
        ))

    for line in lines:
        if line.kind == "section":
            finish()
            active = []
            section_family = line.family_hint
            # 分類標題不歸入上一张工單，亦不丟棄原始行。
            unassigned.append(line.line_id)
        elif line.kind == "order":
            finish()
            active = [line]
            active_family = line.family_hint or section_family
        elif active:
            active.append(line)
        else:
            unassigned.append(line.line_id)
    finish()
    counts: dict[str, int] = {}
    for order in orders:
        if order.source_ref_raw:
            counts[order.source_ref_raw] = counts.get(order.source_ref_raw, 0) + 1
    orders = [replace(order, warnings=order.warnings + ("來源單號重複；已分開保存，不自動合併。",))
              if counts.get(order.source_ref_raw, 0) > 1 else order for order in orders]
    return tuple(orders), tuple(unassigned)


def parse_work_orders(text: str) -> WorkOrderBatch:
    """建立新批次；只切分文字結構，不取得產品代碼或生成任何報價。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("請貼上非空白的生產工單內容。")
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"工單文字超過 {MAX_TEXT_LENGTH} 字元，請分批處理。")
    raw_lines = text.splitlines()
    if len(raw_lines) > MAX_LINES:
        raise ValueError(f"工單超過 {MAX_LINES} 行，請分批處理。")
    batch_id = uuid4().hex
    lines = []
    for number, raw in enumerate(raw_lines, 1):
        family = ""
        section = _SECTION.fullmatch(raw)
        if not raw.strip():
            kind = "blank"
        elif section:
            kind, family = "section", section["family"]
        elif _HEADER.fullmatch(raw):
            kind = "order"
        elif _item_parts(raw.strip()):
            kind = "item"
        else:
            kind = "unclassified"
        lines.append(SourceLine(_identifier(batch_id, f"line:{number}"), number, raw, kind, family))
    source = tuple(lines)
    orders, unassigned = _build_orders(batch_id, source)
    return WorkOrderBatch(batch_id, text, hashlib.sha256(text.encode("utf-8")).hexdigest(),
                          source, orders, unassigned)


def reclassify_line(batch: WorkOrderBatch, *, line_id: str, kind: str, family_hint: str = "",
                    expected_revision: int, actor: str, reason: str) -> WorkOrderBatch:
    """人工修正分類／工單邊界，原子返回新版本；不修改來源或核准報價。"""
    if type(expected_revision) is not int or expected_revision != batch.revision:
        raise ValueError("草稿版本已變更，請重新載入後再修正。")
    if kind not in LINE_KINDS or kind == "blank":
        raise ValueError("請選擇合法的非空白行分類。")
    if family_hint not in ("", *FAMILIES) or (family_hint and kind not in ("section", "order")):
        raise ValueError("產品類別線索只能指定於分類標題或工單標題。")
    if kind == "section" and not family_hint:
        raise ValueError("分類標題必須指定產品類別線索。")
    if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValueError("請填寫修正人與理由；分類修正不代表工程核准。")
    if len(actor) > 80 or len(reason) > 1000:
        raise ValueError("修正人限 80 字，修正理由限 1000 字。")
    source = next((line for line in batch.lines if line.line_id == line_id), None)
    if source is None:
        raise ValueError("找不到本批次來源行，不能修改其他批次。")
    if not source.raw.strip():
        raise ValueError("空白來源行不能轉成工單或明細。")
    if source.kind == kind and source.family_hint == family_hint:
        raise ValueError("分類未變更，無需建立修正版本。")
    updated = replace(source, kind=kind, family_hint=family_hint)
    lines = tuple(updated if line.line_id == line_id else line for line in batch.lines)
    orders, unassigned = _build_orders(batch.batch_id, lines)
    correction = Correction(batch.revision + 1, line_id, source.kind, kind,
                            source.family_hint, family_hint, actor.strip(), reason.strip(),
                            datetime.now(timezone.utc).isoformat())
    return replace(batch, lines=lines, orders=orders, unassigned_line_ids=unassigned,
                   revision=batch.revision + 1, corrections=(*batch.corrections, correction))


def export_review(batch: WorkOrderBatch) -> dict:
    """只輸出核對資料；不是可匯入報價或跳過驗證的正式預覽。"""
    return {"document_type": "work_order_review_only", **asdict(batch)}
