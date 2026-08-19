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
"""

from __future__ import annotations

from enum import Enum


class QuoteStatus(str, Enum):
    """報價流程狀態"""
    NEW = "NEW"                          # 初始狀態，尚未建立報價
    ANALYZING = "ANALYZING"              # Agent 正在解析需求
    CHECKING = "CHECKING"                # 檢查必要欄位
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"  # 等待使用者補充資訊
    PREVIEW = "PREVIEW"                  # 報價草稿試算完成，等待確認
    CONFIRMED = "CONFIRMED"              # 使用者已確認報價
    CREATING = "CREATING"                # 正在建立報價快照
    SNAPSHOT_CREATED = "SNAPSHOT_CREATED"  # 已成功寫入 ordqdt
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

    結構：
    {
        "product_name": str,
        "qty": int | None,
        "selections": {
            "size":     {"path": ..., "code": ..., "codsc": ...},
            "material": {...},
            "color":    {...},
            "leg":      {...},
            "top":      {...},
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


# 必要欄位定義（缺少任一項無法進入 PREVIEW）
REQUIRED_FIELDS: list[tuple[str, str]] = [
    ("qty",       "數量"),
    ("size",      "桌面尺寸"),
    ("material",  "桌面材質"),
    ("color",     "桌面顏色"),
    ("leg",       "腳架類型"),
]


def check_missing_fields(quote_draft: dict) -> list[str]:
    """
    檢查報價草稿中缺少哪些必要欄位。

    Returns:
        list[str] 缺少的欄位中文名稱清單，空清單表示資料完整
    """
    missing: list[str] = []
    for field_key, field_label in REQUIRED_FIELDS:
        if field_key == "qty":
            if not quote_draft.get("qty"):
                missing.append(field_label)
        else:
            if field_key not in quote_draft.get("selections", {}):
                missing.append(field_label)
    return missing
