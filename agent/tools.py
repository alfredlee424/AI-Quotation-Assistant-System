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
    """
    依報價草稿計算成本與售價（不寫入資料庫）。

    Args:
        quote_draft: Agent 維護的報價草稿 dict

    Returns:
        {"total_cost": float, "total_price": float, "items": [...], ...}
    """
    validation = validate_quote_draft(quote_draft)
    if not validation["valid"]:
        raise ValueError("報價規格驗證失敗：" + "；".join(validation["errors"]))

    prodkind = str(quote_draft.get("prodkind", "")).strip()
    if prodkind and repo.expand_ordstr_tree(prodkind):
        from engine.calculator import calculate_from_ordstr
        calc = calculate_from_ordstr(
            prodkind,
            quote_draft.get("selections", {}),
            qty=float(quote_draft.get("qty", 1)),
            discount_rate=float(quote_draft.get("discount_rate", 0.0)),
        )
    else:
        calc = calculate_from_draft(quote_draft)
    return {
        "total_cost": calc.total_cost,
        "subtotal": calc.subtotal,
        "discount_rate": calc.discount_rate,
        "discount_amount": calc.discount_amount,
        "after_discount": calc.after_discount,
        "tax_rate": calc.tax_rate,
        "tax_amount": calc.tax_amount,
        "total_price": calc.total_price,
        "markup_rate": calc.markup_rate,
        "items": calc.items,
    }


def preview_quote(quote_draft: dict) -> dict:
    """
    產生報價預覽（試算但不建立正式報價單）。
    selections 以 optno 為 key，動態組合摘要文字。

    Returns:
        {"status": "preview", "calc": {...}, "summary": str}
    """
    calc_result = calculate_quote(quote_draft)
    qty = quote_draft.get("qty", 0)
    selections = quote_draft.get("selections", {})

    # 動態組合已選規格說明（optdesc: codsc）
    sel_lines = []
    for optno, sel in selections.items():
        optdesc = sel.get("optdesc", optno)
        codsc = sel.get("codsc", "未指定")
        sel_lines.append(f"  {optdesc}：{codsc}")

    sel_text = "\n".join(sel_lines) if sel_lines else "  （尚未選擇規格）"

    summary = (
        f"📋 報價預覽\n"
        f"  產品：{quote_draft.get('product_name', '辦公桌')}  數量：{qty} 張\n"
        f"{sel_text}\n"
        f"  ─────────────────────────\n"
        f"  材料成本：${calc_result['total_cost']:,.0f}\n"
        f"  小計（含加成）：${calc_result['subtotal']:,.0f}\n"
        f"  折扣金額：-${calc_result['discount_amount']:,.0f}\n"
        f"  稅額：${calc_result['tax_amount']:,.0f}\n"
        f"  ─────────────────────────\n"
        f"  ✅ 建議報價總金額：${calc_result['total_price']:,.0f}"
    )

    return {"status": "preview", "calc": calc_result, "summary": summary}


def create_quote(quote_draft: dict, user: str = "SYS") -> dict:
    """
    使用者確認後建立正式報價（寫入 ordqdt_ai 快照）。
    只有在使用者明確確認後才呼叫此函式。

    Args:
        quote_draft: 已完整填寫的報價草稿
        user:        建立人員代號

    Returns:
        {"success": bool, "ref_no": str, "message": str}
    """
    from engine.calculator import calculate_from_draft
    from engine.snapshot import create_quote_snapshot

    validation = validate_quote_draft(quote_draft)
    if not validation["valid"]:
        raise ValueError("報價規格驗證失敗：" + "；".join(validation["errors"]))

    prodkind = str(quote_draft.get("prodkind", "")).strip()
    from engine.calculator import calculate_from_ordstr
    if prodkind and repo.expand_ordstr_tree(prodkind):
        calc = calculate_from_ordstr(
            prodkind,
            quote_draft.get("selections", {}),
            qty=float(quote_draft.get("qty", 1)),
            discount_rate=float(quote_draft.get("discount_rate", 0.0)),
        )
    else:
        calc = calculate_from_draft(quote_draft)
    ref_no = create_quote_snapshot(calc_result=calc, quote_draft=quote_draft, user=user)

    return {
        "success": True,
        "ref_no": ref_no,
        "total_price": calc.total_price,
        "message": f"已成功建立報價單 {ref_no}，報價金額 ${calc.total_price:,.0f} 元。",
    }


def validate_quote_draft(quote_draft: dict) -> dict:
    """驗證草稿中的每個規格是否仍存在於資料庫白名單。

    必選項目檢查以 ordstr.must_chose 為權威來源（透過
    engine.calculator.check_required_selections），
    當 ordstr 查無資料時 fallback 回 config.REQUIRED_OPTNOS。
    """
    from config import REQUIRED_OPTNOS

    errors: list[str] = []
    selections = quote_draft.get("selections", {})

    if not quote_draft.get("qty"):
        errors.append("缺少數量")

    # ── 必選項目檢查：優先採用 ordstr.must_chose（權威來源） ──
    prodkind = str(quote_draft.get("prodkind", PRODUCT_PREFIX)).strip() or PRODUCT_PREFIX
    required_nodes = repo.get_required_nodes(prodkind)

    if required_nodes:
        # ordstr 有必選節點定義：以結構樹為準
        from engine.calculator import check_required_selections
        missing_nodes = check_required_selections(prodkind, selections)
        for node in missing_nodes:
            desc = node.get("dmark") or node.get("optnoc") or node.get("pathc")
            errors.append(f"缺少必選項目：{node.get('pathc')}（{desc}）")
    else:
        # fallback：ordstr 無資料時沿用 REQUIRED_OPTNOS
        missing_optnos = [optno for optno in REQUIRED_OPTNOS if optno not in selections]
        if missing_optnos:
            errors.append("缺少必要規格：" + ", ".join(missing_optnos))

    for key, selection in selections.items():
        path = str(selection.get("path", "")).strip()
        code = str(selection.get("code", "")).strip()
        optno = str(selection.get("optno", "")).strip()
        if not path or not code:
            errors.append(f"{key} 缺少 path 或 code")
            continue

        prefix = prodkind or PRODUCT_PREFIX
        if not path.startswith(f"{prefix}\\"):
            errors.append(f"{path} 不屬於目前產品 {prefix}")
            continue

        # 子部件的 selection 會以自身 optno 為識別（例如
        # CMT1\A001\S004 → S004），不能只取產品路徑下的第一段 A001。
        path_parts = path.removeprefix(f"{prefix}\\").split("\\")
        path_optno = path_parts[-1] if path_parts else ""
        if optno and path_optno and optno != path_optno:
            errors.append(f"{path} 與 optno {optno} 不一致")
            continue

        options = repo.get_options_by_path(path)
        matched = next((item for item in options if item["code"] == code), None)
        if matched is None:
            errors.append(f"{path} 不存在規格代碼 {code}")
            continue

        # 以資料庫內容覆寫可能被 LLM 修改的描述與成本。
        selection["codsc"] = matched["codsc"]
        selection["compri"] = matched["compri"]

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
    "create_quote": create_quote,
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
