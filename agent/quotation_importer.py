"""貼上既有報價單的解析與草稿轉換。

貼上的成本、用量與金額只保存為來源參考；正式試算仍由資料庫與
``engine.calculator`` 重新取得。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from config import PRODUCT_PREFIX
from database import repository as repo


EXPECTED_COLUMNS = 13


@dataclass
class ImportedQuote:
    rows: list[dict[str, Any]]
    draft: dict[str, Any]
    warnings: list[str]


def _number(value: str, field: str, line_no: int) -> float:
    try:
        return float(value.replace(",", ""))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"第 {line_no} 列的 {field} 不是有效數字：{value!r}") from exc


def _split_line(line: str) -> list[str]:
    """優先使用 TSV；也支援複製後 Tab 被轉成空白的資料。"""
    if "\t" in line:
        return [part.strip() for part in line.split("\t")]

    parts = line.split()
    if len(parts) < EXPECTED_COLUMNS:
        return parts
    # 描述欄可能含空白，因此從固定的前 5 欄與後 7 欄反推描述。
    return parts[:5] + [" ".join(parts[5:-7])] + parts[-7:]


def parse_pasted_quote(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """解析 13 欄報價明細，回傳列資料與警告；格式錯誤會指出列號。"""
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        columns = _split_line(line)
        if len(columns) != EXPECTED_COLUMNS:
            errors.append(
                f"第 {line_no} 列應有 {EXPECTED_COLUMNS} 欄，實際為 {len(columns)} 欄"
            )
            continue

        workgroup, ref_no, path, line_qty, opt_code = columns[:5]
        desc = columns[5]
        stdqty, stdpar, source_compri = columns[6:9]
        adddate, addtime, addusrno, source = columns[9:13]
        if "\\" not in path:
            errors.append(f"第 {line_no} 列不是完整產品路徑：{path}")
            continue
        try:
            from engine.configuration import number
            row = {
                "line_no": line_no, "workgroup": workgroup, "ref_no": ref_no,
                "path": path, "opt_code": opt_code, "spec_desc": desc,
                "qty": number(line_qty, "明細數量", positive=True),
                "stdqty": number(stdqty, "用料量"),
                "stdpar": number(stdpar, "裁切量", positive=True),
                "source_compri": number(source_compri, "來源採購單價"),
                "adddate": adddate, "addtime": addtime, "addusrno": addusrno, "source": source,
            }
        except ValueError as exc:
            errors.append(f"第 {line_no} 列：{exc}")
            continue
        rows.append(row)

    if errors:
        raise ValueError("貼上報價單格式錯誤：\n" + "\n".join(errors))
    if not rows:
        raise ValueError("沒有找到可匯入的報價明細。")

    refs = {row["ref_no"] for row in rows}
    if len(refs) != 1:
        raise ValueError(f"貼上資料必須只有一張報價單，目前包含：{', '.join(sorted(refs))}")
    groups = {row["workgroup"] for row in rows}
    if len(groups) != 1:
        raise ValueError("貼上資料的 workgroup 必須一致。")
    return rows, warnings


def build_imported_draft(rows: list[dict[str, Any]], warnings: list[str], *, product_qty=None) -> dict[str, Any]:
    """明細數量只作每件用量；重報產品數量由操作者指定。來源列不被改寫。"""
    from agent.state import new_quote_draft
    from engine.configuration import number
    from config import WORKGROUP
    draft = new_quote_draft()
    products = {r["path"].split("\\", 1)[0] for r in rows}
    if len(products) != 1:
        raise ValueError("一次只能重報一個產品配置")
    if rows[0]["workgroup"] != WORKGROUP:
        raise ValueError("来源事業別與目前事業別不一致")
    draft["prodkind"] = next(iter(products))
    draft["qty"] = number(product_qty, "重報產品數量", positive=True) if product_qty is not None else None
    draft["imported_quote"] = {
        "source_ref_no": rows[0]["ref_no"], "source_workgroup": rows[0]["workgroup"],
        "source_rows": rows, "warnings": list(warnings),
    }
    category = repo.get_product_category(draft["prodkind"], workgroup=WORKGROUP)
    if category:
        draft["product_name"] = category.get("codsc") or draft["prodkind"]
    selections = {}
    for row in rows:
        path = row["path"]
        if path in selections:
            raise ValueError(f"同一路徑有多筆來源明細，請先確認合併方式：{path}")
        selections[path] = {
            "path": path, "optno": path.rsplit("\\", 1)[-1], "code": row["opt_code"],
            "codsc": row["spec_desc"], "line_qty": row["qty"],
            "source_stdqty": row["stdqty"], "source_stdpar": row["stdpar"],
            "source_compri": row["source_compri"],
        }
        options = repo.get_options_by_path(path, workgroup=WORKGROUP)
        match = next((o for o in options if o["code"] == row["opt_code"]), None)
        if row["opt_code"] and (match is None or match.get("codsc") != row["spec_desc"]):
            draft["imported_quote"]["warnings"].append(f"來源規格與現行主檔不一致，請確認：{path} / {row['opt_code']}")
    draft["selections"] = selections
    draft["status"] = "CHECKING"
    if draft["imported_quote"]["warnings"]:
        draft["questions"] = ["請確認來源規格差異後再重報"]
    return draft


def import_pasted_quote(text: str, *, product_qty=None) -> ImportedQuote:
    rows, warnings = parse_pasted_quote(text)
    draft = build_imported_draft(rows, warnings, product_qty=product_qty)
    return ImportedQuote(rows=rows, draft=draft, warnings=draft["imported_quote"]["warnings"])


def restore_historical_matches(user_input: str, quote_draft: dict[str, Any]) -> list[str]:
    """舊呼叫相容：來源只供參考，不得重新套回而覆蓋本回合變更。"""
    return []


def quotation_to_natural_language(rows: list[dict[str, Any]]) -> str:
    """不從材料用量猜產品數量，也不把美耐皿改述成美耐板。"""
    return "來源選配（產品訂購數量另行指定）：\n" + "\n".join(
        f"{r['path']} {r['opt_code']} {r['spec_desc']}，每件項目數量 {r['qty']:g}"
        for r in rows if r.get("opt_code")
    )
