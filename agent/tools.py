"""
agent/tools.py - 共用 Tool 介面定義

所有 Tool 均包裝 repository / engine 的功能，
供規則式解析器（rule_parser）與 LLM Function Calling 兩種模式共用。

每個 Tool 函式：
  - 接受 JSON 可序列化的參數
  - 回傳 JSON 可序列化的結果 dict
  - 不直接操作資料庫或計算金額（呼叫對應模組）

LLM Function Calling 所需的 schema 定義也在此模組。
"""

from __future__ import annotations

from typing import Any

from database import repository as repo
from engine.calculator import calculate_from_draft
from config import WORKGROUP, PRODUCT_PREFIX


# ============================================================
# Tool 函式
# ============================================================

def search_product(keyword: str, workgroup: str = WORKGROUP) -> dict:
    """
    依關鍵字搜尋產品選項。

    Args:
        keyword: 產品名稱或關鍵字，如「桌子」「辦公桌」

    Returns:
        {"results": [...], "count": int}
    """
    try:
        categories = repo.get_product_categories(workgroup=workgroup)
    except Exception:
        categories = []
    keyword_norm = keyword.strip().lower()
    matched_categories = [
        item for item in categories
        if not keyword_norm
        or keyword_norm in str(item.get("prodkind", "")).lower()
        or keyword_norm in str(item.get("codsc", "")).lower()
    ]
    # Product discovery is invdoc-first. Keep the legacy option results only
    # as an explicit fallback for installations without invdoc data.
    if categories:
        return {
            "results": matched_categories,
            "count": len(matched_categories),
            "source": "invdoc",
            "requires_selection": len(matched_categories) != 1,
        }
    results = repo.search_product(keyword=keyword, workgroup=workgroup)
    return {"results": results, "count": len(results), "source": "ordspe"}


def get_product_categories(workgroup: str = WORKGROUP) -> dict:
    """取得 invdoc ordkind=1 的可報價產品類別。"""
    results = repo.get_product_categories(workgroup=workgroup)
    return {"results": results, "count": len(results), "source": "invdoc"}


def get_product_parts(product_path: str, workgroup: str = WORKGROUP) -> dict:
    """
    取得產品路徑下的所有部件（ordqty）。

    Args:
        product_path: 產品路徑，如「DESK\\MATS」

    Returns:
        {"parts": [...], "count": int}
    """
    structure = repo.expand_ordstr_tree(product_path, workgroup=workgroup)
    if structure:
        return {
            "structure": structure,
            "parts": [node for node in structure if node.get("is_leaf")],
            "count": len(structure),
            "source": "ordstr",
        }
    parts = repo.get_product_parts(product_path=product_path, workgroup=workgroup)
    return {"parts": parts, "count": len(parts), "source": "ordqty"}


def search_option(keyword: str, workgroup: str = WORKGROUP) -> dict:
    """
    將自然語言關鍵字轉成正式選項代碼。
    例如：「美耐板」→ CLMT、「木腳」→ WLEG

    Args:
        keyword: 自然語言描述，如「美耐板」「白色」「木腳」

    Returns:
        {"results": [...], "count": int}
        每筆含 path, code, codsc, compri
    """
    results = repo.search_option(keyword=keyword, workgroup=workgroup)
    if results:
        for result in results:
            result["match_type"] = "exact"
        return {"results": results, "count": len(results), "requires_confirmation": False}

    # SQL LIKE 找不到時，改以資料庫既有選項做相似度候選；候選仍完全來自 DB，
    # 但不得在使用者確認前寫入報價草稿。
    results = repo.search_option_fuzzy(keyword=keyword, workgroup=workgroup)
    return {
        "results": results,
        "count": len(results),
        "requires_confirmation": bool(results),
        "message": "以下為最接近的資料庫規格，請確認或選擇其他選項。" if results else "資料庫找不到相近規格。",
    }


def get_options_by_path(path: str, workgroup: str = WORKGROUP) -> dict:
    """
    取得某路徑下所有可選項目（展示給使用者選擇）。

    Args:
        path: 選項路徑，如「DESK\\LEG」「DESK\\MATS」

    Returns:
        {"options": [...], "count": int}
    """
    options = repo.get_options_by_path(path=path, workgroup=workgroup)
    return {"options": options, "count": len(options)}


def get_part_quantity(
    path: str,
    code: str,
    codsc: str,
    workgroup: str = WORKGROUP,
) -> dict:
    """
    取得部件標準用量與裁切量。

    Args:
        path:  選項路徑
        code:  項目代號
        codsc: 項目名稱

    Returns:
        {"found": bool, "data": dict | None}
    """
    result = repo.get_part_quantity(path=path, code=code, codsc=codsc, workgroup=workgroup)
    return {"found": result is not None, "data": result}


def calculate_quote(quote_draft: dict) -> dict:
    """純試算不建立可確認版本；所有模式共用完整配置計價。"""
    from dataclasses import asdict
    from engine.pricing import calculate_configuration
    return asdict(calculate_configuration(quote_draft)[0])


def preview_quote(quote_draft: dict) -> dict:
    """只有本函式成功後才可進入 PREVIEW，預覽持有固定且完整的計算版本。"""
    from engine.preview import freeze_preview
    from utils.option_labels import display_text, short_part
    preview = freeze_preview(quote_draft)
    calc = preview["calc"]
    lines = ["📋 固定版本報價預覽", f"版本：{preview['revision']} / {preview['preview_id'][:8]}",
             f"產品：{quote_draft.get('product_name', quote_draft.get('prodkind'))}，數量：{quote_draft['qty']}"]
    for selection in preview["selections"].values():
        if selection.get("code"):
            label = preview["option_labels"].get(selection["path"], "產品規格")
            lines.append(f"{short_part(label)}：{display_text(selection['codsc'])}（每件 {selection['line_qty']:g}）")
    lines.extend([f"材料與工費成本：{calc['total_cost']:,.2f}",
                  f"加成後小計：{calc['subtotal']:,.2f}",
                  f"折扣：{calc['discount_amount']:,.2f}，稅額：{calc['tax_amount']:,.2f}",
                  f"報價總額：{calc['total_price']:,.2f}",
                  "確認將保存此版本，不重新套用主檔價格；修改需求需重新預覽。"])
    return {"status": "preview", "preview_id": preview["preview_id"], "calc": calc,
            "summary": "\n".join(lines)}


def create_quote(quote_draft: dict, user: str = "SYS", *, preview_id: str) -> dict:
    """UI 確認指定版本；不查規格主檔、不重新計價。相同預覽可安全重試。"""
    from copy import deepcopy
    from engine.preview import checked_preview
    from engine.calculator import CalcResult
    from engine.snapshot import create_quote_snapshot
    preview = checked_preview(quote_draft, preview_id)
    calc = CalcResult(**preview["calc"])
    frozen_draft = {"selections": preview["selections"], "qty": preview["identity"]["qty"]}
    ref_no = create_quote_snapshot(calc_result=calc, quote_draft=frozen_draft, user=user,
                                  workgroup=preview["identity"]["workgroup"], preview=preview)
    quote_draft.update(status="SNAPSHOT_CREATED", ref_no=ref_no, calc_result=deepcopy(preview["calc"]))
    return {"success": True, "ref_no": ref_no, "preview_id": preview_id,
            "total_price": calc.total_price, "calc": deepcopy(preview["calc"]),
            "message": f"已建立報價單 {ref_no}，確認版本總額 {calc.total_price:,.2f}。"}


def validate_quote_draft(quote_draft: dict) -> dict:
    """配置驗證不覆寫來源資料；計價時另驗證用量及金額。"""
    from engine.configuration import resolve_configuration, number
    errors = []
    try:
        number(quote_draft.get("qty"), "產品數量", positive=True)
        resolved = resolve_configuration(quote_draft)
        errors.extend(resolved["errors"] + resolved["missing"])
        if quote_draft.get("questions") or quote_draft.get("pending_options"):
            errors.append("尚有未確認的需求或候選規格")
    except ValueError as exc:
        errors.append(str(exc))
    return {"valid": not errors, "errors": errors}


# ============================================================
# LLM Function Calling Schema（OpenAI tools format）
# ============================================================

TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_product_categories",
            "description": "取得 invdoc 中 ordkind=1 的可報價產品類別、產品前綴與報價率",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_product",
            "description": "依關鍵字搜尋 invdoc 可報價產品類別；只有 invdoc 無資料時才回退搜尋選項",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜尋關鍵字，如「桌子」「辦公桌」",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_option",
            "description": "將自然語言轉為正式規格代碼，如「美耐板」→ CLMT、「木腳」→ WLEG",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "自然語言描述，如「美耐板」「白色」「木腳」「1200x600」",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_options_by_path",
            "description": "取得某類別路徑下所有可選項目（用於列出選項讓使用者選擇）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "選項路徑，如 DESK\\\\LEG、DESK\\\\MATS、DESK\\\\COLOR、DESK\\\\SIZE",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_part_quantity",
            "description": "取得部件的標準用量與裁切量（BOM 用量）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "選項路徑"},
                    "code": {"type": "string", "description": "項目代號"},
                    "codsc": {"type": "string", "description": "項目名稱"},
                },
                "required": ["path", "code", "codsc"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_quote",
            "description": "依目前選擇的規格計算報價金額（不寫入資料庫）",
            "parameters": {
                "type": "object",
                "properties": {
                    "quote_draft": {
                        "type": "object",
                        "description": "包含 qty、selections（size/material/color/leg）的報價草稿",
                    }
                },
                "required": ["quote_draft"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "preview_quote",
            "description": "產生格式化的報價預覽文字（試算但不建立正式報價）",
            "parameters": {
                "type": "object",
                "properties": {
                    "quote_draft": {
                        "type": "object",
                        "description": "包含 qty、selections 的報價草稿",
                    }
                },
                "required": ["quote_draft"],
            },
        },
    },
]


# ============================================================
# Tool 分派（依名稱呼叫對應函式）
# ============================================================

TOOL_REGISTRY: dict[str, Any] = {
    "get_product_categories": get_product_categories,
    "search_product": search_product,
    "get_product_parts": get_product_parts,
    "search_option": search_option,
    "get_options_by_path": get_options_by_path,
    "get_part_quantity": get_part_quantity,
    "calculate_quote": calculate_quote,
    "preview_quote": preview_quote,
}


def dispatch_tool(name: str, args: dict) -> Any:
    """
    依 tool 名稱分派呼叫。

    Args:
        name: tool 函式名稱
        args: 參數 dict

    Returns:
        tool 回傳值

    Raises:
        KeyError: 若 name 不在 TOOL_REGISTRY 中
    """
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        raise KeyError(f"未知的 Tool：{name}")
    return fn(**args)
