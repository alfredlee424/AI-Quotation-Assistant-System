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

    selections 格式（完整路徑作為鍵，對齊真實資料庫結構）：
    {
        "CMT1\\A001": {
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

from config import WORKGROUP


class QuoteStatus(str, Enum):
    """報價流程狀態"""
    NEW = "NEW"                          # 初始狀態，尚未建立報價
    ANALYZING = "ANALYZING"              # Agent 正在解析需求
    PRODUCT_SELECTED = "PRODUCT_SELECTED"  # 已選定 invdoc 產品類別
    STRUCTURE_EXPANDED = "STRUCTURE_EXPANDED"  # 已展開 ordstr 結構樹
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
    QuoteStatus.ANALYZING: [QuoteStatus.PRODUCT_SELECTED, QuoteStatus.CHECKING, QuoteStatus.ERROR],
    QuoteStatus.PRODUCT_SELECTED: [QuoteStatus.STRUCTURE_EXPANDED, QuoteStatus.ERROR],
    QuoteStatus.STRUCTURE_EXPANDED: [QuoteStatus.CHECKING, QuoteStatus.ERROR],
    QuoteStatus.CHECKING: [
        QuoteStatus.WAITING_FOR_INPUT,
        QuoteStatus.PREVIEW,
        QuoteStatus.ERROR,
    ],
    QuoteStatus.WAITING_FOR_INPUT: [QuoteStatus.ANALYZING, QuoteStatus.CHECKING, QuoteStatus.ERROR],
    QuoteStatus.PREVIEW: [QuoteStatus.CONFIRMED, QuoteStatus.ANALYZING, QuoteStatus.CHECKING, QuoteStatus.ERROR],
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
    QuoteStatus.PRODUCT_SELECTED: "已選定產品類別",
    QuoteStatus.STRUCTURE_EXPANDED: "已展開產品結構",
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

    selections 以完整 path 為 key（對齊真實資料庫結構）：
    {
        "product_name": str,
        "qty": int | None,
        "selections": {
            "CMT1\\A001": {
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
        "product_name": "",
        "prodkind": None,
        "workgroup": WORKGROUP,
        "revision": 0,
        "preview": None,
        "qty": None,
        "selections": {},
        "discount_rate": 0.0,
        "status": QuoteStatus.NEW,
        "ref_no": None,
        "missing_fields": [],
        "allowed_options": {},
        "questions": [],
        "calc_result": None,
    }


def check_missing_fields(quote_draft: dict) -> list[str]:
    """以共用配置解析器檢查完整路徑；不吞掉資料庫錯誤或偽造預覽。

    未選產品時不可讓 resolver 使用相容性產品前綴。規格錯誤與缺項皆
    阻止預覽；本函式只更新診斷資訊，不套用選擇或自行進入 PREVIEW。
    """
    from engine.configuration import invalidate_preview, number, resolve_configuration

    missing: list[str] = []
    if quote_draft.get("qty") is None:
        missing.append("數量")
    else:
        try:
            number(quote_draft["qty"], "產品數量", positive=True)
        except ValueError as exc:
            missing.append(str(exc))
    if not quote_draft.get("prodkind"):
        missing.insert(0, "產品類別")
        quote_draft["allowed_options"] = {}
        quote_draft["option_labels"] = {}
    else:
        # 主檔缺失或連線失敗必須傳遞，不可回退到固定 REQUIRED_OPTNOS。
        try:
            resolved = resolve_configuration(quote_draft)
        except Exception:
            invalidate_preview(quote_draft)
            raise
        missing.extend(resolved["errors"])
        missing.extend(resolved["missing"])
        quote_draft["allowed_options"] = resolved["allowed_options"]
        quote_draft["option_labels"] = resolved["option_labels"]
    quote_draft["missing_fields"] = missing
    if missing or quote_draft.get("questions") or quote_draft.get("pending_options"):
        invalidate_preview(quote_draft)
        quote_draft["status"] = QuoteStatus.WAITING_FOR_INPUT
    return missing
