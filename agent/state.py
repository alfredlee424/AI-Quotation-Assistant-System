"""
agent/state.py - Agent 狀態機定義

定義報價流程的所有合法狀態與狀態轉移規則。
使用受控狀態機可以避免 Agent 無限制自由操作，方便除錯與稽核。

狀態流：
    NEW
     ↓ 使用者輸入
    ANALYZING
     ↓
    CHECKING
     ├─ 缺少欄位 → WAITING_FOR_INPUT → (使用者補充) → CHECKING
     └─ 資料完整 → PREVIEW
                   ↓ 使用者確認
                 CONFIRMED
                   ↓
                 CREATING
                   ↓
                 SNAPSHOT_CREATED
                   ↓
                 COMPLETED

錯誤時可進入 ERROR 狀態。

selections 格式（optno 驅動，對齊真實資料庫結構）：
    {
        "A001": {
            "optno": "A001",
            "optdesc": "桌面尺寸",
            "path": "CMT1\\A001",
            "code": "C001",
            "codsc": "60*120",
            "compri": 0.0,
        },
        "S002": {...},
        ...
    }
"""

from __future__ import annotations

from enum import Enum

from config import REQUIRED_OPTNOS


class QuoteStatus(str, Enum):
    """報價流程狀態"""
    NEW = "NEW"                          # 初始狀態，尚未建立報價
    ANALYZING = "ANALYZING"              # Agent 正在解析需求
    CHECKING = "CHECKING"                # 檢查必要欄位
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"  # 等待使用者補充資訊
    PREVIEW = "PREVIEW"                  # 報價草稿試算完成，等待確認
    CONFIRMED = "CONFIRMED"              # 使用者已確認報價
    CREATING = "CREATING"                # 正在建立報價快照
    SNAPSHOT_CREATED = "SNAPSHOT_CREATED"  # 已成功寫入 ordqdt_ai
    COMPLETED = "COMPLETED"              # 完成
    ERROR = "ERROR"                      # 發生錯誤


# 合法狀態轉移表
VALID_TRANSITIONS: dict[QuoteStatus, list[QuoteStatus]] = {
    QuoteStatus.NEW: [QuoteStatus.ANALYZING],
    QuoteStatus.ANALYZING: [QuoteStatus.CHECKING, QuoteStatus.ERROR],
    QuoteStatus.CHECKING: [
        QuoteStatus.WAITING_FOR_INPUT,
        QuoteStatus.PREVIEW,
        QuoteStatus.ERROR,
    ],
    QuoteStatus.WAITING_FOR_INPUT: [QuoteStatus.CHECKING, QuoteStatus.ERROR],
    QuoteStatus.PREVIEW: [QuoteStatus.CONFIRMED, QuoteStatus.CHECKING, QuoteStatus.ERROR],
    QuoteStatus.CONFIRMED: [QuoteStatus.CREATING, QuoteStatus.ERROR],
    QuoteStatus.CREATING: [QuoteStatus.SNAPSHOT_CREATED, QuoteStatus.ERROR],
    QuoteStatus.SNAPSHOT_CREATED: [QuoteStatus.COMPLETED],
    QuoteStatus.COMPLETED: [],
    QuoteStatus.ERROR: [QuoteStatus.NEW],
}

# 狀態對應的中文說明（用於 UI 顯示）
STATUS_LABEL: dict[QuoteStatus, str] = {
    QuoteStatus.NEW: "未開始",
    QuoteStatus.ANALYZING: "分析需求中",
    QuoteStatus.CHECKING: "檢查欄位",
    QuoteStatus.WAITING_FOR_INPUT: "等待補充資訊",
    QuoteStatus.PREVIEW: "報價試算完成",
    QuoteStatus.CONFIRMED: "已確認",
    QuoteStatus.CREATING: "建立報價中",
    QuoteStatus.SNAPSHOT_CREATED: "報價快照已建立",
    QuoteStatus.COMPLETED: "完成",
    QuoteStatus.ERROR: "發生錯誤",
}


def can_transition(current: QuoteStatus, target: QuoteStatus) -> bool:
    """檢查狀態轉移是否合法"""
    return target in VALID_TRANSITIONS.get(current, [])


def transition(current: QuoteStatus, target: QuoteStatus) -> QuoteStatus:
    """
    執行狀態轉移，若不合法則引發 ValueError。

    Returns:
        QuoteStatus 新狀態
    """
    if not can_transition(current, target):
        raise ValueError(
            f"非法狀態轉移：{current.value} → {target.value}。"
            f"合法目標：{[s.value for s in VALID_TRANSITIONS.get(current, [])]}"
        )
    return target


# ============================================================
# 報價草稿（Agent 工作記憶）
# ============================================================

def new_quote_draft() -> dict:
    """
    建立空白報價草稿。
    Agent 在整個對話過程中維護並更新此 dict。

    selections 以 optno 為 key（對齊真實資料庫結構）：
    {
        "product_name": str,
        "qty": int | None,
        "selections": {
            "A001": {
                "optno": "A001",
                "optdesc": "桌面尺寸",
                "path": "CMT1\\A001",
                "code": "C001",
                "codsc": "60*120",
                "compri": 0.0,
            },
            ...
        },
        "discount_rate": float,
        "status": QuoteStatus,
        "ref_no": str | None,
        "missing_fields": list[str],
        "calc_result": dict | None,
    }
    """
    return {
        "product_name": "辦公桌",
        "qty": None,
        "selections": {},
        "discount_rate": 0.0,
        "status": QuoteStatus.NEW,
        "ref_no": None,
        "missing_fields": [],
        "calc_result": None,
    }


def check_missing_fields(quote_draft: dict) -> list[str]:
    """
    檢查報價草稿中缺少哪些必要欄位。

    必填項目由 config.REQUIRED_OPTNOS 決定（如 ["A001"]），
    表示至少要選完這些 optno 類別才能進入報價試算。
    數量（qty）永遠必填。

    Returns:
        list[str] 缺少的欄位中文名稱清單，空清單表示資料完整
    """
    missing: list[str] = []

    # 數量必填
    if not quote_draft.get("qty"):
        missing.append("數量")

    # 依 REQUIRED_OPTNOS 檢查必填選項類別
    selections = quote_draft.get("selections", {})
    for optno in REQUIRED_OPTNOS:
        if optno not in selections:
            # 嘗試從資料庫取得 optdesc 作為顯示名稱
            try:
                from database import repository as repo
                cats = repo.get_all_option_categories()
                cat_map = {c["optno"]: c["optdesc"] for c in cats}
                label = cat_map.get(optno, optno)
            except Exception:
                label = optno
            missing.append(label)

    return missing
