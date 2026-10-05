"""只在獨立記憶體資料庫驗證待審查關聯，不執行正式遷移。"""
import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError

from database.connection import Base
from database.models import QuoteSnapshotDocument, ordqdt_ai
from database.multi_snapshot_schema import proposed_schema, proposed_ddl


@pytest.fixture
def schema_db():
    metadata, products, links = proposed_schema()
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_fk(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    # 只在此隔離測試建立舊表及新表，並非應用程式的遷移服務。
    metadata.create_all(engine)
    yield engine, metadata, products, links
    engine.dispose()


def seed(connection, metadata, products, *, ref="Q-test", workgroup="103", line="a" * 32, order=1, seq="00001"):
    documents = metadata.tables["quote_snapshot_document"]
    details = metadata.tables["ordqdt_ai"]
    connection.execute(documents.insert().values(workgroup=workgroup, preview_id=ref, ref_no=ref,
                                               payload='{"document_type":"isolated-schema-test"}'))
    connection.execute(products.insert().values(workgroup=workgroup, ref_no=ref, line_id=line,
                                                display_order=order, line_revision=0, prodkind="CMT1",
                                                label="測試產品", product_qty=1))
    connection.execute(details.insert().values(workgroup=workgroup, ref_no=ref, seq_no=seq,
                                               part_code="C001", path=r"CMT1\A001"))


def test_schema_does_not_register_in_startup_metadata_or_change_old_keys(isolated_db):
    before = set(Base.metadata.tables)
    metadata, products, links = proposed_schema()
    assert set(Base.metadata.tables) == before
    assert products.name not in before and links.name not in before
    assert metadata is not Base.metadata
    assert [c.name for c in ordqdt_ai.__table__.primary_key] == ["workgroup", "ref_no", "seq_no"]
    assert ordqdt_ai.__table__.c.seq_no.type.length == 5
    with isolated_db() as db:
        assert products.name not in inspect(db.get_bind()).get_table_names()
        assert links.name not in inspect(db.get_bind()).get_table_names()


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "mssql"])
def test_ddl_only_adds_two_proposed_tables(dialect):
    ddl = proposed_ddl(dialect)
    assert ddl.count("CREATE TABLE") == 2
    assert "CREATE TABLE ordqdt_ai" not in ddl and "CREATE TABLE quote_snapshot_document" not in ddl
    assert "fk_snapshot_row_product" in ddl and "fk_snapshot_row_detail" in ddl
    assert "ALTER TABLE" not in ddl and "DROP TABLE" not in ddl
    assert "REVIEW ONLY" in ddl


def test_unsupported_ddl_rejected():
    with pytest.raises(ValueError):
        proposed_ddl("unknown")


def test_same_path_in_different_products_allowed_but_row_belongs_to_one_product(schema_db):
    engine, metadata, products, links = schema_db
    with engine.begin() as db:
        seed(db, metadata, products)
        db.execute(products.insert().values(workgroup="103", ref_no="Q-test", line_id="b" * 32,
            display_order=2, line_revision=1, prodkind="CMT1", label="第二筆", product_qty=3))
        db.execute(metadata.tables["ordqdt_ai"].insert().values(workgroup="103", ref_no="Q-test",
            seq_no="00002", part_code="C003", path=r"CMT1\A001"))
        for seq, line in (("00001", "a" * 32), ("00002", "b" * 32)):
            db.execute(links.insert().values(workgroup="103", ref_no="Q-test", seq_no=seq, line_id=line, path=r"CMT1\A001"))
    with engine.begin() as db:
        assert len(db.execute(links.select()).all()) == 2
        with pytest.raises(IntegrityError):
            db.execute(links.insert().values(workgroup="103", ref_no="Q-test", seq_no="00001", line_id="b" * 32,
                                            path=r"CMT1\OTHER"))


@pytest.mark.parametrize("invalid", ["foreign_order", "foreign_workgroup", "missing_product", "missing_row", "duplicate_path"])
def test_invalid_relation_cannot_be_inserted(schema_db, invalid):
    engine, metadata, products, links = schema_db
    with engine.begin() as db:
        seed(db, metadata, products)
        db.execute(links.insert().values(workgroup="103", ref_no="Q-test", seq_no="00001",
                                        line_id="a" * 32, path=r"CMT1\A001"))
        db.execute(metadata.tables["ordqdt_ai"].insert().values(workgroup="103", ref_no="Q-test", seq_no="00002",
                                                             part_code="C001", path=r"CMT1\A001"))
    value = dict(workgroup="103", ref_no="Q-test", seq_no="00002", line_id="a" * 32, path=r"CMT1\A001")
    if invalid == "foreign_order":
        value["ref_no"] = "Q-other"
    elif invalid == "foreign_workgroup":
        value["workgroup"] = "104"
    elif invalid == "missing_product":
        value["line_id"] = "b" * 32
    elif invalid == "missing_row":
        value.update(seq_no="99999", path="missing")
    with pytest.raises(IntegrityError):
        with engine.begin() as db:
            db.execute(links.insert().values(**value))


@pytest.mark.parametrize("field,value", [("display_order", 0), ("line_revision", -1), ("product_qty", 0)])
def test_product_positive_and_revision_constraints(schema_db, field, value):
    engine, metadata, products, _ = schema_db
    with engine.begin() as db:
        seed(db, metadata, products)
    with pytest.raises(IntegrityError):
        with engine.begin() as db:
            db.execute(products.update().values(**{field: value}))


def test_full_test_transaction_rolls_back_all_four_tables(schema_db):
    engine, metadata, products, links = schema_db
    with pytest.raises(IntegrityError):
        with engine.begin() as db:
            seed(db, metadata, products)
            db.execute(links.insert().values(workgroup="103", ref_no="Q-test", seq_no="00001",
                                            line_id="a" * 32, path=r"CMT1\A001"))
            assert len(db.execute(products.select()).all()) == len(db.execute(links.select()).all()) == 1
            db.execute(links.insert().values(workgroup="103", ref_no="Q-test", seq_no="99999",
                                            line_id="a" * 32, path="missing-row"))
    with engine.connect() as db:
        assert all(not db.execute(table.select()).all() for table in metadata.tables.values())


def test_historical_document_needs_no_new_relationships(schema_db):
    engine, metadata, products, links = schema_db
    document = metadata.tables[QuoteSnapshotDocument.__tablename__]
    payload = '{"legacy":"unchanged-price-and-version"}'
    with engine.begin() as db:
        db.execute(document.insert().values(workgroup="103", ref_no="Q-old", preview_id="old", payload=payload))
    with engine.connect() as db:
        assert db.execute(document.select()).one().payload == payload
        assert not db.execute(products.select()).all() and not db.execute(links.select()).all()
