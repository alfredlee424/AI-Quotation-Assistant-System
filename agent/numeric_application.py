"""數值候選的人工受控套用；不取代需求／工程核准，不修改診斷原文。"""
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json

from agent import work_order_diagnostics as diagnostics
from agent.multi_quote import DraftChange, _identity, apply_multi_command, export_multi_draft
from agent.multi_quote_agent import configuration_view
from engine.configuration import load_catalog


VERSION = "numeric-application-v1"
_FIELDS = {"finding_id", "line_id", "mode", "source_unit", "catalog_unit", "axis_order", "code", "reference", "explanation"}
_GEOMETRY_ISSUES = {"overall_panel_size_conflict", "assembly_interpretation_required", "assembly_units_unresolved",
                    "geometry_definition_required", "segment_count_required", "unsupported_dimension_chain"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _base(draft):
    return _digest({"draft": export_multi_draft(draft), "version": VERSION, "diagnostics_version": diagnostics.VERSION})


def numeric_candidates(draft, batch, line_id):
    """由目前來源重新診斷，不接受外部下載檔或使用者提供的正規化值。"""
    from agent.multi_quote import check_source_current

    if draft.source.get("kind") != "work_order" or batch is None:
        raise ValueError("受控套用只接受有來源批次的工單轉接草稿。")
    check_source_current(draft, batch)
    line = next((line for line in draft.lines if line.line_id == line_id), None)
    if line is None:
        raise ValueError("找不到本草稿活動明細；不能套用其他或已封存明細。")
    order = next(order for order in batch.orders if order.order_id == draft.source["order_id"])
    source_ids = set(line.source_line_ids) & set(order.line_ids)
    if not source_ids:
        raise ValueError("此明細沒有本工單直接來源，不能自動移用其他明細數值。")
    result = diagnostics.diagnose_work_order(batch)
    return {"version": result["version"],
            "findings": [f for f in result["findings"] if f["order_id"] == order.order_id and f["source_line_id"] in source_ids],
            "issues": [issue for issue in result["issues"] if issue["source_line_id"] in source_ids]}


def dimension_options(draft, line_id):
    line = next((line for line in draft.lines if line.line_id == line_id), None)
    if line is None or line.configuration.get("prodkind") != "CMT1":
        raise ValueError("本批尺寸套用只支援已指定環式產品的桌面尺寸部位；其他產品須另行核准映射。")
    path = r"CMT1\A001"
    catalog = load_catalog("CMT1", draft.workgroup)
    return [{"code": option["code"], "description": option.get("codsc") or ""}
            for option in catalog["options"].get(path, []) if option.get("code")]


def _metric(values, units, confirmed_unit, label):
    if confirmed_unit not in ("", "cm", "mm"):
        raise ValueError(f"{label}僅接受公分／毫米人工確認，不支援台尺換算。")
    if any(unit not in ("unknown", "cm", "mm") for unit in units):
        raise ValueError(f"{label}含尺或未支援單位，不能用人工勾選改成公分／毫米。")
    if "unknown" in units and not confirmed_unit:
        raise ValueError(f"{label}缺少單位，須明確人工確認並提供依據，不依數值大小猜測。")
    if confirmed_unit and any(unit not in ("unknown", confirmed_unit) for unit in units):
        raise ValueError(f"{label}人工單位與原文明確單位不一致，不能覆蓋原文。")
    units = tuple(confirmed_unit if unit == "unknown" else unit for unit in units)
    cm = diagnostics._cm(values, units)
    if cm is None or len(cm) != 2:
        raise ValueError(f"{label}須為兩個正有限長度；不接受三軸、片數或厚度。")
    return cm, units


def _catalog_dimensions(description, unit):
    if not isinstance(description, str):
        raise ValueError("主檔尺寸說明不是可核對文字。")
    match = diagnostics._CHAIN.fullmatch(description.strip().translate(diagnostics._FOLD))
    if match is None or match["c"] is not None:
        raise ValueError("主檔尺寸須為完整兩軸數值，不以局部數字或描述相似度猜規格。")
    units = [diagnostics._unit(match["ua"]), diagnostics._unit(match["ub"])]
    explicit = {value for value in units if value != "unknown"}
    if len(explicit) == 1:
        units = [next(iter(explicit)) if value == "unknown" else value for value in units]
    return _metric((match["a"], match["b"]), units, unit, "主檔尺寸")


@dataclass(frozen=True)
class PreparedNumericApplication:
    base_digest: str
    actor: str
    request: dict
    finding: dict
    normalization: dict
    diagnostics_issues: list
    catalog_digest: str
    proposal: dict
    before: dict
    after: dict
    content_digest: str = ""


def prepare_numeric_application(draft, request, *, actor, source_batch):
    """建立單一活動明細的固定配置差異，不修改原草稿或解除阻擋。"""
    if (not isinstance(request, dict) or set(request) != _FIELDS
            or any(not isinstance(value, str) for value in request.values())):
        raise ValueError("數值套用欄位不正確；不接受直接指定數值、成本或核准狀態。")
    reference, explanation = request["reference"], request["explanation"]
    if not reference.strip() or len(reference) > 500:
        raise ValueError("請提供 1 至 500 字的人工確認依據識別／版本。")
    _identity(draft, draft.draft_id, draft.revision, actor, explanation, source_batch)
    report = numeric_candidates(draft, source_batch, request["line_id"])
    finding = next((f for f in report["findings"] if f["finding_id"] == request["finding_id"]), None)
    if finding is None:
        raise ValueError("數值候選不屬於目標明細的直接來源，不能跨單或跨明細套用。")
    line = next(line for line in draft.lines if line.line_id == request["line_id"])
    owners = [r for r in draft.lines if finding["source_line_id"] in r.source_line_ids]
    if len(owners) != 1:
        raise ValueError("同一來源行對應多筆活動明細，須先釐清分配，不自動重複套用。")
    product = line.configuration.get("prodkind")
    if not product:
        raise ValueError("請先明確指定產品類別，不使用預設產品套用數值。")
    catalog = load_catalog(product, draft.workgroup)
    issues = [issue for issue in report["issues"] if issue["source_line_id"] == finding["source_line_id"]]
    normalization = {"diagnostics_version": diagnostics.VERSION, "application_version": VERSION}
    if request["mode"] == "product_quantity":
        if finding["kind"] != "row_quantity_candidate" or finding["scope"] != "source_row":
            raise ValueError("只有行尾來源明細數量可核對為成品數；孔數、配件數與分片數不能代入。")
        if any(request[key] for key in ("source_unit", "catalog_unit", "axis_order", "code")):
            raise ValueError("成品數量套用不得夾帶尺寸、規格或單位變更。")
        order = next(order for order in source_batch.orders if order.order_id == draft.source["order_id"])
        candidate = next((item for item in order.items if item.source_line_id == finding["source_line_id"]), None)
        if candidate is None or candidate.quantity_candidate is None:
            raise ValueError("來源數量不是有效正整數，不接受截取小數、分數或異常後綴。")
        quantity = candidate.quantity_candidate
        if line.configuration.get("qty") == quantity:
            raise ValueError("成品數量未改變，不產生重複套用。")
        proposal = {"qty": quantity}
        normalization.update(product_qty=quantity, semantic="human_confirmed_finished_product_count")
    elif request["mode"] == "table_dimensions":
        if product != "CMT1" or finding["kind"] != "table_dimensions_candidate":
            raise ValueError("本批只接受環式桌面兩軸尺寸候選；不套用孔徑、配件、假厚或分片尺寸。")
        if any(issue["code"] in _GEOMETRY_ISSUES for issue in issues):
            raise ValueError("來源有分片、幾何或尺寸矛盾，不能直接套用標準桌面尺寸。")
        if sum(f["kind"] == "table_dimensions_candidate" and f["source_line_id"] == finding["source_line_id"]
               for f in report["findings"]) != 1:
            raise ValueError("同一來源有多組桌面尺寸，須先釐清用途，不採第一組或最後一組。")
        if request["axis_order"] not in ("as_written", "swapped"):
            raise ValueError("須明確確認來源兩軸對應主檔的原順序或交換順序。")
        cm, units = _metric(finding["values"], finding["units"], request["source_unit"], "來源尺寸")
        path = r"CMT1\A001"
        matches = [option for option in catalog["options"].get(path, []) if option.get("code") == request["code"]]
        if not request["code"] or len(matches) != 1:
            raise ValueError("請明確選擇唯一合法的桌面尺寸規格，不自動替代候選。")
        option = matches[0]
        catalog_cm, catalog_units = _catalog_dimensions(option.get("codsc"), request["catalog_unit"])
        compared = cm if request["axis_order"] == "as_written" else tuple(reversed(cm))
        if tuple(map(Decimal, compared)) != tuple(map(Decimal, catalog_cm)):
            raise ValueError("來源尺寸與指定主檔規格不完全相等；不取整、不近似、不自行修正原文。")
        existing = line.configuration.get("selections", {}).get(path, {})
        if existing.get("code") == option["code"]:
            raise ValueError("桌面尺寸規格未改變，不產生重複套用。")
        proposal = {"changes": [{"op": "set", "path": path, "code": option["code"],
                                  "line_qty": existing.get("line_qty", 1)}]}
        normalization.update(centimeters=cm, confirmed_source_units=units, catalog_centimeters=catalog_cm,
                             confirmed_catalog_units=catalog_units, axis_order=request["axis_order"],
                             path=path, code=option["code"], catalog_description=option["codsc"],
                             semantic="human_confirmed_tabletop_plan_dimensions")
    else:
        raise ValueError("本批只支援成品數量與桌面兩軸尺寸，不套用材料用量或加工數。")
    updated = apply_multi_command(draft, {"op": "configure", "line_id": line.line_id, "proposal": proposal},
                                  draft_id=draft.draft_id, expected_revision=draft.revision,
                                  actor=actor, reason=explanation, source_batch=source_batch)
    prepared = PreparedNumericApplication(_base(draft), actor.strip(), deepcopy(request), deepcopy(finding), normalization,
                                          deepcopy(issues), _digest(catalog), proposal,
                                          configuration_view(draft), configuration_view(updated))
    content = asdict(prepared)
    content.pop("content_digest")
    return replace(prepared, content_digest=_digest(content))


def checked_numeric_application(draft, prepared, *, actor, source_batch):
    """檢查固定差異本身及草稿／來源／操作者，不在畫面重繪時讀主檔。"""
    if not isinstance(prepared, PreparedNumericApplication):
        raise ValueError("數值套用提案格式不正確。")
    _identity(draft, draft.draft_id, draft.revision, actor, prepared.request.get("explanation", ""), source_batch)
    content = asdict(prepared)
    content.pop("content_digest")
    if prepared.actor != actor.strip() or prepared.base_digest != _base(draft) or prepared.content_digest != _digest(content):
        raise ValueError("數值提案、草稿、來源或操作人已變更，請重新核對差異。")
    return deepcopy(prepared)


def commit_numeric_application(draft, prepared, *, actor, source_batch):
    checked_numeric_application(draft, prepared, actor=actor, source_batch=source_batch)
    # 套用前重查合法選單；主檔或自動必選變更不得偷偷替換操作者看過的差異。
    fresh = prepare_numeric_application(draft, prepared.request, actor=actor, source_batch=source_batch)
    if fresh != prepared:
        raise ValueError("主檔或數值套用差異已變更，請重新產生並核對。")
    updated = apply_multi_command(draft, {"op": "configure", "line_id": prepared.request["line_id"], "proposal": prepared.proposal},
                                  draft_id=draft.draft_id, expected_revision=draft.revision, actor=actor,
                                  reason=prepared.request["explanation"], source_batch=source_batch)
    if configuration_view(updated) != prepared.after:
        raise ValueError("套用時配置已改變，本次原子拒絕，請重新核對主檔。")
    event = DraftChange(updated.revision, "numeric_application", (prepared.request["line_id"],), actor.strip(),
                        prepared.request["explanation"].strip(), datetime.now(timezone.utc).isoformat(), {
                            "request": deepcopy(prepared.request), "finding": deepcopy(prepared.finding),
                            "normalization": deepcopy(prepared.normalization), "diagnostics_issues": deepcopy(prepared.diagnostics_issues),
                            "before": deepcopy(prepared.before), "after": deepcopy(prepared.after),
                            "catalog_digest": prepared.catalog_digest, "application_version": VERSION,
                            "formal_approval": False,
                        })
    return replace(updated, changes=(*deepcopy(draft.changes), event))
