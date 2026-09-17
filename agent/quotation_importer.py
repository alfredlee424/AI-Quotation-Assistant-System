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

        workgroup, ref_no, path, status, opt_code = columns[:5]
        desc = columns[5]
        qty, stdqty, source_compri = columns[6:9]
        adddate, addtime, addusrno, source = columns[9:13]

        if not path.startswith(f"{PRODUCT_PREFIX}\\"):
            errors.append(f"第 {line_no} 列 path 不符合產品前綴：{path}")
            continue
        if not re.match(r"^\d{4}[./-]\d{1,2}[./-]\d{1,2}$", adddate):
            warnings.append(f"第 {line_no} 列日期格式非標準：{adddate}")

        try:
            row = {
                "line_no": line_no,
                "workgroup": workgroup,
                "ref_no": ref_no,
                "path": path,
                "status": status,
                "opt_code": opt_code,
                "spec_desc": desc,
                "qty": _number(qty, "qty", line_no),
                "stdqty": _number(stdqty, "stdqty", line_no),
                # 第 9 欄是來源採購成本（你的完整報價單格式沒有 stdpar）。
                "source_compri": _number(source_compri, "compri", line_no),
                "adddate": adddate,
                "addtime": addtime,
                "addusrno": addusrno,
                "source": source,
            }
        except ValueError as exc:
            errors.append(str(exc))
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


def build_imported_draft(rows: list[dict[str, Any]], warnings: list[str]) -> dict[str, Any]:
    """將來源列轉成現有 Agent/Quote Engine 可使用的草稿。"""
    from agent.state import new_quote_draft

    draft = new_quote_draft()
    draft["qty"] = rows[0]["qty"] if rows else None
    draft["ref_no"] = rows[0]["ref_no"]
    draft["imported_quote"] = {
        "source_ref_no": rows[0]["ref_no"],
        "source_workgroup": rows[0]["workgroup"],
        "source_rows": rows,
        "warnings": list(warnings),
    }

    selections: dict[str, dict[str, Any]] = {}
    try:
        categories = repo.get_all_option_categories()
        category_map = {item["optno"]: item["optdesc"] for item in categories}
    except Exception:
        category_map = {}
    for row in rows:
        if not row["opt_code"]:
            continue
        relative_path = row["path"].removeprefix(f"{PRODUCT_PREFIX}\\")
        option_path = row["path"]
        matches = repo.get_options_by_path(option_path)
        db_option = next((item for item in matches if item["code"] == row["opt_code"]), None)
        if db_option is None:
            draft["imported_quote"]["warnings"].append(
                f"無法在資料庫對應 {option_path} / {row['opt_code']}，僅保留來源資料。"
            )
            continue

        optno = relative_path.split("\\")[-1]
        selections[relative_path] = {
            "optno": optno,
            "optdesc": category_map.get(optno, optno),
            "path": option_path,
            "code": db_option["code"],
            "codsc": db_option.get("codsc") or row["spec_desc"],
            "compri": db_option.get("compri", 0.0),
            "source_compri": row["source_compri"],
            "source_qty": row["qty"],
            "source_stdqty": row["stdqty"],
        }

    draft["selections"] = selections
    draft["status"] = "PREVIEW"
    return draft


def import_pasted_quote(text: str) -> ImportedQuote:
    rows, warnings = parse_pasted_quote(text)
    return ImportedQuote(
        rows=rows,
        draft=build_imported_draft(rows, warnings),
        warnings=warnings,
    )


def restore_historical_matches(user_input: str, quote_draft: dict[str, Any]) -> list[str]:
    """匯入歷史報價後，將自然語句中的既有規格綁回原始 path/code。

    避免 LLM 用全資料庫模糊搜尋，把「817胡桃木」或「單掀」誤配到
    其他產品樹；只有使用者輸入新規格時，才讓一般解析器重新查詢。
    """
    imported = quote_draft.get("imported_quote")
    if not imported:
        return []
    text = str(user_input or "").replace("×", "*").replace("＊", "*")
    compact = "".join(text.split()).lower()
    qty_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:張|個|件|台|套)", text)
    if qty_match:
        quote_draft["qty"] = float(qty_match.group(1))
    elif imported.get("source_rows"):
        quote_draft["qty"] = imported["source_rows"][0].get("qty")
    explicit_change = bool(re.search(r"改成|改為|更換|換成|變更", text))
    changed_roots: set[str] = set()
    if re.search(r"尺寸|桌面|大小", text) and explicit_change:
        changed_roots.add("A001")
    if re.search(r"桌板|板材|材質|色紙|胡桃|美耐", text) and explicit_change:
        changed_roots.add("A001")
    if re.search(r"腳架|桌腳|木腳|鐵腳|腳型", text) and explicit_change:
        changed_roots.update({"B001", "B002"})

    source_paths = {
        row["path"]
        for row in imported.get("source_rows", [])
        if row.get("opt_code") and row.get("path")
    }
    # 沒有指定「改哪個規格」時，匯入報價就是完整的歷史模板；
    # 清掉自然語言解析器產生的孤立 key，避免正式快照只剩一兩筆。
    if not explicit_change:
        quote_draft["selections"] = {
            key: value
            for key, value in quote_draft.get("selections", {}).items()
            if value.get("path") in source_paths
        }
    elif changed_roots:
        quote_draft["selections"] = {
            key: value
            for key, value in quote_draft.get("selections", {}).items()
            if value.get("path", "").split("\\")[1:2][0:1]
            and value.get("path", "").split("\\")[1] not in changed_roots
        }
    restored_paths: list[str] = []
    for row in imported.get("source_rows", []):
        code = str(row.get("opt_code") or "").strip()
        desc = str(row.get("spec_desc") or "").strip()
        path = str(row.get("path") or "").strip()
        if not code or not desc or not path:
            continue
        desc_compact = "".join(desc.replace("×", "*").split()).lower()
        root = path.split("\\")[1] if len(path.split("\\")) > 1 else ""
        matched = not explicit_change or root not in changed_roots or desc_compact in compact
        if not matched and "胡桃" in compact and "胡桃" in desc and (
            "美耐" in compact or "木" in compact or "色" in compact
        ):
            matched = True
        if not matched and "單掀" in compact and "單掀" in desc:
            matched = True
        if not matched and "置中" in compact and "置中" in desc:
            matched = True
        if not matched:
            continue

        current = next(
            (item for item in repo.get_options_by_path(path) if item["code"] == code),
            None,
        )
        if current is None:
            continue
        relative_path = path.removeprefix(f"{PRODUCT_PREFIX}\\")
        optno = relative_path.split("\\")[-1]
        quote_draft.setdefault("selections", {})[relative_path] = {
            "optno": optno,
            "optdesc": optno,
            "path": path,
            "code": code,
            "codsc": current.get("codsc") or desc,
            "compri": current.get("compri", 0.0),
            "source_compri": row.get("source_compri"),
            "source_qty": row.get("qty"),
            "source_stdqty": row.get("stdqty"),
        }
        restored_paths.append(path)

    if restored_paths and quote_draft.get("pending_options"):
        quote_draft["pending_options"] = [
            item for item in quote_draft["pending_options"]
            if item.get("path") not in restored_paths
        ]
        if not quote_draft["pending_options"]:
            quote_draft.pop("pending_options", None)
    return restored_paths


def quotation_to_natural_language(rows: list[dict[str, Any]]) -> str:
    """將報價明細轉成可供業務閱讀或交給 LLM 的繁體中文需求描述。

    結構節點（沒有代碼的列）與成本欄位不放入句子，避免把內部 BOM
    用量或採購成本誤當成客戶需求；有代碼的規格則依產品樹分組保留。
    """
    if not rows:
        return "目前沒有可轉換的報價明細。"

    qty = rows[0].get("qty", 1)
    qty_text = f"{qty:g}" if isinstance(qty, float) and qty.is_integer() else str(qty)
    descriptions = [
        str(row.get("spec_desc") or "").strip()
        for row in rows
        if row.get("opt_code") and row.get("spec_desc")
    ]
    paths = {str(row.get("path") or ""): str(row.get("spec_desc") or "").strip() for row in rows}

    # 只抽取客戶會說的規格；完整 BOM 明細仍保留在 imported_quote.source_rows。
    size = paths.get(f"{PRODUCT_PREFIX}\\A001", "")
    leg = paths.get(f"{PRODUCT_PREFIX}\\B001", "")
    has_walnut = any("胡桃" in value for value in descriptions)
    has_mdf = any("MDF" in value.upper() for value in descriptions)
    material = "胡桃木美耐版" if has_walnut and has_mdf else ("胡桃木" if has_walnut else "")
    operation = "單掀" if any("單掀" in value for value in descriptions) else ""
    position = "置中" if "置中" in descriptions else ""

    details = [value for value in (size, material, operation, position, leg) if value]
    suffix = " ".join(details) if details else "沿用歷史報價規格"
    return f"需要 {qty_text} 張辦公桌 {suffix}"
