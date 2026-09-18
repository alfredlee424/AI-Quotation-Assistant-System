"""
test/test_calculator_ordqty_rule.py
====================================
驗證 ordqty 用量查詢修正後的正確性。

測試策略：
  - 以 ordqty 規則為準（非 ordopt 快照）
  - 使用真實四主檔的巢狀樹結構（CMT1\\A001\\S004 等）
  - 重點驗證：_get_root_optno / _get_driver_code / calculate_from_draft

已驗證資料來源（logs/ss.txt）：
  QU26731001-01001（A001 選 C005=60*180）：
    CMT1\\A001\\S004 + code=C005 → stdqty=4.5
  QU26824014-01001（B001 選 C001=45寬環式腳）：
    CMT1\\B001\\S004 + code=C001 → stdqty=8.0
    CMT1\\B001\\S005\\S008 + code=C001 → stdqty=4.0
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from engine.calculator import (
    _get_root_optno,
    _get_driver_code,
    calculate_from_draft,
    CalcResult,
)


# ============================================================
# 1. 輔助函式：_get_root_optno
# ============================================================

class TestGetRootOptno:
    """驗證由子部件路徑正確取出根節點 optno。"""

    def test_root_path_itself(self):
        """根節點路徑本身 → 回傳自身 optno"""
        assert _get_root_optno("CMT1\\A001") == "A001"
        assert _get_root_optno("CMT1\\B001") == "B001"

    def test_one_level_child(self):
        """一層子部件 → 回傳根 optno"""
        assert _get_root_optno("CMT1\\A001\\S004") == "A001"
        assert _get_root_optno("CMT1\\B001\\S004") == "B001"

    def test_two_level_child(self):
        """兩層子部件 → 仍回傳根 optno"""
        assert _get_root_optno("CMT1\\A001\\S005\\S001") == "A001"
        assert _get_root_optno("CMT1\\B001\\S005\\S008") == "B001"

    def test_three_level_child(self):
        """三層子部件 → 仍回傳根 optno"""
        assert _get_root_optno("CMT1\\A001\\W001\\W010") == "A001"
        assert _get_root_optno("CMT1\\A001\\S005\\S000\\S006") == "A001"

    def test_custom_prefix(self):
        """自定義前綴"""
        assert _get_root_optno("DT1\\A001\\S004", prefix="DT1") == "A001"

    def test_empty_path(self):
        """空路徑 → 回傳空字串（不 crash）"""
        assert _get_root_optno("") == ""

    def test_path_without_prefix(self):
        """路徑不含前綴 → 回傳第一段"""
        result = _get_root_optno("A001\\S004", prefix="CMT1")
        # 不含前綴時取第一段 = "A001"
        assert result == "A001"


# ============================================================
# 2. 輔助函式：_get_driver_code
# ============================================================

class TestGetDriverCode:
    """驗證由 selections 正確取出驅動根節點所選代碼。"""

    SELECTIONS = {
        "A001": {"path": "CMT1\\A001", "code": "C005", "codsc": "60*180"},
        "S004": {"path": "CMT1\\A001\\S004", "code": "C001", "codsc": "25mm MDF"},
        "B001": {"path": "CMT1\\B001", "code": "C001", "codsc": "45寬環式腳"},
        "S008": {"path": "CMT1\\B001\\S005\\S008", "code": "C001", "codsc": "胡桃紙"},
    }

    def test_root_node_returns_own_code(self):
        """根節點路徑 → 驅動代碼即為自身 code"""
        code = _get_driver_code("CMT1\\A001", self.SELECTIONS)
        assert code == "C005"

    def test_a001_tree_child_uses_a001_code(self):
        """A001 樹下子部件 → 驅動代碼用 A001 所選 C005（非 S004 自身的 C001）"""
        code = _get_driver_code("CMT1\\A001\\S004", self.SELECTIONS)
        assert code == "C005"   # A001 選的尺寸代碼

    def test_b001_tree_child_uses_b001_code(self):
        """B001 樹下子部件 → 驅動代碼用 B001 所選 C001"""
        code = _get_driver_code("CMT1\\B001\\S004", self.SELECTIONS)
        assert code == "C001"   # B001 選的腳型代碼

    def test_deep_nested_still_uses_root_code(self):
        """深層巢狀子部件 → 仍使用根節點代碼"""
        code = _get_driver_code("CMT1\\B001\\S005\\S008", self.SELECTIONS)
        assert code == "C001"   # B001 選的腳型代碼

    def test_root_not_in_selections_fallback(self):
        """根節點不在 selections → 回傳空字串（呼叫端自行 fallback）"""
        code = _get_driver_code("CMT1\\X999\\S004", self.SELECTIONS)
        assert code == ""


# ============================================================
# 3. calculate_from_draft：整合驗證
# ============================================================

class TestCalculateFromDraftOrdqtyRule:
    """新入口查整條路徑的 rules，再依 part_path 選根代碼，不允許靜默 fallback。"""

    def test_get_part_quantity_called_with_driver_code(self, current_draft, monkeypatch):
        from database import repository as repo
        original = repo.get_quantity_rules
        spy = MagicMock(wraps=original)
        monkeypatch.setattr(repo, "get_quantity_rules", spy)
        result = calculate_from_draft(current_draft)
        spy.assert_any_call(r"CMT1\A001\S004", workgroup="103")
        item = next(i for i in result.items if i["path"] == r"CMT1\A001\S004")
        assert item["driver_path"] == r"CMT1\A001"
        assert item["driver_code"] == "C005"
        assert item["spc_code"] == "C001"

    def test_stdqty_correctly_affects_cost(self, current_draft):
        result = calculate_from_draft(current_draft)
        assert isinstance(result, CalcResult)
        item = next(i for i in result.items if i["path"] == r"CMT1\A001\S004")
        assert (item["stdqty"], item["stdpar"], item["part_cost"]) == (4.5, 24, 131.25)

    def test_missing_stdqty_rejects_instead_of_fallback(self, current_draft, monkeypatch):
        from database import repository as repo
        monkeypatch.setattr(repo, "get_quantity_rules", lambda *args, **kw: [])
        with pytest.raises(ValueError, match="缺少用量規則"):
            calculate_from_draft(current_draft)

    def test_b001_tree_uses_b001_driver_code(self, current_draft):
        result = calculate_from_draft(current_draft)
        item = next(i for i in result.items if i["path"] == r"CMT1\B001\S004")
        assert item["driver_path"] == r"CMT1\B001"
        assert item["driver_code"] == "C001"
        assert (item["stdqty"], item["stdpar"], item["part_cost"]) == (8, 120, 32)


# ============================================================
# 4. 純函式邊界測試（不需 mock）
# ============================================================

class TestHelperEdgeCases:
    """_get_root_optno / _get_driver_code 邊界條件。"""

    def test_get_root_optno_single_backslash(self):
        assert _get_root_optno("CMT1\\A001") == "A001"

    def test_get_root_optno_no_separator(self):
        """路徑無分隔符時，整段作為 optno"""
        result = _get_root_optno("CMT1A001", prefix="CMT1")
        # 去掉前綴後 = "A001"
        assert result == "A001"

    def test_get_driver_code_empty_selections(self):
        """空 selections → 回傳空字串"""
        code = _get_driver_code("CMT1\\A001\\S004", {})
        assert code == ""

    def test_get_driver_code_root_without_children(self):
        """根節點本身無子部件時，驅動代碼 = 自身 code"""
        selections = {"A001": {"path": "CMT1\\A001", "code": "C999"}}
        code = _get_driver_code("CMT1\\A001", selections)
        assert code == "C999"
