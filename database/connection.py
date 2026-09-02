"""
database/connection.py - SQLAlchemy 資料庫連線管理

- 未設定 DB_CONN_STR → SQLite（dev.db，開發模式）
- 設定 DB_CONN_STR    → MSSQL（mssql+pyodbc://...）
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from config import DB_CONN_STR, IS_SQLITE, IS_MYSQL, IS_MSSQL


# ============================================================
# SQLAlchemy Engine
# ============================================================

def _build_engine():
    if IS_SQLITE:
        engine = create_engine(
            DB_CONN_STR,
            connect_args={"check_same_thread": False},
            echo=False,
        )
        # SQLite 啟用外鍵約束
        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    elif IS_MYSQL:
        engine = create_engine(
            DB_CONN_STR,
            pool_pre_ping=True,   # 自動偵測斷線重連
            echo=False,
        )
        
    else:
        # MSSQL：fast_executemany 提升批次寫入效率
        engine = create_engine(
            DB_CONN_STR,
            fast_executemany=True,
            echo=False,
        )
    return engine


engine = _build_engine()

# Session Factory
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


# ============================================================
# Base（所有 ORM 模型繼承此類）
# ============================================================

class Base(DeclarativeBase):
    pass


# ============================================================
# 依賴注入：取得 Session（上下文管理器）
# ============================================================

def get_session():
    """
    取得 SQLAlchemy Session。
    建議使用 with get_session() as session: 形式。
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db():
    """
    直接回傳一個 Session（非 generator）。
    呼叫端需自行負責 commit / close。
    """
    return SessionLocal()
