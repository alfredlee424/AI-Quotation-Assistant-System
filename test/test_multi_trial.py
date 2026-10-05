"""多明細內部計價與彙總，任何結果皆不能建立正式報價。"""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP

import pytest
from sqlalchemy import event

from agent.multi_quote import apply_multi_command, export_multi_draft, from_single_quote, from_work_order, new_multi_quote
from agent.work_orders import parse_work_orders
from database import repository as repo
from database.models import Invdoc, Ordstr, Ordspe, Ordqty
from engine import multi_trial as trial
from engine.pricing import calculate_configuration


def command(draft, content, batch=None):
    return apply_multi_command(draft, content, draft_id=draft.draft_id, expected_revision=draft.revision,
                               actor="測試", reason="內部核對", source_batch=batch)


def build(draft, batch=None):
    return trial.build_internal_trial(draft, actor="測試", source_batch=batch)


@pytest.fixture
def two_cmt(current_draft):
    draft = from_single_quote(current_draft)
    draft = command(draft, {"op": "add", "label": "第二筆桌面"})
    return command(draft, {"op": "configure", "line_id": draft.lines[1].line_id,
                           "proposal": {"prodkind": "CMT1", "qty": 3, "changes": [
                               {"op": "set", "path": s["path"], "code": s["code"], "line_qty": s.get("line_qty", 1)}
                               for s in current_draft["selections"].values()]}})


@pytest.fixture
def mixed_products(isolated_db):
    # 明示合成主檔，不宣稱 MT／DT1 真實產品政策已驗收。
    with isolated_db() as db:
        for product, rate in (("MT", 1.5), ("DT1", 2.0)):
            path = product + r"\A001"
            db.add(Invdoc(workgroup="103", prodkind=product, codsc="合成產品 " + product, ordkind="1", quo_rate=rate))
            db.add(Ordstr(workgroup="103", pathf=product, pathc=path, optnof=product, optnoc="A001", must_chose="Y", seq=1))
            db.add(Ordspe(workgroup="103", path=path, code="C001", codsc="測試規格", compri=0.335))
            db.add(Ordqty(workgroup="103", path=path, code="C001", part_path=path, stdqty=1, stdpar=1))
        db.commit()
    draft = new_multi_quote()
    for product in ("MT", "DT1"):
        draft = command(draft, {"op": "add", "label": product})
        draft = command(draft, {"op": "configure", "line_id": draft.lines[-1].line_id,
                               "proposal": {"prodkind": product, "qty": 1,
                                            "changes": [{"op": "set", "path": product + r"\A001", "code": "C001"}]}})
    return draft


def test_single_internal_trial_matches_existing_price_without_modification(current_draft):
    draft = from_single_quote(current_draft)
    before = export_multi_draft(draft)
    report = build(draft)
    expected, _ = calculate_configuration(current_draft)
    for key in ("total_cost", "subtotal", "discount_amount", "tax_amount", "total_price"):
        assert report["totals"][key] == getattr(expected, key)
    assert report["calculated_count"] == 1
    assert report["can_quote"] is report["can_confirm"] is False
    assert "preview_id" not in report and "ref_no" not in report
    assert report["order_blockers"] == list(draft.blockers)
    assert export_multi_draft(draft) == before


def test_same_paths_are_kept_separate_by_stable_line_id(two_cmt):
    report = build(two_cmt)
    assert report["calculated_count"] == 2 and report["totals"] is not None
    first, second = report["lines"]
    assert first["line_id"] != second["line_id"]
    for row in report["lines"]:
        assert all(item["line_id"] == row["line_id"] for item in row["calculation"]["items"])
    assert float(second["calculation"]["raw_total_cost"]) == pytest.approx(float(first["calculation"]["raw_total_cost"]) * 3)


def test_per_product_rates_order_discount_and_tax_applied_once(mixed_products):
    draft = command(mixed_products, {"op": "set_discount", "discount_rate": 0.1})
    before = deepcopy(draft)
    report = build(draft)
    assert [row["calculation"]["quote_rate"] for row in report["lines"]] == [1.5, 2.0]
    raw_subtotal = 0.335 * 1.5 + 0.335 * 2.0
    amount = lambda value: float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    assert report["totals"]["subtotal"] == amount(raw_subtotal)
    assert report["totals"]["discount_amount"] == amount(raw_subtotal * 0.1)
    after = raw_subtotal - raw_subtotal * 0.1
    assert report["totals"]["tax_amount"] == amount(after * trial.TAX_RATE)
    assert report["totals"]["total_price"] == amount(after + after * trial.TAX_RATE)
    assert draft == before


def test_unrounded_cost_sum_not_sum_of_displayed_costs(mixed_products):
    report = build(mixed_products)
    assert [row["calculation"]["total_cost"] for row in report["lines"]] == [0.34, 0.34]
    assert report["totals"]["total_cost"] == 0.67
    assert report["totals"]["raw_amounts"]["total_cost"] == repr(0.335 + 0.335)


def test_unrounded_subtotal_difference_is_disclosed(mixed_products, isolated_db):
    with isolated_db() as db:
        db.query(Invdoc).update({Invdoc.quo_rate: 1.0})
        db.commit()
    totals = build(mixed_products)["totals"]
    assert totals["subtotal"] == 0.67 and totals["display_subtotal_difference"] == 0.01


@pytest.mark.parametrize("kind", ["missing_product", "missing_quantity", "question", "pending", "wrong_workgroup", "line_discount", "unknown_spec"])
def test_bad_line_is_not_silently_omitted_from_total(two_cmt, kind):
    draft = deepcopy(two_cmt)
    config = draft.lines[1].configuration
    if kind == "missing_product":
        config["prodkind"] = None
    elif kind == "missing_quantity":
        config["qty"] = None
    elif kind == "question":
        config["questions"].append("孔位圖尚未確認")
    elif kind == "pending":
        config["pending_options"] = [{"code": "C999"}]
    elif kind == "wrong_workgroup":
        config["workgroup"] = "999"
    elif kind == "line_discount":
        config["discount_rate"] = 0.1
    else:
        config["selections"][r"CMT1\A001"]["code"] = "INVALID"
    report = build(draft)
    assert len(report["lines"]) == 2 and report["calculated_count"] == 1
    assert report["lines"][1]["issues"] and report["lines"][1]["calculation"] is None
    assert report["totals"] is None and report["status"] == "PARTIAL_OR_BLOCKED"


def test_missing_master_data_blocks_only_affected_product(mixed_products, isolated_db):
    with isolated_db() as db:
        db.query(Ordqty).filter(Ordqty.path == r"MT\A001").delete()
        db.commit()
    report = build(mixed_products)
    assert [row["status"] for row in report["lines"]] == ["BLOCKED", "CALCULATED"]
    assert "缺少用量規則" in report["lines"][0]["issues"][0]
    assert report["totals"] is None


def test_read_failure_keeps_error_not_old_prices(mixed_products, monkeypatch):
    original = repo.get_product_category
    def read(product, **kwargs):
        if product == "DT1":
            raise RuntimeError("sensitive-host-password")
        return original(product, **kwargs)
    monkeypatch.setattr(repo, "get_product_category", read)
    report = build(mixed_products)
    assert report["calculated_count"] == 1 and report["totals"] is None
    assert "sensitive-host-password" not in str(report)


def test_unknown_work_order_requirements_block_even_complete_configuration(current_draft):
    batch = parse_work_orders("環式會議桌生產工單\n20990101-1測試\n平直60X180-----2\n照圖補強木板")
    draft = from_work_order(batch, batch.orders[0].order_id)
    draft = command(draft, {"op": "configure", "line_id": draft.lines[0].line_id,
                            "proposal": {"prodkind": "CMT1", "qty": 2, "changes": [
                                {"op": "set", "path": s["path"], "code": s["code"]} for s in current_draft["selections"].values()]}}, batch)
    report = build(draft, batch)
    assert report["calculated_count"] == 0 and report["totals"] is None
    assert any("來源需求" in message for message in report["lines"][0]["issues"])


def test_archived_line_means_no_order_total(two_cmt):
    draft = command(two_cmt, {"op": "remove", "line_id": two_cmt.lines[1].line_id})
    report = build(draft)
    assert report["calculated_count"] == 1
    assert report["totals"] is None
    assert report["archived_line_ids"] == [two_cmt.lines[1].line_id]


@pytest.mark.parametrize("value", [True, None, -0.1, 2, float("inf"), float("nan")])
def test_bad_order_discount_atomically_rejected(two_cmt, value):
    before = deepcopy(two_cmt)
    with pytest.raises(ValueError):
        command(two_cmt, {"op": "set_discount", "discount_rate": value})
    assert two_cmt == before


def test_discount_command_changes_only_order_revision_and_audit(two_cmt):
    changed = command(two_cmt, {"op": "set_discount", "discount_rate": 0.1})
    assert changed.revision == two_cmt.revision + 1 and changed.discount_rate == 0.1
    assert changed.lines == two_cmt.lines and changed.source == two_cmt.source
    assert changed.blockers == two_cmt.blockers
    assert changed.changes[-1].operation == "set_discount"
    with pytest.raises(ValueError, match="未改變"):
        command(changed, {"op": "set_discount", "discount_rate": 0.1})


@pytest.mark.parametrize("change", ["discount", "reorder", "configuration", "source", "actor", "tax", "tamper"])
def test_old_trial_invalidated_by_review_content_changes(two_cmt, monkeypatch, change):
    report = build(two_cmt)
    draft, actor = two_cmt, "測試"
    if change == "discount":
        draft = command(draft, {"op": "set_discount", "discount_rate": 0.1})
    elif change == "reorder":
        draft = command(draft, {"op": "reorder", "line_ids": [line.line_id for line in reversed(draft.lines)]})
    elif change == "configuration":
        draft = deepcopy(draft)
        draft.lines[0].configuration["qty"] = 8
    elif change == "source":
        draft = replace(draft, source={**draft.source, "changed": True})
    elif change == "actor":
        actor = "其他人"
    elif change == "tax":
        monkeypatch.setattr(trial, "TAX_RATE", 0.07)
    else:
        report["totals"]["total_price"] = 1
    with pytest.raises(ValueError):
        trial.checked_internal_trial(draft, report, actor=actor)


def test_existing_trial_check_does_not_reprice_after_master_change(two_cmt, cmt1_db, monkeypatch):
    report = build(two_cmt)
    with cmt1_db() as db:
        db.query(Ordspe).update({Ordspe.compri: 99})
        db.commit()
    monkeypatch.setattr(repo, "_session", lambda: pytest.fail("核對不應查詢"))
    assert trial.checked_internal_trial(two_cmt, report, actor="測試") == report


def test_readonly_and_internal_report_rejected_by_formal_preview(two_cmt, cmt1_db):
    from engine.preview import checked_preview, freeze_preview
    with cmt1_db() as db:
        engine = db.get_bind()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        report = build(two_cmt)
        with pytest.raises(ValueError):
            checked_preview({"preview": report, "status": "PREVIEW"}, report["trial_id"])
        with pytest.raises(ValueError, match="尚有未確認"):
            freeze_preview(deepcopy(two_cmt.lines[0].configuration))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)


def test_empty_duplicate_and_stale_source_rejected(two_cmt):
    with pytest.raises(ValueError):
        build(new_multi_quote())
    with pytest.raises(ValueError, match="識別重複"):
        build(replace(two_cmt, lines=(two_cmt.lines[0], two_cmt.lines[0])))
    batch = parse_work_orders("環式會議桌生產工單\n20990101-1測試\n平直60X180-----2")
    draft = from_work_order(batch, batch.orders[0].order_id)
    with pytest.raises(ValueError, match="來源工單"):
        build(draft, replace(batch, revision=batch.revision + 1))


@pytest.fixture
def reviewed_order(current_draft):
    from agent.requirement_review import apply_review_command
    batch = parse_work_orders("環式會議桌生產工單\n20990101-1測試\n平直60X180-----2")
    draft = from_work_order(batch, batch.orders[0].order_id)
    draft = command(draft, {"op": "configure", "line_id": draft.lines[0].line_id,
                            "proposal": {"prodkind": "CMT1", "qty": 2, "changes": [
                                {"op": "set", "path": s["path"], "code": s["code"]} for s in current_draft["selections"].values()]}}, batch)
    for record in draft.requirements:
        draft = apply_review_command(draft, {"op": "record", "requirement_id": record.requirement_id,
            "disposition": "metadata", "targets": [], "bindings": [], "reference": "合成測試-v1",
            "explanation": "僅建立測試所需已處置帳本，不宣稱真實語意核准"},
            draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="測試核對", source_batch=batch)
    assert build(draft, batch)["calculated_count"] == 1
    return batch, draft


def test_explicit_forbidden_required_branch_still_blocks_trial(reviewed_order):
    from agent.conditions import apply_condition_command
    batch, draft = reviewed_order
    line_id = draft.lines[0].line_id
    draft = apply_condition_command(draft, {"op": "record", "condition_id": "", "subject": "surface_hole",
        "state": "FORBIDDEN", "scope": "line", "group_label": "", "targets": [line_id], "value": "",
        "requirement_ids": [draft.requirements[-1].requirement_id], "overrides": [],
        "bindings": [{"line_id": line_id, "path": r"CMT1\A001\W001"}],
        "reference": "測試部位限制-v1", "explanation": "以必選工費節點測試否定攔截，不代表實際孔位映射"},
        draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="測試否定", source_batch=batch)
    before = deepcopy(draft)
    report = build(draft, batch)
    assert report["calculated_count"] == 0 and report["totals"] is None
    assert any("禁止" in message for message in report["lines"][0]["issues"])
    assert draft == before


def test_engineering_requirement_blocks_trial_after_other_requirements_reviewed(reviewed_order):
    from agent.requirement_review import apply_review_command
    batch, draft = reviewed_order
    draft = apply_review_command(draft, {"op": "record", "requirement_id": draft.requirements[-1].requirement_id,
        "disposition": "engineering_review", "targets": [draft.lines[0].line_id], "bindings": [],
        "reference": "非標核對-v1", "explanation": "用量須工程核定"}, draft_id=draft.draft_id,
        expected_revision=draft.revision, actor="測試", reason="非標需求", source_batch=batch)
    report = build(draft, batch)
    assert report["calculated_count"] == 0
    assert any("NEEDS_ENGINEERING" in message for message in report["lines"][0]["issues"])


def test_order_ai_question_prevents_partial_assumption(two_cmt):
    from agent.multi_quote import apply_multi_proposal
    draft = apply_multi_proposal(two_cmt, {"draft_id": two_cmt.draft_id, "revision": two_cmt.revision,
        "updates": [], "questions": [{"target_id": two_cmt.draft_id, "text": "所有桌子是否需要特殊補強？"}]},
        allowed_line_ids=tuple(line.line_id for line in two_cmt.lines), actor="測試", reason="整單追問")
    report = build(draft)
    assert report["calculated_count"] == 0
    assert all("所有桌子是否需要特殊補強？" in row["issues"] for row in report["lines"])


def test_zero_usage_and_missing_denominator_keep_existing_formula(mixed_products, isolated_db):
    with isolated_db() as db:
        db.get(Ordqty, ("103", r"MT\A001", "C001")).stdqty = 0
        db.commit()
    report = build(mixed_products)
    assert report["lines"][0]["calculation"]["total_cost"] == 0
    with isolated_db() as db:
        db.get(Ordqty, ("103", r"MT\A001", "C001")).stdpar = 0
        db.commit()
    report = build(mixed_products)
    assert report["lines"][0]["calculation"] is None and report["totals"] is None
