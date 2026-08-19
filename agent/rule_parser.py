"""
agent/rule_parser.py - 規則式自然語言解析器

無需 LLM API 即可從使用者輸入中擷取：
  - 數量（20張、100張、20個）
  - 桌面尺寸（1200x600、1500×600）
  - 桌面材質（美耐板、實木、玻璃）
  - 顏色（白色、黑色、米色、胡桃）
  - 腳架類型（木腳、金屬腳、鋼管）
  - 確認意圖（是、確認、建立、OK）
  - 取消意圖（否、取消、不要）

並查詢資料庫將關鍵字轉成正式代碼。
"""

from __future__ import annotations

import re
from typing import Optional

from database import repository as repo
from config import WORKGROUP


# ============================================================
# 關鍵字對映表（自然語言 → 資料庫搜尋關鍵字）
# ============================================================

# 材質對映
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

# 顏色對映
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

# 腳架對映
LEG_KEYWORDS: dict[str, str] = {
    "木腳": "木腳",
    "木製腳": "木腳",
    "實木腳": "木腳",
    "金屬腳": "金屬腳",
    "金屬": "金屬腳",
    "鐵腳": "金屬腳",
    "鋼管腳": "鋼管腳",
    "鋼管": "鋼管腳",
    "U型腳": "U型腳",
    "U形腳": "U型腳",
    "u腳": "U型腳",
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
    # 匹配「數字 + 量詞」如「20張」「100個」「50件」「20套」
    m = re.search(r"(\d+)\s*[張個件套台]", text)
    if m:
        return int(m.group(1))

    # 純數字（例如使用者說「20」）
    m = re.search(r"(\d+)", text)
    if m:
        return int(m.group(1))

    return None


# ============================================================
# 尺寸解析
# ============================================================

def extract_size(text: str) -> Optional[dict]:
    """
    從文字中擷取桌面尺寸，並查詢對應代碼。
    支援：「1200x600」「1200×600」「1200*600」「1200X600」

    Returns:
        dict {path, code, codsc} 或 None
    """
    # 匹配 WxH 格式
    m = re.search(r"(\d{3,4})\s*[xX×*]\s*(\d{3,4})", text)
    if not m:
        return None

    w, h = m.group(1), m.group(2)
    keyword = f"{w}×{h}"

    # 先嘗試精確搜尋，再模糊搜尋
    results = repo.search_option(keyword=keyword, workgroup=WORKGROUP)
    if not results:
        results = repo.search_option(keyword=f"{w}", workgroup=WORKGROUP)

    # 過濾出尺寸路徑
    size_results = [r for r in results if "SIZE" in r.get("path", "")]
    if size_results:
        r = size_results[0]
        return {"path": r["path"], "code": r["code"], "codsc": r["codsc"]}

    return None


# ============================================================
# 通用選項解析（材質、顏色、腳架）
# ============================================================

def _lookup_option(keyword: str, path_filter: str) -> Optional[dict]:
    """
    查詢資料庫中符合關鍵字且路徑包含 path_filter 的選項。

    Returns:
        dict {path, code, codsc} 或 None
    """
    results = repo.search_option(keyword=keyword, workgroup=WORKGROUP)
    filtered = [r for r in results if path_filter in r.get("path", "")]
    if filtered:
        r = filtered[0]
        return {"path": r["path"], "code": r["code"], "codsc": r["codsc"]}
    return None


def extract_material(text: str) -> Optional[dict]:
    """從文字擷取桌面材質並轉成正式代碼"""
    for kw, search_kw in MATERIAL_KEYWORDS.items():
        if kw in text:
            return _lookup_option(search_kw, "MATS")
    return None


def extract_color(text: str) -> Optional[dict]:
    """從文字擷取顏色並轉成正式代碼"""
    for kw, search_kw in COLOR_KEYWORDS.items():
        if kw in text:
            return _lookup_option(search_kw, "COLOR")
    return None


def extract_leg(text: str) -> Optional[dict]:
    """從文字擷取腳架類型並轉成正式代碼"""
    for kw, search_kw in LEG_KEYWORDS.items():
        if kw in text:
            return _lookup_option(search_kw, "LEG")
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

    # 尺寸
    size = extract_size(user_input)
    if size:
        quote_draft.setdefault("selections", {})["size"] = size
        found.append(f"桌面尺寸：{size['codsc']}")

    # 材質
    material = extract_material(user_input)
    if material:
        quote_draft.setdefault("selections", {})["material"] = material
        found.append(f"桌面材質：{material['codsc']}")

    # 顏色
    color = extract_color(user_input)
    if color:
        quote_draft.setdefault("selections", {})["color"] = color
        found.append(f"顏色：{color['codsc']}")

    # 腳架
    leg = extract_leg(user_input)
    if leg:
        quote_draft.setdefault("selections", {})["leg"] = leg
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
    if "size" in selections:
        confirmed.append(f"尺寸：{selections['size']['codsc']}")
    if "material" in selections:
        confirmed.append(f"材質：{selections['material']['codsc']}")
    if "color" in selections:
        confirmed.append(f"顏色：{selections['color']['codsc']}")
    if "leg" in selections:
        confirmed.append(f"腳架：{selections['leg']['codsc']}")

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
