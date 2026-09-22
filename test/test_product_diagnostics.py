"""產品選擇診斷：重現名稱不符／歧義，不改變原有選擇行為。"""
from copy import deepcopy

import pytest

from agent import core
from agent.state import QuoteStatus, new_quote_draft
from database import repository as repo
from database.models import Invdoc


EXAMPLE = "我要 1 張 60*180 的環式會議桌，MDF，胡桃，銀鋁單掀，45寬環式腳"


@pytest.mark.parametrize("text", ["環式會議桌", EXAMPLE])
@pytest.mark.parametrize("names,outcome", [
    ([], "no_categories"),
    (["辦公桌"], "no_match"),
    (["環式會議桌", "會議桌"], "ambiguous"),
    (["會議桌"], "selected"),
])
def test_product_match_diagnostics_preserve_decision(monkeypatch, text, names, outcome):
    categories = [{"prodkind": f"P{i}", "codsc": name} for i, name in enumerate(names)]
    monkeypatch.setattr(repo, "get_product_categories", lambda **kw: categories)
    events = []
    monkeypatch.setattr(core, "log_action", lambda action, **kw: events.append(dict(action=action, **kw)))
    draft = new_quote_draft()
    message = core.LLMAgent._prepare_product_context(text, draft)
    event = next(e for e in events if e["action"] == "quote_product_match")
    result = event["result"]
    assert event["params"]["workgroup"] == "103"
    assert result["category_count"] == len(categories)
    assert result["outcome"] == outcome
    assert result["matched_count"] == (len(categories) if outcome in ("ambiguous", "selected") else 0)
    if outcome == "selected":
        assert message is None
        assert draft["prodkind"] == "P0"
    else:
        assert message.startswith("請先選擇報價產品類別：")
        assert not draft.get("prodkind")


def test_existing_product_is_retained_and_logged(monkeypatch):
    monkeypatch.setattr(repo, "get_product_categories", lambda **kw: [])
    events = []
    monkeypatch.setattr(core, "log_action", lambda action, **kw: events.append(dict(action=action, **kw)))
    draft = new_quote_draft()
    draft["prodkind"] = "CMT1"
    before = deepcopy(draft)
    assert core.LLMAgent._prepare_product_context("改成兩張", draft) is None
    assert draft == before
    assert events[-1]["result"]["outcome"] == "keep_existing_product"


@pytest.mark.parametrize("workgroup,ordkind,count", [("103", "1", 1), ("999", "1", 0), ("103", "2", 0)])
def test_category_query_diagnostics(isolated_db, monkeypatch, workgroup, ordkind, count):
    with isolated_db() as db:
        db.add(Invdoc(workgroup="103", prodkind="CMT1   ", codsc="會議桌   ", ordkind="1", quo_rate=1.3))
        db.commit()
    events = []
    monkeypatch.setattr(repo, "log_action", lambda action, **kw: events.append(dict(action=action, **kw)))
    categories = repo.get_product_categories(workgroup=workgroup, ordkind=ordkind)
    assert len(categories) == count
    event = events[-1]
    assert event["action"] == "quote_product_categories_loaded"
    assert event["params"] == {"workgroup": workgroup, "ordkind": ordkind, "backend": "sqlite"}
    assert event["result"] == {
        "category_count": count,
        "categories": [{"prodkind": "CMT1", "codsc": "會議桌"}] if count else [],
        "categories_truncated": False,
    }
    if count:
        assert categories[0]["quo_rate"] == 1.3


def test_rule_route_and_query_failure_are_logged(isolated_db, monkeypatch):
    events = []
    capture = lambda action, **kw: events.append(dict(action=action, **kw))
    monkeypatch.setattr(core, "log_action", capture)
    monkeypatch.setattr(repo, "log_action", capture)
    monkeypatch.setattr(core, "USE_LLM", False)
    with isolated_db() as db:
        Invdoc.__table__.drop(db.get_bind())
    message, draft = core.run_quote_agent(EXAMPLE, new_quote_draft())
    assert draft["status"] == QuoteStatus.WAITING_FOR_INPUT
    assert message.startswith("無法套用需求")
    assert next(e for e in events if e["action"] == "quote_agent_route")["result"] == {"mode": "rule"}
    assert next(e for e in events if e["action"] == "quote_product_categories_failed")["result"] == {
        "error_type": "OperationalError",
    }
    assert next(e for e in events if e["action"] == "quote_rule_turn_failed")["result"]["error_type"] == "OperationalError"
