"""
engine/calculator.py - 報價計算引擎

所有金額計算集中於此模組，AI Agent 不得介入任何計算。

計算流程：
    部件成本 = 數量 × stdqty × compri
    材料總成本 = Σ 部件成本
    加成後售價 = 材料總成本 × (1 + MARKUP_RATE)
    折扣後售價 = 加成後售價 × (1 - discount_rate)
    含稅金額   = 折扣後售價 × (1 + TAX_RATE)

資料結構：
    QuoteItem（計算用）：
        part_code   : str    部件代碼
        part_desc   : str    部件名稱
        path        : str    選配路徑
        spc_code    : str    規格代碼
        spdsc       : str    規格說明
        qty         : float  數量
        stdqty      : float  標準用量
        stdpar      : float  裁切量
        compri      : float  採購成本（由 repository 取得）

    CalcResult（計算結果）：
        items           : list[dict]  各明細成本、單價、金額
        total_cost      : float       材料總成本
        total_price     : float       含稅最終報價
        subtotal        : float       加成後小計
        discount_amount : float       折扣金額
        tax_amount      : float       稅額
        markup_rate     : float       加成率
        discount_rate   : float       折扣率
        tax_rate        : float       稅率
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from config import MARKUP_RATE, TAX_RATE, MAX_DISCOUNT_RATE
from database import repository as repo


# ============================================================
# 資料結構
# ============================================================

@dataclass
class QuoteItem:
    """計算用報價明細項目（由 Agent 組裝後傳入計算引擎）"""
    part_code: str
    part_desc: str
    path: str
    spc_code: str
    spdsc: str
    qty: float
    stdqty: float
    stdpar: float
    compri: float          # 採購成本（由 repository 即時取得）
    codsc: str = ""        # ordspe.codsc（對映 ordqty 用）

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
# 從報價草稿（dict）計算
# ============================================================

def calculate_from_draft(quote_draft: dict) -> CalcResult:
    """
    從報價草稿（agent 維護的 current_quote dict）計算報價。

    quote_draft 格式：
    {
        "product_name": "辦公桌",
        "qty": 20,
        "selections": {
            "size":     {"path": "DESK\\SIZE", "code": "S1200600", "codsc": "1200×600mm"},
            "material": {"path": "DESK\\MATS", "code": "CLMT",     "codsc": "美耐板"},
            "color":    {"path": "DESK\\COLOR","code": "WHITE",     "codsc": "白色"},
            "leg":      {"path": "DESK\\LEG",  "code": "WLEG",      "codsc": "木腳"},
        },
        "discount_rate": 0.0,
    }
    """
    qty: float = float(quote_draft.get("qty", 1))
    selections: dict = quote_draft.get("selections", {})
    discount_rate: float = float(quote_draft.get("discount_rate", 0.0))

    items: list[QuoteItem] = []

    for sel_key, sel in selections.items():
        path = sel.get("path", "")
        code = sel.get("code", "")
        codsc = sel.get("codsc", "")

        # 取得用量規則
        qty_rule = repo.get_part_quantity(path=path, code=code, codsc=codsc)
        stdqty = qty_rule["stdqty"] if qty_rule else 1.0
        stdpar = qty_rule["stdpar"] if qty_rule else 1.0

        # 取得最新採購成本（快照前的主檔值）
        compri = repo.get_option_price(path=path, code=code, codsc=codsc)

        # 顏色等 compri=0 的選項不計入成本
        if compri == 0.0 and sel_key == "color":
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
            codsc=codsc,
        ))

    # 加入共用部件（橫樑、螺絲）
    common_parts = [
        {"path": "DESK\\PARTS", "code": "BEAM",  "codsc": "橫樑"},
        {"path": "DESK\\PARTS", "code": "SCREW", "codsc": "螺絲組"},
    ]
    for cp in common_parts:
        qty_rule = repo.get_part_quantity(**cp)
        compri = repo.get_option_price(**cp)
        if compri == 0.0:
            continue
        stdqty = qty_rule["stdqty"] if qty_rule else 1.0
        stdpar = qty_rule["stdpar"] if qty_rule else 1.0
        items.append(QuoteItem(
            part_code=cp["code"],
            part_desc=cp["codsc"],
            path=cp["path"],
            spc_code=cp["code"],
            spdsc=cp["codsc"],
            qty=qty,
            stdqty=stdqty,
            stdpar=stdpar,
            compri=compri,
        ))

    return calculate_quote(items, discount_rate=discount_rate)
