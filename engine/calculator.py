"""
engine/calculator.py - 報價計算引擎

所有金額計算集中於此模組，AI Agent 不得介入任何計算。

計算流程：
    部件成本 = 數量 × stdqty ÷ stdpar × compri
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
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from config import MARKUP_RATE, TAX_RATE, MAX_DISCOUNT_RATE, PRODUCT_PREFIX
from database import repository as repo
from utils.logger import log_action


def _quo_rate_to_markup(quo_rate: Optional[float]) -> float:
    """
    將 invdoc.quo_rate（報價係數）轉為 calculate_quote 使用的加成率。

    語意：quo_rate 為「報價係數」，直接與成本相乘（如 1.3 表示售價=成本×1.3）。
    calculate_quote 內部公式為 unit_cost × (1 + markup_rate)，
    因此 markup_rate = quo_rate - 1（quo_rate=1 → markup=0；quo_rate=1.3 → markup=0.3）。

    當 quo_rate 為 None（invdoc.quo_rate 為 NULL）時，
    fallback 回 config.MARKUP_RATE。

    Args:
        quo_rate : invdoc.quo_rate（報價係數）；可能為 None

    Returns:
        float 加成率（供 calculate_quote 的 markup_rate 參數）
    """
    if quo_rate is None:
        return MARKUP_RATE
    return float(quo_rate) - 1.0


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
        """數量 × 用料量 ÷ 裁切量 × 採購單價；零用量合法，零分母不合法。"""
        from engine.configuration import number
        quantity = number(self.qty, "明細數量", positive=True)
        usage = number(self.stdqty, "用料量")
        divisor = number(self.stdpar, "裁切量", positive=True)
        price = number(self.compri, "採購單價")
        return float(Decimal(str(quantity)) * Decimal(str(usage)) / Decimal(str(divisor)) * Decimal(str(price)))


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
    from engine.configuration import number
    _markup = float(markup_rate if markup_rate is not None else MARKUP_RATE)
    number(1 + _markup, "報價係數", positive=True)
    _tax = number(tax_rate if tax_rate is not None else TAX_RATE, "稅率")
    _discount = number(discount_rate, "折扣率")
    if _discount > min(MAX_DISCOUNT_RATE, 1):
        raise ValueError("折扣超過允許範圍")
    money = lambda value: float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

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
            "part_cost": money(part_cost),
            "unit_cost": money(unit_cost),
            "unit_price": money(unit_price),
            "amount": money(amount),
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
        total_cost=money(total_cost),
        subtotal=money(subtotal),
        discount_rate=_discount,
        discount_amount=money(discount_amount),
        after_discount=money(after_discount),
        tax_rate=_tax,
        tax_amount=money(tax_amount),
        total_price=money(total_price),
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
    root_sel = next((item for item in selections.values() if item.get("path") == prefix + "\\" + root_optno), {})
    return root_sel.get("code", "")


# ============================================================
# 從報價草稿（dict）計算
# ============================================================

def calculate_from_draft(quote_draft: dict) -> CalcResult:
    """相容入口：所有新報價均須通過完整結構驗證。"""
    from engine.pricing import calculate_configuration
    return calculate_configuration(quote_draft)[0]


# ============================================================
# 必選項目完整性檢查（以 ordstr.must_chose 為權威來源）
# ============================================================

def check_required_selections(prodkind: str, selections: dict) -> list[dict]:
    from engine.configuration import resolve_configuration
    resolved = resolve_configuration({"prodkind": prodkind, "selections": selections})
    if resolved["errors"]:
        raise ValueError("；".join(resolved["errors"]))
    return [{"pathc": path, "optnoc": path.rsplit("\\", 1)[-1], "dmark": path}
            for path in resolved["allowed_options"]]


# ============================================================
# 以 ordstr 結構樹計價（改善後流程）
# ============================================================

def calculate_from_ordstr(
    prodkind: str, selections: dict, qty: float = 1.0, discount_rate: float = 0.0,
) -> CalcResult:
    """相容入口：完整路徑配置、條件分支、驅動尺寸與工費共用同一解析器。"""
    return calculate_from_draft({"prodkind": prodkind, "selections": selections,
                                 "qty": qty, "discount_rate": discount_rate})
