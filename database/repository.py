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

from difflib import SequenceMatcher

from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import or_

from database.connection import get_db
from database.models import Ordspd, Ordspe, Ordqty, Invdoc, Ordstr, ordqdt_ai
from config import WORKGROUP, PRODUCT_PREFIX, ORDKIND_PRODUCT
from utils.logger import log_action


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
                "compri": r.compri,
                # nested path 的選項類別是最後一段（例如 S001），
                # 不能回傳根節點 A001，否則 LLM 會用色紙覆寫尺寸。
                "optno": (r.path or "").strip().rsplit("\\", 1)[-1],
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
                "optno": (r.path or "").strip().rsplit("\\", 1)[-1],
            }
            for r in rows
        ]
    finally:
        db.close()


def search_option_fuzzy(
    keyword: str,
    path_prefix: str | None = None,
    workgroup: str = WORKGROUP,
    limit: int = 8,
) -> list[dict]:
    """以資料庫選項為全集，依文字相似度回傳候選，不自行產生規格。"""
    def normalize(value: str) -> str:
        return "".join(value.lower().split()).replace("×", "x").replace("＊", "*")

    query = normalize(keyword)
    if not query:
        return []

    db = _session()
    try:
        query_obj = db.query(Ordspe).filter(Ordspe.workgroup == workgroup)
        if path_prefix:
            # 模糊候選只搜尋目前選項節點，避免把子路徑或其他部件混入候選。
            query_obj = query_obj.filter(Ordspe.path == path_prefix)
        rows = query_obj.all()
        candidates: list[dict] = []
        for row in rows:
            path = (row.path or "").strip()
            code = (row.code or "").strip()
            codsc = (row.codsc or "").strip()
            values = [normalize(codsc), normalize(code)]
            score = max(
                SequenceMatcher(None, query, value).ratio()
                for value in values
                if value
            )
            if any(query in value for value in values if value):
                score = max(score, 0.9)
            candidates.append({
                "path": path,
                "code": code,
                "codsc": codsc,
                "compri": row.compri or 0.0,
                "optno": path.rsplit("\\", 1)[-1],
                "match_score": round(score, 4),
                "match_type": "fuzzy",
            })
        candidates.sort(key=lambda item: (-item["match_score"], item["path"], item["code"]))
        return candidates[:limit]
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
                "compri": r.compri,
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
            "stdqty": row.stdqty,
            "stdpar": row.stdpar,
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

def get_quantity_rules(path: str, workgroup: str = WORKGROUP) -> list[dict]:
    """先取得 part_path，再由該部位所選 code 決定用量；不以材料 code 猜測。"""
    db = _session()
    try:
        rows = db.query(Ordqty).filter(Ordqty.workgroup == workgroup, Ordqty.path == path).all()
        return [{"path": r.path.strip(), "code": r.code.strip(),
                 "part_path": (r.part_path or "").strip(),
                 "stdqty": r.stdqty, "stdpar": r.stdpar} for r in rows]
    finally:
        db.close()


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
    preview: Optional[dict] = None,
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
        if preview is not None:
            import json
            from database.models import QuoteSnapshotDocument
            existing = db.get(QuoteSnapshotDocument, (workgroup, preview["preview_id"]))
            if existing:
                if json.loads(existing.payload).get("digest") != preview.get("digest"):
                    raise ValueError("同一預覽版本的保存內容不一致")
                return existing.ref_no
            db.add(QuoteSnapshotDocument(workgroup=workgroup, preview_id=preview["preview_id"],
                                        ref_no=ref_no, payload=json.dumps(preview, ensure_ascii=False, allow_nan=False)))
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


# ============================================================
# 10. get_product_categories - 取得產品類別清單（invdoc）
# ============================================================

def get_product_categories(
    ordkind: str = ORDKIND_PRODUCT,
    workgroup: str = WORKGROUP,
) -> list[dict]:
    """
    取得 invdoc 中指定 ordkind 的產品類別清單。

    Args:
        ordkind   : 類別種類（預設 ORDKIND_PRODUCT="1" 報價產品）
        workgroup : 事業別

    Returns:
        list[dict] 每筆含 prodkind, codsc, quo_rate, ordkind
    """
    db = _session()
    try:
        rows = (
            db.query(Invdoc)
            .filter(
                Invdoc.workgroup == workgroup,
                Invdoc.ordkind == ordkind,
            )
            .order_by(Invdoc.prodkind)
            .all()
        )
        categories = [
            {
                "prodkind": (r.prodkind or "").strip(),
                "codsc": (r.codsc or "").strip() if r.codsc else None,
                "quo_rate": r.quo_rate,
                "ordkind": (r.ordkind or "").strip() if r.ordkind else None,
            }
            for r in rows
        ]
        # 不額外查詢、不記錄連線字串／帳密／報價率；名稱保留內部字元供診斷。
        log_action("quote_product_categories_loaded", params={
            "workgroup": workgroup, "ordkind": ordkind,
            "backend": db.get_bind().dialect.name,
        }, result={
            "category_count": len(categories),
            "categories": [{"prodkind": c["prodkind"], "codsc": c["codsc"]}
                           for c in categories[:50]],
            "categories_truncated": len(categories) > 50,
        })
        return categories
    except Exception as exc:
        log_action("quote_product_categories_failed", params={
            "workgroup": workgroup, "ordkind": ordkind,
        }, result={"error_type": type(exc).__name__})
        raise
    finally:
        db.close()


# ============================================================
# 11. get_product_category - 依 prodkind 取得單一產品類別
# ============================================================

def get_product_category(
    prodkind: str,
    workgroup: str = WORKGROUP,
) -> Optional[dict]:
    """
    依 prodkind 取得單一產品類別（含 quo_rate）。

    Args:
        prodkind  : 產品類別代碼（如 "CMT1"）
        workgroup : 事業別

    Returns:
        dict 含 prodkind, codsc, quo_rate, ordkind；若不存在回傳 None
    """
    db = _session()
    try:
        row = (
            db.query(Invdoc)
            .filter(
                Invdoc.workgroup == workgroup,
                Invdoc.prodkind == prodkind,
            )
            .first()
        )
        if row is None:
            return None
        return {
            "prodkind": (row.prodkind or "").strip(),
            "codsc": (row.codsc or "").strip() if row.codsc else None,
            "quo_rate": row.quo_rate,
            "ordkind": (row.ordkind or "").strip() if row.ordkind else None,
        }
    finally:
        db.close()


# ============================================================
# 12. get_ordstr_children - 取得某父節點的直接子節點（ordstr）
# ============================================================

def get_ordstr_children(
    pathf: str,
    workgroup: str = WORKGROUP,
) -> list[dict]:
    """
    取得某父節點 pathf 的直接子節點（依 seq 排序）。

    Args:
        pathf     : 父節點路徑（如 "CMT1" 或 "CMT1\\A001"）
        workgroup : 事業別

    Returns:
        list[dict] 每筆含 pathf, pathc, optnof, optnoc,
                   must_chose, has_name, has_inname, seq, dmark
    """
    db = _session()
    try:
        rows = (
            db.query(Ordstr)
            .filter(
                Ordstr.workgroup == workgroup,
                Ordstr.pathf == pathf,
            )
            .order_by(Ordstr.seq)
            .all()
        )
        return [
            {
                "pathf": (r.pathf or "").strip(),
                "pathc": (r.pathc or "").strip(),
                "optnof": (r.optnof or "").strip(),
                "optnoc": (r.optnoc or "").strip(),
                "must_chose": (r.must_chose or "").strip() if r.must_chose else None,
                "has_name": (r.has_name or "").strip() if r.has_name else None,
                "has_inname": (r.has_inname or "").strip() if r.has_inname else None,
                "seq": r.seq,
                "dmark": r.dmark,
            }
            for r in rows
        ]
    finally:
        db.close()


# ============================================================
# 13. expand_ordstr_tree - 遞迴展開整棵結構樹（ordstr）
# ============================================================

def expand_ordstr_tree(
    root_path: str,
    workgroup: str = WORKGROUP,
) -> list[dict]:
    """
    以 root_path 為根，遞迴展開整棵結構樹（DFS，依 seq 排序）。

    每個節點標註：
        depth      : 相對於根的深度（root 的直接子節點 depth=1）
        is_leaf    : 是否為葉節點（不再作為 pathf）
        must_chose : 是否必選（Y/空）

    葉節點（不再作為 pathf）視為計價候選，
    需再回 ordspe 取成本、回 ordqty 取用量。

    遞迴保護：以 visited set 記錄已展開的 pathc，
    避免資料異常（循環邊）造成無限遞迴。

    Args:
        root_path : 結構樹根路徑，通常為 prodkind（如 "CMT1"）
        workgroup : 事業別

    Returns:
        list[dict] 扁平化的節點清單（保留 depth/is_leaf/must_chose 等階層資訊）
    """
    result: list[dict] = []
    visited: set[str] = set()

    def _walk(pathf: str, depth: int) -> None:
        if pathf in visited:
            return
        visited.add(pathf)

        children = get_ordstr_children(pathf, workgroup=workgroup)
        for child in children:
            pathc = child["pathc"]
            grandchildren = get_ordstr_children(pathc, workgroup=workgroup)
            is_leaf = len(grandchildren) == 0
            result.append({
                "pathf": child["pathf"],
                "pathc": pathc,
                "optnof": child["optnof"],
                "optnoc": child["optnoc"],
                "must_chose": child["must_chose"],
                "seq": child["seq"],
                "dmark": child["dmark"],
                "depth": depth,
                "is_leaf": is_leaf,
            })
            if not is_leaf:
                _walk(pathc, depth + 1)

    _walk(root_path, 1)
    return result


# ============================================================
# 14. get_required_nodes - 取得必選節點清單（ordstr.must_chose）
# ============================================================

def get_required_nodes(
    root_path: str,
    workgroup: str = WORKGROUP,
) -> list[dict]:
    """
    回傳整棵樹中 must_chose='Y' 的必選節點清單（供完整性檢查）。

    Args:
        root_path : 結構樹根路徑，通常為 prodkind（如 "CMT1"）
        workgroup : 事業別

    Returns:
        list[dict] 每筆含 pathf, pathc, optnoc, must_chose, seq, dmark, depth, is_leaf
    """
    nodes = expand_ordstr_tree(root_path, workgroup=workgroup)
    return [n for n in nodes if (n.get("must_chose") or "").upper() == "Y"]


def get_actionable_required_nodes(
    root_path: str,
    workgroup: str = WORKGROUP,
    selections: Optional[dict] = None,
) -> list[dict]:
    """取得需要使用者選擇的必選節點，排除結構／製程節點。

    ordstr 的 must_chose 也可能標在沒有可選規格的父節點，或 W001
    製程節點。這些節點應由結構樹與成本規則處理，不應直接顯示給客戶。
    """
    result: list[dict] = []
    seen: set[str] = set()
    tree = expand_ordstr_tree(root_path, workgroup=workgroup)
    required_nodes = [n for n in tree if (n.get("must_chose") or "").upper() == "Y"]
    chosen_paths = {
        str(value.get("path", "")).strip()
        for value in (selections or {}).values()
        if isinstance(value, dict)
    }
    # 呼叫端會在草稿中提供選擇；此 helper 的保守版本只排除明確的
    # 製程節點與沒有選項的結構節點，分支啟用狀態由 calculator 再判斷。
    for node in required_nodes:
        path = str(node.get("pathc", "")).strip()
        parts = path.split("\\")
        if not path or path in seen or "W001" in parts or parts[-1] == "S000":
            continue
        # 必選節點位於非必選分支時，只有該分支有實際選擇才需要詢問。
        ancestor_parts = parts[1:-1]
        active = True
        for index, part in enumerate(ancestor_parts, start=1):
            ancestor = "\\".join(parts[: index + 1])
            ancestor_node = next((n for n in tree if n.get("pathc") == ancestor), None)
            if ancestor_node and (ancestor_node.get("must_chose") or "").upper() != "Y":
                active = any(
                    selected == ancestor or selected.startswith(ancestor + "\\")
                    for selected in chosen_paths
                )
                if not active:
                    break
        if not active:
            continue
        options = get_options_by_path(path, workgroup=workgroup)
        if not options:
            continue
        seen.add(path)
        result.append(node)
    return result
