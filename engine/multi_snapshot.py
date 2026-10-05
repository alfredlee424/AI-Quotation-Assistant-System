"""多明細快照關聯演練：只使用固定內部試算，不查價、不產生單號或保存。"""
from copy import deepcopy
from decimal import Decimal

from database.models import ordqdt_ai
from database.multi_snapshot_schema import SCHEMA_VERSION, proposed_schema
from engine.configuration import number
from engine.multi_trial import checked_internal_trial, _digest


LAYOUT_VERSION = "multi-snapshot-layout-v1"
MAX_SNAPSHOT_ROWS = 99999
_TYPE = "multi_quote_snapshot_layout_only"


def snapshot_sequence(index):
    if type(index) is not int or not 1 <= index <= MAX_SNAPSHOT_ROWS:
        raise ValueError("全單快照列序號須介於 00001 至 99999，不得截斷或循環重用。")
    return f"{index:05d}"


def _capacity_issues(values, table, *, line_id, seq_no=""):
    issues = []
    for column in table.columns:
        if column.name not in values:
            continue
        value = values[column.name]
        limit = getattr(column.type, "length", None)
        if limit is not None and (not isinstance(value, str) or len(value) > limit):
            issues.append({"line_id": line_id, "seq_no": seq_no, "table": table.name,
                           "field": column.name, "limit": limit,
                           "message": f"{table.name}.{column.name} 超出 {limit} 字或不是文字；不得截斷。"})
    return issues


def _assemble(draft, trial, actor):
    if not isinstance(trial.get("lines"), list) or [r.get("line_id") for r in trial["lines"]] != [r.line_id for r in draft.lines]:
        raise ValueError("試算明細識別、範圍或順序與草稿不一致。")
    _, product_table, _ = proposed_schema()
    products, rows, excluded, storage_issues = [], [], [], []
    for position, (line, frozen) in enumerate(zip(draft.lines, trial["lines"]), 1):
        if (frozen.get("line_revision"), frozen.get("prodkind"), frozen.get("qty"), frozen.get("label")) != (
                line.revision, line.configuration.get("prodkind"), line.configuration.get("qty"), line.label):
            raise ValueError("試算產品配置識別與草稿不一致。")
        product = {"workgroup": draft.workgroup, "line_id": line.line_id, "display_order": position,
                   "line_revision": line.revision, "prodkind": frozen["prodkind"], "label": frozen["label"],
                   "product_qty": frozen["qty"], "trial_status": frozen["status"], "row_sequences": [], "pricing": None}
        products.append(product)
        if frozen["status"] == "BLOCKED" and frozen["calculation"] is None:
            excluded.append({"line_id": line.line_id, "issues": deepcopy(frozen["issues"])})
            continue
        if frozen["status"] != "CALCULATED" or frozen["issues"] or not isinstance(frozen["calculation"], dict):
            raise ValueError("試算明細狀態與計算內容不一致。")
        calc = frozen["calculation"]
        selections, items = calc.get("selections"), calc.get("items")
        if not isinstance(selections, dict) or not selections or not isinstance(items, list) or not items:
            raise ValueError("已試算明細須包含固定選配與完整成本列。")
        product["pricing"] = {key: deepcopy(calc[key]) for key in (
            "quote_rate", "total_cost", "subtotal", "raw_total_cost", "raw_subtotal")}
        storage_issues.extend(_capacity_issues(product, product_table, line_id=line.line_id))
        quantity = number(frozen["qty"], "成品數量", positive=True)
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("成本列格式不正確。")
            path = item.get("path")
            if not isinstance(path, str) or path not in selections or path in seen or item.get("line_id") != line.line_id:
                raise ValueError("成本列含重複、越界或未對應的產品明細／部位。")
            selected = selections[path]
            if not isinstance(selected, dict) or selected.get("path") != path or selected.get("code") != item.get("spc_code"):
                raise ValueError("固定選配與成本列的部位／規格代碼不一致。")
            seen.add(path)
            per_item = number(item.get("line_qty"), "每件項目數量", positive=True)
            if (per_item != number(selected.get("line_qty"), "選配項目數量", positive=True)
                    or number(item.get("product_qty"), "成本成品數量", positive=True) != quantity
                    or number(item.get("qty"), "成本實際數量", positive=True) != quantity * per_item):
                raise ValueError("成品數量、每件項目數量與實際數量不一致。")
            for key in ("stdqty", "compri", "part_cost", "unit_cost", "unit_price", "amount"):
                number(item.get(key), f"固定成本列 {key}")
            number(item.get("stdpar"), "固定裁切量", positive=True)
            if number(selected.get("compri"), "固定選配單價") != item["compri"]:
                raise ValueError("固定選配單價與成本列不一致。")
            if item.get("source") not in ("ordqty", "per_item_policy", "zero_cost"):
                raise ValueError("成本列缺少已辨識的用量來源。")
            if item["source"] == "ordqty":
                driver = selections.get(item.get("driver_path"))
                if not driver or driver.get("code") != item.get("driver_code"):
                    raise ValueError("用量驅動部位未對應本產品明細的固定選配。")
            seq = snapshot_sequence(len(rows) + 1)
            # 不走旧單品的 path 去重或補值；全部欄位從同一明細的固定列取得。
            values = {name: deepcopy(item.get(source)) for name, source in {
                "part_code": "part_code", "part_desc": "part_desc", "path": "path",
                "opt_code": "optno", "opt_desc": "optdesc", "spc_code": "spc_code", "spdsc": "spdsc",
                "qty": "qty", "stdqty": "stdqty", "stdpar": "stdpar", "compri": "compri",
                "unit_price": "unit_price", "amount": "amount", "unit": "unit",
            }.items()}
            # 特意不提供 ref_no、確認狀態及建立人代碼，不能交給既有保存介面。
            rows.append({"seq_no": seq, "line_id": line.line_id, "path": path, "snapshot_values": values,
                         "cost_evidence": deepcopy(item)})
            product["row_sequences"].append(seq)
            storage_issues.extend(_capacity_issues(values, ordqdt_ai.__table__, line_id=line.line_id, seq_no=seq))
        if seen != set(selections):
            raise ValueError("固定選配有未對應成本列，不能略過零價或結構節點。")
    # 人工操作人最長 80 字；舊明細欄位只有 8 字，必須揭露而不是默默截短。
    storage_issues.extend(_capacity_issues({"addusrno": actor.strip()}, ordqdt_ai.__table__, line_id=""))
    relation_rows = [{"workgroup": draft.workgroup, "seq_no": row["seq_no"], "line_id": row["line_id"],
                      "path": row["path"]} for row in rows]
    difference = None
    if trial["totals"] is not None:
        difference = float(sum((Decimal(str(r["snapshot_values"]["amount"])) for r in rows), Decimal(0))
                           - Decimal(str(trial["totals"]["subtotal"])))
    document = {
        "document_type": _TYPE, "schema_version": 1, "layout_version": LAYOUT_VERSION,
        "storage_schema_version": SCHEMA_VERSION, "can_quote": False, "can_confirm": False, "can_save": False,
        "draft_id": draft.draft_id, "draft_revision": draft.revision, "draft_digest": trial["draft_digest"],
        "trial_id": trial["trial_id"], "trial_digest": trial["content_digest"], "actor": actor.strip(),
        "workgroup": draft.workgroup, "products": products, "rows": rows, "row_links": relation_rows,
        "excluded_lines": excluded, "archived_line_ids": deepcopy(trial["archived_line_ids"]),
        "storage_issues": storage_issues, "row_count": len(rows), "sequence_limit": MAX_SNAPSHOT_ROWS,
        "layout_complete": bool(rows) and not excluded and not trial["archived_line_ids"] and not storage_issues,
        "totals": deepcopy(trial["totals"]), "display_row_amount_difference": difference,
        "requirements": deepcopy(trial["requirements"]), "conditions": deepcopy(trial["conditions"]),
        "drawings": deepcopy(trial["drawings"]), "order_blockers": deepcopy(trial["order_blockers"]),
        "warning": "僅為多明細快照關聯演練，不是正式預覽或可保存文件；未核准關卡仍保留。",
    }
    document["content_digest"] = _digest(document)
    return document


def build_snapshot_layout(draft, trial, *, actor, source_batch=None):
    frozen = checked_internal_trial(draft, trial, actor=actor, source_batch=source_batch)
    try:
        return _assemble(draft, frozen, actor)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("固定內部試算缺少快照關聯必要欄位或格式不正確。") from exc


def checked_snapshot_layout(draft, trial, layout, *, actor, source_batch=None):
    """重建純關聯核對，不重新讀主檔或計價；任何內容差異都拒絕下載舊版。"""
    expected = build_snapshot_layout(draft, trial, actor=actor, source_batch=source_batch)
    if not isinstance(layout, dict) or layout != expected:
        raise ValueError("快照關聯演練與目前草稿、試算或文件內容不一致，請重新產生。")
    return deepcopy(layout)
