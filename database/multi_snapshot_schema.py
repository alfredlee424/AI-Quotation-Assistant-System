"""待 DBA 核准的加法式關聯契約；獨立 metadata，不加入啟動建表。"""
from sqlalchemy import (CheckConstraint, Column, Float, ForeignKeyConstraint, Integer,
                        MetaData, String, Table, Unicode, UniqueConstraint)
from sqlalchemy.schema import CreateTable

from database.models import QuoteSnapshotDocument, ordqdt_ai


SCHEMA_VERSION = "multi-snapshot-relations-proposal-v1"


def proposed_schema():
    """每次建立獨立契約；既有表僅用於解析 FK，不能以此 metadata 全量建表。"""
    metadata = MetaData()
    QuoteSnapshotDocument.__table__.to_metadata(metadata)
    ordqdt_ai.__table__.to_metadata(metadata)
    products = Table(
        "quote_snapshot_product_line", metadata,
        Column("workgroup", String(3), primary_key=True),
        Column("ref_no", String(21), primary_key=True),
        Column("line_id", String(32), primary_key=True),
        Column("display_order", Integer, nullable=False),
        Column("line_revision", Integer, nullable=False),
        Column("prodkind", String(10), nullable=False),
        Column("label", Unicode(200), nullable=False),
        Column("product_qty", Float, nullable=False),
        UniqueConstraint("workgroup", "ref_no", "display_order", name="uq_snapshot_product_order"),
        CheckConstraint("display_order > 0", name="ck_snapshot_product_order"),
        CheckConstraint("line_revision >= 0", name="ck_snapshot_product_revision"),
        CheckConstraint("product_qty > 0", name="ck_snapshot_product_qty"),
        ForeignKeyConstraint(["workgroup", "ref_no"],
                             ["quote_snapshot_document.workgroup", "quote_snapshot_document.ref_no"],
                             name="fk_snapshot_product_document"),
    )
    links = Table(
        "quote_snapshot_row_line", metadata,
        Column("workgroup", String(3), primary_key=True),
        Column("ref_no", String(21), primary_key=True),
        Column("seq_no", String(5), primary_key=True),
        Column("line_id", String(32), nullable=False),
        Column("path", String(100), nullable=False),
        UniqueConstraint("workgroup", "ref_no", "line_id", "path", name="uq_snapshot_line_path"),
        ForeignKeyConstraint(["workgroup", "ref_no", "line_id"],
                             ["quote_snapshot_product_line.workgroup", "quote_snapshot_product_line.ref_no",
                              "quote_snapshot_product_line.line_id"], name="fk_snapshot_row_product"),
        ForeignKeyConstraint(["workgroup", "ref_no", "seq_no"],
                             ["ordqdt_ai.workgroup", "ordqdt_ai.ref_no", "ordqdt_ai.seq_no"],
                             name="fk_snapshot_row_detail"),
    )
    return metadata, products, links


def proposed_ddl(dialect_name):
    """只產生供審查的 SQL 文字，不接受連線、不執行 DDL。"""
    from sqlalchemy.dialects import sqlite, mysql, mssql

    dialects = {"sqlite": sqlite.dialect, "mysql": mysql.dialect, "mssql": mssql.dialect}
    if dialect_name not in dialects:
        raise ValueError("只提供 SQLite、MySQL 與 MSSQL 的待審查建表文字。")
    _, products, links = proposed_schema()
    dialect = dialects[dialect_name]()
    return "-- REVIEW ONLY: not an approved migration; do not run on production.\n" + "\n".join(
        str(CreateTable(table).compile(dialect=dialect)).strip() + ";" for table in (products, links))
