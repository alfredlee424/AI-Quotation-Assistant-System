"""
agent/rule_parser.py - 規則式自然語言解析器

無需 LLM API 即可從使用者輸入中擷取：
  - 數量（20張、100張、20個）
  - 桌面尺寸（60*120、75*180、120x75）→ 查詢 optno=A001 根節點
  - 板材關鍵字（MDF、夾板）→ 查詢 CMT1\\A001\\S004 路徑
  - 色紙關鍵字（胡桃、白橡）→ 查詢 CMT1\\A001\\S005\\S001 路徑
  - 腳架關鍵字（45寬、55寬、木腳）→ 查詢 optno=B001 根節點
  - 確認意圖（是、確認、建立、OK）
  - 取消意圖（否、取消、不要）

並查詢資料庫將關鍵字轉成正式代碼（optno 驅動）。

路徑結構（巢狀樹，對應真實 DB 與 seed_data）：
    CMT1\\A001         → 桌面尺寸根節點（C001=60*120, C002=60*150, C003=60*180）
    CMT1\\A001\\S004   → 板材子部件（C001=25mm MDF, C002=18mm 夾板）
    CMT1\\A001\\S005\\S001 → 色紙子部件（C001=817胡桃, C003=932揚胡桃）
    CMT1\\B001         → 木腳根節點（C001=45寬, C002=55寬, C003=60寬, C004=75寬）

selections 格式（以 optno 為 key）：
    {
        "A001": {"optno": "A001", "optdesc": "桌面尺寸",
                 "path": "CMT1\\A001", "code": "C001", "codsc": "60*120", "compri": 0.0},
        "S004": {"optno": "S004", "optdesc": "板材",
                 "path": "CMT1\\A001\\S004", "code": "C001", "codsc": "25mm MDF", "compri": 700.0},
        "B001": {"optno": "B001", "optdesc": "木腳",
                 "path": "CMT1\\B001", "code": "C001", "codsc": "45寬環式腳", "compri": 0.0},
        ...
    }
"""

from __future__ import annotations

import re
from typing import Optional

from database import repository as repo
from config import WORKGROUP, PRODUCT_PREFIX


# ============================================================
# 關鍵字對映表（自然語言 → 資料庫搜尋關鍵字）
# ============================================================

# 板材關鍵字（搜尋 ordspe，path = CMT1\\A001\\S004）
# codsc 對應 seed_data / 真實 DB 中的實際名稱
BOARD_KEYWORDS: dict[str, str] = {
    "MDF": "MDF",
    "mdf": "MDF",
    "密集板": "MDF",
    "美耐板": "MDF",   # 美耐板桌面底材預設使用 MDF
    "美耐": "MDF",
    "夾板": "夾板",
    "合板": "夾板",
    "木芯板": "夾板",
}

# 色紙關鍵字（搜尋 ordspe，path 前綴 = CMT1\\A001\\S005\\S001）
# codsc 對應真實 DB 中的名稱（如 817胡桃、932揚胡桃）
COLOR_KEYWORDS: dict[str, str] = {
    "胡桃": "胡桃",
    "胡桃木": "胡桃",
    "揚胡桃": "揚胡桃",
    "白橡": "白橡",
    "紅木": "紅木",
    "美耐板": "美耐板",
    "美耐": "美耐板",
}

# 腳架關鍵字（搜尋 ordspe，path = CMT1\\B001 根節點）
# codsc 對應 seed_data 中的實際名稱（45寬環式腳、55寬環式腳等）
LEG_KEYWORDS: dict[str, str] = {
    "木腳": "環式腳",
    "木製腳": "環式腳",
    "實木腳": "環式腳",
    "45寬": "45寬",
    "55寬": "55寬",
    "60寬": "60寬",
    "75寬": "75寬",
    "環式腳": "環式腳",
}

# 確認意圖關鍵字
CONFIRM_KEYWORDS: list[str] = [
    "是", "是的", "確認", "建立", "沒問題", "ok", "OK", "好", "可以", "對", "正確",
    "建立報價", "建立報價單", "確認建立",
]

# 取消意圖關鍵字
CANCEL_KEYWORDS: list[str] = [
    "否", "不", "取消", "不要", "不用", "算了", "重來",
]


# ============================================================
# 數量解析
# ============================================================

def extract_quantity(text: str) -> Optional[int]:
    """
    從文字中擷取數量。
    支援：「20張」「100個」「20 張」「20」（純數字）

    Returns:
        int 數量，或 None（若無法識別）
    """
    m = re.search(r"(\d+)\s*[張個件套台]", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)", text)
    if m:
        return int(m.group(1))
    return None


# ============================================================
# 通用選項查詢（依關鍵字 + optno 過濾）
# ============================================================

def _lookup_option_by_optno(keyword: str, optno: str) -> Optional[dict]:
    """
    查詢 ordspe 中符合關鍵字且屬於指定 optno 的選項。
    path 格式為 {PRODUCT_PREFIX}\\{optno}，如 CMT1\\S002。

    Returns:
        dict {optno, optdesc, path, code, codsc, compri} 或 None
    """
    target_path = repo.build_option_path(optno)
    results = repo.search_option(keyword=keyword, workgroup=WORKGROUP)
    filtered = [r for r in results if r.get("path", "") == target_path]
    if filtered:
        r = filtered[0]
        # 取得 optdesc
        cats = repo.get_all_option_categories()
        cat_map = {c["optno"]: c["optdesc"] for c in cats}
        return {
            "optno": optno,
            "optdesc": cat_map.get(optno, optno),
            "path": r["path"],
            "code": r["code"],
            "codsc": r["codsc"],
            "compri": r["compri"],
        }
    return None


def _lookup_option_by_path_prefix(keyword: str, path_prefix: str) -> Optional[dict]:
    """
    查詢 ordspe 中符合關鍵字且 path 開頭符合 path_prefix 的選項。
    用於 optno 有多個選擇的情況（如木腳可能是 B001 或 B002）。

    Returns:
        dict {optno, optdesc, path, code, codsc, compri} 或 None
    """
    results = repo.search_option(keyword=keyword, workgroup=WORKGROUP)
    prefix = f"{PRODUCT_PREFIX}\\{path_prefix}" if not path_prefix.startswith(PRODUCT_PREFIX) else path_prefix
    filtered = [r for r in results if r.get("path", "").startswith(prefix)]
    if filtered:
        r = filtered[0]
        optno = r.get("optno", repo.optno_from_path(r["path"]))
        cats = repo.get_all_option_categories()
        cat_map = {c["optno"]: c["optdesc"] for c in cats}
        return {
            "optno": optno,
            "optdesc": cat_map.get(optno, optno),
            "path": r["path"],
            "code": r["code"],
            "codsc": r["codsc"],
            "compri": r["compri"],
        }
    return None


# ============================================================
# 尺寸解析
# ============================================================

def extract_size(text: str) -> Optional[dict]:
    """
    從文字中擷取桌面尺寸，對應 ordspd.optno=A001。
    支援：「60*120」「75×180」「1200x600」（自動換算 cm/mm 格式）

    Returns:
        dict {optno, optdesc, path, code, codsc, compri} 或 None
    """
    # 匹配 WxH 格式（支援 cm 格式如 60*120 或 mm 格式如 1200x600）
    m = re.search(r"(\d{2,4})\s*[xX×*]\s*(\d{2,4})", text)
    if not m:
        return None

    w_raw, h_raw = m.group(1), m.group(2)

    # mm 轉 cm（>= 100 認為是 mm）
    w = int(w_raw) // 10 if int(w_raw) >= 100 else int(w_raw)
    h = int(h_raw) // 10 if int(h_raw) >= 100 else int(h_raw)

    # 嘗試各種格式搜尋（如 60*120、75*180）
    for kw in [f"{w}*{h}", f"{h}*{w}", str(w), str(h)]:
        result = _lookup_option_by_optno(kw, "A001")
        if result:
            return result

    return None


# ============================================================
# 板材解析（CMT1\\A001\\S004 子部件）
# ============================================================

def extract_board(text: str) -> Optional[dict]:
    """
    從文字擷取板材規格（CMT1\\A001\\S004）並查詢正式代碼。
    回傳 selections entry，optno 使用 "S004"。
    """
    board_path_prefix = f"{PRODUCT_PREFIX}\\A001\\S004"
    for kw, search_kw in BOARD_KEYWORDS.items():
        if kw in text:
            results = repo.search_option(keyword=search_kw, workgroup=WORKGROUP)
            filtered = [r for r in results if r.get("path", "") == board_path_prefix]
            if filtered:
                r = filtered[0]
                return {
                    "optno": "S004",
                    "optdesc": "板材",
                    "path": r["path"],
                    "code": r["code"],
                    "codsc": r["codsc"],
                    "compri": r["compri"],
                }
    return None


# ============================================================
# 色紙解析（CMT1\\A001\\S005\\S001 子部件）
# ============================================================

def extract_color(text: str) -> Optional[dict]:
    """
    從文字擷取表板色紙（CMT1\\A001\\S005\\S001）並查詢正式代碼。
    回傳 selections entry，optno 使用 "S001"。
    """
    color_path_prefix = f"{PRODUCT_PREFIX}\\A001\\S005\\S001"
    for kw, search_kw in COLOR_KEYWORDS.items():
        if kw in text:
            results = repo.search_option(keyword=search_kw, workgroup=WORKGROUP)
            filtered = [r for r in results if r.get("path", "") == color_path_prefix]
            if filtered:
                r = filtered[0]
                return {
                    "optno": "S001",
                    "optdesc": "色紙",
                    "path": r["path"],
                    "code": r["code"],
                    "codsc": r["codsc"],
                    "compri": r["compri"],
                }
    return None


# ============================================================
# 腳架解析（CMT1\\B001 根節點）
# ============================================================

def extract_leg(text: str) -> Optional[dict]:
    """
    從文字擷取腳架類型（CMT1\\B001 根節點）並查詢正式代碼。
    回傳 selections entry，optno 使用 "B001"。
    """
    leg_path = repo.build_option_path("B001")
    for kw, search_kw in LEG_KEYWORDS.items():
        if kw in text:
            results = repo.search_option(keyword=search_kw, workgroup=WORKGROUP)
            filtered = [r for r in results if r.get("path", "") == leg_path]
            if filtered:
                r = filtered[0]
                cats = repo.get_all_option_categories()
                cat_map = {c["optno"]: c["optdesc"] for c in cats}
                return {
                    "optno": "B001",
                    "optdesc": cat_map.get("B001", "木腳"),
                    "path": r["path"],
                    "code": r["code"],
                    "codsc": r["codsc"],
                    "compri": r["compri"],
                }
    return None


# ============================================================
# 意圖識別
# ============================================================

def is_confirm(text: str) -> bool:
    """使用者是否表示確認"""
    t = text.strip()
    return any(kw in t for kw in CONFIRM_KEYWORDS)


def is_cancel(text: str) -> bool:
    """使用者是否表示取消"""
    t = text.strip()
    return any(kw in t for kw in CANCEL_KEYWORDS)


# ============================================================
# 主解析函式：從使用者輸入更新報價草稿
# ============================================================

def parse_and_update(user_input: str, quote_draft: dict) -> tuple[dict, list[str]]:
    """
    解析使用者輸入，擷取所有可識別的欄位並更新報價草稿。
    selections 以 optno 為 key。

    Args:
        user_input  : 使用者原始輸入
        quote_draft : 目前報價草稿（會被就地更新）

    Returns:
        (updated_draft, found_fields)
        found_fields: 本次成功擷取的欄位名稱清單（用於回覆使用者）
    """
    found: list[str] = []

    # 數量
    qty = extract_quantity(user_input)
    if qty is not None:
        quote_draft["qty"] = qty
        found.append(f"數量：{qty} 張")

    # 尺寸（A001）
    size = extract_size(user_input)
    if size:
        quote_draft.setdefault("selections", {})[size["optno"]] = size
        found.append(f"桌面尺寸：{size['codsc']}")

    # 板材（S004，CMT1\\A001\\S004）
    board = extract_board(user_input)
    if board:
        quote_draft.setdefault("selections", {})[board["optno"]] = board
        found.append(f"板材：{board['codsc']}")

    # 色紙（S001，CMT1\\A001\\S005\\S001）
    color = extract_color(user_input)
    if color:
        quote_draft.setdefault("selections", {})[color["optno"]] = color
        found.append(f"色紙：{color['codsc']}")

    # 腳架（B001 / B002）
    leg = extract_leg(user_input)
    if leg:
        quote_draft.setdefault("selections", {})[leg["optno"]] = leg
        found.append(f"腳架：{leg['codsc']}")

    return quote_draft, found


# ============================================================
# 產生缺項提示文字
# ============================================================

def format_missing_prompt(missing_fields: list[str], current_draft: dict) -> str:
    """
    依缺少的欄位清單產生詢問提示。

    Returns:
        str 提示訊息
    """
    selections = current_draft.get("selections", {})
    confirmed: list[str] = []

    if current_draft.get("qty"):
        confirmed.append(f"數量：{current_draft['qty']} 張")

    for optno, sel in selections.items():
        optdesc = sel.get("optdesc", optno)
        codsc = sel.get("codsc", "")
        confirmed.append(f"{optdesc}：{codsc}")

    lines: list[str] = []

    if confirmed:
        lines.append("✅ 已確認：" + "、".join(confirmed))

    if missing_fields:
        lines.append("")
        lines.append("⚠️ 還缺少以下資訊：")
        for i, f in enumerate(missing_fields, start=1):
            lines.append(f"  {i}. {f}")
        lines.append("")
        lines.append("請繼續補充，或回覆「使用標準規格」套用預設值。")

    return "\n".join(lines)
