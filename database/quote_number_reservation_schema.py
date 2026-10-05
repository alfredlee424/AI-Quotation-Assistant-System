"""獨立保留 registry；不匯入正式 Base，不建表，不連線。"""
from sqlalchemy import (CheckConstraint, Column, ForeignKeyConstraint, Integer,
                        MetaData, String, Table, UnicodeText, UniqueConstraint)


SCHEMA_VERSION = "quote-number-registry-dev-v1"
REQUEST_VERSION = "quote-number-request-dev-v1"


def reservation_schema():
    metadata = MetaData()
    header = Table(
        "quote_number_reservation", metadata,
        Column("batch_id", String(32), primary_key=True),
        Column("workgroup", String(3), nullable=False),
        Column("request_id", String(32), nullable=False, unique=True),
        Column("preview_identity", String(32), nullable=False, unique=True),
        Column("prefix", String(14), nullable=False, unique=True),
        Column("reserved_at", String(32), nullable=False),
        Column("schema_version", String(40), nullable=False),
        Column("request_version", String(40), nullable=False),
        Column("state", String(16), nullable=False),
        Column("origin", String(80), nullable=False),
        Column("child_count", Integer, nullable=False),
        Column("content_digest", String(64), nullable=False),
        Column("payload", UnicodeText, nullable=False),
        UniqueConstraint("batch_id", "workgroup", "prefix", name="uq_qnr_owner"),
        CheckConstraint("child_count BETWEEN 1 AND 999", name="ck_qnr_capacity"),
        CheckConstraint("state = 'RESERVED'", name="ck_qnr_state"),
        CheckConstraint(f"schema_version = '{SCHEMA_VERSION}'", name="ck_qnr_schema"),
        CheckConstraint(f"request_version = '{REQUEST_VERSION}'", name="ck_qnr_request"),
    )
    children = Table(
        "quote_number_reserved_child", metadata,
        Column("batch_id", String(32), primary_key=True),
        Column("child_quote_id", String(32), primary_key=True),
        Column("workgroup", String(3), nullable=False),
        Column("prefix", String(14), nullable=False),
        Column("source_item_id", String(32), nullable=False),
        Column("configuration_line_id", String(32), nullable=False),
        Column("position", Integer, nullable=False),
        Column("suffix", String(3), nullable=False),
        Column("reserved_ref_no", String(18), nullable=False, unique=True),
        UniqueConstraint("child_quote_id", name="uq_qnr_child_identity"),
        UniqueConstraint("batch_id", "source_item_id", name="uq_qnr_source"),
        UniqueConstraint("batch_id", "configuration_line_id", name="uq_qnr_config"),
        UniqueConstraint("batch_id", "position", name="uq_qnr_position"),
        UniqueConstraint("batch_id", "suffix", name="uq_qnr_suffix"),
        CheckConstraint("position BETWEEN 1 AND 999", name="ck_qnr_position"),
        ForeignKeyConstraint(
            ["batch_id", "workgroup", "prefix"],
            ["quote_number_reservation.batch_id", "quote_number_reservation.workgroup",
             "quote_number_reservation.prefix"], name="fk_qnr_child_owner"),
    )
    return metadata, header, children
