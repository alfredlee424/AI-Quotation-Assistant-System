"""獨立 SQLite 關聯／交易與待審查 DDL；不是正式遷移或配號驗收。"""
import json

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects import mssql, mysql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from agent.quote_batch import build_quote_batch
from database.connection import Base
from database.quote_batch_schema import SCHEMA_VERSION, proposed_batch_schema
from quote_batch_fixtures import four_items


@pytest.fixture
def schema():
    definition = proposed_batch_schema()
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    definition[0].create_all(engine)
    yield engine, definition
    engine.dispose()


def batch_row(key="a", group="103"):
    return dict(workgroup=group, batch_id=key * 32, source_batch_id="s" * 32, source_order_id=key * 32,
                draft_id=key * 32, revision=0, schema_version=SCHEMA_VERSION,
                document_version="order-child-expansion-v1", content_digest="d" * 64, payload="{}")


def source_row(key="a", group="103", item="i"):
    return dict(workgroup=group, batch_id=key * 32, source_item_id=item * 32, source_order_id=key * 32,
                source_line_id=item * 32, source_line_number=3, payload="{}")


def child_row(key="a", group="103", item="i", child="c", order=1):
    return dict(workgroup=group, batch_id=key * 32, child_quote_id=child * 32, source_item_id=item * 32,
                configuration_line_id=item * 32, display_order=order, line_revision=0, product_qty=2,
                document_version="independent-child-description-v1", payload="{}")


def seed(connection, definition, key="a", group="103"):
    _, batches, sources, children, _ = definition
    connection.execute(batches.insert().values(**batch_row(key, group)))
    connection.execute(sources.insert().values(**source_row(key, group)))
    connection.execute(children.insert().values(**child_row(key, group, child=key)))


def test_independent_metadata_preserves_existing_models_and_old_draft():
    from database.models import ordqdt_ai, QuoteSnapshotDocument
    from database.multi_snapshot_schema import proposed_schema
    before = set(Base.metadata.tables)
    metadata, *tables = proposed_batch_schema()
    assert metadata is not Base.metadata and set(Base.metadata.tables) == before
    assert not {t.name for t in tables} & before
    assert list(metadata.tables[ordqdt_ai.__tablename__].primary_key.columns.keys()) == list(ordqdt_ai.__table__.primary_key.columns.keys())
    assert metadata.tables["ordqdt_ai"].c.seq_no.type.length == 5
    assert metadata.tables["quote_snapshot_document"].c.ref_no.type.length == 21
    assert "quote_snapshot_product_line" not in metadata.tables
    assert "quote_batch_child" not in proposed_schema()[0].tables
    assert QuoteSnapshotDocument.__table__.metadata is Base.metadata


@pytest.mark.parametrize("dialect", [mysql.dialect(), mssql.dialect()])
def test_ddl_compiles_for_review_only(dialect):
    metadata, *tables = proposed_batch_schema()
    ddl = "\n".join(str(CreateTable(t).compile(dialect=dialect)) for t in metadata.sorted_tables)
    assert all(t.name in ddl for t in tables)
    assert "FOREIGN KEY" in ddl and "UNIQUE" in ddl and "CHECK" in ddl
    assert "fk_qbatch_child_source" in ddl and "ck_qbatch_child_capacity" in ddl


@pytest.mark.parametrize("kind", ["source_other_order", "source_other_group", "child_other_batch", "child_other_group", "child_missing_source",
                                  "bridge_other_batch", "bridge_other_group", "bridge_missing_document"])
def test_composite_foreign_keys_reject_cross_scope(schema, kind):
    engine, definition = schema
    metadata, batches, sources, children, bridge = definition
    with engine.begin() as c:
        seed(c, definition)
        c.execute(batches.insert().values(**batch_row("b")))
        c.execute(metadata.tables["quote_snapshot_document"].insert().values(workgroup="103", preview_id="p", ref_no="TEST-REF", payload="{}"))
    if kind.startswith("source"):
        table = sources
        row = source_row(item="j")
        row["source_order_id" if kind == "source_other_order" else "workgroup"] = "b" * 32 if kind == "source_other_order" else "999"
    elif kind.startswith("child"):
        table, row = children, child_row(child="d", order=2)
        row[{"child_other_batch": "batch_id", "child_other_group": "workgroup", "child_missing_source": "source_item_id"}[kind]] = "999" if kind == "child_other_group" else "b" * 32
    else:
        table = bridge
        row = dict(workgroup="103", batch_id="a" * 32, child_quote_id="a" * 32, ref_no="TEST-REF")
        row[{"bridge_other_batch": "batch_id", "bridge_other_group": "workgroup", "bridge_missing_document": "ref_no"}[kind]] = "999" if kind == "bridge_other_group" else "b" * 32
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(table.insert().values(**row))


@pytest.mark.parametrize("field,value", [("display_order", 0), ("display_order", 1000), ("product_qty", 0),
    ("line_revision", -1), ("document_version", "legacy-multi-snapshot"), ("display_order", 1),
    ("source_item_id", "i" * 32), ("configuration_line_id", "i" * 32), ("child_quote_id", "a" * 32)])
def test_child_constraints(schema, field, value):
    engine, definition = schema
    _, _, sources, children, _ = definition
    with engine.begin() as c:
        seed(c, definition)
        c.execute(sources.insert().values(**source_row(item="j")))
    row = child_row(item="j", child="d", order=2)
    row[field] = value
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(children.insert().values(**row))


@pytest.mark.parametrize("field,value", [("schema_version", "old"), ("document_version", "multi_quote_internal_trial_only"), ("revision", -1)])
def test_batch_version_constraints(schema, field, value):
    engine, definition = schema
    row = batch_row()
    row[field] = value
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(definition[1].insert().values(**row))


def write_isolated_layout(c, definition, doc, fail_at=None):
    """測試專用插入，不提供應用保存入口；不產生正式編號。"""
    metadata, batches, sources, children, bridges = definition
    common = dict(workgroup=doc["workgroup"], batch_id=doc["quote_batch_id"])
    c.execute(batches.insert().values(**common, source_batch_id=doc["source_batch_id"], source_order_id=doc["source_order_id"],
        draft_id=doc["draft_id"], revision=doc["draft_revision"], schema_version=SCHEMA_VERSION,
        document_version=doc["document_version"], content_digest=doc["content_digest"], payload=json.dumps(doc)))
    for index, child in enumerate(doc["children"], 1):
        c.execute(sources.insert().values(**common, source_item_id=child["source_item_id"], source_order_id=child["source_order_id"],
            source_line_id=child["source_line"]["line_id"], source_line_number=child["source_line"]["number"], payload=json.dumps(child["source_item"])))
        c.execute(children.insert().values(**common, **{k: child[k] for k in ("child_quote_id", "source_item_id", "configuration_line_id",
            "display_order", "line_revision", "product_qty", "document_version")}, payload=json.dumps(child)))
        if fail_at == index:
            raise RuntimeError("合成子報價插入失敗")
        ref = f"ISOLATED-{index}"
        c.execute(metadata.tables["quote_snapshot_document"].insert().values(workgroup="103", preview_id=f"p{index}", ref_no=ref, payload='{"test_only":true}'))
        c.execute(metadata.tables["ordqdt_ai"].insert().values(workgroup="103", ref_no=ref, seq_no="00001", path=r"CMT1\A001", part_code="C001"))
        if fail_at == "bridge" and index == 4:
            raise RuntimeError("合成關聯插入失敗")
        c.execute(bridges.insert().values(**common, child_quote_id=child["child_quote_id"], ref_no=ref))


@pytest.mark.parametrize("fail_at", [2, 4, "bridge"])
def test_whole_transaction_rolls_back_all_six_tables(schema, fail_at):
    engine, definition = schema
    batch, draft = four_items()
    doc = build_quote_batch(draft, actor="測試", source_batch=batch)
    with pytest.raises(RuntimeError), engine.begin() as c:
        write_isolated_layout(c, definition, doc, fail_at)
    with engine.connect() as c:
        assert all(c.scalar(select(func.count()).select_from(t)) == 0 for t in definition[0].tables.values())


def test_each_child_keeps_own_cost_sequence_and_description(schema):
    engine, definition = schema
    batch, draft = four_items()
    doc = build_quote_batch(draft, actor="測試", source_batch=batch)
    with engine.begin() as c:
        write_isolated_layout(c, definition, doc)
    metadata, _, _, children, bridge = definition
    with engine.connect() as c:
        payloads = c.execute(select(children.c.payload).order_by(children.c.display_order)).scalars().all()
        assert [json.loads(p) for p in payloads] == doc["children"]
        rows = c.execute(select(metadata.tables["ordqdt_ai"])).mappings().all()
        assert len(rows) == 4 and {r["seq_no"] for r in rows} == {"00001"}
        assert len({r["ref_no"] for r in rows}) == 4
        assert c.scalar(select(func.count()).select_from(bridge)) == 4


def test_one_snapshot_cannot_belong_to_two_children(schema):
    engine, definition = schema
    metadata, _, _, _, bridge = definition
    with engine.begin() as c:
        seed(c, definition, "a")
        seed(c, definition, "b")
        c.execute(metadata.tables["quote_snapshot_document"].insert().values(workgroup="103", preview_id="p", ref_no="TEST-REF", payload="{}"))
        c.execute(bridge.insert().values(workgroup="103", batch_id="a" * 32, child_quote_id="a" * 32, ref_no="TEST-REF"))
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(bridge.insert().values(workgroup="103", batch_id="b" * 32, child_quote_id="b" * 32, ref_no="TEST-REF"))
