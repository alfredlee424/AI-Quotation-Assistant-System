"""T29 開發專用加法式定義；無正式 Base／連線／隱式建表。"""
from sqlalchemy import (CheckConstraint, Column, ForeignKeyConstraint, Integer,
                        String, Table, UnicodeText, UniqueConstraint)

from database.quote_number_reservation_schema import reservation_schema


SCHEMA_VERSION = "development-atomic-schema-v1"


def development_atomic_schema():
    # Registry 定義僅用來解析 FK；呼叫端須明確選取欲建立的表。
    metadata, _, _ = reservation_schema()
    batch = Table(
        "development_atomic_batch", metadata,
        Column("batch_id", String(32), primary_key=True),
        Column("workgroup", String(3), nullable=False),
        Column("request_id", String(32), nullable=False, unique=True),
        Column("preview_identity", String(32), nullable=False, unique=True),
        Column("save_id", String(32), nullable=False, unique=True),
        Column("prefix", String(14), nullable=False),
        Column("schema_version", String(40), nullable=False),
        Column("state", String(32), nullable=False),
        Column("created_at", String(32), nullable=False),
        Column("child_count", Integer, nullable=False),
        Column("fixed_digest", String(64), nullable=False),
        Column("final_digest", String(64), nullable=False),
        Column("payload", UnicodeText, nullable=False),
        UniqueConstraint("batch_id", "workgroup", "prefix", name="uq_dab_owner"),
        ForeignKeyConstraint(["batch_id", "workgroup", "prefix"],
                             ["quote_number_reservation.batch_id", "quote_number_reservation.workgroup", "quote_number_reservation.prefix"], name="fk_dab_registry"),
        CheckConstraint("child_count BETWEEN 1 AND 999", name="ck_dab_count"),
        CheckConstraint("state = 'DEVELOPMENT_ONLY'", name="ck_dab_state"),
        CheckConstraint(f"schema_version = '{SCHEMA_VERSION}'", name="ck_dab_version"),
    )
    child = Table(
        "development_atomic_child", metadata,
        Column("batch_id", String(32), primary_key=True),
        Column("child_quote_id", String(32), primary_key=True),
        Column("workgroup", String(3), nullable=False),
        Column("prefix", String(14), nullable=False),
        Column("ref_no", String(18), nullable=False, unique=True),
        Column("position", Integer, nullable=False),
        Column("row_count", Integer, nullable=False),
        Column("payload", UnicodeText, nullable=False),
        UniqueConstraint("batch_id", "position", name="uq_dac_position"),
        ForeignKeyConstraint(["batch_id", "workgroup", "prefix"],
                             ["development_atomic_batch.batch_id", "development_atomic_batch.workgroup", "development_atomic_batch.prefix"], name="fk_dac_batch"),
        ForeignKeyConstraint(["batch_id", "child_quote_id"],
                             ["quote_number_reserved_child.batch_id", "quote_number_reserved_child.child_quote_id"], name="fk_dac_registry"),
        ForeignKeyConstraint(["ref_no"], ["quote_number_reserved_child.reserved_ref_no"], name="fk_dac_ref"),
        CheckConstraint("position BETWEEN 1 AND 999", name="ck_dac_position"),
        CheckConstraint("row_count BETWEEN 1 AND 99999", name="ck_dac_rows"),
    )
    link = Table(
        "development_atomic_source_link", metadata,
        Column("batch_id", String(32), primary_key=True),
        Column("child_quote_id", String(32), primary_key=True),
        Column("source_batch_id", String(32), nullable=False),
        Column("source_order_id", String(32), nullable=False),
        Column("source_item_id", String(32), nullable=False),
        Column("configuration_line_id", String(32), nullable=False),
        Column("source_line_no", Integer, nullable=False),
        Column("position", Integer, nullable=False),
        Column("product_qty", Integer, nullable=False),
        Column("payload", UnicodeText, nullable=False),
        UniqueConstraint("batch_id", "source_item_id", name="uq_dal_source"),
        UniqueConstraint("batch_id", "configuration_line_id", name="uq_dal_config"),
        UniqueConstraint("batch_id", "position", name="uq_dal_position"),
        ForeignKeyConstraint(["batch_id", "child_quote_id"], ["development_atomic_child.batch_id", "development_atomic_child.child_quote_id"], name="fk_dal_child"),
        CheckConstraint("product_qty BETWEEN 1 AND 999999999", name="ck_dal_qty"),
        CheckConstraint("source_line_no BETWEEN 1 AND 10000", name="ck_dal_line"),
    )
    cost = Table(
        "development_atomic_cost", metadata,
        Column("batch_id", String(32), primary_key=True),
        Column("child_quote_id", String(32), primary_key=True),
        Column("seq_no", String(5), primary_key=True),
        Column("position", Integer, nullable=False),
        Column("path", String(256), nullable=False),
        Column("payload", UnicodeText, nullable=False),
        UniqueConstraint("batch_id", "child_quote_id", "path", name="uq_dar_path"),
        UniqueConstraint("batch_id", "child_quote_id", "position", name="uq_dar_position"),
        ForeignKeyConstraint(["batch_id", "child_quote_id"], ["development_atomic_child.batch_id", "development_atomic_child.child_quote_id"], name="fk_dar_child"),
        CheckConstraint("position BETWEEN 1 AND 99999", name="ck_dar_position"),
    )
    return metadata, batch, child, link, cost
