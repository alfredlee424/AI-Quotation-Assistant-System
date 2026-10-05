"""新版批次展開：只核對文字與關聯，不讀主檔、不配號、不計價。"""
from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from agent import quote_batch as core
from agent.multi_quote import apply_multi_command, from_work_order, new_multi_quote
from agent.work_orders import parse_work_orders, reclassify_line
from quote_batch_fixtures import SHARED, condition, four_items


@pytest.fixture
def four():
    return four_items()


def build(batch, draft):
    return core.build_quote_batch(draft, actor="測試", source_batch=batch)


def check(batch, draft, document):
    return core.checked_quote_batch(draft, document, actor="測試", source_batch=batch)


def test_four_independent_children_not_eleven_and_no_side_effects(four, isolated_db, monkeypatch):
    from database import repository as repo
    from database.models import QuoteSnapshotDocument, ordqdt_ai
    batch, draft = four
    before = deepcopy(draft)
    monkeypatch.setattr(repo, "_session", Mock(side_effect=AssertionError("禁止讀寫資料庫")))
    document = build(batch, draft)
    children = document["children"]
    assert len(children) == 4 and [c["product_qty"] for c in children] == [2, 1, 6, 2]
    assert sum(c["product_qty"] for c in children) == 11
    assert len({c["child_quote_id"] for c in children}) == 4
    assert document["quote_batch_id"] != draft.draft_id
    assert [c["source_item_id"] for c in children] == [i.item_id for i in batch.orders[0].items]
    assert all(c["child_quote_id"] != c["source_item_id"] for c in children)
    assert [c["future_suffix_position"] for c in children] == [1, 2, 3, 4]
    assert all(c["formal_suffix"] is c["ref_no"] is None for c in children)
    assert document["numbering_policy"]["cost_row_seq_width"] == 5
    assert document["source_item_coverage_complete"] and document["expansion_review_complete"]
    assert not any(document[key] for key in ("can_save", "can_quote", "can_confirm"))
    assert "totals" not in document and "calc" not in document
    assert draft == before and check(batch, draft, json.loads(json.dumps(document))) == document
    repo._session.assert_not_called()
    with isolated_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == db.query(ordqdt_ai).count() == 0


def test_full_shared_descriptions_and_evidence_on_each_child(four):
    batch, draft = four
    document = build(batch, draft)
    for child in document["children"]:
        descriptions = child["descriptions"]
        for text in SHARED:
            matches = [d for d in descriptions if d["text"] == text]
            assert len(matches) == 1
            description = matches[0]
            assert description["scope_status"] == "RECORDED_SCOPE"
            assert description["applies_to_child"] is True
            assert description["source"]["raw"][description["start"]:description["end"]] == text
            assert description["requirement"]["evidence"][-1]["reference"] == "合成文字範圍-v1"
            assert not any(description[k] for k in ("selects_option", "charges_fee", "engineering_approved"))
    assert "面無孔有桌下走線" in document["children"][0]["source_item"]["description_raw"]
    assert all(not any("面無孔" in d["text"] for d in c["descriptions"]) for c in document["children"][1:])


def test_same_paths_remain_independent_and_download_is_deep_copy(four):
    batch, draft = four
    doc = build(batch, draft)
    assert [c["configuration"]["selections"][r"CMT1\A001"]["code"] for c in doc["children"]] == ["合成規格" + str(i) for i in range(4)]
    doc["children"][0]["configuration"]["selections"][r"CMT1\A001"]["code"] = "竄改"
    assert draft.lines[0].configuration["selections"][r"CMT1\A001"]["code"] == "合成規格0"
    with pytest.raises(ValueError):
        check(batch, draft, doc)


def test_negative_hole_wiring_and_partition_are_separate(four):
    batch, draft = four
    target = [draft.lines[0].line_id]
    own = next(r for r in draft.requirements if "面無孔" in r.text)
    shared = next(r for r in draft.requirements if r.text == "孔靠單/格靠單")
    for subject, state, req in (("surface_hole", "FORBIDDEN", own), ("underdesk_wiring", "REQUIRED", own),
                               ("hole_position", "NOT_APPLICABLE", shared), ("partition_position", "REQUIRED", shared)):
        draft = condition(batch, draft, subject, state, target, req.requirement_id)
    before = deepcopy(draft)
    document = build(batch, draft)
    states = {c["subject"]: c["state"] for c in document["children"][0]["effective_conditions"]}
    assert states == {"surface_hole": "FORBIDDEN", "underdesk_wiring": "REQUIRED", "hole_position": "NOT_APPLICABLE",
                      "partition_position": "REQUIRED", "leg_panel_hole": "UNSPECIFIED"}
    assert document["children"][0]["blockers"]  # 未綁部位、位置仍待工程核對
    assert draft == before
    for child in document["children"][1:]:
        assert next(c for c in child["effective_conditions"] if c["subject"] == "surface_hole")["state"] == "UNSPECIFIED"


def test_unknown_quantity_never_uses_source_candidates():
    batch, draft = four_items(quantities=False, reviewed=False)
    assert [i.quantity_candidate for i in batch.orders[0].items] == [2, 1, 6, 2]
    doc = build(batch, draft)
    assert len(doc["children"]) == 4
    assert all(c["product_qty"] is None and c["quantity_status"] == "PENDING" for c in doc["children"])
    assert not doc["expansion_review_complete"]
    assert all(next(d for d in c["descriptions"] if d["text"] == SHARED[0])["scope_status"] == "PENDING" for c in doc["children"])


@pytest.mark.parametrize("qty", [None, 0, -1, 1.5, True, "2"])
def test_invalid_quantity_is_blocked_without_dropping_child(four, qty):
    batch, draft = four
    first = replace(draft.lines[0], configuration={**draft.lines[0].configuration, "qty": qty})
    doc = build(batch, replace(draft, lines=(first, *draft.lines[1:])))
    assert len(doc["children"]) == 4 and doc["children"][0]["product_qty"] is None
    assert not doc["expansion_review_complete"]


def test_operator_quantity_is_not_overwritten_by_candidate(four):
    batch, draft = four
    first = replace(draft.lines[0], configuration={**draft.lines[0].configuration, "qty": 7})
    doc = build(batch, replace(draft, lines=(first, *draft.lines[1:])))
    assert doc["children"][0]["product_qty"] == 7
    assert doc["children"][0]["source_item"]["quantity_candidate"] == 2


@pytest.mark.parametrize("kind", ["missing", "duplicate", "archive", "extra", "coordinate", "workgroup"])
def test_coverage_structural_failures_atomically_rejected(four, kind):
    batch, draft = four
    if kind == "missing":
        draft = replace(draft, lines=draft.lines[1:])
    elif kind == "duplicate":
        draft = replace(draft, lines=(*draft.lines, draft.lines[0]))
    elif kind == "archive":
        draft = replace(draft, lines=draft.lines[1:], archived_lines=(draft.lines[0],))
    elif kind == "extra":
        draft = replace(draft, lines=(*draft.lines, replace(draft.lines[0], line_id="f" * 32)))
    elif kind == "coordinate":
        draft = replace(draft, lines=(replace(draft.lines[0], source_line_ids=draft.lines[1].source_line_ids), *draft.lines[1:]))
    else:
        draft = replace(draft, lines=(replace(draft.lines[0], configuration={**draft.lines[0].configuration, "workgroup": "999"}), *draft.lines[1:]))
    with pytest.raises(ValueError):
        build(batch, draft)


@pytest.mark.parametrize("key", ["source_ref_raw", "lines", "candidates", "unassigned_lines", "classification_history"])
def test_forged_source_snapshot_rejected(four, key):
    batch, draft = four
    source = deepcopy(draft.source)
    source[key] = "偽造" if key == "source_ref_raw" else [{"forged": True}]
    with pytest.raises(ValueError, match="來源"):
        build(batch, replace(draft, source=source))


@pytest.mark.parametrize("kind", ["raw", "row", "order", "version", "parser", "other_batch"])
def test_current_source_must_be_authentic_and_compatible(four, kind):
    batch, draft = four
    if kind == "raw":
        batch = replace(batch, raw_text=batch.raw_text + "改")
    elif kind == "row":
        batch = replace(batch, lines=(replace(batch.lines[0], raw="改"), *batch.lines[1:]))
    elif kind == "order":
        batch = replace(batch, orders=(replace(batch.orders[0], items=batch.orders[0].items[1:]),))
    elif kind == "version":
        batch = replace(batch, revision=batch.revision + 1)
    elif kind == "parser":
        batch = replace(batch, parser_version="new")
    else:
        batch, _ = four_items()
    with pytest.raises(ValueError):
        build(batch, draft)


def test_cross_order_even_same_raw_reference_rejected():
    batch = parse_work_orders("環式會議桌生產工單\n20990101-1匿名A\n桌一-----2\n20990101-1匿名B\n桌二-----3")
    draft = from_work_order(batch, batch.orders[0].order_id)
    other = from_work_order(batch, batch.orders[1].order_id)
    with pytest.raises(ValueError):
        build(batch, replace(draft, lines=other.lines))


def test_reorder_preserves_ids_but_invalidates_old_document(four):
    batch, draft = four
    old = build(batch, draft)
    changed = apply_multi_command(draft, {"op": "reorder", "line_ids": [l.line_id for l in reversed(draft.lines)]},
        draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="重排", source_batch=batch)
    with pytest.raises(ValueError):
        check(batch, changed, old)
    new = build(batch, changed)
    assert old["quote_batch_id"] == new["quote_batch_id"]
    assert {c["source_item_id"]: c["child_quote_id"] for c in old["children"]} == {c["source_item_id"]: c["child_quote_id"] for c in new["children"]}
    assert old["children"][0]["child_quote_id"] == new["children"][-1]["child_quote_id"]
    assert new["children"][-1]["future_suffix_position"] == 4


@pytest.mark.parametrize("kind", ["actor", "revision", "label", "qty", "discount", "requirement", "version"])
def test_any_context_change_invalidates_old_document(four, kind, monkeypatch):
    batch, draft = four
    doc = build(batch, draft)
    if kind == "actor":
        with pytest.raises(ValueError):
            core.checked_quote_batch(draft, doc, actor="另一人", source_batch=batch)
        return
    if kind == "version":
        monkeypatch.setattr(core, "DOCUMENT_VERSION", "next")
    elif kind == "revision":
        draft = replace(draft, revision=draft.revision + 1)
    elif kind == "discount":
        draft = replace(draft, discount_rate=0.1)
    elif kind == "requirement":
        draft = replace(draft, requirements=(replace(draft.requirements[0], disposition="engineering_review"), *draft.requirements[1:]))
    else:
        first = replace(draft.lines[0], label="改名稱") if kind == "label" else replace(draft.lines[0], configuration={**draft.lines[0].configuration, "qty": 8})
        draft = replace(draft, lines=(first, *draft.lines[1:]))
    with pytest.raises(ValueError):
        check(batch, draft, doc)


@pytest.mark.parametrize("kind", ["qty", "description", "remove_child", "id", "version", "flag", "extra"])
def test_content_tampering_rejected_even_after_rehash(four, kind):
    batch, draft = four
    doc = build(batch, draft)
    if kind == "qty":
        doc["children"][0]["product_qty"] = 99
    elif kind == "description":
        doc["children"][0]["descriptions"].clear()
    elif kind == "remove_child":
        doc["children"].pop()
    elif kind == "id":
        doc["children"][0]["child_quote_id"] = "f" * 32
    elif kind == "version":
        doc["document_version"] = "legacy"
    elif kind == "flag":
        doc["can_confirm"] = True
    else:
        doc["ref_no"] = "SHOULD-NOT-EXIST"
    doc["content_digest"] = core._digest({k: v for k, v in doc.items() if k != "content_digest"})
    with pytest.raises(ValueError):
        check(batch, draft, doc)


@pytest.mark.parametrize("kind", ["multi_quote_internal_trial_only", "multi_quote_snapshot_layout_only", "multi_quote_configuration_only"])
def test_legacy_documents_not_accepted_as_batch(four, kind):
    batch, draft = four
    with pytest.raises(ValueError):
        check(batch, draft, {"document_type": kind})
    with pytest.raises(ValueError):
        core.build_quote_batch({"document_type": kind}, actor="測試", source_batch=batch)


def test_non_work_order_draft_is_rejected():
    with pytest.raises(ValueError):
        core.build_quote_batch(new_multi_quote(), actor="測試")


@pytest.mark.parametrize("count", [999, 1000])
def test_capacity_no_truncation(count):
    batch = parse_work_orders("環式會議桌生產工單\n20990101-1匿名\n" + "\n".join(f"測試品項{i}-----1" for i in range(count)))
    draft = from_work_order(batch, batch.orders[0].order_id)
    if count == 1000:
        with pytest.raises(ValueError, match="999"):
            build(batch, draft)
    else:
        doc = build(batch, draft)
        assert len(doc["children"]) == 999 and doc["children"][-1]["future_suffix_position"] == 999


def test_missing_requirement_keeps_full_text_and_blocks(four):
    batch, draft = four
    draft = replace(draft, requirements=tuple(r for r in draft.requirements if r.text != SHARED[0]))
    doc = build(batch, draft)
    assert not doc["expansion_review_complete"]
    assert all(any(d["text"] == SHARED[0] and d["kind"] == "uncovered" for d in child["descriptions"]) for child in doc["children"])


@pytest.mark.parametrize("kind", ["text", "coordinates", "scope", "duplicate"])
def test_invalid_requirement_rejected(four, kind):
    batch, draft = four
    first = draft.requirements[0]
    if kind == "text":
        first = replace(first, text="竄改")
    elif kind == "coordinates":
        first = replace(first, start=-1)
    elif kind == "scope":
        first = replace(first, targets=("other-order",))
    else:
        draft = replace(draft, requirements=(*draft.requirements, first))
    if kind != "duplicate":
        draft = replace(draft, requirements=(first, *draft.requirements[1:]))
    with pytest.raises(ValueError):
        build(batch, draft)


def test_new_documents_rejected_by_formal_repository_before_session(four, monkeypatch):
    from database import repository as repo
    batch, draft = four
    doc = build(batch, draft)
    monkeypatch.setattr(repo, "_session", Mock(side_effect=AssertionError("不得開 DB")))
    for items, preview in ((doc, None), (doc["children"], None), ([{"ref_no": "fake"}], doc),
                           ([doc["children"][0]], None)):
        with pytest.raises(ValueError):
            repo.save_quote_snapshot(items, preview=preview)
    repo._session.assert_not_called()


def test_new_document_rejected_by_old_trial_and_snapshot_contracts(four):
    from engine.multi_trial import checked_internal_trial
    from engine.multi_snapshot import build_snapshot_layout
    from agent.multi_quote import from_single_quote
    batch, draft = four
    doc = build(batch, draft)
    for function in (checked_internal_trial, build_snapshot_layout):
        with pytest.raises(ValueError):
            function(draft, doc, actor="測試", source_batch=batch)
    with pytest.raises(ValueError):
        from_single_quote(doc)


def test_reviewed_partial_scope_does_not_spread_to_other_children(four):
    from agent.requirement_review import apply_review_command
    batch, draft = four
    record = next(r for r in draft.requirements if r.text == SHARED[0])
    draft = apply_review_command(draft, {"op": "record", "requirement_id": record.requirement_id,
        "targets": [draft.lines[1].line_id], "disposition": "production_note", "bindings": [],
        "reference": "合成單筆範圍-v2", "explanation": "只適用第二張"}, draft_id=draft.draft_id,
        expected_revision=draft.revision, actor="測試", reason="限定範圍", source_batch=batch)
    doc = build(batch, draft)
    assert [any(d["text"] == SHARED[0] for d in c["descriptions"]) for c in doc["children"]] == [False, True, False, False]


def test_split_shared_line_retains_full_original_on_each_child(four):
    from agent.requirement_review import apply_review_command
    batch, draft = four
    record = next(r for r in draft.requirements if r.text == SHARED[1])
    draft = apply_review_command(draft, {"op": "split", "requirement_id": record.requirement_id, "offset": 4},
        draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="孔格分開", source_batch=batch)
    doc = build(batch, draft)
    for child in doc["children"]:
        assert SHARED[1] in [row["raw"] for row in child["description_sources"]]
        fragments = [d for d in child["descriptions"] if d["source"]["raw"] == SHARED[1]]
        assert [d["text"] for d in fragments] == ["孔靠單/", "格靠單"]
        assert all(d["scope_status"] == "PENDING" for d in fragments)


def test_condition_scope_carries_its_source_coordinates_even_without_requirement_scope():
    batch, draft = four_items(reviewed=False)
    source = next(r for r in draft.requirements if "面無孔" in r.text)
    draft = condition(batch, draft, "underdesk_wiring", "REQUIRED", [draft.lines[1].line_id], source.requirement_id)
    doc = build(batch, draft)
    text = next(d for d in doc["children"][1]["descriptions"] if d["requirement"]["requirement_id"] == source.requirement_id)
    assert text["source"]["line_id"] == source.source_line_id
    assert text["scope_status"] == "RECORDED_CONDITION_SCOPE"
    assert text["inherited_condition_ids"] == [draft.conditions[0].condition_id]
    assert not doc["expansion_review_complete"]  # 文字需求本身仍未核對


def test_condition_and_drawing_changes_invalidate_document(four):
    from agent.drawing_review import DrawingDependency
    batch, draft = four
    doc = build(batch, draft)
    source = next(r for r in draft.requirements if "面無孔" in r.text)
    changed = condition(batch, draft, "surface_hole", "FORBIDDEN", [draft.lines[0].line_id], source.requirement_id)
    with pytest.raises(ValueError):
        check(batch, changed, doc)
    drawing = DrawingDependency("d" * 32, (draft.lines[0].line_id,), (source.requirement_id,), "定位圖")
    changed = replace(draft, drawings=(drawing,))
    with pytest.raises(ValueError):
        check(batch, changed, doc)
    assert any("圖面" in m for m in build(batch, changed)["children"][0]["blockers"])


def test_corrected_source_can_reconnect_but_old_version_is_rejected(four):
    batch, draft = four
    doc = build(batch, draft)
    line = next(line for line in batch.lines if line.raw == SHARED[0])
    changed = reclassify_line(batch, line_id=line.line_id, kind="note", expected_revision=batch.revision,
                              actor="測試", reason="人工分類")
    with pytest.raises(ValueError):
        check(changed, draft, doc)
    new = from_work_order(changed, changed.orders[0].order_id)
    assert len(build(changed, new)["children"]) == 4


def test_archive_and_restore_does_not_reuse_old_document(four):
    batch, draft = four
    doc = build(batch, draft)
    def command(current, op):
        return apply_multi_command(current, {"op": op, "line_id": draft.lines[0].line_id}, draft_id=current.draft_id,
            expected_revision=current.revision, actor="測試", reason="封存恢復核對", source_batch=batch)
    archived = command(draft, "remove")
    restored = command(archived, "restore")
    with pytest.raises(ValueError):
        check(batch, restored, doc)
    new = build(batch, restored)
    assert new["quote_batch_id"] == doc["quote_batch_id"]


@pytest.mark.parametrize("quantity", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_quantity_fails_closed(four, quantity):
    batch, draft = four
    first = replace(draft.lines[0], configuration={**draft.lines[0].configuration, "qty": quantity})
    with pytest.raises(ValueError):
        build(batch, replace(draft, lines=(first, *draft.lines[1:])))
