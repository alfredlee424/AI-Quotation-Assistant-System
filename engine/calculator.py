"""
engine/calculator.py - 報價計算引擎

所有金額計算集中於此模組，AI Agent 不得介入任何計算。

計算流程：
    部件成本 = 數量 × stdqty × compri
    材料總成本 = Σ 部件成本
    加成後售價 = 材料總成本 × (1 + MARKUP_RATE)
    折扣後售價 = 加成後售價 × (1 - discount_rate)
    含稅金額   = 折扣後售價 × (1 + TAX_RATE)

selections 格式（optno 驅動，對齊真實資料庫）：
    quote_draft["selections"] = {
        "A001": {
            "optno": "A001",
            "optdesc": "桌面尺寸",
            "path": "CMT1\\A001",
            "code": "C005",       # 使用者選的尺寸代碼
            "codsc": "60*180",
            "compri": 0.0,
        },
        "S004": {
            "optno": "S004",
            "optdesc": "板材",
            "path": "CMT1\\A001\\S004",   # 子部件路徑（A001 樹下）
            "code": "C001",               # 材質代碼（S004 自身）
            "codsc": "25mm MDF",
            "compri": 700.0,
        },
        "B001": {...},  # 木腳根節點
        ...
    }

路徑格式說明：
    ordspe.path = {PRODUCT_PREFIX}\\{optno}  （如 CMT1\\A001）
    子部件路徑  = {PRODUCT_PREFIX}\\{root_optno}\\{sub_optno}\\...（如 CMT1\\A001\\S004）

ordqty 查詢規則（已驗證）：
    ordqty.path = 子部件完整路徑（如 CMT1\\A001\\S004）
    ordqty.code = 產品樹根節點當下所選代碼（如 A001 選 C005 = 60*180）
    → 同一子部件的 stdqty 會因「選了哪個尺寸」而不同
    → 查詢時須用「驅動根節點所選代碼」，而非子部件自身代碼

    真實範例：
      get_part_quantity(path="CMT1\\A001\\S004", code="C005") → stdqty=4.5
      get_part_quantity(path="CMT1\\B001\\S004", code="C001") → stdqty=8.0

    若子部件路徑只有一層（如 CMT1\\B001），驅動節點即為自身。
    查詢不使用 codsc（真實 ordqty.codsc 全為 NULL）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from config import MARKUP_RATE, TAX_RATE, MAX_DISCOUNT_RATE, PRODUCT_PREFIX
from database import repository as repo


# ============================================================
# 資料結構
# ============================================================

@dataclass
class QuoteItem:
    """計算用報價明細項目（由 Agent 組裝後傳入計算引擎）"""
    part_code: str        # 項目代號（ordspe.code）
    part_desc: str        # 項目名稱（ordspe.codsc）
    path: str             # 選配路徑（ordspe.path，如 CMT1\\S002）
    spc_code: str         # 規格代號（同 code）
    spdsc: str            # 規格說明（同 codsc）
    qty: float            # 數量（使用者訂購數量）
    stdqty: float         # 標準用量（ordqty.stdqty）
    stdpar: float         # 裁切量（ordqty.stdpar）
    compri: float         # 採購成本（由 repository 即時取得）
    optno: str = ""       # 選項類別代碼（ordspd.optno，如 A001）
    optdesc: str = ""     # 選項類別說明（ordspd.optdesc，如 桌面尺寸）

    @property
    def part_cost(self) -> float:
        """單項部件成本 = 數量 × 標準用量 × 採購成本"""
        return self.qty * self.stdqty * self.compri


@dataclass
class CalcResult:
    """計算結果"""
    items: list[dict] = field(default_factory=list)
    total_cost: float = 0.0       # 材料總成本
    subtotal: float = 0.0         # 加成後小計（未折扣）
    discount_rate: float = 0.0    # 實際套用折扣率
    discount_amount: float = 0.0  # 折扣金額
    after_discount: float = 0.0   # 折扣後金額
    tax_rate: float = 0.0         # 稅率
    tax_amount: float = 0.0       # 稅額
    total_price: float = 0.0      # 含稅最終報價
    markup_rate: float = 0.0      # 加成率


# ============================================================
# 核心計算函式
# ============================================================

def calculate_quote(
    items: list[QuoteItem],
    discount_rate: float = 0.0,
    markup_rate: Optional[float] = None,
    tax_rate: Optional[float] = None,
) -> CalcResult:
    """
    依 QuoteItem 清單計算完整報價。

    Args:
        items         : 報價明細清單（已包含 compri、stdqty）
        discount_rate : 折扣率（0.0 ~ MAX_DISCOUNT_RATE）
        markup_rate   : 加成率（預設使用 config.MARKUP_RATE）
        tax_rate      : 稅率（預設使用 config.TAX_RATE）

    Returns:
        CalcResult 完整計算結果
    """
    _markup = markup_rate if markup_rate is not None else MARKUP_RATE
    _tax = tax_rate if tax_rate is not None else TAX_RATE

    # 折扣率上限保護
    _discount = min(discount_rate, MAX_DISCOUNT_RATE)

    # ── 計算各明細成本與單價 ──────────────────────────────
    result_items: list[dict] = []
    total_cost: float = 0.0

    for item in items:
        part_cost = item.part_cost
        unit_cost = part_cost / item.qty if item.qty else 0.0
        unit_price = unit_cost * (1 + _markup)
        amount = unit_price * item.qty

        result_items.append({
            "optno": item.optno,
            "optdesc": item.optdesc,
            "part_code": item.part_code,
            "part_desc": item.part_desc,
            "path": item.path,
            "spc_code": item.spc_code,
            "spdsc": item.spdsc,
            "qty": item.qty,
            "stdqty": item.stdqty,
            "stdpar": item.stdpar,
            "compri": item.compri,
            "part_cost": round(part_cost, 2),
            "unit_cost": round(unit_cost, 2),
            "unit_price": round(unit_price, 2),
            "amount": round(amount, 2),
            "unit": "PCS",
        })
        total_cost += part_cost

    # ── 整體計算 ─────────────────────────────────────────
    subtotal = total_cost * (1 + _markup)
    discount_amount = subtotal * _discount
    after_discount = subtotal - discount_amount
    tax_amount = after_discount * _tax
    total_price = after_discount + tax_amount

    return CalcResult(
        items=result_items,
        total_cost=round(total_cost, 2),
        subtotal=round(subtotal, 2),
        discount_rate=_discount,
        discount_amount=round(discount_amount, 2),
        after_discount=round(after_discount, 2),
        tax_rate=_tax,
        tax_amount=round(tax_amount, 2),
        total_price=round(total_price, 2),
        markup_rate=_markup,
    )


# ============================================================
# 輔助函式：推導驅動根節點
# ============================================================

def _get_root_optno(path: str, prefix: str = PRODUCT_PREFIX) -> str:
    """
    從子部件完整路徑取出產品樹根節點的 optno。

    規則：去掉路徑前綴後，取第一個 optno 段。

    範例：
        "CMT1\\A001"              → "A001"
        "CMT1\\A001\\S004"        → "A001"
        "CMT1\\A001\\S005\\S001"  → "A001"
        "CMT1\\B001\\S004"        → "B001"

    Args:
        path   : 子部件完整路徑（如 CMT1\\A001\\S004）
        prefix : 產品路徑前綴（如 CMT1）

    Returns:
        根節點 optno 字串（如 "A001"）；若解析失敗回傳空字串
    """
    stripped = path.strip()
    pfx = prefix.strip()
    # 去掉前綴與緊接的分隔符
    if stripped.startswith(pfx + "\\"):
        tail = stripped[len(pfx) + 1:]
    elif stripped.startswith(pfx):
        tail = stripped[len(pfx):]
    else:
        tail = stripped
    # 取第一段（即根節點 optno）
    return tail.split("\\")[0] if tail else ""


def _get_driver_code(path: str, selections: dict, prefix: str = PRODUCT_PREFIX) -> str:
    """
    取得指定子部件路徑所對應的「驅動根節點所選代碼」。

    ordqty 查詢規則（已用 QU26731001 / QU26824014 雙重驗證）：
        ordqty.code = 產品樹根節點（part_path）當下所選的代碼
        例：CMT1\\A001\\S004 的 stdqty，要用 A001 所選尺寸代碼（C005）查詢，
            而非 S004 自身的材質代碼（C001）。

    Args:
        path       : 子部件完整路徑（如 CMT1\\A001\\S004）
        selections : 報價草稿的 selections dict（以 optno 為 key）
        prefix     : 產品路徑前綴（如 CMT1）

    Returns:
        驅動根節點的選擇代碼字串；若根節點不在 selections 中，回傳空字串
    """
    root_optno = _get_root_optno(path, prefix)
    root_sel = selections.get(root_optno, {})
    return root_sel.get("code", "")


# ============================================================
# 從報價草稿（dict）計算
# ============================================================

def calculate_from_draft(quote_draft: dict) -> CalcResult:
    """
    從報價草稿（agent 維護的 current_quote dict）計算報價。

    quote_draft 格式（optno 驅動）：
    {
        "product_name": "辦公桌",
        "qty": 20,
        "selections": {
            "A001": {
                "optno": "A001",
                "optdesc": "桌面尺寸",
                "path": "CMT1\\A001",
                "code": "C005",         # 使用者選的尺寸代碼（驅動 A001 樹所有子部件）
                "codsc": "60*180",
                "compri": 0.0,
            },
            "S004": {
                "optno": "S004",
                "optdesc": "板材",
                "path": "CMT1\\A001\\S004",   # A001 樹的子部件
                "code": "C001",               # S004 自身選擇（材質）
                "codsc": "25mm MDF",
                "compri": 700.0,
            },
            "B001": {
                "optno": "B001",
                "optdesc": "木腳尺寸",
                "path": "CMT1\\B001",
                "code": "C001",         # 使用者選的木腳代碼（驅動 B001 樹所有子部件）
                "codsc": "45寬環式腳",
                "compri": 0.0,
            },
        },
        "discount_rate": 0.0,
    }

    ordqty 查詢修正說明：
        - 修正前（錯誤）：用每個選項自身的 code 查 ordqty
          → CMT1\\A001\\S004 用 code=C001（材質代碼），查不到正確 stdqty，fallback 為 1.0
        - 修正後（正確）：用子部件所屬產品樹根節點所選的 code 查 ordqty
          → CMT1\\A001\\S004 用 A001 的 code=C005（尺寸代碼），正確取得 stdqty=4.5
    """
    qty: float = float(quote_draft.get("qty", 1))
    selections: dict = quote_draft.get("selections", {})
    discount_rate: float = float(quote_draft.get("discount_rate", 0.0))

    items: list[QuoteItem] = []

    for optno, sel in selections.items():
        path = sel.get("path", "")
        code = sel.get("code", "")
        codsc = sel.get("codsc", "")
        optdesc = sel.get("optdesc", "")

        # ── 修正核心：取驅動根節點所選代碼查 ordqty ─────────────
        # 真實 ordqty 結構：ordqty.code = 驅動根節點（part_path）所選代碼
        # 例：CMT1\A001\S004 的 stdqty 必須用 A001 的 code（如 C005=60*180）查詢
        driver_code = _get_driver_code(path, selections)
        # 若找不到驅動代碼（根節點不在 selections），fallback 用自身 code
        lookup_code = driver_code if driver_code else code

        # 取得用量規則（不傳 codsc，因真實 ordqty.codsc 全為 NULL）
        qty_rule = repo.get_part_quantity(path=path, code=lookup_code)
        stdqty = qty_rule["stdqty"] if qty_rule else 1.0
        stdpar = qty_rule["stdpar"] if qty_rule else 1.0

        # 取得最新採購成本（快照前的主檔值），仍用部件自身 code 查 ordspe
        compri = repo.get_option_price(path=path, code=code)

        # compri = 0 時略過此項（無成本的結構節點，如桌面尺寸根節點、標準顏色）
        if compri == 0.0:
            continue

        items.append(QuoteItem(
            part_code=code,
            part_desc=codsc,
            path=path,
            spc_code=code,
            spdsc=codsc,
            qty=qty,
            stdqty=stdqty,
            stdpar=stdpar,
            compri=compri,
            optno=optno,
            optdesc=optdesc,
        ))

    return calculate_quote(items, discount_rate=discount_rate)
