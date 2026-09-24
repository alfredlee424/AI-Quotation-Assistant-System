"""階段 0 的隔離保證與現有工單入口限制；不新增正式解析能力。"""
import socket

import pytest

from work_order_fixtures import CASES


def test_database_and_model_configuration_are_isolated(isolated_db):
    import config
    from database import connection
    from database.models import QuoteSnapshotDocument, ordqdt_ai

    assert config.DB_CONN_STR == "sqlite:///:memory:"
    assert config.USE_LLM is False and config.USE_AZURE is False
    assert connection.engine.url.database == ":memory:"
    with isolated_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == 0
        assert db.query(ordqdt_ai).count() == 0


@pytest.mark.parametrize("method", ["connect", "connect_ex", "create_connection"])
def test_network_guard_rejects_connections_before_io(method):
    # loopback 也不允許；不可依賴外部服務是否在線來讓測試通過。
    with pytest.raises(AssertionError, match="禁止網路連線"):
        if method == "create_connection":
            socket.create_connection(("127.0.0.1", 9))
        else:
            with socket.socket() as connection:
                getattr(connection, method)(("127.0.0.1", 9))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key)
def test_existing_quantity_extractor_does_not_infer_dash_counts(case):
    from agent.rule_parser import extract_quantity

    # 現有單品解析的特性基線，不是未來工單解析器的預期 API。
    assert extract_quantity(case.raw_text) is None


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key)
def test_work_orders_are_rejected_by_legacy_13_column_importer(case, isolated_db):
    from agent.quotation_importer import import_pasted_quote
    from database.models import QuoteSnapshotDocument, ordqdt_ai

    with pytest.raises(ValueError, match="貼上報價單格式錯誤"):
        import_pasted_quote(case.raw_text, product_qty=1)
    with isolated_db() as db:
        assert db.query(QuoteSnapshotDocument).count() == 0
        assert db.query(ordqdt_ai).count() == 0
