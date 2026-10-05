"""數量與尺寸受控套用：明確單位、來源隔離、原子拒絕及固定差異。"""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from unittest.mock import Mock

import pytest

from agent import numeric_application as numeric
from agent.multi_quote import apply_multi_command, export_multi_draft, from_work_order
from agent.work_orders import parse_work_orders
from database import repository as repo
from database.models import Ordspe


def case(current_draft, text="桌面600mmX1200mm-----2 面2孔6公分圓孔"):
    batch = parse_work_orders("環式會議桌生產工單\n20990101-1測試\n" + text + "\n桌面60X180cm-----3")
    draft = from_work_order(batch, batch.orders[0].order_id)
    for line in draft.lines:
        draft = manage(batch, draft, {"op": "configure", "line_id": line.line_id, "proposal": {
            "prodkind": "CMT1", "qty": 1, "changes": [{"op": "set", "path": s["path"], "code": s["code"],
            "line_qty": s.get("line_qty", 1)} for s in current_draft["selections"].values()]}})
    return batch, draft


@pytest.fixture
def numeric_case(current_draft):
    return case(current_draft)


def manage(batch, draft, content):
    return apply_multi_command(draft, content, draft_id=draft.draft_id, expected_revision=draft.revision,
                               actor="測試", reason="測試配置", source_batch=batch)


def request(batch, draft, application_mode="product_quantity", **changes):
    report = numeric.numeric_candidates(draft, batch, draft.lines[0].line_id)
    kind = "row_quantity_candidate" if application_mode == "product_quantity" else "table_dimensions_candidate"
    finding = next((f for f in report["findings"] if f["finding_id"] == changes.get("finding_id")), None)
    if finding is None:
        finding = next(f for f in report["findings"] if f["kind"] == kind)
    value = {"finding_id": finding["finding_id"], "line_id": draft.lines[0].line_id, "mode": application_mode,
             "source_unit": "", "catalog_unit": "cm" if application_mode == "table_dimensions" else "",
             "axis_order": "as_written" if application_mode == "table_dimensions" else "", "code": "C003" if application_mode == "table_dimensions" else "",
             "reference": "人工依據-TEST-R1", "explanation": "確認來源數值的成品／桌面用途、單位及軸順序"}
    value.update(changes)
    return value


def prepare(batch, draft, application_mode="product_quantity", **changes):
    return numeric.prepare_numeric_application(draft, request(batch, draft, application_mode, **changes), actor="測試", source_batch=batch)


def commit(batch, draft, proposal):
    return numeric.commit_numeric_application(draft, proposal, actor="測試", source_batch=batch)


def test_quantity_preparation_is_read_only_and_commit_keeps_other_line(numeric_case, isolated_db):
    batch, draft = numeric_case
    before = export_multi_draft(draft)
    proposal = prepare(batch, draft)
    assert export_multi_draft(draft) == before
    assert proposal.normalization["product_qty"] == 2 and proposal.finding["raw"] == "2"
    result = commit(batch, draft, proposal)
    assert result.lines[0].configuration["qty"] == 2
    assert result.lines[1] == draft.lines[1] and result.source == draft.source and result.blockers == draft.blockers
    assert result.revision == draft.revision + 1 and result.lines[0].revision == draft.lines[0].revision + 1
    assert result.lines[0].configuration["selections"] == draft.lines[0].configuration["selections"]
    assert result.changes[-1].operation == "numeric_application"
    assert result.changes[-1].details["formal_approval"] is False
    assert result.requirements == draft.requirements
    assert result.lines[0].configuration["preview"] is None
    with pytest.raises(ValueError):
        commit(batch, result, proposal)
    from database.models import QuoteSnapshotDocument, ordqdt_ai
    with isolated_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == db.query(ordqdt_ai).count() == 0


def test_metric_dimensions_exactly_match_after_explicit_master_unit(numeric_case):
    batch, draft = numeric_case
    proposal = prepare(batch, draft, "table_dimensions")
    assert proposal.normalization["centimeters"] == ("60", "120")
    assert proposal.normalization["confirmed_source_units"] == ("mm", "mm")
    assert proposal.normalization["catalog_description"] == "60*120"
    updated = commit(batch, draft, proposal)
    assert updated.lines[0].configuration["selections"][r"CMT1\A001"]["code"] == "C003"
    assert updated.lines[0].configuration["qty"] == draft.lines[0].configuration["qty"]
    assert updated.lines[1] == draft.lines[1]
    assert updated.source == draft.source


@pytest.mark.parametrize("raw,changes", [
    ("桌面60X120-----2", {"source_unit": "cm"}),
    ("桌面600X1200-----2", {"source_unit": "mm"}),
    ("桌面120cmX60cm-----2", {"axis_order": "swapped"}),
    ("桌面600毫米X120公分-----2", {}),
    ("桌面６００ｍｍＸ１２００ｍｍ-----2", {}),
])
def test_explicit_unit_or_axis_confirmation_is_recorded(current_draft, raw, changes):
    batch, draft = case(current_draft, raw)
    proposal = prepare(batch, draft, "table_dimensions", **changes)
    result = commit(batch, draft, proposal)
    assert result.lines[0].configuration["selections"][r"CMT1\A001"]["code"] == "C003"
    assert result.changes[-1].details["finding"]["raw"] in raw
    assert result.changes[-1].details["request"]["reference"] == "人工依據-TEST-R1"


@pytest.mark.parametrize("raw,changes", [
    ("桌面60X120-----2", {}),
    ("桌面600X1200-----2", {"source_unit": "cm"}),
    ("桌面600mmX1200mm-----2", {"source_unit": "cm"}),
    ("桌面2尺X4尺-----2", {"source_unit": "cm"}),
    ("桌面60.01cmX120cm-----2", {}),
    ("桌面120cmX60cm-----2", {}),
    ("桌面20cmX360cm（120cmX180cmX2）-----2", {}),
    ("桌面60cmX120cm（60cmX60cmX2）-----2", {}),
    ("桌面60cmX120cm 與70cmX140cm-----2", {}),
])
def test_ambiguity_no_rounding_or_geometry_autofix(current_draft, raw, changes):
    batch, draft = case(current_draft, raw)
    before = export_multi_draft(draft)
    with pytest.raises(ValueError):
        prepare(batch, draft, "table_dimensions", **changes)
    assert export_multi_draft(draft) == before


@pytest.mark.parametrize("changes", [
    {"catalog_unit": ""}, {"catalog_unit": "taiwan_foot"}, {"axis_order": ""}, {"code": "invalid"},
    {"code": "C005"}, {"source_unit": "auto"}, {"qty": 9}, {"price": 1}, {"status": "APPROVED"},
    {"reference": ""}, {"reference": "x" * 501}, {"explanation": ""}, {"explanation": "x" * 1001},
    {"mode": "material_usage"}, {"code": None},
])
def test_invalid_size_request_is_atomic(numeric_case, changes):
    batch, draft = numeric_case
    before = export_multi_draft(draft)
    with pytest.raises(ValueError):
        prepare(batch, draft, "table_dimensions", **changes)
    assert export_multi_draft(draft) == before


@pytest.mark.parametrize("kind", ["hole_count", "hole_diameter_candidate", "table_dimensions_candidate"])
def test_non_product_numbers_cannot_be_used_as_finished_quantity(numeric_case, kind):
    batch, draft = numeric_case
    finding = next(f for f in numeric.numeric_candidates(draft, batch, draft.lines[0].line_id)["findings"] if f["kind"] == kind)
    with pytest.raises(ValueError, match="只有行尾"):
        prepare(batch, draft, finding_id=finding["finding_id"])


@pytest.mark.parametrize("raw", ["桌面60cmX120cm-----0", "桌面60cmX120cm-----2.5", "桌面60cmX120cm-----2/3"])
def test_invalid_row_quantity_never_uses_partial_integer(current_draft, raw):
    batch, draft = case(current_draft, raw)
    with pytest.raises(ValueError, match="正整數"):
        prepare(batch, draft)


def test_source_scope_rejects_other_line_order_archived_and_duplicate(numeric_case):
    batch, draft = numeric_case
    value = request(batch, draft)
    value["line_id"] = draft.lines[1].line_id
    with pytest.raises(ValueError, match="跨單或跨明細"):
        numeric.prepare_numeric_application(draft, value, actor="測試", source_batch=batch)
    with pytest.raises(ValueError):
        prepare(batch, draft, line_id="foreign")
    with pytest.raises(ValueError):
        prepare(replace(batch, revision=batch.revision + 1), draft)
    archived = manage(batch, draft, {"op": "remove", "line_id": draft.lines[0].line_id})
    with pytest.raises(ValueError):
        numeric.prepare_numeric_application(archived, request(batch, draft), actor="測試", source_batch=batch)
    duplicate = replace(draft, lines=(draft.lines[0], replace(draft.lines[1], source_line_ids=draft.lines[0].source_line_ids)))
    with pytest.raises(ValueError, match="多筆活動明細"):
        prepare(batch, duplicate)


def test_wrong_product_and_missing_product_do_not_default(numeric_case):
    batch, draft = numeric_case
    for product in (None, "MT"):
        config = deepcopy(draft.lines[0].configuration)
        config["prodkind"] = product
        changed = replace(draft, lines=(replace(draft.lines[0], configuration=config), draft.lines[1]))
        with pytest.raises(ValueError):
            prepare(batch, changed, "table_dimensions")


def test_explicit_master_units_and_mismatch(numeric_case, isolated_db):
    batch, draft = numeric_case
    with isolated_db() as db:
        row = db.query(Ordspe).filter_by(workgroup="103", path=r"CMT1\A001", code="C003").one()
        row.codsc = "600mmX1200mm"
        db.commit()
    proposal = prepare(batch, draft, "table_dimensions", catalog_unit="")
    assert proposal.normalization["catalog_centimeters"] == ("60", "120")
    with pytest.raises(ValueError, match="原文明確單位"):
        prepare(batch, draft, "table_dimensions")


@pytest.mark.parametrize("description", ["60*120特製", "60X120X2", "2尺X4尺", "非尺寸說明"])
def test_master_description_must_be_complete_two_axis(numeric_case, isolated_db, description):
    batch, draft = numeric_case
    with isolated_db() as db:
        db.query(Ordspe).filter_by(workgroup="103", path=r"CMT1\A001", code="C003").one().codsc = description
        db.commit()
    with pytest.raises(ValueError):
        prepare(batch, draft, "table_dimensions")


def test_master_change_after_preview_rejects_without_partial_mutation(numeric_case, isolated_db):
    batch, draft = numeric_case
    proposal = prepare(batch, draft, "table_dimensions")
    before = export_multi_draft(draft)
    with isolated_db() as db:
        db.query(Ordspe).filter_by(workgroup="103", path=r"CMT1\A001", code="C003").one().codsc = "60*120cm"
        db.commit()
    with pytest.raises(ValueError, match="主檔或數值套用差異"):
        commit(batch, draft, proposal)
    assert export_multi_draft(draft) == before


def test_render_check_does_not_query_catalog_and_detects_tampering(numeric_case, monkeypatch):
    batch, draft = numeric_case
    proposal = prepare(batch, draft)
    monkeypatch.setattr(repo, "_session", Mock(side_effect=AssertionError("畫面核對不得查價")))
    assert numeric.checked_numeric_application(draft, proposal, actor="測試", source_batch=batch) == proposal
    for changed in (replace(proposal, actor="另一位"), replace(proposal, proposal={"qty": 99})):
        with pytest.raises(ValueError):
            numeric.checked_numeric_application(draft, changed, actor="測試", source_batch=batch)
    repo._session.assert_not_called()


def test_commit_invalidates_trial_and_drawing_and_keeps_sources_unreviewed(numeric_case):
    from test_drawing_review import declare, record
    from engine.multi_trial import build_internal_trial, checked_internal_trial
    batch, draft = numeric_case
    draft = record(declare(draft, batch, requirement_ids=[draft.requirements[-2].requirement_id]), batch)
    trial = build_internal_trial(draft, actor="測試", source_batch=batch)
    updated = commit(batch, draft, prepare(batch, draft))
    assert updated.drawings[0].status == "STALE"
    assert all(r.status == "UNREVIEWED" for r in updated.requirements)
    with pytest.raises(ValueError):
        checked_internal_trial(updated, trial, actor="測試", source_batch=batch)


def test_evidence_not_sent_to_model_and_contains_no_prices(numeric_case):
    from agent.multi_quote_agent import build_multi_context
    batch, draft = numeric_case
    proposal = prepare(batch, draft, reference="SECRET-UNIT-EVIDENCE", explanation="SECRET-UNIT-EXPLANATION")
    assert "compri" not in json.dumps(asdict(proposal))
    updated = commit(batch, draft, proposal)
    context = json.dumps(build_multi_context(updated, (updated.lines[0].line_id,)))
    assert "SECRET-UNIT" not in context


def test_size_selection_keeps_existing_per_item_quantity(numeric_case):
    batch, draft = numeric_case
    draft = manage(batch, draft, {"op": "configure", "line_id": draft.lines[0].line_id, "proposal": {
        "changes": [{"op": "set", "path": r"CMT1\A001", "code": "C005", "line_qty": 2}]}})
    updated = commit(batch, draft, prepare(batch, draft, "table_dimensions"))
    assert updated.lines[0].configuration["selections"][r"CMT1\A001"]["line_qty"] == 2


@pytest.mark.parametrize("raw,kind", [
    ("桌面60cmX120cm（60cmX60cmX2）-----2", "panel_count_candidate"),
    ("桌面60cmX120cm 假厚36mm-----2", "false_thickness"),
    ("桌面60cmX120cm 線盒40cmX13.5cm-----2", "accessory_dimensions_candidate"),
])
def test_component_and_thickness_candidates_cannot_replace_table_dimensions(current_draft, raw, kind):
    batch, draft = case(current_draft, raw)
    finding = next(f for f in numeric.numeric_candidates(draft, batch, draft.lines[0].line_id)["findings"] if f["kind"] == kind)
    with pytest.raises(ValueError):
        prepare(batch, draft, "table_dimensions", finding_id=finding["finding_id"])
    with pytest.raises(ValueError):
        prepare(batch, draft, finding_id=finding["finding_id"])


def test_cross_order_candidate_and_old_proposal_rejected(numeric_case):
    batch, draft = numeric_case
    other = parse_work_orders("環式會議桌生產工單\n20990101-2另一單\n桌面60X120cm-----5")
    other_draft = from_work_order(other, other.orders[0].order_id)
    finding = numeric.numeric_candidates(other_draft, other, other_draft.lines[0].line_id)["findings"][-1]
    with pytest.raises(ValueError):
        prepare(batch, draft, finding_id=finding["finding_id"])
    proposal = prepare(batch, draft)
    changed = manage(batch, draft, {"op": "set_discount", "discount_rate": .1})
    with pytest.raises(ValueError, match="已變更"):
        commit(batch, changed, proposal)


def test_core_condition_guard_still_rejects_numeric_apply(numeric_case):
    from agent.conditions import apply_condition_command
    batch, draft = numeric_case
    draft = apply_condition_command(draft, {"op": "record", "condition_id": "", "subject": "surface_hole",
        "state": "FORBIDDEN", "scope": "line", "group_label": "", "targets": [draft.lines[0].line_id], "value": "測試禁止部位",
        "requirement_ids": [draft.requirements[-2].requirement_id], "bindings": [{"line_id": draft.lines[0].line_id, "path": r"CMT1\A001"}],
        "overrides": [], "reference": "測試否定-v1", "explanation": "測試數值命令不能繞過既有否定保護"},
        draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="測試禁止", source_batch=batch)
    before = export_multi_draft(draft)
    with pytest.raises(ValueError, match="否定條件"):
        prepare(batch, draft)
    assert export_multi_draft(draft) == before
