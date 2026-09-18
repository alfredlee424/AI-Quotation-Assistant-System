"""規則模式的尺寸、部件上下文、移除及歧義契約。"""
from copy import deepcopy

import pytest

from agent.core import RuleBasedAgent
from agent.rule_parser import extract_size, extract_quantity, is_cancel, is_confirm, parse_and_update, select_candidate
from engine.configuration import apply_proposal
from engine.preview import freeze_preview


@pytest.mark.parametrize("text,code", [("60*180", "C005"), ("60cm×180cm", "C005"),
    ("600x1800mm", "C005"), ("600毫米*1800毫米", "C005"), ("1200x600", "C003"),
    ("60*120公分", "C003"), ("桌面60*180，線盒40*13.5銀鋁單掀", "C005")])
def test_cm_mm_dimensions(cmt1_db, text, code):
    result = extract_size(text)
    assert result["path"] == r"CMT1\A001"
    assert result["code"] == code


def test_tabletop_and_accessory_in_same_sentence(current_draft):
    draft, _ = parse_and_update("桌面60*120搭配40*13.5 銀鋁單掀", current_draft)
    assert draft["selections"][r"CMT1\A001"]["code"] == "C003"
    assert draft["selections"][r"CMT1\A001\S019\S020"]["code"] == "C001"


@pytest.mark.parametrize("text,expected", [("3張桌子每張2個圓孔", 3), ("每張2個圓孔", None),
    ("40*13.5銀鋁單掀", None), ("2個圓孔", None), ("桌子數量改成5", 5), ("數量改成4", 4)])
def test_contextual_product_quantity(text, expected):
    assert extract_quantity(text) == expected


def test_accessory_quantity_does_not_change_product_qty(current_draft):
    current_draft["qty"] = 3
    draft, _ = parse_and_update("每張2個6公分圓孔", current_draft)
    assert draft["qty"] == 3
    assert draft["selections"][r"CMT1\A001\S019\S030"]["line_qty"] == 2


def test_ambiguous_accessory_total_waits_without_preview(current_draft):
    freeze_preview(current_draft)
    message, draft = RuleBasedAgent().run("加2個6公分圓孔", current_draft, [])
    assert draft["qty"] == 1
    assert not draft.get("preview")
    assert draft["status"] == "WAITING_FOR_INPUT"
    assert draft.get("questions")


def test_removal_is_not_global_cancel_and_does_not_select_first(current_draft):
    path = r"CMT1\A001\S019\S020"
    apply_proposal(current_draft, {"changes": [{"op": "set", "path": path, "code": "C001"}]})
    before_root = deepcopy(current_draft["selections"][r"CMT1\A001"])
    text = "取消40*13.5 銀鋁單掀"
    assert not is_cancel(text)
    _, draft = RuleBasedAgent().run(text, current_draft, [])
    assert path not in draft["selections"]
    assert draft["selections"][r"CMT1\A001"]["code"] == before_root["code"]
    assert draft["qty"] == 1


def test_ambiguous_candidates_do_not_silently_choose_first(current_draft):
    path = r"CMT1\A001\S019\S020"
    _, draft = RuleBasedAgent().run("銀鋁單掀", current_draft, [])
    assert path not in draft["selections"]
    assert len(draft["pending_options"]) >= 2
    assert not draft.get("preview")
    assert select_candidate("確認", draft["pending_options"]) is None
    assert select_candidate("1", draft["pending_options"]) == draft["pending_options"][0]


def test_confirm_prefix_is_not_exact_confirmation():
    assert is_confirm("確認")
    assert not is_confirm("確認但尺寸改成60*120")
    assert is_cancel("取消報價")
    assert not is_cancel("不要木腳")


def test_no_wood_leg_removes_leg_branch_not_the_whole_quote(current_draft):
    _, draft = RuleBasedAgent().run("不要木腳", current_draft, [])
    assert not any(path.startswith(r"CMT1\B001") for path in draft["selections"])
    assert draft["selections"][r"CMT1\A001"]["code"] == "C005"
    assert draft["qty"] == 1
    assert draft["status"] == "PREVIEW"
    assert draft["calc_result"]["total_cost"] == 2457.75


def test_explicit_full_path_removal_never_removes_ancestor(current_draft):
    path = r"CMT1\A001\S019\S020"
    apply_proposal(current_draft, {"changes": [{"op": "set", "path": path, "code": "C001"}]})
    before = deepcopy(current_draft["selections"])
    _, draft = RuleBasedAgent().run("移除 " + path, current_draft, [])
    assert path not in draft["selections"]
    assert draft["selections"][r"CMT1\A001"]["code"] == before[r"CMT1\A001"]["code"]
    assert draft["selections"][r"CMT1\A001\S005\S007"]["code"] == "C001"
    assert draft["selections"][r"CMT1\B001"]["code"] == "C001"
    assert draft["status"] == "PREVIEW"
