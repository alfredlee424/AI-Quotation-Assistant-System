"""
database/repository.py - 資料庫查詢層（Repository Pattern）

所有資料庫存取都集中在此模組。
UI、AI Agent 與計價引擎均不得直接執行 SQL，只能呼叫此模組的函式。

提供以下查詢函式：
  - search_product()        : 依關鍵字搜尋產品（路徑）
  - get_product_parts()     : 取得產品所有部件路徑
  - search_option()         : 依自然語言關鍵字搜尋選項代碼
  - get_options_by_path()   : 取得某路徑下所有可選項目
  - get_part_quantity()     : 取得部件標準用量與裁切量
  - get_option_price()      : 取得選項採購成本（用於計價）
  - save_quote_snapshot()   : 將報價快照寫入 ordqdt_ai
  - get_quote_snapshot()    : 讀取已建立的報價快照
  - build_option_path()     : 由 optno 組合 ordspe 路徑
  - optno_from_path()       : 由 path 解出 optno

路徑格式說明：
  真實資料庫的 ordspe.path = {PRODUCT_PREFIX}\\{optno}
  例：CMT1\\A001（桌面尺寸）、CMT1\\B001（木腳尺寸）

查詢 Bug 修正記錄（2026-08-27）：
  ordqty.codsc 在真實資料庫全部為 NULL；
  SQL NULL = 'xxx' 永遠為假，導致 get_part_quantity 永遠查不到。
  修正：(workgroup, path, code) 三欄比對，移除 codsc 條件。
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import or_

from database.connection import get_db
from database.models import Ordspd, Ordspe, Ordqty, ordqdt_ai
from config import WORKGROUP, PRODUCT_PREFIX


# ============================================================
# 共用：取得 Session
# ============================================================

def _session() -> Session:
    return get_db()


# ============================================================
# 路徑輔助函式
# ============================================================

def build_option_path(optno: str, prefix: str = PRODUCT_PREFIX) -> str:
    """
    由 optno 組合 ordspe / ordqty 的 path。

    Args:
        optno  : ordspd.optno，例如 "A001"
        prefix : 產品路徑前綴，預設 PRODUCT_PREFIX（如 "CMT1"）

    Returns:
        str  例如 "CMT1\\A001"
    """
    return f"{prefix}\\{optno}"


def optno_from_path(path: str, prefix: str = PRODUCT_PREFIX) -> str:
    """
    由 ordspe.path 解出 ordspd.optno。

    路徑格式為 "{prefix}\\{optno}[\\{子optno}...]"，
    取 prefix 之後、第一個反斜線之前的段落作為 optno。

    Args:
        path   : 例如 "CMT1\\A001" 或 "CMT1\\A001\\W001"
        prefix : 產品路徑前綴，預設 PRODUCT_PREFIX

    Returns:
        str optno，例如 "A001"；若格式不符則回傳空字串
    """
    stripped = path.removeprefix(prefix).lstrip("\\")
    return stripped.split("\\")[0] if stripped else ""


# ============================================================
# 1. search_product - 依關鍵字搜尋產品
# ============================================================

def search_product(keyword: str, workgroup: str = WORKGROUP) -> list[dict]:
    """
    依產品名稱或代碼關鍵字搜尋可選項目。
    實際上搜尋 ordspe.codsc 與 ordspe.code 包含關鍵字的資料。

    Returns:
        list[dict] 每筆含 path, code, codsc, compri, optno
    """
    db = _session()
    try:
        kw = f"%{keyword}%"
        rows = (
            db.query(Ordspe)
            .filter(
                Ordspe.workgroup == workgroup,
                or_(
                    Ordspe.codsc.like(kw),
                    Ordspe.code.like(kw),
                ),
            )
            .limit(20)
            .all()
        )
        return [
            {
                "path": (r.path or "").strip(),
                "code": (r.code or "").strip(),
                "codsc": (r.codsc or "").strip(),
                "compri": r.compri or 0.0,
                "optno": optno_from_path((r.path or "").strip()),
            }
            for r in rows
        ]
    finally:
        db.close()


# ============================================================
# 2. get_product_parts - 取得產品所有部件路徑
# ============================================================

def get_product_parts(product_path: str, workgroup: str = WORKGROUP) -> list[dict]:
    """
    依產品路徑取得該產品在 ordqty 中定義的所有部件。

    Args:
        product_path: 產品的根路徑，例如 "CMT1\\A001"

    Returns:
        list[dict] 每筆含 path, code, codsc, part_path, stdqty, stdpar
    """
    db = _session()
    try:
        rows = (
            db.query(Ordqty)
            .filter(
                Ordqty.workgroup == workgroup,
                Ordqty.path.like(f"{product_path}%"),
            )
            .all()
        )
        return [
            {
                "path": (r.path or "").strip(),
                "code": (r.code or "").strip(),
                "codsc": (r.codsc or "").strip() if r.codsc else None,
                "part_path": (r.part_path or "").strip() if r.part_path else None,
                "stdqty": r.stdqty or 1.0,
                "stdpar": r.stdpar or 1.0,
            }
            for r in rows
        ]
    finally:
        db.close()


# ============================================================
# 3. search_option - 將自然語言關鍵字轉成正式選項代碼
# ============================================================

def search_option(keyword: str, workgroup: str = WORKGROUP) -> list[dict]:
    """
    搜尋 ordspe.codsc 與 ordspe.code 包含關鍵字的所有選項。
    用於將自然語言轉成正式代碼，例如「木腳」→ code=C001 path=CMT1\\B001。

    Returns:
        list[dict] 每筆含 path, code, codsc, compri, optno
    """
    db = _session()
    try:
        kw = f"%{keyword}%"
        rows = (
            db.query(Ordspe)
            .filter(
                Ordspe.workgroup == workgroup,
                or_(
                    Ordspe.codsc.like(kw),
                    Ordspe.code.like(kw),
                ),
            )
            .limit(10)
            .all()
        )
        return [
            {
                "path": (r.path or "").strip(),
                "code": (r.code or "").strip(),
                "codsc": (r.codsc or "").strip(),
                "compri": r.compri or 0.0,
                "optno": optno_from_path((r.path or "").strip()),
            }
            for r in rows
        ]
    finally:
        db.close()


# ============================================================
# 4. get_options_by_path - 取得某路徑下所有可選項目
# ============================================================

def get_options_by_path(path: str, workgroup: str = WORKGROUP) -> list[dict]:
    """
    取得 ordspe 中某路徑下的所有可選項目。

    Args:
        path: 例如 "CMT1\\A001"（桌面尺寸）

    Returns:
        list[dict] 每筆含 code, codsc, compri
    """
    db = _session()
    try:
        rows = (
            db.query(Ordspe)
            .filter(
                Ordspe.workgroup == workgroup,
                Ordspe.path == path,
            )
            .order_by(Ordspe.code)
            .all()
        )
        return [
            {
                "code": (r.code or "").strip(),
                "codsc": (r.codsc or "").strip(),
                "compri": r.compri or 0.0,
            }
            for r in rows
        ]
    finally:
        db.close()


# ============================================================
# 5. get_part_quantity - 取得部件標準用量
#
# 修正 Bug（2026-08-27）：
#   原版查詢包含 codsc 條件，但真實 ordqty.codsc 全為 NULL，
#   SQL NULL = 'xxx' 永遠為假，導致永遠查不到。
#   改為只用 (workgroup, path, code) 三欄比對。
# ============================================================

def get_part_quantity(
    path: str,
    code: str,
    codsc: str = "",          # 保留參數相容舊呼叫，但不進 WHERE
    workgroup: str = WORKGROUP,
) -> Optional[dict]:
    """
    依 (workgroup, path, code) 取得 ordqty 中的用量規則。

    注意：codsc 參數保留供呼叫端傳入，但不作為查詢條件，
    因為真實資料庫 ordqty.codsc 欄位全部為 NULL。

    Returns:
        dict 含 stdqty, stdpar, part_path；若不存在則回傳 None
    """
    db = _session()
    try:
        row = (
            db.query(Ordqty)
            .filter(
                Ordqty.workgroup == workgroup,
                Ordqty.path == path,
                Ordqty.code == code,
                # codsc 不進 WHERE（真實資料全為 NULL）
            )
            .first()
        )
        if row is None:
            return None
        return {
            "path": (row.path or "").strip(),
            "code": (row.code or "").strip(),
            "codsc": (row.codsc or "").strip() if row.codsc else None,
            "part_path": (row.part_path or "").strip() if row.part_path else None,
            "stdqty": row.stdqty or 1.0,
            "stdpar": row.stdpar or 1.0,
        }
    finally:
        db.close()


# ============================================================
# 6. get_option_price - 取得選項採購成本
#
# 修正 Bug（2026-08-27）：
#   同上，移除 codsc WHERE 條件。
#   改為只用 (workgroup, path, code) 三欄比對。
# ============================================================

def get_option_price(
    path: str,
    code: str,
    codsc: str = "",          # 保留參數相容舊呼叫，但不進 WHERE
    workgroup: str = WORKGROUP,
) -> float:
    """
    取得 ordspe 中某選項的採購成本（compri）。

    注意：codsc 參數保留供呼叫端傳入，但不作為查詢條件，
    以相容 codsc 可能為 NULL 的情況。

    Returns:
        float 採購成本；若找不到回傳 0.0
    """
    db = _session()
    try:
        row = (
            db.query(Ordspe)
            .filter(
                Ordspe.workgroup == workgroup,
                Ordspe.path == path,
                Ordspe.code == code,
                # codsc 不進 WHERE
            )
            .first()
        )
        return row.compri if (row and row.compri is not None) else 0.0
    finally:
        db.close()


# ============================================================
# 7. save_quote_snapshot - 將報價快照寫入 ordqdt_ai
# ============================================================

def save_quote_snapshot(
    items: list[dict],
    workgroup: str = WORKGROUP,
) -> str:
    """
    將報價明細清單寫入 ordqdt_ai（報價快照）。

    Args:
        items: list[dict]，每筆至少包含：
            ref_no, seq_no, part_code, part_desc,
            path, opt_code, opt_desc, spc_code, spdsc,
            qty, stdqty, stdpar, compri,
            unit_price, amount, unit, status,
            adddate, addusrno

    Returns:
        str ref_no 報價單號
    """
    if not items:
        raise ValueError("items 不能為空")

    ref_no = items[0]["ref_no"]
    db = _session()
    try:
        for item in items:
            snapshot = ordqdt_ai(
                workgroup=item.get("workgroup", workgroup),
                ref_no=item["ref_no"],
                seq_no=item["seq_no"],
                part_code=item["part_code"],
                part_desc=item.get("part_desc"),
                path=item["path"],
                opt_code=item.get("opt_code"),
                opt_desc=item.get("opt_desc"),
                spc_code=item.get("spc_code"),
                spdsc=item.get("spdsc"),
                qty=item.get("qty"),
                stdqty=item.get("stdqty"),
                stdpar=item.get("stdpar"),
                compri=item.get("compri"),
                unit_price=item.get("unit_price"),
                amount=item.get("amount"),
                unit=item.get("unit", "PCS"),
                status=item.get("status", "C"),
                transferred=item.get("transferred", "N"),
                adddate=item.get("adddate"),
                addusrno=item.get("addusrno", "SYS"),
            )
            db.add(snapshot)
        db.commit()
        return ref_no
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ============================================================
# 8. get_quote_snapshot - 讀取已建立的報價快照
# ============================================================

def get_quote_snapshot(ref_no: str, workgroup: str = WORKGROUP) -> list[dict]:
    """
    依報價單號取得 ordqdt_ai 所有明細。

    Returns:
        list[dict]
    """
    db = _session()
    try:
        rows = (
            db.query(ordqdt_ai)
            .filter(
                ordqdt_ai.workgroup == workgroup,
                ordqdt_ai.ref_no == ref_no,
            )
            .order_by(ordqdt_ai.seq_no)
            .all()
        )
        return [
            {
                "ref_no": r.ref_no,
                "seq_no": r.seq_no,
                "part_code": r.part_code,
                "part_desc": r.part_desc,
                "path": r.path,
                "opt_code": r.opt_code,
                "opt_desc": r.opt_desc,
                "spc_code": r.spc_code,
                "spdsc": r.spdsc,
                "qty": r.qty,
                "stdqty": r.stdqty,
                "stdpar": r.stdpar,
                "compri": r.compri,
                "unit_price": r.unit_price,
                "amount": r.amount,
                "unit": r.unit,
                "status": r.status,
                "transferred": r.transferred,
            }
            for r in rows
        ]
    finally:
        db.close()


# ============================================================
# 9. get_all_option_categories - 取得所有選項類別（ordspd）
# ============================================================

def get_all_option_categories(workgroup: str = WORKGROUP) -> list[dict]:
    """
    取得 ordspd 中所有選項類別定義。

    Returns:
        list[dict] 每筆含 optno, optdesc, kind, code
    """
    db = _session()
    try:
        rows = (
            db.query(Ordspd)
            .filter(Ordspd.workgroup == workgroup)
            .order_by(Ordspd.optno)
            .all()
        )
        return [
            {
                "optno": (r.optno or "").strip(),
                "optdesc": (r.optdesc or "").strip(),
                "kind": (r.kind or "").strip() if r.kind else None,
                "code": (r.code or "").strip() if r.code else None,
            }
            for r in rows
        ]
    finally:
        db.close()
