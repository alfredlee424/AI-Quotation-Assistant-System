"""批次→獨立子報價→來源品項的加法式關聯草案；不註冊啟動 metadata。

僅回傳獨立 SQLAlchemy 定義，沒有 engine、建表或保存服務。
正式文件／成本仍沿用原表；橋接表僅供未來同交易保存設計審查。
"""
from sqlalchemy import (CheckConstraint, Column, ForeignKeyConstraint, Integer,
                        MetaData, Numeric, String, Table, UnicodeText, UniqueConstraint)


SCHEMA_VERSION = "batch-child-source-draft-v1"


def proposed_batch_schema():
    from database.models import QuoteSnapshotDocument, ordqdt_ai

    metadata = MetaData()
    QuoteSnapshotDocument.__table__.to_metadata(metadata)
    ordqdt_ai.__table__.to_metadata(metadata)
    batches = Table(
        "quote_batch_document", metadata,
        Column("workgroup", String(3), primary_key=True),
        Column("batch_id", String(32), primary_key=True),
        Column("source_batch_id", String(32), nullable=False),
        Column("source_order_id", String(32), nullable=False),
        Column("draft_id", String(32), nullable=False),
        Column("revision", Integer, nullable=False),
        Column("schema_version", String(40), nullable=False),
        Column("document_version", String(40), nullable=False),
        Column("content_digest", String(64), nullable=False),
        Column("payload", UnicodeText, nullable=False),
        UniqueConstraint("workgroup", "batch_id", "source_order_id", name="uq_qbatch_source_order"),
        UniqueConstraint("workgroup", "draft_id", name="uq_qbatch_draft"),
        CheckConstraint("revision >= 0", name="ck_qbatch_revision"),
        CheckConstraint("schema_version = 'batch-child-source-draft-v1'", name="ck_qbatch_schema"),
        CheckConstraint("document_version = 'order-child-expansion-v1'", name="ck_qbatch_document"),
    )
    sources = Table(
        "quote_batch_source_item", metadata,
        Column("workgroup", String(3), primary_key=True),
        Column("batch_id", String(32), primary_key=True),
        Column("source_item_id", String(32), primary_key=True),
        Column("source_order_id", String(32), nullable=False),
        Column("source_line_id", String(32), nullable=False),
        Column("source_line_number", Integer, nullable=False),
        Column("payload", UnicodeText, nullable=False),
        UniqueConstraint("workgroup", "batch_id", "source_line_id", name="uq_qbatch_source_line"),
        CheckConstraint("source_line_number > 0", name="ck_qbatch_source_coord"),
        ForeignKeyConstraint(["workgroup", "batch_id", "source_order_id"],
                             ["quote_batch_document.workgroup", "quote_batch_document.batch_id",
                              "quote_batch_document.source_order_id"], name="fk_qbatch_source_order"),
    )
    children = Table(
        "quote_batch_child", metadata,
        Column("workgroup", String(3), primary_key=True),
        Column("batch_id", String(32), primary_key=True),
        Column("child_quote_id", String(32), primary_key=True),
        Column("source_item_id", String(32), nullable=False),
        Column("configuration_line_id", String(32), nullable=False),
        Column("display_order", Integer, nullable=False),
        Column("line_revision", Integer, nullable=False),
        Column("product_qty", Numeric(18, 0), nullable=False),
        Column("document_version", String(40), nullable=False),
        Column("payload", UnicodeText, nullable=False),
        UniqueConstraint("workgroup", "child_quote_id", name="uq_qbatch_child_identity"),
        UniqueConstraint("workgroup", "batch_id", "source_item_id", name="uq_qbatch_child_source"),
        UniqueConstraint("workgroup", "batch_id", "configuration_line_id", name="uq_qbatch_child_config"),
        UniqueConstraint("workgroup", "batch_id", "display_order", name="uq_qbatch_child_order"),
        CheckConstraint("display_order >= 1 AND display_order <= 999", name="ck_qbatch_child_capacity"),
        CheckConstraint("line_revision >= 0", name="ck_qbatch_child_revision"),
        CheckConstraint("product_qty > 0", name="ck_qbatch_child_qty"),
        CheckConstraint("document_version = 'independent-child-description-v1'", name="ck_qbatch_child_document"),
        ForeignKeyConstraint(["workgroup", "batch_id", "source_item_id"],
                             ["quote_batch_source_item.workgroup", "quote_batch_source_item.batch_id",
                              "quote_batch_source_item.source_item_id"], name="fk_qbatch_child_source"),
    )
    snapshots = Table(
        "quote_batch_child_snapshot", metadata,
        Column("workgroup", String(3), primary_key=True),
        Column("batch_id", String(32), primary_key=True),
        Column("child_quote_id", String(32), primary_key=True),
        Column("ref_no", String(21), nullable=False),
        UniqueConstraint("workgroup", "ref_no", name="uq_qbatch_child_snapshot"),
        ForeignKeyConstraint(["workgroup", "batch_id", "child_quote_id"],
                             ["quote_batch_child.workgroup", "quote_batch_child.batch_id",
                              "quote_batch_child.child_quote_id"], name="fk_qbatch_snapshot_child"),
        ForeignKeyConstraint(["workgroup", "ref_no"],
                             ["quote_snapshot_document.workgroup", "quote_snapshot_document.ref_no"],
                             name="fk_qbatch_snapshot_document"),
    )
    return metadata, batches, sources, children, snapshots
