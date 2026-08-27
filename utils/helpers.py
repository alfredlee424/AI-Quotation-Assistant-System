"""
utils/helpers.py - 格式化輸出與數值轉換工具

提供各模組共用的輔助函式：
  - 金額格式化
  - 百分比格式化
  - 報價草稿 → 可顯示的 DataFrame
  - 狀態標籤取得
  - 選項摘要動態列出（依 optdesc，不寫死四個欄位）
"""

from __future__ import annotations

from typing import Any, Optional
import pandas as pd


# ============================================================
# 數值格式化
# ============================================================

def fmt_money(value: Optional[float], currency: str = "$") -> str:
    """
    格式化金額為帶千分位的字串。
    例如：12345.6 → "$12,346"

    Returns:
        str 格式化後的金額字串
    """
    if value is None:
        return f"{currency}0"
    return f"{currency}{value:,.0f}"


def fmt_percent(value: float) -> str:
    """
    格式化百分比。
    例如：0.3 → "30%"
    """
    return f"{value:.0%}"


def fmt_qty(value: Optional[float]) -> str:
    """格式化數量（去除不必要的小數點）"""
    if value is None:
        return "0"
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


# ============================================================
# 報價明細 → DataFrame（用於 Streamlit 顯示）
# ============================================================

def calc_items_to_df(items: list[dict]) -> pd.DataFrame:
    """
    將計算結果 items 轉成 Streamlit 可顯示的 DataFrame。

    Args:
        items: engine.calculator.CalcResult.items

    Returns:
        pd.DataFrame 含中文欄位名稱
    """
    if not items:
        return pd.DataFrame(columns=["類別", "規格", "數量", "標準用量", "採購成本", "單價", "金額"])

    rows = []
    for item in items:
        rows.append({
            "類別": item.get("optdesc") or item.get("part_desc", ""),
            "規格": item.get("codsc") or item.get("spdsc", ""),
            "數量": fmt_qty(item.get("qty")),
            "標準用量": fmt_qty(item.get("stdqty")),
            "採購成本": fmt_money(item.get("compri")),
            "單價": fmt_money(item.get("unit_price")),
            "金額": fmt_money(item.get("amount")),
        })
    return pd.DataFrame(rows)


def snapshot_to_df(snapshot_items: list[dict]) -> pd.DataFrame:
    """
    將 ordqdt_ai 快照明細轉成 DataFrame。

    Args:
        snapshot_items: repository.get_quote_snapshot() 回傳的清單

    Returns:
        pd.DataFrame
    """
    if not snapshot_items:
        return pd.DataFrame()

    rows = []
    for item in snapshot_items:
        rows.append({
            "序號": item.get("seq_no", ""),
            "部件代碼": item.get("part_code", ""),
            "部件名稱": item.get("part_desc", ""),
            "規格": item.get("spdsc", ""),
            "數量": fmt_qty(item.get("qty")),
            "採購成本（快照）": fmt_money(item.get("compri")),
            "報價單價": fmt_money(item.get("unit_price")),
            "報價金額": fmt_money(item.get("amount")),
            "狀態": item.get("status", ""),
        })
    return pd.DataFrame(rows)


# ============================================================
# 報價草稿摘要（動態依 selections 列出，不寫死四欄）
# ============================================================

def quote_draft_summary(quote_draft: dict) -> dict[str, str]:
    """
    從報價草稿擷取關鍵欄位，回傳可直接顯示的 dict。
    selections 以 optno 為 key，動態列出所有已選項目。

    Returns:
        dict {label: value}，固定欄位 + 動態選項
    """
    selections = quote_draft.get("selections", {})
    calc = quote_draft.get("calc_result", {}) or {}

    result: dict[str, str] = {
        "產品": quote_draft.get("product_name", "辦公桌"),
        "數量": f"{quote_draft.get('qty', '--')} 張" if quote_draft.get("qty") else "--",
    }

    # 動態加入每個已選選項（依 optdesc 作為顯示標題）
    for optno, sel in selections.items():
        label = sel.get("optdesc") or optno
        codsc = sel.get("codsc", "--")
        compri = sel.get("compri", 0.0)
        # compri > 0 時顯示成本提示
        if compri and compri > 0:
            result[label] = f"{codsc}（${compri:,.0f}）"
        else:
            result[label] = codsc

    result.update({
        "材料成本": fmt_money(calc.get("total_cost")) if calc else "--",
        "建議售價": fmt_money(calc.get("total_price")) if calc else "--",
        "折扣率": fmt_percent(calc.get("discount_rate", 0)) if calc else "--",
        "含稅總額": fmt_money(calc.get("total_price")) if calc else "--",
    })

    return result


def quote_selections_list(quote_draft: dict) -> list[dict]:
    """
    將 selections 整理為有序的清單，供 UI 逐行顯示。

    Returns:
        list[dict] 每筆含 optno, optdesc, codsc, compri
    """
    selections = quote_draft.get("selections", {})
    return [
        {
            "optno": optno,
            "optdesc": sel.get("optdesc", optno),
            "codsc": sel.get("codsc", "--"),
            "compri": sel.get("compri", 0.0),
        }
        for optno, sel in selections.items()
    ]


# ============================================================
# 狀態標籤（搭配 agent.state.STATUS_LABEL）
# ============================================================

def get_status_badge(status_value: str) -> str:
    """
    依狀態值回傳含 emoji 的標籤。

    Args:
        status_value: QuoteStatus 的值字串，如 "PREVIEW"

    Returns:
        str 含 emoji 的中文標籤
    """
    badges = {
        "NEW":               "⚪ 未開始",
        "ANALYZING":         "🔍 分析需求中",
        "CHECKING":          "🔎 檢查欄位",
        "WAITING_FOR_INPUT": "⏳ 等待補充資訊",
        "PREVIEW":           "📋 報價試算完成",
        "CONFIRMED":         "✅ 已確認",
        "CREATING":          "⚙️ 建立報價中",
        "SNAPSHOT_CREATED":  "🎉 報價快照已建立",
        "COMPLETED":         "✅ 完成",
        "ERROR":             "❌ 發生錯誤",
    }
    return badges.get(status_value, f"❓ {status_value}")


# ============================================================
# 對話歷史截斷（控制 Token 數量）
# ============================================================

def trim_messages(messages: list[dict], max_turns: int = 20) -> list[dict]:
    """
    保留最近 max_turns 條訊息，避免 LLM Context 過長。

    Args:
        messages  : 對話歷史清單
        max_turns : 最大保留條數

    Returns:
        list[dict] 截斷後的對話歷史
    """
    if len(messages) <= max_turns:
        return messages
    return messages[-max_turns:]
