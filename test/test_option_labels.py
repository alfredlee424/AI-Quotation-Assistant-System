"""短選單、舊版回答與尺寸顯示的相容性。"""
from copy import deepcopy

import pytest

from agent.rule_parser import format_missing_prompt, format_candidate_confirmation, select_candidate
from utils.option_labels import display_text, menu_labels, short_part, open_question


@pytest.mark.parametrize("label, expected", [
    ("桌面尺寸／顏色材質", "桌面材質"),
    ("木腳尺寸／顏色材質", "木腳材質"),
    ("桌面尺寸／桌面外型", "桌面外型"),
    ("桌面尺寸／顏色材質／下板", "桌面下板"),
    ("木腳尺寸／工費／裁切費", "木腳裁切費"),
])
def test_short_part(label, expected):
    assert short_part(label) == expected


def test_prompt_only_shows_next_group_and_no_redundant_paths():
    path = r"CMT1\A001\S010"
    options = [{"path": path, "code": "C001", "codsc": "平直", "display_path": "桌面尺寸／桌面外型"},
               {"path": path, "code": "C002", "codsc": "平彎", "display_path": "桌面尺寸／桌面外型"}]
    draft = {"qty": 1, "option_labels": {path: "桌面尺寸／桌面外型"}}
    message = format_missing_prompt(["桌面外型", "木腳貼合費"], draft, {path: options})
    assert message == "請選擇桌面外型：\n\n1. 平直\n2. 平彎\n\n回覆編號或名稱即可。"
    assert "木腳" not in message
    assert "產品數量" not in message


@pytest.mark.parametrize("value", ["60*180", "60x180", "60 X 180", "60＊180"])
def test_dimensions_are_visible_and_copied_menu_remains_selectable(value):
    candidate = {"path": r"CMT1\A001", "code": "C005", "codsc": value, "display_path": "桌面尺寸"}
    before = deepcopy(candidate)
    assert display_text(value) == "60×180"
    assert menu_labels([candidate]) == ["60×180"]
    assert select_candidate("1. 60×180", [candidate]) == candidate
    assert select_candidate("60×180", [candidate]) == candidate
    assert candidate == before


def test_duplicate_names_keep_part_context_and_old_long_label_still_works():
    candidates = [
        {"path": r"CMT1\A001\S005\S001", "code": "C001", "codsc": "817胡桃木", "display_path": "桌面尺寸／顏色材質／美耐皿顏色"},
        {"path": r"CMT1\B001\S005\S001", "code": "C001", "codsc": "817胡桃木", "display_path": "木腳尺寸／顏色材質／美耐皿顏色"},
    ]
    labels = menu_labels(candidates)
    assert "桌面" in labels[0] and "木腳" in labels[1]
    assert "／" not in format_candidate_confirmation(candidates)
    assert select_candidate(labels[1], candidates) == candidates[1]
    assert select_candidate("817胡桃木（木腳尺寸／顏色材質／美耐皿顏色）", candidates) == candidates[1]
    assert select_candidate("817胡桃木", candidates) is None


def test_actual_question_is_visible_but_model_prices_are_not():
    question = "請確認圓孔總數還是每張用量？"
    assert open_question(question, {}) == question
    assert "999999" not in open_question("模型捏造價格999999", {})
    path = r"CMT1\A001\S010"
    assert open_question(f"請選擇 {path}", {path: "桌面尺寸／桌面外型"}) == "請選擇 桌面外型"


def test_missing_quantity_without_candidates_asks_quantity_only():
    assert format_missing_prompt(["數量"], {}, {}) == "請問需要幾件產品？"
