"""新版逐張金額、完整來源、不可正式化及不查價重繪契約。"""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import event

from agent import quote_batch
from agent.multi_quote import export_multi_draft, from_work_order
from agent.work_orders import parse_work_orders
from database import repository as repo
from database.models import Invdoc, Ordspe, Ordqty, QuoteSnapshotDocument, ordqdt_ai
from engine import quote_batch_trial as core
from engine.multi_trial import build_internal_trial, checked_internal_trial
from engine.pricing import calculate_configuration
from quote_batch_trial_fixtures import priced_pair, priced_four, mixed_products, review_all
from quote_batch_fixtures import SHARED, condition
from test_multi_trial import command
from test_drawing_review import declare as declare_drawing, record


def declare(draft, batch):
    requirement = next(r for r in draft.requirements if draft.lines[0].line_id in r.targets)
    return declare_drawing(draft, batch, requirement_ids=[requirement.requirement_id])


def build(pair):
    batch, draft = pair
    return core.build_batch_trial(draft, actor="測試", source_batch=batch)


def check(pair, doc, actor="測試"):
    batch, draft = pair
    return core.checked_batch_trial(draft, doc, actor=actor, source_batch=batch)


def test_independent_rounding_not_old_order_sum(priced_pair, isolated_db):
    batch, draft = priced_pair
    with isolated_db() as db:
        db.query(Invdoc).update({Invdoc.quo_rate: 1})
        db.commit()
    doc = build(priced_pair)
    assert [c["calculation"]["after_discount"] for c in doc["children"]] == ["0.34", "0.34"]
    assert doc["totals"]["after_discount"] == "0.68"
    old = build_internal_trial(draft, actor="測試", source_batch=batch)
    assert old["totals"]["after_discount"] == 0.67
    assert doc["totals"]["tax_amount"] == "0.04"
    assert doc["totals"]["total_price"] == "0.70"
    assert doc["totals"]["after_discount_plus_tax_minus_total_price"] == "0.02"
    for child in doc["children"]:
        assert child["calculation"]["rounding_differences"]["after_discount_plus_tax_minus_total_price"] == "0.01"
    assert json.loads(json.dumps(doc))["totals"] == doc["totals"]


@pytest.mark.parametrize("discount", [0, .05, .1])
def test_each_product_matches_single_core_discount_tax_once(priced_pair, discount):
    batch, draft = priced_pair
    draft = replace(draft, discount_rate=discount)
    before = export_multi_draft(draft)
    doc = build((batch, draft))
    assert [c["calculation"]["quote_rate"] for c in doc["children"]] == ["1.5", "2.0"]
    for line, child in zip(draft.lines, doc["children"]):
        config = {**deepcopy(line.configuration), "questions": [], "discount_rate": discount}
        expected, _ = calculate_configuration(config)
        calc = child["calculation"]
        for key in core.MONEY_FIELDS:
            assert Decimal(calc[key]) == Decimal(str(getattr(expected, key)))
            assert isinstance(calc[key], str)
        assert calc["discount_rate"] == str(float(discount)) and calc["tax_rate"] == "0.05"
        assert calc["formula_version"] == core.FORMULA_VERSION
    for key in core.MONEY_FIELDS:
        assert Decimal(doc["totals"][key]) == sum(Decimal(c["calculation"][key]) for c in doc["children"])
    assert export_multi_draft(draft) == before


def test_exact_sum_not_float_or_ambient_context():
    with localcontext() as context:
        context.prec = 3
        assert core._sum(["999999999999999999999999.99", "0.01"] * 200) == "200000000000000000000000000.00"
        assert core._difference("999999999999999999999999.99", "999999999999999999999999.98") == "0.01"


def test_four_children_eleven_products_complete_text_no_extra_charges(priced_four):
    batch, draft = priced_four
    doc = build(priced_four)
    assert doc["calculated_count"] == 4, [c["blockers"] for c in doc["children"]]
    assert doc["totals"]["product_qty"] == 11
    assert len({c["child_quote_id"] for c in doc["children"]}) == 4
    for line, child in zip(draft.lines, doc["children"]):
        assert [d["text"] for d in child["descriptions"] if d["text"] in SHARED] == list(SHARED)
        assert child["source_item_id"] == line.line_id
        expected, _ = calculate_configuration({**line.configuration, "questions": []})
        assert Decimal(child["calculation"]["total_price"]) == Decimal(str(expected.total_price))
        assert len(child["calculation"]["items"]) == len(expected.items)
        assert all(i["line_id"] == line.line_id and i["child_quote_id"] == child["child_quote_id"] for i in child["calculation"]["items"])
    assert "面無孔有桌下走線" in doc["children"][0]["source_line"]["raw"]
    assert all(c["can_quote"] is c["can_confirm"] is c["can_save"] is False for c in [doc, *doc["children"]])
    assert all(c["ref_no"] is c["formal_suffix"] is None for c in [doc, *doc["children"]])


def test_cost_row_rounding_difference_and_parent_zero_nodes(priced_four, isolated_db):
    # 明示合成付費父節點；不因有子節點而漏收，其他零價容器仍保留。
    code = priced_four[1].lines[0].configuration["selections"][r"CMT1\A001"]["code"]
    with isolated_db() as db:
        db.query(Ordspe).filter(Ordspe.path == r"CMT1\A001", Ordspe.code == code).update({Ordspe.compri: .335})
        db.query(Ordqty).filter(Ordqty.path == r"CMT1\A001").delete()
        db.add(Ordqty(workgroup="103", path=r"CMT1\A001", code=code, part_path=r"CMT1\A001", stdqty=1, stdpar=1))
        db.commit()
    doc = build(priced_four)
    for child in doc["children"]:
        calc = child["calculation"]
        assert any(i["source"] == "zero_cost" for i in calc["items"])
        assert any(i["compri"] > 0 and any(j["path"].startswith(i["path"] + "\\") for j in calc["items"]) for i in calc["items"])
        diff = sum(Decimal(i["part_cost"]) for i in calc["items"]) - Decimal(calc["total_cost"])
        assert Decimal(calc["rounding_differences"]["display_cost_rows_minus_total_cost"]) == diff
        assert all(i["driver_path"] and i["driver_code"] for i in calc["items"] if i["source"] == "ordqty")


def test_per_item_qty_separate_from_product_qty(priced_pair):
    batch, draft = deepcopy(priced_pair)
    draft.lines[0].configuration["qty"] = 3
    draft.lines[0].configuration["selections"][r"MT\A001"]["line_qty"] = 2
    draft = review_all(batch, draft)
    doc = build((batch, draft))
    first = doc["children"][0]
    assert first["product_qty"] == 3
    item = first["calculation"]["items"][0]
    assert (item["qty"], item["product_qty"], item["line_qty"]) == (6, 3, 2)
    assert first["calculation"]["total_cost"] == "2.01"
    assert doc["totals"]["product_qty"] == 4


@pytest.mark.parametrize("kind", ["product", "qty", "fraction", "question", "pending", "spec", "discount", "requirement", "coverage", "engineering"])
def test_blocked_children_retained_no_total(priced_pair, kind):
    batch, draft = deepcopy(priced_pair)
    config = draft.lines[0].configuration
    if kind == "product": config["prodkind"] = None
    elif kind == "qty": config["qty"] = None
    elif kind == "fraction": config["qty"] = 1.5
    elif kind == "question": config["questions"].append("孔位未確認")
    elif kind == "pending": config["pending_options"] = [{"code": "C999"}]
    elif kind == "spec": config["selections"][r"MT\A001"]["code"] = "INVALID"
    elif kind == "discount": config["discount_rate"] = .1
    elif kind == "coverage": draft = replace(draft, requirements=draft.requirements[:-1])
    else:
        requirement = next(r for r in draft.requirements if draft.lines[0].line_id in r.targets)
        draft = replace(draft, requirements=tuple(replace(r, status="NEEDS_ENGINEERING" if kind == "engineering" else "UNREVIEWED")
            if r == requirement else r for r in draft.requirements))
    doc = build((batch, draft))
    assert len(doc["children"]) == 2 and doc["totals"] is None
    assert any(c["calculation"] is None and c["blockers"] for c in doc["children"])


@pytest.mark.parametrize("kind", ["price", "usage", "divisor", "rate", "read_error"])
def test_master_failure_does_not_use_old_or_imported_price(priced_pair, isolated_db, monkeypatch, kind):
    if kind == "read_error":
        monkeypatch.setattr(repo, "get_quantity_rules", Mock(side_effect=RuntimeError("SECRET-HOST")))
    else:
        with isolated_db() as db:
            if kind == "price": db.query(Ordspe).filter(Ordspe.path == r"MT\A001").update({Ordspe.compri: None})
            elif kind == "usage": db.query(Ordqty).filter(Ordqty.path == r"MT\A001").delete()
            elif kind == "divisor": db.query(Ordqty).filter(Ordqty.path == r"MT\A001").update({Ordqty.stdpar: 0})
            else: db.query(Invdoc).filter(Invdoc.prodkind == "MT").update({Invdoc.quo_rate: None})
            db.commit()
    doc = build(priced_pair)
    assert doc["totals"] is None and doc["children"][0]["calculation"] is None
    assert "SECRET-HOST" not in json.dumps(doc)


@pytest.mark.parametrize("kind", ["missing", "stale", "condition"])
def test_drawing_and_condition_blockers_preserved(priced_pair, kind):
    batch, draft = priced_pair
    if kind == "condition":
        draft = condition(batch, draft, "hole_position", "REQUIRED", [draft.lines[0].line_id], draft.requirements[-1].requirement_id)
    else:
        draft = declare(draft, batch)
        if kind == "stale":
            draft = record(draft, batch)
            draft = replace(draft, drawings=(replace(draft.drawings[0], status="STALE"),))
    doc = build((batch, draft))
    assert doc["totals"] is None and doc["children"][0]["blockers"]
    assert doc["children"][0]["calculation"] is None


@pytest.mark.parametrize("kind", ["missing", "archive", "duplicate", "foreign", "swap", "workgroup", "source", "current_source", "stale", "no_source"])
def test_bidirectional_source_rejected_before_pricing(priced_pair, monkeypatch, kind):
    batch, draft = deepcopy(priced_pair)
    if kind == "missing": draft = replace(draft, lines=draft.lines[1:])
    elif kind == "archive": draft = replace(draft, lines=draft.lines[1:], archived_lines=(draft.lines[0],))
    elif kind == "duplicate": draft = replace(draft, lines=(draft.lines[0], draft.lines[0]))
    elif kind == "foreign": draft = replace(draft, lines=(replace(draft.lines[0], line_id="foreign"), draft.lines[1]))
    elif kind == "swap": draft = replace(draft, lines=tuple(replace(l, source_line_ids=r.source_line_ids) for l, r in zip(draft.lines, reversed(draft.lines))))
    elif kind == "workgroup": draft.lines[0].configuration["workgroup"] = "999"
    elif kind == "source": draft.source["source_ref_raw"] = "FORGED"
    elif kind == "current_source": batch = replace(batch, raw_text=batch.raw_text + "偽造")
    elif kind == "stale": batch = replace(batch, revision=batch.revision + 1)
    else: batch = None
    spy = Mock(side_effect=AssertionError("不得查價"))
    monkeypatch.setattr(core, "resolve_configuration", spy)
    with pytest.raises(ValueError): build((batch, draft))
    spy.assert_not_called()


@pytest.mark.parametrize("count,expected", [(200, None), (201, "200"), (999, "200"), (1000, "999")])
def test_capacity_contract_no_silent_truncation(count, expected, monkeypatch):
    batch = parse_work_orders("環式會議桌生產工單\n20990101-1合成容量\n" + "\n".join("平直60X120-----1" for _ in range(count)))
    draft = from_work_order(batch, batch.orders[0].order_id)
    spy = Mock(side_effect=AssertionError("尚未核對不得查價"))
    monkeypatch.setattr(core, "resolve_configuration", spy)
    if expected:
        with pytest.raises(ValueError, match=expected): build((batch, draft))
    else:
        doc = build((batch, draft))
        assert len(doc["children"]) == count and doc["totals"] is None
    spy.assert_not_called()


@pytest.mark.parametrize("kind", ["actor", "qty", "reorder", "discount", "source", "drawing", "condition", "tax", "version", "formula", "policy", "max_discount", "per_item", "description_version"])
def test_current_context_invalidates_old_amounts(priced_pair, monkeypatch, kind):
    batch, draft = deepcopy(priced_pair)
    doc, actor = build((batch, draft)), "測試"
    if kind == "actor": actor = "另一人"
    elif kind == "qty": draft.lines[0].configuration["qty"] = 8
    elif kind == "reorder": draft = replace(draft, lines=tuple(reversed(draft.lines)))
    elif kind == "discount": draft = command(draft, {"op": "set_discount", "discount_rate": .1}, batch)
    elif kind == "source": batch = replace(batch, revision=batch.revision + 1)
    elif kind == "drawing": draft = declare(draft, batch)
    elif kind == "condition": draft = condition(batch, draft, "hole_position", "REQUIRED", [draft.lines[0].line_id], draft.requirements[-1].requirement_id)
    elif kind == "per_item": monkeypatch.setattr(core, "PER_ITEM_OPTIONS", {"CHANGED"})
    elif kind == "description_version": monkeypatch.setattr(quote_batch, "CHILD_VERSION", "changed")
    else:
        key = {"tax": "TAX_RATE", "version": "TRIAL_VERSION", "formula": "FORMULA_VERSION", "policy": "POLICY_VERSION", "max_discount": "MAX_DISCOUNT_RATE"}[kind]
        monkeypatch.setattr(core, key, .07 if kind in ("tax", "max_discount") else "changed")
    with pytest.raises(ValueError): check((batch, draft), doc, actor)


@pytest.mark.parametrize("kind", ["amount", "child", "order", "flag", "version", "actor", "trial_id", "seal"])
def test_tamper_even_rehashed_is_rejected(priced_pair, kind):
    doc = build(priced_pair)
    if kind == "amount": doc["totals"]["total_price"] = "0.01"
    elif kind == "child": doc["children"][0]["calculation"]["total_price"] = "0.01"
    elif kind == "order": doc["children"].reverse()
    elif kind == "flag": doc["can_confirm"] = True
    elif kind == "version": doc["document_version"] = "old"
    elif kind == "actor": doc["actor"] = "偽造"
    elif kind == "trial_id": doc["trial_id"] = "forged"
    else: doc["process_seal"] = "forged"
    doc["content_digest"] = quote_batch._digest({k: v for k, v in doc.items() if k not in ("content_digest", "process_seal")})
    with pytest.raises(ValueError): check(priced_pair, doc)


def test_display_and_child_download_never_reprice_after_master_change(priced_pair, isolated_db, monkeypatch):
    batch, draft = priced_pair
    doc = build(priced_pair)
    with isolated_db() as db:
        db.query(Ordspe).update({Ordspe.compri: 999})
        db.commit()
    spy = Mock(side_effect=AssertionError("不得重新計價或查詢"))
    monkeypatch.setattr(repo, "_session", spy)
    monkeypatch.setattr(core, "calculate_quote", spy)
    assert check(priced_pair, doc) == doc
    for child in doc["children"]:
        assert core.checked_child_trial(draft, doc, child["child_quote_id"], actor="測試", source_batch=batch) == child
    with pytest.raises(ValueError): core.checked_child_trial(draft, doc, "foreign", actor="測試", source_batch=batch)
    spy.assert_not_called()


def test_one_cost_lookup_per_child_and_no_sql_writes(priced_pair, isolated_db, monkeypatch):
    with isolated_db() as db: engine = db.get_bind()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany): statements.append(statement)
    spy = Mock(wraps=core._configuration_cost_inputs)
    monkeypatch.setattr(core, "_configuration_cost_inputs", spy)
    event.listen(engine, "before_cursor_execute", capture)
    try: doc = build(priced_pair)
    finally: event.remove(engine, "before_cursor_execute", capture)
    assert spy.call_count == 2 and doc["calculated_count"] == 2
    assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
    with isolated_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == db.query(ordqdt_ai).count() == 0


@pytest.mark.parametrize("target", ["preview", "confirm", "save", "old_trial", "old_snapshot", "description"])
@pytest.mark.parametrize("part", ["batch", "child"])
def test_new_documents_cannot_enter_old_services(priced_pair, target, part):
    from engine.preview import freeze_preview, checked_preview
    from engine.multi_snapshot import build_snapshot_layout
    batch, draft = priced_pair
    document = build(priced_pair)
    if part == "child": document = document["children"][0]
    with pytest.raises(ValueError):
        if target == "preview": freeze_preview(document)
        elif target == "confirm": checked_preview({"preview": document, "status": "PREVIEW"}, document["trial_id"])
        elif target == "save": repo.save_quote_snapshot([], preview=document)
        elif target == "old_trial": checked_internal_trial(draft, document, actor="測試", source_batch=batch)
        elif target == "old_snapshot": build_snapshot_layout(draft, document, actor="測試", source_batch=batch)
        else: quote_batch.checked_quote_batch(draft, document, actor="測試", source_batch=batch)


def test_old_document_not_renamed_as_new(priced_pair):
    batch, draft = priced_pair
    old = build_internal_trial(draft, actor="測試", source_batch=batch)
    with pytest.raises(ValueError): check(priced_pair, old)
    old.update(document_type=core.DOCUMENT_TYPE, trial_version=core.TRIAL_VERSION)
    with pytest.raises(ValueError): check(priced_pair, old)
    with pytest.raises(ValueError): core.build_batch_trial(old, actor="測試", source_batch=batch)
