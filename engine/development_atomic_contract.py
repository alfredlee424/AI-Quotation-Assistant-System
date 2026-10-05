"""T29 獨立開發固定內容；不是 trial/review 轉換器或正式核准契約。"""
from dataclasses import asdict, dataclass, fields
from decimal import Decimal, localcontext
import hashlib
import json
import re

from engine.quote_number_reservation import (
    DevelopmentReservationChild, DevelopmentReservationRequest, _identifier,
    _validate as validate_reservation,
)


REQUEST_VERSION = "development-atomic-request-v1"
SCHEMA_VERSION = "development-atomic-schema-v1"
DOCUMENT_TYPE = "development-atomic-snapshot"
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_DOCUMENT_BYTES = 128 * 1024 * 1024
MAX_ROWS = 100000  # 整批資源限制；每張另限 99999，不默拆。
MONEY_FIELDS = ("total_cost", "subtotal", "discount_amount", "after_discount", "tax_amount", "total_price")


@dataclass(frozen=True)
class DevelopmentSourceLine:
    line_no: int
    text: str
    kind: str  # item/shared/context/blank；完整原文逐行覆蓋。
    source_item_id: str | None
    applies_to: tuple[str, ...]


@dataclass(frozen=True)
class DevelopmentSource:
    source_batch_id: str
    source_order_id: str
    original_order_no: str
    origin: str
    parser_version: str
    raw_text: str
    lines: tuple[DevelopmentSourceLine, ...]
    history_json: str


@dataclass(frozen=True)
class DevelopmentSelection:
    path: str
    code: str
    line_qty: str
    configuration_json: str


@dataclass(frozen=True)
class DevelopmentCostRow:
    seq_no: str
    path: str
    part_code: str
    part_desc: str
    opt_code: str
    opt_desc: str
    spc_code: str
    spdsc: str
    unit: str
    product_qty: int
    line_qty: str
    qty: str
    original_qty: str
    stdqty: str
    stdpar: str
    compri: str
    part_cost: str
    unit_cost: str
    unit_price: str
    amount: str
    quantity_source: str
    quantity_evidence_json: str
    price_evidence_json: str
    configuration_json: str


@dataclass(frozen=True)
class DevelopmentAmounts:
    total_cost: str
    subtotal: str
    discount_amount: str
    after_discount: str
    tax_amount: str
    total_price: str
    quote_rate: str
    discount_rate: str
    tax_rate: str
    raw_amounts_json: str
    cost_display_difference: str
    tax_rounding_difference: str


@dataclass(frozen=True)
class DevelopmentAtomicChild:
    child_quote_id: str
    source_item_id: str
    configuration_line_id: str
    source_line_no: int
    position: int
    product_qty: int
    revision: int
    prodkind: str
    label: str
    description: str
    shared_description: str
    selections: tuple[DevelopmentSelection, ...]
    rows: tuple[DevelopmentCostRow, ...]
    amounts: DevelopmentAmounts
    dimensions_json: str
    requirements_json: str
    conditions_json: str
    drawings_json: str


@dataclass(frozen=True)
class DevelopmentAtomicRequest:
    workgroup: str
    batch_id: str
    request_id: str
    preview_identity: str
    save_id: str
    revision: int
    origin: str
    source: DevelopmentSource
    children: tuple[DevelopmentAtomicChild, ...]
    totals_json: str
    policy_json: str
    request_version: str = REQUEST_VERSION
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True)
class DevelopmentSnapshotIdentity:
    workgroup: str
    batch_id: str
    request_id: str
    preview_identity: str
    save_id: str
    schema_version: str = SCHEMA_VERSION


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def text(value, capacity, *, empty=False):
    if (type(value) is not str or len(value) > capacity or "\x00" in value
            or (not empty and not value.strip())):
        raise ValueError(f"字串缺漏／超過容量 {capacity}；不截斷")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise ValueError("字串不是有效 Unicode") from error


def fixed_object(value, *, capacity=MAX_PAYLOAD_BYTES):
    text(value, capacity)
    if len(value.encode("utf-8")) > capacity:
        raise ValueError("固定 JSON 超過 byte 容量")

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("重複 JSON key")
            result[key] = item
        return result

    def reject_float(_):
        raise ValueError("固定 JSON 數量／金額必須使用 decimal strings，不接受 float/nonfinite")

    try:
        result = json.loads(value, object_pairs_hook=pairs, parse_float=reject_float, parse_constant=reject_float)
    except (RecursionError, OverflowError) as error:
        raise ValueError("JSON 超出資源限制") from error
    if type(result) is not dict or not result:
        raise ValueError("固定 JSON 必須為非空 object")
    return result


def decimal_string(value, *, positive=False, signed=False, money=False):
    # 保守上限：18 位整數、18 位小數；拒絕 exponent/NaN/Infinity/負零/float。
    pattern = r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,18})?" if signed else r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,18})?"
    if type(value) is not str or not re.fullmatch(pattern, value):
        raise ValueError("固定精確十進位字串格式／容量錯誤")
    number = Decimal(value)
    if (positive and number <= 0) or (number == 0 and value.startswith("-")):
        raise ValueError("數量／係數／分母必須為正有限數，不接受負零")
    if money and not re.fullmatch(r"(?:0|[1-9][0-9]{0,17})\.[0-9]{2}", value):
        raise ValueError("固定金額必須為兩位小數")
    return number


def integer(value, lower=0, upper=999999999):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError("整數超出容量或非正確型別")


def cost_sequence(position):
    integer(position, 1, 99999)
    return f"{position:05d}"


def _tuple(value, cls, lower=1, upper=999):
    if type(value) is not tuple or not lower <= len(value) <= upper or any(type(v) is not cls for v in value):
        raise ValueError("必須為完整 typed tuple，數量超出開發資源限制")


def identity_of(request):
    return DevelopmentSnapshotIdentity(**{f.name: getattr(request, f.name) for f in fields(DevelopmentSnapshotIdentity)})


def validate_identity(identity):
    if type(identity) is not DevelopmentSnapshotIdentity or identity.schema_version != SCHEMA_VERSION:
        raise ValueError("僅接受同版開發快照 identity")
    if type(identity.workgroup) is not str or not re.fullmatch(r"[0-9]{3}", identity.workgroup):
        raise ValueError("事業別錯誤")
    ids = [getattr(identity, key) for key in ("batch_id", "request_id", "preview_identity", "save_id")]
    for value in ids:
        _identifier(value)
    if len(set(ids)) != 4:
        raise ValueError("batch/request/preview/save 識別必須分離")


def validate_request(request):
    if type(request) is not DevelopmentAtomicRequest:
        raise ValueError("僅接受獨立 typed development atomic snapshot；不接受 trial/review/preview dict")
    if request.request_version != REQUEST_VERSION or request.schema_version != SCHEMA_VERSION:
        raise ValueError("開發快照版本不相容")
    validate_identity(identity_of(request))
    integer(request.revision)
    text(request.origin, 80)
    source = request.source
    if type(source) is not DevelopmentSource:
        raise ValueError("必須提供完整 typed 來源")
    for value in (source.source_batch_id, source.source_order_id):
        _identifier(value)
    for value in (source.original_order_no, source.origin, source.parser_version):
        text(value, 256)
    text(source.raw_text, 1024 * 1024)
    fixed_object(source.history_json)
    _tuple(source.lines, DevelopmentSourceLine, upper=10000)
    if source.raw_text != "\n".join(line.text for line in source.lines):
        raise ValueError("來源原文與逐行內容不完整／不一致")
    _tuple(request.children, DevelopmentAtomicChild)
    for child in request.children:
        _tuple(child.rows, DevelopmentCostRow, upper=99999)
    if sum(len(child.rows) for child in request.children) > MAX_ROWS:
        raise ValueError("整批成本列超出 100000 資源限制，不拆批")
    source_items = {}
    for position, line in enumerate(source.lines, 1):
        integer(line.line_no, 1, 10000)
        text(line.text, 65535, empty=True)
        if line.line_no != position or line.kind not in ("item", "shared", "context", "blank"):
            raise ValueError("來源行號／分類錯誤")
        if (line.kind == "blank") != (not line.text.strip()):
            raise ValueError("來源空行分類錯誤")
        if type(line.applies_to) is not tuple or len(set(line.applies_to)) != len(line.applies_to):
            raise ValueError("來源作用範圍重複或非固定 tuple")
        if line.kind == "item":
            _identifier(line.source_item_id)
            if line.source_item_id in source_items or line.applies_to != (line.source_item_id,):
                raise ValueError("來源品項重複／歸屬錯誤")
            source_items[line.source_item_id] = line
        elif line.source_item_id is not None or (line.kind == "shared" and not line.applies_to):
            raise ValueError("共用來源缺範圍或非品項帶有品項身份")
    for line in source.lines:
        if not set(line.applies_to) <= source_items.keys():
            raise ValueError("來源範圍引用未知品項")
    if [c.source_item_id for c in request.children] != list(source_items):
        raise ValueError("來源／子報價順序必須完整雙向覆蓋，不可缺張或重排")
    policy = fixed_object(request.policy_json)
    if set(policy) != {"formula_version", "rounding_version", "pricing_version", "currency", "discount_rate", "tax_rate"}:
        raise ValueError("完整政策欄位缺漏／未知")
    for key in ("formula_version", "rounding_version", "pricing_version", "currency"):
        text(policy[key], 80)
    for key in ("discount_rate", "tax_rate"):
        if decimal_string(policy[key]) > 1:
            raise ValueError("稅率／折扣率超出開發契約")
    unique = {key: set() for key in ("child_quote_id", "configuration_line_id")}
    with localcontext() as ctx:
        ctx.prec = 100  # 僅驗證固定數量／加總／差額，不執行價格公式或重新捨入。
        for position, child in enumerate(request.children, 1):
            for key, seen in unique.items():
                value = getattr(child, key)
                _identifier(value)
                if value in seen:
                    raise ValueError("子報價／配置重複")
                seen.add(value)
            integer(child.position, 1, 999)
            integer(child.source_line_no, 1, 10000)
            integer(child.product_qty, 1)
            integer(child.revision)
            if child.position != position or child.source_line_no != source_items[child.source_item_id].line_no:
                raise ValueError("子報價來源行／位置不一致")
            if child.description != source_items[child.source_item_id].text:
                raise ValueError("子報價專屬原文不一致")
            shared = "\n".join(line.text for line in source.lines if line.kind == "shared" and child.source_item_id in line.applies_to)
            if child.shared_description != shared:
                raise ValueError("共用原文缺漏或作用範圍錯誤")
            text(child.prodkind, 16)
            text(child.label, 256)
            for key in ("dimensions_json", "requirements_json", "conditions_json", "drawings_json"):
                fixed_object(getattr(child, key))
            _validate_rows(child)
            _validate_amounts(child.amounts, policy)
            if Decimal(child.amounts.cost_display_difference) != sum((Decimal(row.part_cost) for row in child.rows), Decimal(0)) - Decimal(child.amounts.total_cost):
                raise ValueError("成本展示列差額不一致；禁止湊數")
        totals = fixed_object(request.totals_json)
        if set(totals) != set(MONEY_FIELDS):
            raise ValueError("批次金額欄位缺漏／未知")
        for key in MONEY_FIELDS:
            if decimal_string(totals[key], money=True) != sum((Decimal(getattr(c.amounts, key)) for c in request.children), Decimal(0)):
                raise ValueError("批次總額非逐張固定金額加總")
    payload = canonical(asdict(request))
    if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("整批固定內容超過 16 MiB，不截斷或拆批")
    return payload


def _validate_rows(child):
    _tuple(child.selections, DevelopmentSelection, upper=99999)
    _tuple(child.rows, DevelopmentCostRow, upper=99999)
    selected = {}
    for selection in child.selections:
        text(selection.path, 256)
        text(selection.code, 32)
        decimal_string(selection.line_qty, positive=True)
        fixed_object(selection.configuration_json)
        if selection.path in selected:
            raise ValueError("同張配置 path 重複")
        selected[selection.path] = selection
    if [r.path for r in child.rows] != list(selected):
        raise ValueError("成本列與配置缺漏／未知／重複或順序不一致")
    for position, row in enumerate(child.rows, 1):
        if row.seq_no != cost_sequence(position):
            raise ValueError("各張成本列須為連續五位序號 00001..99999")
        selection = selected[row.path]
        if (row.spc_code, row.line_qty, row.configuration_json) != (selection.code, selection.line_qty, selection.configuration_json):
            raise ValueError("成本列與配置內容不一致")
        integer(row.product_qty, 1)
        if row.product_qty != child.product_qty:
            raise ValueError("成本列成品數不一致")
        for key in ("part_code", "opt_code", "spc_code", "unit"):
            text(getattr(row, key), 32)
        for key in ("part_desc", "opt_desc", "spdsc"):
            text(getattr(row, key), 1024, empty=True)
        for key in ("line_qty", "qty", "original_qty", "stdpar"):
            decimal_string(getattr(row, key), positive=True)
        if Decimal(row.qty) != child.product_qty * Decimal(row.line_qty):
            raise ValueError("實際數量與成品／每件數不一致")
        for key in ("stdqty", "compri", "part_cost", "unit_cost", "unit_price", "amount"):
            decimal_string(getattr(row, key))
        if row.quantity_source not in ("ordqty", "per_item_policy", "zero_cost", "structural"):
            raise ValueError("未知用量來源")
        if row.quantity_source in ("zero_cost", "structural") and any(Decimal(getattr(row, key)) != 0 for key in (
                "compri", "part_cost", "unit_cost", "unit_price", "amount")):
            raise ValueError("零價／結構節點不得夾帶付費金額")
        for key in ("quantity_evidence_json", "price_evidence_json"):
            fixed_object(getattr(row, key))


def _validate_amounts(amounts, policy):
    if type(amounts) is not DevelopmentAmounts:
        raise ValueError("必須提供 typed 逐張固定金額")
    for key in MONEY_FIELDS:
        decimal_string(getattr(amounts, key), money=True)
    decimal_string(amounts.quote_rate, positive=True)
    for key in ("discount_rate", "tax_rate"):
        if getattr(amounts, key) != policy[key]:
            raise ValueError("逐張政策率與整批固定政策不一致")
    raw = fixed_object(amounts.raw_amounts_json)
    if set(raw) != set(MONEY_FIELDS):
        raise ValueError("未捨入金額缺漏／未知")
    for value in raw.values():
        decimal_string(value)
    decimal_string(amounts.cost_display_difference, signed=True)
    difference = decimal_string(amounts.tax_rounding_difference, signed=True)
    if difference != Decimal(amounts.total_price) - Decimal(amounts.after_discount) - Decimal(amounts.tax_amount):
        raise ValueError("固定稅捨入差額不一致；禁止湊數")


def build_development_reservation_request(request):
    """只序列化獨立開發型別；全部內容（含 save_id/金額/列）在保留前固定。"""
    payload = validate_request(request)
    result = DevelopmentReservationRequest(
        request.workgroup, request.batch_id, request.request_id, request.preview_identity,
        request.source.source_batch_id, request.source.source_order_id, request.revision,
        request.origin, payload, tuple(DevelopmentReservationChild(
            request.workgroup, request.batch_id, request.source.source_order_id,
            c.child_quote_id, c.source_item_id, c.configuration_line_id, c.product_qty,
            canonical(asdict(c))) for c in request.children))
    validate_reservation(result)
    return result


def decode_request(payload):
    """只供持久開發快照按版本查回；不公開通用文件轉型。"""
    def construct(cls, data):
        if type(data) is not dict or set(data) != {f.name for f in fields(cls)}:
            raise ValueError("持久開發文件欄位缺漏／未知")
        return cls(**data)

    try:
        data = fixed_object(payload)
        source = data["source"]
        source["lines"] = tuple(construct(DevelopmentSourceLine, {**line, "applies_to": tuple(line["applies_to"])}) for line in source["lines"])
        data["source"] = construct(DevelopmentSource, source)
        children = []
        for child in data["children"]:
            child["selections"] = tuple(construct(DevelopmentSelection, s) for s in child["selections"])
            child["rows"] = tuple(construct(DevelopmentCostRow, r) for r in child["rows"])
            child["amounts"] = construct(DevelopmentAmounts, child["amounts"])
            children.append(construct(DevelopmentAtomicChild, child))
        data["children"] = tuple(children)
        result = construct(DevelopmentAtomicRequest, data)
        if validate_request(result) != payload:
            raise ValueError("持久文件非原始固定序列化")
        return result
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("持久開發文件格式錯誤") from error
