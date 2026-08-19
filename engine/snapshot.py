"""
engine/snapshot.py - 報價快照組裝與 ref_no 產生

職責：
  1. 產生唯一報價單號（ref_no），格式：Q{YYYYMMDD}{4碼序號}
  2. 將計算結果（CalcResult）與報價草稿組裝成 ordqdt 明細清單
  3. 呼叫 repository.save_quote_snapshot() 寫入資料庫

設計原則：
  - 確保歷史報價快照包含當時的成本、用量、規格、單價、金額
  - ref_no 每日序號從 0001 開始，自動遞增（當日已用單號 +1）
"""

from __future__ import annotations

import datetime
from config import WORKGROUP
from engine.calculator import CalcResult
from database import repository as repo


# ============================================================
# ref_no 產生
# ============================================================

def generate_ref_no(workgroup: str = WORKGROUP) -> str:
    """
    產生唯一報價單號。
    格式：Q{YYYYMMDD}{4碼流水號}  例如 Q202608190001

    流水號依資料庫既有當日報價單數量 +1，確保不重複。
    若資料庫無此日報價，從 0001 開始。
    """
    today = datetime.date.today().strftime("%Y%m%d")
    prefix = f"Q{today}"

    # 查詢當日已建立的報價數量
    existing = _count_today_quotes(prefix, workgroup)
    seq = existing + 1
    return f"{prefix}{seq:04d}"


def _count_today_quotes(prefix: str, workgroup: str) -> int:
    """查詢今日已使用的報價單號數量（避免流水號衝突）"""
    from database.connection import get_db
    from database.models import Ordqdt
    db = get_db()
    try:
        # 取得不重複的 ref_no 數量
        count = (
            db.query(Ordqdt.ref_no)
            .filter(
                Ordqdt.workgroup == workgroup,
                Ordqdt.ref_no.like(f"{prefix}%"),
            )
            .distinct()
            .count()
        )
        return count
    finally:
        db.close()


# ============================================================
# 組裝 ordqdt 快照資料
# ============================================================

def build_snapshot_items(
    calc_result: CalcResult,
    quote_draft: dict,
    ref_no: str,
    user: str = "SYS",
    workgroup: str = WORKGROUP,
) -> list[dict]:
    """
    將計算結果（CalcResult.items）組裝為可寫入 ordqdt 的明細清單。

    Args:
        calc_result  : 計算引擎回傳的 CalcResult
        quote_draft  : Agent 維護的報價草稿 dict
        ref_no       : 報價單號
        user         : 建立人員代號
        workgroup    : 事業別

    Returns:
        list[dict]   可傳入 repository.save_quote_snapshot() 的資料
    """
    today = datetime.date.today().strftime("%Y-%m-%d")
    selections: dict = quote_draft.get("selections", {})

    # 建立 spc_code → 選項說明的對映（供快照備用）
    sel_map: dict[str, dict] = {
        v.get("code", ""): v for v in selections.values()
    }

    snapshot_items: list[dict] = []

    for idx, item in enumerate(calc_result.items, start=1):
        seq_no = f"{idx:05d}"
        spc_code = item.get("spc_code", "")
        sel_info = sel_map.get(spc_code, {})

        snapshot_items.append({
            "workgroup": workgroup,
            "ref_no": ref_no,
            "seq_no": seq_no,
            "part_code": item.get("part_code", ""),
            "part_desc": item.get("part_desc", ""),
            "path": item.get("path", ""),
            "opt_code": sel_info.get("opt_code", ""),
            "opt_desc": sel_info.get("optdesc", ""),
            "spc_code": spc_code,
            "spdsc": item.get("spdsc", ""),
            "qty": item.get("qty", 0),
            "stdqty": item.get("stdqty", 1.0),
            "stdpar": item.get("stdpar", 1.0),
            "compri": item.get("compri", 0.0),      # 採購成本快照
            "unit_price": item.get("unit_price", 0.0),
            "amount": item.get("amount", 0.0),
            "unit": item.get("unit", "PCS"),
            "status": "C",  # C = 已確認
            "adddate": today,
            "addusrno": user,
        })

    return snapshot_items


# ============================================================
# 主入口：建立報價快照
# ============================================================

def create_quote_snapshot(
    calc_result: CalcResult,
    quote_draft: dict,
    user: str = "SYS",
    workgroup: str = WORKGROUP,
) -> str:
    """
    產生報價單號、組裝快照資料、寫入 ordqdt。

    Args:
        calc_result : 計算引擎回傳的 CalcResult
        quote_draft : Agent 維護的報價草稿
        user        : 建立人員代號
        workgroup   : 事業別

    Returns:
        str ref_no 報價單號
    """
    ref_no = generate_ref_no(workgroup)
    items = build_snapshot_items(
        calc_result=calc_result,
        quote_draft=quote_draft,
        ref_no=ref_no,
        user=user,
        workgroup=workgroup,
    )
    saved_ref_no = repo.save_quote_snapshot(items=items, workgroup=workgroup)
    return saved_ref_no
