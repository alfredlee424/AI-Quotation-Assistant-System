"""測試程序隔離：匯入 production 之前設定環境，所有 DB 只存在記憶體。

不注入假 database 模組，不讀寫開發 DB，不允許 SDK 發出網路請求。
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory

import pytest


_process_tmp = TemporaryDirectory(prefix="quote-tests-")
os.environ.update({
    "DB_CONN_STR": "sqlite:///:memory:",
    "OPENAI_API_KEY": "", "OPENAI_BASE_URL": "",
    "AZURE_OPENAI_API_KEY": "", "AZURE_OPENAI_ENDPOINT": "",
    "AZURE_OPENAI_AD_TOKEN": "", "AZURE_OPENAI_API_VERSION": "2024-10-21",
    "WORKGROUP": "103", "PRODUCT_PREFIX": "CMT1", "ORDKIND_PRODUCT": "1",
    "MARKUP_RATE": "0.3", "TAX_RATE": "0.05", "MAX_DISCOUNT_RATE": "0.1",
    "REQUIRED_OPTNOS": "A001", "LOG_FILE": str(Path(_process_tmp.name) / "quote.log"),
})


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from database import connection, repository
    from database import models  # 註冊真實 ORM，禁止 MagicMock 掩蓋欄位错误

    def deny_network(*args, **kwargs):
        raise AssertionError("隔離式測試禁止網路連線；請 mock SDK 回應")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    assert connection.engine.url.get_backend_name() == "sqlite"
    assert connection.engine.url.database == ":memory:"
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    sessions = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(connection, "engine", engine)
    monkeypatch.setattr(connection, "SessionLocal", sessions)
    monkeypatch.setattr(repository, "_session", sessions)
    connection.Base.metadata.create_all(engine)
    yield sessions
    engine.dispose()


@pytest.fixture
def cmt1_db(isolated_db):
    """四份原始主檔不改值；只有 invdoc 為明示的測試類別設定。"""
    from cmt1_fixture import load_cmt1_fixture
    from database.models import Invdoc, Ordqty, Ordspd, Ordspe, Ordstr

    models = {"ordstr": Ordstr, "ordspe": Ordspe, "ordqty": Ordqty, "ordspd": Ordspd}
    with isolated_db() as db:
        db.add(Invdoc(workgroup="103", prodkind="CMT1", codsc="辦公桌",
                      ordkind="1", quo_rate=1.0))
        for name, rows in load_cmt1_fixture().items():
            model = models[name]
            columns = set(model.__table__.columns.keys())
            for row in rows:
                db.add(model(**{key: value for key, value in row.items() if key in columns}))
        db.commit()
    return isolated_db


@pytest.fixture
def current_draft(cmt1_db):
    from agent.state import new_quote_draft
    from cmt1_fixture import current_selections

    draft = new_quote_draft()
    draft.update(prodkind="CMT1", product_name="辦公桌", qty=1,
                 selections=current_selections())
    return draft
