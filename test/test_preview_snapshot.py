"""真實 SQLite 的固定預覽、確認、冪等及交易 rollback 測試。"""
from copy import deepcopy
import json

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from agent.tools import create_quote, preview_quote
from database import repository as repo
from database.models import Ordspe, QuoteSnapshotDocument, ordqdt_ai
from engine.preview import checked_preview, freeze_preview


def counts(sessions):
    with sessions() as db:
        return db.query(QuoteSnapshotDocument).count(), db.query(ordqdt_ai).count()


def test_freeze_mutates_state_but_returned_payload_is_a_copy(current_draft):
    frozen = freeze_preview(current_draft)
    assert current_draft["status"] == "PREVIEW"
    assert current_draft["calc_result"] == frozen["calc"]
    assert current_draft["preview"] == frozen
    assert frozen["formula_version"] == "usage-divided-by-yield-v1"
    assert checked_preview(current_draft, frozen["preview_id"]) == frozen
    frozen["calc"]["total_price"] = -100
    assert current_draft["calc_result"]["total_price"] > 0
    assert current_draft["preview"]["calc"]["total_price"] > 0


def test_confirmation_uses_frozen_prices_without_any_master_reread(current_draft, cmt1_db, monkeypatch):
    current_draft["discount_rate"] = .05
    preview_quote(current_draft)
    frozen = deepcopy(current_draft["preview"])
    with cmt1_db() as db:
        db.query(Ordspe).update({Ordspe.compri: 99999})
        db.commit()

    def forbidden(*args, **kwargs):
        raise AssertionError("確認固定預覽不得重新讀取主檔或計價")

    for method in ("get_options_by_path", "get_option_price", "get_quantity_rules",
                   "get_product_category", "get_product_categories", "expand_ordstr_tree"):
        monkeypatch.setattr(repo, method, forbidden)
    monkeypatch.setattr("engine.pricing.calculate_configuration", forbidden)
    result = create_quote(current_draft, preview_id=frozen["preview_id"])
    assert result["calc"] == frozen["calc"]
    assert result["total_price"] == frozen["calc"]["total_price"]
    assert current_draft["status"] == "SNAPSHOT_CREATED"
    with cmt1_db() as db:
        header = db.get(QuoteSnapshotDocument, ("103", frozen["preview_id"]))
        assert json.loads(header.payload) == frozen
        lines = db.query(ordqdt_ai).filter_by(ref_no=result["ref_no"]).all()
        assert len(lines) == len(frozen["calc"]["items"])
        by_path = {line.path: line for line in lines}
        for item in frozen["calc"]["items"]:
            saved = by_path[item["path"]]
            assert saved.opt_code == item["optno"]
            assert saved.spc_code == item["spc_code"]
            assert (saved.qty, saved.stdqty, saved.stdpar, saved.compri) == (
                item["qty"], item["stdqty"], item["stdpar"], item["compri"])
        assert by_path[r"CMT1\A001"].compri == 0
        assert by_path[r"CMT1\A001\S005"].compri == 0
        assert by_path[r"CMT1\A001\S004"].compri == 700


def test_confirm_twice_and_retry_copy_have_one_header_and_no_duplicate_lines(current_draft, cmt1_db):
    frozen = freeze_preview(current_draft)
    retry_copy = deepcopy(current_draft)
    first = create_quote(current_draft, preview_id=frozen["preview_id"])
    second = create_quote(current_draft, preview_id=frozen["preview_id"])
    third = create_quote(retry_copy, preview_id=frozen["preview_id"])
    assert first == second == third
    assert counts(cmt1_db) == (1, len(frozen["calc"]["items"]))


@pytest.mark.parametrize("mutation", ["qty", "code", "line_qty", "discount", "revision", "workgroup",
                                      "product", "questions", "token", "content", "status", "missing"])
def test_invalid_modified_or_missing_preview_rejects_without_writes(current_draft, cmt1_db, mutation):
    frozen = freeze_preview(current_draft)
    token = frozen["preview_id"]
    if mutation == "qty":
        current_draft["qty"] = 2
    elif mutation == "code":
        current_draft["selections"][r"CMT1\A001"]["code"] = "C003"
    elif mutation == "line_qty":
        current_draft["selections"][r"CMT1\A001"]["line_qty"] = 2
    elif mutation == "discount":
        current_draft["discount_rate"] = .05
    elif mutation == "revision":
        current_draft["revision"] += 1
    elif mutation == "workgroup":
        current_draft["workgroup"] = "999"
    elif mutation == "product":
        current_draft["prodkind"] = "OTHER"
    elif mutation == "questions":
        current_draft["questions"] = ["尚待確認"]
    elif mutation == "token":
        token = "wrong-preview"
    elif mutation == "content":
        current_draft["preview"]["calc"]["total_price"] = 1
    elif mutation == "status":
        current_draft["status"] = "WAITING_FOR_INPUT"
    else:
        current_draft.pop("preview")
    with pytest.raises(ValueError):
        create_quote(current_draft, preview_id=token)
    assert counts(cmt1_db) == (0, 0)


def test_preview_token_is_required_keyword(current_draft, cmt1_db):
    freeze_preview(current_draft)
    with pytest.raises(TypeError):
        create_quote(current_draft)
    assert counts(cmt1_db) == (0, 0)


def test_database_failure_rolls_back_header_and_lines_then_retry_succeeds(current_draft, cmt1_db):
    frozen = freeze_preview(current_draft)
    inserted = []

    def fail_after_actual_inserts(session, context):
        # 先證明真實 DB 已收到主檔和明細，再製造 SQL 錯誤，而非 mock save。
        inserted.append((session.query(QuoteSnapshotDocument).count(), session.query(ordqdt_ai).count()))
        session.execute(text("INSERT INTO deliberately_missing_table VALUES (1)"))

    event.listen(Session, "after_flush", fail_after_actual_inserts)
    try:
        with pytest.raises(OperationalError):
            create_quote(current_draft, preview_id=frozen["preview_id"])
    finally:
        event.remove(Session, "after_flush", fail_after_actual_inserts)
    assert inserted == [(1, len(frozen["calc"]["items"]))]
    assert counts(cmt1_db) == (0, 0)
    assert current_draft["status"] == "PREVIEW"
    assert current_draft["ref_no"] is None
    create_quote(current_draft, preview_id=frozen["preview_id"])
    assert counts(cmt1_db) == (1, len(frozen["calc"]["items"]))


def test_paid_parent_and_child_both_survive_preview_and_snapshot(current_draft, cmt1_db):
    from engine.configuration import apply_proposal
    parent = r"CMT1\A001\S050"
    child = parent + r"\S005\S008"
    with cmt1_db() as db:
        db.query(Ordspe).filter_by(path=parent, code="C001").one().compri = 120
        db.commit()
    apply_proposal(current_draft, {"changes": [
        {"op": "set", "path": parent, "code": "C001"},
        {"op": "set", "path": child, "code": "C003"},
    ]})
    frozen = freeze_preview(current_draft)
    result = create_quote(current_draft, preview_id=frozen["preview_id"])
    with cmt1_db() as db:
        rows = {r.path: r for r in db.query(ordqdt_ai).filter_by(ref_no=result["ref_no"]).all()}
        assert rows[parent].compri == 120
        assert rows[parent].amount == 120
        assert rows[parent].opt_code == "S050"
        assert rows[child].compri == 140
        assert rows[child].amount == 93.33
        assert rows[child].opt_code == "S008"


def test_saved_preview_id_rejects_conflicting_payload(current_draft, cmt1_db):
    from engine.calculator import CalcResult
    from engine.snapshot import build_snapshot_items
    frozen = freeze_preview(current_draft)
    create_quote(current_draft, preview_id=frozen["preview_id"])
    conflicting = deepcopy(frozen)
    conflicting["digest"] = "different-content"
    items = build_snapshot_items(CalcResult(**frozen["calc"]), current_draft, frozen["ref_no"])
    with pytest.raises(ValueError, match="不一致"):
        repo.save_quote_snapshot(items, workgroup="103", preview=conflicting)
    assert counts(cmt1_db) == (1, len(frozen["calc"]["items"]))
