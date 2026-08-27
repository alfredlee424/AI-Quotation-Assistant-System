"""
agent/rule_parser.py - 規則式自然語言解析器

無需 LLM API 即可從使用者輸入中擷取：
  - 數量（20張、100張、20個）
  - 桌面尺寸（60*120、75*180、120x75）
  - 材質關鍵字（美耐板、實木、玻璃）→ 查詢對應 optno=S002
  - 腳架關鍵字（木腳、鐵腳）→ 查詢對應 optno=B001/B002
  - 顏色關鍵字（白色、黑色、胡桃）→ 查詢對應 optno=S005
  - 確認意圖（是、確認、建立、OK）
  - 取消意圖（否、取消、不要）

並查詢資料庫將關鍵字轉成正式代碼（optno 驅動）。

selections 格式（以 optno 為 key）：
    {
        "A001": {"optno": "A001", "optdesc": "桌面尺寸",
                 "path": "CMT1\\A001", "code": "C001", "codsc": "60*120", "compri": 0.0},
        "S002": {"optno": "S002", "optdesc": "材質",
                 "path": "CMT1\\S002", "code": "C001", "codsc": "美耐板", "compri": 500.0},
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

# 材質關鍵字（搜尋 ordspe，path 含 optno=S002）
MATERIAL_KEYWORDS: dict[str, str] = {
    "美耐板": "美耐板",
    "美耐": "美耐板",
    "防火板": "美耐板",
    "實木": "實木貼皮",
    "木紋": "實木貼皮",
    "貼皮": "實木貼皮",
    "玻璃": "強化玻璃",
    "強化玻璃": "強化玻璃",
    "密集板": "密集板",
    "塑合板": "密集板",
}

# 顏色關鍵字（搜尋 ordspe，path 含 optno=S005）
COLOR_KEYWORDS: dict[str, str] = {
    "白色": "白色",
    "白": "白色",
    "純白": "白色",
    "黑色": "黑色",
    "黑": "黑色",
    "米色": "米色",
    "米": "米色",
    "米白": "米色",
    "胡桃": "胡桃木色",
    "胡桃木": "胡桃木色",
    "木色": "胡桃木色",
}

# 腳架關鍵字（搜尋 ordspe，path 含 optno=B001/B002）
LEG_KEYWORDS: dict[str, str] = {
    "木腳": "木腳",
    "木製腳": "木腳",
    "實木腳": "木腳",
    "鐵腳": "鐵腳",
    "金屬腳": "鐵腳",
    "鋼管腳": "鋼管腳",
    "鋼管": "鋼管腳",
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
# 材質解析（optno=S002）
# ============================================================

def extract_material(text: str) -> Optional[dict]:
    """從文字擷取桌面材質（optno=S002）並查詢正式代碼"""
    for kw, search_kw in MATERIAL_KEYWORDS.items():
        if kw in text:
            result = _lookup_option_by_optno(search_kw, "S002")
            if result:
                return result
    return None


# ============================================================
# 顏色解析（optno=S005）
# ============================================================

def extract_color(text: str) -> Optional[dict]:
    """從文字擷取顏色（optno=S005）並查詢正式代碼"""
    for kw, search_kw in COLOR_KEYWORDS.items():
        if kw in text:
            result = _lookup_option_by_optno(search_kw, "S005")
            if result:
                return result
    return None


# ============================================================
# 腳架解析（optno=B001 木腳 / B002 鐵腳）
# ============================================================

def extract_leg(text: str) -> Optional[dict]:
    """從文字擷取腳架類型（optno=B001 或 B002）並查詢正式代碼"""
    for kw, search_kw in LEG_KEYWORDS.items():
        if kw in text:
            # 先找 B001（木腳類），再找 B002（鐵腳類）
            result = (
                _lookup_option_by_optno(search_kw, "B001") or
                _lookup_option_by_optno(search_kw, "B002")
            )
            if result:
                return result
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

    # 材質（S002）
    material = extract_material(user_input)
    if material:
        quote_draft.setdefault("selections", {})[material["optno"]] = material
        found.append(f"材質：{material['codsc']}")

    # 顏色（S005）
    color = extract_color(user_input)
    if color:
        quote_draft.setdefault("selections", {})[color["optno"]] = color
        found.append(f"顏色：{color['codsc']}")

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
