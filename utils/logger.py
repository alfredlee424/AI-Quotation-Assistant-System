"""
utils/logger.py - AI 報價稽核日誌

記錄所有 AI Agent 操作，包含：
  - 使用者輸入
  - AI 呼叫的 Tool 名稱與參數
  - Tool 回傳結果
  - 報價建立事件

日誌格式：JSON Lines（每行一筆 JSON），方便後續分析。
日誌路徑由 config.LOG_FILE 設定（預設 logs/ai_quote.log）。
"""

from __future__ import annotations

import json
import logging
import datetime
from pathlib import Path
from typing import Any

from config import LOG_FILE


# ============================================================
# 設定 Python logging
# ============================================================

def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("ai_quote")
    if logger.handlers:
        return logger  # 已初始化，避免重複添加 handler

    logger.setLevel(logging.INFO)

    # 確保目錄存在
    log_path = Path(LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # 檔案 Handler（UTF-8，追加模式）
    fh = logging.FileHandler(log_path, encoding="utf-8", mode="a")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(message)s"))  # 只輸出訊息本體（JSON）
    logger.addHandler(fh)

    return logger


_logger = _setup_logger()


# ============================================================
# 記錄函式
# ============================================================

def log_action(
    action: str,
    params: Any = None,
    result: Any = None,
    user: str = "SYS",
    quote_no: str = "",
    extra: dict | None = None,
) -> None:
    """
    記錄一筆 AI 操作到稽核日誌。

    Args:
        action   : 操作名稱（如 search_option、calculate_quote、create_quote）
        params   : 操作參數（dict 或任意可序列化物件）
        result   : 操作結果
        user     : 執行人員代號
        quote_no : 相關報價單號（若已知）
        extra    : 額外資訊（可選）
    """
    entry = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "user": user,
        "action": action,
        "quote_no": quote_no,
        "params": _safe_serialize(params),
        "result": _safe_serialize(result),
    }
    if extra:
        entry["extra"] = _safe_serialize(extra)

    try:
        _logger.info(json.dumps(entry, ensure_ascii=False))
    except Exception:
        pass  # 日誌失敗不應中斷主流程


def log_user_input(user_input: str, user: str = "SYS") -> None:
    """記錄使用者輸入"""
    log_action("user_input", params={"text": user_input}, user=user)


def log_quote_created(ref_no: str, total_price: float, user: str = "SYS") -> None:
    """記錄正式報價建立事件"""
    log_action(
        "quote_created",
        params={"ref_no": ref_no, "total_price": total_price},
        quote_no=ref_no,
        user=user,
    )


def log_error(error: str, context: Any = None, user: str = "SYS") -> None:
    """記錄錯誤事件"""
    log_action(
        "error",
        params={"error": error, "context": _safe_serialize(context)},
        user=user,
    )


# ============================================================
# 輔助
# ============================================================

def _safe_serialize(obj: Any) -> Any:
    """將物件轉成可 JSON 序列化的格式"""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _safe_serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_serialize(v) for v in obj]
    # 其他物件嘗試轉成字串
    try:
        return str(obj)
    except Exception:
        return "<不可序列化>"
