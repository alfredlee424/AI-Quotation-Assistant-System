"""純展示不產生數字或權限；真實固定內容逐列對照及邊界文字。"""
from copy import deepcopy
import json

import pytest

from acceptance import business, presentation as p
from test_acceptance_ui import worker


@pytest.fixture(scope="module")
def fixed_views(tmp_path_factory):
    return worker(tmp_path_factory.mktemp("presentation").resolve() / "private", '''
        app = prepared(initialized())
        preview = created(app)
        no_pricing = True
        verified_tables(app)
        click(app, "save")
        saved = verified_tables(app)
        emit({"preview": preview, "saved": saved})
    ''')


@pytest.mark.parametrize("phase", ["preview", "saved"])
@pytest.mark.parametrize("position", range(4))
def test_every_fixed_column_row_header_and_amount(fixed_views, phase, position):
    view = fixed_views[phase]
    before = deepcopy(view)
    summary = p.fixed_summary(view)
    child = view["document"]["children"][position]["fixed_child"]
    actual = summary["children"][position]
    for raw, row in zip(child["rows"], actual["rows"], strict=True):
        for label, key in p.COST_COLUMNS:
            assert row[label] == p.display(raw[key])
        assert row["本張總項目數"] == raw["qty"]
    assert actual["header"]["成品數"] == str(child["product_qty"])
    assert actual["header"]["操作人（自填）"] == view["document"]["context"]["actor"]
    assert actual["header"]["事業別"] == view["document"]["fixed_request"]["workgroup"]
    assert actual["header"]["來源程式（固定來源標記）"] == view["document"]["fixed_request"]["origin"]
    assert actual["header"]["固定內容建立時間"] == view["document"]["created_at"]
    assert actual["header"]["快照保存時間"] == (view["saved"]["created_at"] if phase == "saved" else p.NOT_CREATED)
    for row, (label, key) in zip(actual["amounts"], p.AMOUNT_COLUMNS, strict=True):
        assert row == {"金額項目": label, "固定值": child["amounts"][key]}
    for label, key in p.MONEY_COLUMNS:
        assert summary["totals"][label] == json.loads(view["document"]["fixed_request"]["totals_json"])[key]
    assert view == before


@pytest.mark.parametrize("value,expected", [(None, p.MISSING), ("", p.EMPTY), ("0", "0"),
    (0, "0"), ("0.00", "0.00"), ("1.000000000000000001", "1.000000000000000001"),
    ("999999999999999999.123456789012345678", "999999999999999999.123456789012345678")])
def test_zero_missing_empty_and_precision_never_coerced(value, expected):
    row = {key: value for _, key in p.COST_COLUMNS}
    result = p.cost_rows({"rows": [row]})
    assert result == [{label: expected for label, _ in p.COST_COLUMNS}]
    assert p.cost_rows({"rows": [{}]}) == [{label: p.MISSING for label, _ in p.COST_COLUMNS}]


def test_no_aggregation_filtering_or_repricing(fixed_views):
    view = deepcopy(fixed_views["preview"])
    original = p.fixed_summary(view)
    for child in view["document"]["children"]:
        # 展示邊界測試，不送回 workflow；檢查它不以列重算核心總額。
        child["fixed_child"]["rows"][0].update(path="同一路徑\\" + "完整部位" * 60,
            spc_code="", compri="0", part_cost="0.000000000000000001", qty="8", line_qty="4")
    result = p.fixed_summary(view)
    assert result["totals"] == original["totals"]
    for index, child in enumerate(result["children"]):
        assert child["amounts"] == original["children"][index]["amounts"]
        assert child["rows"][0]["完整路徑"].endswith("完整部位" * 60)
        assert child["rows"][0]["本張總項目數"] == "8"
        assert child["rows"][0]["規格代碼"] == p.EMPTY
    assert len(result["overview"]) == 4


def test_absent_headers_not_inferred_from_clock_or_preview(fixed_views):
    view = deepcopy(fixed_views["saved"])
    del view["saved"]["created_at"]
    del view["document"]["created_at"]
    del view["document"]["fixed_request"]["origin"]
    result = p.fixed_summary(view)["children"][0]["header"]
    assert result["固定內容建立時間"] == result["快照保存時間"] == p.NOT_CREATED
    assert result["來源程式（固定來源標記）"] == p.NOT_CREATED


def test_legal_empty_spec_description_structural_row_is_not_dropped(fixed_views):
    from engine.development_atomic_contract import canonical, decode_request, validate_request
    document = deepcopy(fixed_views["preview"]["document"]["fixed_request"])
    row = document["children"][0]["rows"][0]
    # 契約允許空「說明」，但仍要求規格代碼；不放寬原有驗證器。
    row.update(spdsc="", part_desc="", opt_desc="", quantity_source="structural")
    request = decode_request(canonical(document))
    validate_request(request)
    shown = p.cost_rows(document["children"][0])
    assert len(shown) == len(document["children"][0]["rows"])
    assert shown[0]["規格說明"] == shown[0]["部位名稱"] == p.EMPTY
    assert shown[0]["採購單價"] == "0" and shown[0]["明細成本"] == "0.00"


def test_source_summary_and_gap_scope_are_unpriced_and_unchanged():
    source = business.inspect_source(business.SAMPLE)
    before = deepcopy(source)
    result = p.source_summary(source)
    assert [c["header"]["成品數"] for c in result] == ["2", "1", "6", "2"]
    for actual, fixed in zip(result, source["children"], strict=True):
        assert actual["description"] == fixed["raw"]
        assert actual["header"]["驗收單號"] == "尚未配號"
        assert actual["header"]["成本狀態"] == "尚未計算成本"
        assert "rows" not in actual and "amounts" not in actual
        assert actual["shared_description"] == "\n".join(business.SAMPLE.splitlines()[5:])
    assert "孔位不適用，格位仍待" in result[0]["exception"]
    gaps = p.gap_rows(source["formal_gaps"])
    assert len(gaps) == 8
    assert all(g["正式核准狀態"] == "未核准" for g in gaps)
    assert source == before


@pytest.mark.parametrize("flag", [False, True, None])
def test_engineering_status_uses_fixed_evidence_never_grants_authority(fixed_views, flag):
    view = deepcopy(fixed_views["preview"])
    child = view["document"]["children"][0]["fixed_child"]
    conditions = json.loads(child["conditions_json"])
    drawings = json.loads(child["drawings_json"])
    conditions["formal_approved"] = flag
    drawings["geometry_verified"] = flag
    child["conditions_json"] = json.dumps(conditions)
    child["drawings_json"] = json.dumps(drawings)
    result = p.fixed_summary(view)["children"][0]["conditions"]
    assert all(row["正式工程"] == ("未核准／幾何未驗證" if flag is False else "未驗證（展示不授權）／幾何狀態未驗證")
               for row in result)
    assert not view["document"]["can_save"] and not view["document"]["formal_approved"]


@pytest.mark.parametrize("phase,stage,invalidated,expected", [
    ("RESERVING", 0, False, "缺配號"), ("RESERVATION_FAILED", 0, False, "待原身份重試"),
    ("PREVIEW", 0, False, "待第 1 次"), ("PREVIEW", 1, False, "待第 2 次"),
    ("PREVIEW", 2, False, "可保存"), ("SAVE_FAILED", 2, False, "保存失敗待重試"),
    ("SAVED", 2, False, "已保存"), ("SAVED", 0, True, "已失效"),
    ("UNKNOWN", 0, False, "未驗證"),
])
def test_workflow_states_not_inferred_from_cost_table(phase, stage, invalidated, expected):
    assert expected in p.phase_label(dict(phase=phase, stage=stage, invalidated=invalidated))


@pytest.mark.parametrize("text", ["<script>alert(1)</script>", '<img src="https://example.invalid/x" onerror="alert(1)">',
    "![圖](https://example.invalid/x)", "[連結](javascript:alert(1))", "`code` **text** & < >"])
def test_untrusted_text_is_preserved_not_interpreted(text, fixed_views):
    view = deepcopy(fixed_views["preview"])
    child = view["document"]["children"][0]["fixed_child"]
    child["description"] = child["shared_description"] = child["label"] = text
    child["rows"][0]["spdsc"] = text
    result = p.fixed_summary(view)["children"][0]
    assert result["description"] == result["shared_description"] == result["header"]["品項"] == text
    assert result["rows"][0]["規格說明"] == text
