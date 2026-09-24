"""工單尺寸／數量的唯讀診斷，不套規格、不推定台尺、不產生報價。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import re
from uuid import UUID, uuid5

from agent.work_orders import WorkOrderBatch


VERSION = "work-order-numeric-diagnostics-v1"
_N = r"[0-9]{1,9}(?:\.[0-9]{1,6})?"
_U = r"(?:mm|cm|毫米|公分|台尺|尺)"
_CHAIN = re.compile(
    rf"(?<![0-9A-Za-z./+\-])(?P<a>{_N})(?:\s*(?P<ua>{_U}))?\s*[xX*×]\s*"
    rf"(?P<b>{_N})(?:\s*(?P<ub>{_U}))?(?:\s*[xX*×]\s*(?P<c>{_N})(?:\s*(?P<uc>{_U}))?)?(?![0-9.eE])", re.I)
_EXTRA_AXES = re.compile(rf"(?:\s*[xX*×]\s*{_N}(?:\s*{_U})?)+", re.I)
_SCALAR = re.compile(rf"(?<![0-9A-Za-z./+\-])(?P<value>{_N})\s*(?P<unit>{_U})(?![A-Za-z])", re.I)
_COUNT = re.compile(rf"(?<![0-9A-Za-z./+\-])(?P<value>{_N})\s*(?P<unit>張|個|件|套|組|片|孔)")
_FRACTION = re.compile(r"(?<![0-9./])(?P<part>[0-9]{1,3})\s*/\s*(?P<whole>[0-9]{1,3})\s*圓")
_ALUMINUM = re.compile(rf"(?<![0-9A-Za-z./+\-]){_N}\s*鋁(?:合金)?")
_EQUATION = re.compile(rf"(?<![0-9.]){_N}\s*-\s*{_N}\s*=\s*{_N}(?![0-9.])")
_FOLD = str.maketrans("０１２３４５６７８９．Ｘｘ＊ＭｍＣｃ／（）", "0123456789.Xx*MmCc/()")


@dataclass(frozen=True)
class NumericFinding:
    finding_id: str
    order_id: str | None
    source_line_id: str
    line_number: int
    start: int
    end: int
    raw: str
    kind: str
    values: tuple[str, ...]
    units: tuple[str, ...]
    centimeters: tuple[str, ...] | None
    scope: str
    status: str
    note: str


@dataclass(frozen=True)
class DiagnosticIssue:
    code: str
    source_line_id: str
    line_number: int
    start: int
    end: int
    raw: str
    message: str


def _decimal_text(value: Decimal) -> str:
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _unit(raw: str | None) -> str:
    return {"mm": "mm", "毫米": "mm", "cm": "cm", "公分": "cm", "尺": "chi", "台尺": "taiwan_foot"}.get((raw or "").lower(), "unknown")


def _cm(values, units):
    if not values or any(Decimal(value) <= 0 for value in values) or any(unit not in ("cm", "mm") for unit in units):
        return None
    return tuple(_decimal_text(Decimal(value) / (10 if unit == "mm" else 1)) for value, unit in zip(values, units))


def _role(prefix: str, suffix: str, scalar=False):
    if scalar and re.search(r"假厚\s*$", prefix):
        return "false_thickness"
    if scalar and re.search(r"(?:板厚|厚度|實厚|厚)\s*$", prefix):
        return "board_thickness"
    if scalar and (suffix.lstrip().startswith("圓孔") or re.search(r"(?:孔|孔徑|直徑)\s*$", prefix)):
        return "hole_diameter_candidate" if "孔" in (prefix[-6:] + suffix[:4]) else "diameter"
    if re.search(r"(?:單鋁|單掀鋁|雙掀鋁|線盒|毛刷)\s*$", prefix) or re.match(r"\s*(?:單鋁|單掀鋁|雙掀鋁|線盒)", suffix):
        return "accessory_dimensions_candidate"
    if scalar and re.match(r"\s*(?:圓|轉盤)", suffix):
        return "circle_diameter_candidate"
    if scalar and suffix.lstrip().startswith("用"):
        return "compatible_table_size"
    if scalar and suffix.lstrip().startswith("鋁"):
        return "accessory_dimension_candidate"
    return "length" if scalar else "table_dimensions_candidate"


def diagnose_work_order(batch: WorkOrderBatch) -> dict:
    """回傳可下載診斷；所有座標對應原文，沒有缺省公分或台尺換算。"""
    findings, issues = [], []
    owners = {key: order.order_id for order in batch.orders for key in order.line_ids}
    items = {item.source_line_id: item for order in batch.orders for item in order.items}

    for line in batch.lines:
        raw, text = line.raw, line.raw.translate(_FOLD)
        if not text.strip():
            continue

        def issue(code, start, end, message):
            candidate = DiagnosticIssue(code, line.line_id, line.number, start, end, raw[start:end], message)
            if candidate not in issues:
                issues.append(candidate)

        def finding(kind, start, end, values=(), units=(), centimeters=None, scope="unspecified", note="", status=None):
            identifier = uuid5(UUID(batch.batch_id), f"numeric:{line.line_id}:{kind}:{start}:{end}").hex
            findings.append(NumericFinding(identifier, owners.get(line.line_id), line.line_id, line.number,
                                             start, end, raw[start:end], kind, tuple(values), tuple(units), centimeters,
                                             scope, status or ("NORMALIZED" if centimeters is not None else "NEEDS_CONFIRMATION"), note))

        def dimension_issues(start, end, values, units):
            if any(Decimal(value) <= 0 for value in values):
                issue("nonpositive_dimension", start, end, "尺寸必須大於零，請確認原始數值。")
            if "unknown" in units:
                issue("unit_required", start, end, "尺寸缺少單位；不依數字大小預設公分、毫米或台尺。")
            if any(unit in ("chi", "taiwan_foot") for unit in units):
                issue("foot_policy_required", start, end, "尺的定義、換算與取整慣例尚未核准，不自動換算。")

        spans = []
        pairs = []
        for match in _CHAIN.finditer(text):
            start, end = match.span()
            # 拒絕截取多於三軸的尺寸鏈，不能把第四數字遺失後當成有效規格。
            extra = _EXTRA_AXES.match(text, end)
            if extra:
                issue("unsupported_dimension_chain", start, extra.end(), "尺寸鏈超過目前支援範圍，需人工拆分。")
                spans.append((start, extra.end()))
                continue
            if any(a <= start < b for a, b in spans):
                continue
            spans.append((start, end))
            values = [match["a"], match["b"]]
            units = [_unit(match["ua"]), _unit(match["ub"])]
            third = match["c"]
            parenthesized = text[:start].rstrip().endswith("(") and text[end:].lstrip().startswith(")")
            panel = third is not None and parenthesized and not match["uc"]
            if third is not None and not panel:
                values.append(third)
                units.append(_unit(match["uc"]))
            explicit = {unit for unit in units if unit != "unknown"}
            if len(explicit) == 1:
                units = [next(iter(explicit)) if unit == "unknown" else unit for unit in units]
            role = "panel_dimensions_candidate" if panel else ("three_axis_dimensions" if third else _role(text[:start], text[end:]))
            cm = _cm(values, units)
            finding(role, start, end, values, units, cm, note="尺寸用途仍須核對；不自動查選配或計價。")
            dimension_issues(start, end, values, units)
            pairs.append((role, start, end, values, units, cm))
            if panel:
                cstart, cend = match.span("c")
                finding("panel_count_candidate", cstart, cend, (third,), ("pieces",), scope="per_product_candidate",
                        note="括號末項可能為分片數，不是成品數或厚度；需人工確認。")
                issue("assembly_interpretation_required", start, end, "請確認括號表示分片尺寸及每件片數，不自動當成三軸尺寸。")
                if Decimal(third) <= 0 or Decimal(third) != Decimal(third).to_integral_value():
                    issue("invalid_panel_count", cstart, cend, "候選分片數不是正整數，不能套入用量。")
        for match in _SCALAR.finditer(text):
            if any(a <= match.start() < b for a, b in spans):
                continue
            start, end = match.span()
            value, unit = match["value"], _unit(match["unit"])
            kind = _role(text[:start], text[end:], scalar=True)
            finding(kind, start, end, (value,), (unit,), _cm((value,), (unit,)), note="數值换算不等於用途、板材或加工方案已確認。")
            dimension_issues(start, end, (value,), (unit,))
            if kind == "false_thickness":
                issue("false_thickness_not_board", start, end, "假厚是完成邊厚，不能當作整片板材實厚；需另確認基板與加厚結構。")
        for match in _FRACTION.finditer(text):
            start, end = match.span()
            finding("circle_segment", start, end, (match["part"], match["whole"]), ("fraction",),
                    note="分片形狀不是產品數量，不換成0.25或0.5件產品。")
            issue("segment_count_required", start, end, "請另行確認每件成品的分片數及拼接方式。")
        for match in _COUNT.finditer(text):
            if any(a <= match.start() < b for a, b in spans):
                continue
            start, end = match.span()
            prefix = re.split(r"[，,；;。]", text[:start])[-1]
            each = bool(re.search(r"每\s*(?:桌|張|件|台|個產品)", prefix))
            scope = "per_product_explicit" if each else ("per_product_candidate" if line.line_id in items and match["unit"] == "孔" else "unspecified")
            kind = "hole_count" if match["unit"] == "孔" else ("component_count" if match["unit"] == "片" else "item_count")
            finding(kind, start, end, (match["value"],), (match["unit"],), scope=scope,
                    note="保留數量層級；不與行尾成品候選相乘或合併。")
            value = Decimal(match["value"])
            if value <= 0 or value != value.to_integral_value():
                issue("invalid_item_count", start, end, "件數候選不是正整數，請確認數量及單位。")
            if scope != "per_product_explicit":
                issue("quantity_scope_required", start, end, "請確認為每件用量、整單總量、成品或部件數量。")
        item = items.get(line.line_id)
        if item and item.quantity_raw:
            delimiter = re.search(r"[\-─—－]{3,}\s*", text)
            if delimiter:
                start = delimiter.end()
                end = start + len(item.quantity_raw)
                finding("row_quantity_candidate", start, end, (item.quantity_raw,), ("items",), scope="source_row",
                        note="行尾是來源明細數量候選；桌面、腳座及改裝項目不等於相同成品。")
                if item.quantity_candidate is None:
                    issue("invalid_row_quantity", start, end, "未取得有效正整數明細數量。")
        for match in _ALUMINUM.finditer(text):
            issue("aluminum_spec_required", *match.span(), "鋁件前的數字缺單位或用途；不得自行補成尺、公分或型號。")
        for match in _EQUATION.finditer(text):
            finding("geometry_formula", *match.span(), note="尺寸算式不等於唯一的曲線幾何定義。")
            issue("geometry_definition_required", *match.span(), "請確認各數值的幾何意義及圖面，不自行產生弧度。")
        for phrase, replacement in (("走縣", "走線"), ("置物版", "置物板"), ("船形", "船型")):
            for match in re.finditer(re.escape(phrase), text):
                issue("alias_confirmation_required", *match.span(), f"「{phrase}」可能對應「{replacement}」，僅提出候選，不改寫原文或否定條件。")
        for match in re.finditer(r"照圖|靠單|如圖面|實內尺寸", text):
            issue("external_evidence_required", *match.span(), "需要明確圖面、訂單附圖或實測依據；文字解析不能補齊。")
        # 只檢查必要的外框容納条件，不推定拼接方向或把算式當正式幾何。
        overall = next((pair for pair in pairs if pair[0] == "table_dimensions_candidate"), None)
        panel = next((pair for pair in pairs if pair[0] == "panel_dimensions_candidate"), None)
        if overall and panel:
            if overall[5] is not None and panel[5] is not None:
                outer, inner = overall[5], panel[5]
                comparable = True
            else:
                outer, inner = overall[3], panel[3]
                comparable = overall[4] == panel[4] == ["unknown", "unknown"]
            if comparable:
                a, b = sorted(map(Decimal, outer)), sorted(map(Decimal, inner))
                if any(inner_dim > outer_dim for inner_dim, outer_dim in zip(b, a)):
                    issue("overall_panel_size_conflict", overall[1], panel[2],
                          "若總尺寸與分片採相同單位，分片無法容納於外框；請確認尺寸是否誤植，禁止自動補字。")
            else:
                issue("assembly_units_unresolved", overall[1], panel[2], "總尺寸與分片單位不足以比較，須先確認各自單位。")

    return {"document_type": "work_order_numeric_diagnostics_only", "version": VERSION,
            "batch_id": batch.batch_id, "source_digest": batch.source_digest, "batch_revision": batch.revision,
            "findings": [asdict(finding) for finding in findings], "issues": [asdict(issue) for issue in issues],
            "can_apply_configuration": False, "can_quote": False}
