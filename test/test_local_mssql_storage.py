"""Offline only: MSSQL protocol bridge + real isolated SQLite transactions.

Run with unittest discovery (no project conftest, private settings, ODBC or network).
The bridge does NOT establish SQL Server/ODBC correctness on a live server.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import re
import socket
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import Unicode, create_engine, text
from sqlalchemy.dialects.mssql.pyodbc import MSDialect_pyodbc

from local_quote import backend, business, dynamic_storage as ds, mssql_schema, storage
from local_quote.backend import LocalDatabase, SchemaError
from local_quote.business import MasterSource
from local_quote.dynamic_storage import DynamicStore
from local_quote.storage import BatchStore, LockTimeout, StorageError, freeze_preview
from test_local_business import fixture_tables
from test_local_dynamic_storage import BOARD, fixture, request
from test_local_mssql_backend import CatalogConnection, catalog_rows, column
from test_local_storage import SETTINGS, STAMP, fixture_schema, preview


CANARY = "synthetic-password-and-payload-do-not-echo"
FORBIDDEN = re.compile(r"\bLIMIT\b|\bsubstr\s*\(|\bFOR UPDATE\b|\bDATABASE\s*\(|"
                       r"\bGET_LOCK\s*\(|\bRELEASE_LOCK\s*\(|\+\s*0e0", re.I)


def scalar(value):
    result = Mock()
    result.scalar_one.return_value = value
    return result


class BridgeConnection:
    """Only protocol/metadata and TOP/hints are emulated; DML runs in SQLite."""
    def __init__(self, db, base):
        self.db, self.base = db, base
        self.events, self.statements, self.validations = [], [], []
        self.held = False
        self.inserts = 0

    @property
    def invalidated(self):
        return self.base.invalidated

    def invalidate(self):
        self.events.append("INVALIDATE")
        self.held = False
        self.base.invalidate()

    def execute(self, statement, params=None):
        sql = statement.text  # Preserve :ids/:refs before expanding-bind postcompile.
        assert not FORBIDDEN.search(sql), sql
        # TextClause compilation alone does not validate T-SQL: assert grammar too.
        compiled = str(statement.compile(dialect=MSDialect_pyodbc()))
        assert not FORBIDDEN.search(compiled), compiled
        self.statements.append((sql, deepcopy(params), statement))
        if "sys.sp_getapplock" in sql:
            self.events.append("ACQUIRE")
            self.base.execute(text("SELECT 1"))  # reproduce autobegin
            assert params == {"resource": storage.MSSQL_LOCK_RESOURCE}
            assert "@LockMode='Exclusive'" in sql and "@LockOwner='Session'" in sql
            assert "@DbPrincipal='public'" in sql and "@LockTimeout=5000" in sql
            assert sql.startswith("DECLARE @result int; EXEC @result=")
            assert sql.endswith("; SELECT @result")
            self.held = type(self.db.acquire) is int and self.db.acquire >= 0
            return scalar(self.db.acquire)
        if "sys.sp_releaseapplock" in sql:
            self.events.append("RELEASE")
            assert not self.base.in_transaction(), "release before write rollback"
            assert self.held
            assert params == {"resource": storage.MSSQL_LOCK_RESOURCE}
            assert "@LockOwner='Session'" in sql and "@DbPrincipal='public'" in sql
            self.base.execute(text("SELECT 1"))
            self.held = False
            return scalar(0)
        if "HAS_PERMS_BY_NAME" in sql or "sys." in sql:
            self.events.append("CONTEXT" if "HAS_PERMS_BY_NAME" in sql else "SCHEMA")
            self.base.execute(text("SELECT 1"))
            return self.db.catalog.execute(statement)
        self.events.append(sql)
        if sql.startswith("INSERT"):
            assert self.held and self.base.in_transaction()
            assert "SCHEMA" in self.events
            table = sql.split()[2]
            rows = params if isinstance(params, list) else [params]
            for row in rows:
                assert (table, row) in self.validations, "every row preflight before INSERT"
                for name, spec in self.db._write_capacities[table].items():
                    assert name in row, "nullable text omitted"
                    assert isinstance(statement._bindparams[name].type, Unicode)
                for name in ds.NUMERIC_FIELDS:
                    if name in row:
                        assert not isinstance(statement._bindparams[name].type, Unicode)
            params = deepcopy(params)
            projected = params if isinstance(params, list) else [params]
            if table == "ordqdt_ai" and self.db.numeric_profile == "binary32":
                for row in projected:
                    for name in ds.NUMERIC_FIELDS:
                        if row[name] is not None:
                            row[name] = struct.unpack("!f", struct.pack("!f", row[name]))[0]
            if self.db.corrupt and self.db.corrupt[0] == table:
                _, key, value = self.db.corrupt
                projected[-1][key] = value
            self.inserts += 1
            if self.db.fail_after == self.inserts:
                raise RuntimeError(CANARY)
        top = re.match(r"SELECT TOP (1|100|20001) ", sql)
        if top:
            sql = "SELECT " + sql[top.end():] + " LIMIT " + top[1]
        sql = sql.replace(" WITH (UPDLOCK,HOLDLOCK)", "").replace("SUBSTRING(", "substr(")
        translated = text(sql).bindparams(*statement._bindparams.values())
        return self.base.execute(translated, params or {})

    def begin(self):
        assert self.held
        self.events.append("BEGIN")
        transaction = self.base.begin()
        connection = self

        class Transaction:
            def __enter__(self):
                return transaction.__enter__()

            def __exit__(self, kind, value, traceback):
                assert connection.held
                connection.events.append("COMMIT" if kind is None else "TX_ROLLBACK")
                return transaction.__exit__(kind, value, traceback)

        return Transaction()

    def commit(self):
        assert self.held
        self.events.append("COMMIT")
        self.base.commit()

    def rollback(self):
        self.events.append("ROLLBACK")
        self.base.rollback()


class BridgeDatabase(LocalDatabase):
    """Test-only protocol adapter; production injection restrictions unchanged."""
    def __init__(self, engine, *, profile="binary64", workgroup="103"):
        self.sqlite = LocalDatabase(SETTINGS, engine=engine, testing=True)
        self.workgroup, self.testing = workgroup, False
        self._numeric_profile, self._write_capacities = None, None
        self.connections = []
        self.acquire, self.fail_after, self.corrupt = 0, None, None
        rows = catalog_rows()
        if profile == "binary32":
            for name in ds.NUMERIC_FIELDS:
                column(rows, "ordqdt_ai", name).update(type_name="real", max_length=4, precision=24)
        self.catalog = CatalogConnection(rows)
        self.engine = SimpleNamespace(dialect=SimpleNamespace(name="mssql"), connect=self.connect)
        self.active = None

    @contextmanager
    def connect(self):
        with self.sqlite.engine.connect() as base:
            conn = BridgeConnection(self, base)
            self.connections.append(conn)
            self.active = conn
            try:
                yield conn
            finally:
                self.active = None

    @contextmanager
    def read(self):
        # SQLite's real query-only guard, plus the actual MSSQL context contract.
        with self.sqlite.read() as base:
            conn = BridgeConnection(self, base)
            self.connections.append(conn)
            mssql_schema.check_context(conn)
            yield conn

    def validate_write_values(self, table, values):
        assert self.active is not None and self.active.base.in_transaction()
        assert not self.active.inserts, "all preflight must precede first INSERT"
        super().validate_write_values(table, values)
        self.active.validations.append((table, deepcopy(values)))


class MSSQLStorageTests(unittest.TestCase):
    def setUp(self):
        for target in (patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")),
                       patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")),
                       patch.object(backend, "dotenv_values", side_effect=AssertionError("private settings forbidden")),
                       patch.object(storage, "_now", return_value=STAMP),
                       patch.object(ds, "_now", return_value=STAMP)):
            target.start()
            self.addCleanup(target.stop)
        self.engine = create_engine("sqlite:///:memory:", hide_parameters=True)
        self.addCleanup(self.engine.dispose)
        self.metadata = fixture_schema(self.engine)
        self.db = BridgeDatabase(self.engine)
        self.analysis, self.frozen = request()
        self.old = freeze_preview(preview())

    def save(self, kind="dynamic", *, db=None, frozen=None):
        db = db or self.db
        if kind == "batch":
            frozen = frozen or self.old
            return BatchStore(db).save(frozen, confirmed=True, input_digest=json.loads(frozen.payload)["input_digest"])
        return DynamicStore(db).save(frozen or self.frozen, confirmed=True, current=self.analysis,
                                     trusted_catalog_digest=self.analysis["catalog_digest"])

    def counts(self):
        with self.engine.connect() as conn:
            return tuple(conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
                         for t in ("quote_snapshot_document", "ordqdt_ai"))

    def clear(self):
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM ordqdt_ai"))
            conn.execute(text("DELETE FROM quote_snapshot_document"))

    def test_new_and_old_save_read_list_retry_share_resource_and_transaction(self):
        for kind, count in (("dynamic", (1, 3)), ("batch", (4, 4))):
            with self.subTest(kind=kind):
                saved = self.save(kind)
                self.assertEqual(self.save(kind), saved)
                self.assertEqual(self.counts(), count)
                for conn in self.db.connections[-2:]:
                    self.assertEqual(conn.events[:3], ["ACQUIRE", "ROLLBACK", "BEGIN"])
                    self.assertIn("COMMIT", conn.events)
                    self.assertEqual(conn.events[-3:], ["ROLLBACK", "RELEASE", "ROLLBACK"])
                    self.assertTrue(any("WITH (UPDLOCK,HOLDLOCK)" in s for s, _, _ in conn.statements)
                                    or not conn.inserts)
                # Fresh service can read without a numeric_profile or master inspection.
                reader_db = BridgeDatabase(self.engine)
                store = DynamicStore(reader_db) if kind == "dynamic" else BatchStore(reader_db)
                identity = self.frozen.confirmation_id if kind == "dynamic" else saved["batch_id"]
                with patch.object(reader_db, "inspect_schema", side_effect=AssertionError("history must not inspect masters")):
                    self.assertEqual(store.read(identity), saved)
                    listed = store.list_saved()
                    self.assertEqual(len(listed), 1)
                for conn in reader_db.connections:
                    self.assertEqual(conn.events[0], "CONTEXT")
                    self.assertFalse(any(e == "SCHEMA" for e in conn.events))
                pages = [s for c in reader_db.connections for s, _, _ in c.statements if "TOP 100 " in s]
                self.assertTrue(pages)
                self.assertTrue(all("ORDER BY ref_no DESC" in s for s in pages))
                self.clear()

    def test_profiles_precise_numeric_readback_and_no_tolerance(self):
        cat = fixture()
        cat["options"][BOARD][0]["compri"] = "12345.6789012345"
        self.analysis, self.frozen = request(catalog=cat)
        for profile in ("binary32", "binary64"):
            with self.subTest(profile=profile):
                db = BridgeDatabase(self.engine, profile=profile)
                saved = self.save(db=db)
                self.assertEqual(DynamicStore(db).read(self.frozen.confirmation_id), saved)
                with self.engine.begin() as conn:
                    payload = conn.execute(text("SELECT payload FROM quote_snapshot_document")).scalar_one()
                    self.assertEqual(ds._load_snapshot(payload)["numeric_storage"], profile)
                    conn.execute(text("UPDATE ordqdt_ai SET compri=12345.7 WHERE path=:path"), {"path": BOARD})
                with self.assertRaises(StorageError):
                    DynamicStore(db).read(self.frozen.confirmation_id)
                queries = [s for c in db.connections for s, _, _ in c.statements]
                for field in ds.NUMERIC_FIELDS:
                    self.assertTrue(any(f"CAST(q.{field} AS FLOAT(53))" in s for s in queries))
                self.clear()

    def test_same_identity_changed_valid_document_is_not_an_idempotent_retry(self):
        self.save()
        original_analysis, original_frozen = self.analysis, self.frozen
        cat = fixture()
        cat["options"][BOARD][0]["compri"] = "456.25"
        self.analysis, changed = request(catalog=cat)
        changed = ds.prepare_document(json.loads(changed.payload), confirmation_id=self.frozen.confirmation_id)
        with self.assertRaisesRegex(StorageError, "不同內容"):
            self.save(frozen=changed)
        self.assertEqual(self.counts(), (1, 3))
        self.analysis, self.frozen = original_analysis, original_frozen
        self.save()
        self.clear()
        self.save("batch")
        changed = json.loads(self.old.payload)
        changed["source"] += "不同來源"
        with self.assertRaisesRegex(StorageError, "不同內容"):
            self.save("batch", frozen=freeze_preview(changed))
        self.assertEqual(self.counts(), (4, 4))

    def test_legacy_ntext_payload_uses_unicode_binding(self):
        column(self.db.catalog.rows, "quote_snapshot_document", "payload").update(type_name="ntext", max_length=16)
        self.save()
        self.save("batch")
        self.assertEqual(self.counts(), (5, 7))

    def test_legacy_real_rejects_lossy_projection_without_changing_v1(self):
        data = preview()
        data["children"][0]["calc"]["items"][0]["compri"] = "0.1"
        frozen = freeze_preview(data)
        db = BridgeDatabase(self.engine, profile="binary32")
        with self.assertRaisesRegex(StorageError, "有損"):
            self.save("batch", db=db, frozen=frozen)
        self.assertEqual(self.counts(), (0, 0))
        self.assertNotIn("numeric_storage", json.loads(frozen.payload))

    def test_partial_write_failures_rollback_then_retry_same_identity(self):
        for kind in ("dynamic", "batch"):
            with self.subTest(kind=kind):
                self.db.fail_after = 2
                with self.assertRaises(StorageError) as caught:
                    self.save(kind)
                self.assertNotIn(CANARY, str(caught.exception))
                self.assertEqual(self.counts(), (0, 0))
                self.assertEqual(self.db.connections[-1].events[-3:], ["ROLLBACK", "RELEASE", "ROLLBACK"])
                self.db.fail_after = None
                result = self.save(kind)
                self.assertEqual(self.save(kind), result)
                self.clear()

    def test_readback_corruption_of_document_numeric_or_text_rolls_back(self):
        for kind in ("dynamic", "batch"):
            for corrupt in (("quote_snapshot_document", "payload", CANARY),
                            ("ordqdt_ai", "amount", 1.00000000001),
                            ("ordqdt_ai", "part_desc", "?")):
                with self.subTest(kind=kind, column=corrupt[1]):
                    self.db.corrupt = corrupt
                    with self.assertRaises(StorageError) as caught:
                        self.save(kind)
                    self.assertNotIn(CANARY, str(caught.exception))
                    self.assertEqual(self.counts(), (0, 0))
        self.db.corrupt = None

    def test_saved_partial_or_changed_details_reject_read_list_and_retry(self):
        for kind in ("dynamic", "batch"):
            result = self.save(kind)
            with self.engine.begin() as conn:
                conn.execute(text("DELETE FROM ordqdt_ai WHERE ref_no=:ref"),
                             {"ref": result["ref_no"] if kind == "dynamic" else result["ref_nos"][0]})
            store = DynamicStore(self.db) if kind == "dynamic" else BatchStore(self.db)
            identity = self.frozen.confirmation_id if kind == "dynamic" else result["batch_id"]
            counts = self.counts()
            for action in (lambda: self.save(kind), lambda: store.read(identity), store.list_saved):
                with self.subTest(kind=kind), self.assertRaises(StorageError):
                    action()
            self.assertEqual(self.counts(), counts)
            self.clear()

    def test_capacity_preflight_all_rows_nullable_fields_and_full_header_names(self):
        for kind in ("dynamic", "batch"):
            self.save(kind)
            validations = self.db.connections[-1].validations
            headers = [r for t, r in validations if t == "quote_snapshot_document"]
            details = [r for t, r in validations if t == "ordqdt_ai"]
            self.assertEqual(len(headers), 1 if kind == "dynamic" else 4)
            self.assertEqual(len(details), 3 if kind == "dynamic" else 4)
            self.assertTrue(all(set(r) == {"workgroup", "preview_id", "ref_no", "payload"} for r in headers))
            self.assertTrue(all(set(r) == set(ds.ROW_FIELDS) for r in details))
            if kind == "batch":
                self.assertTrue(all(r[k] is None for r in details for k in ("abndat", "abntim", "usrno")))
            self.clear()

    def test_capacity_failure_in_last_row_prevents_any_insert(self):
        for kind in ("dynamic", "batch"):
            original = self.db.validate_write_values
            count = 0

            def fail_last(table, row):
                nonlocal count
                original(table, row)
                if table == "ordqdt_ai":
                    count += 1
                    if count == (3 if kind == "dynamic" else 4):
                        raise SchemaError("MSSQL 文字容量檢查失敗")

            with patch.object(self.db, "validate_write_values", side_effect=fail_last), self.assertRaises(SchemaError):
                self.save(kind)
            self.assertEqual(self.db.connections[-1].inserts, 0)
            self.assertEqual(self.counts(), (0, 0))

    def test_real_utf16_and_utf8_capacity_checks_and_unicode_payload(self):
        data = preview()
        data["children"][-1]["calc"]["items"][0]["part_desc"] = "😀" * 40
        frozen = freeze_preview(data)  # 40 codepoints, 80 UTF-16 units; cannot fit NVARCHAR(40)
        with self.assertRaises(SchemaError):
            self.save("batch", frozen=frozen)
        self.assertEqual(self.db.connections[-1].inserts, 0)
        # All UTF8 varchar write fields still get Unicode binds, not ANSI bytes.
        for c in self.db.catalog.rows["columns"]:
            if c["table_name"] in backend.WRITE_TABLES and c["type_name"] == "nvarchar":
                c.update(type_name="varchar", codepage=65001,
                         max_length=c["max_length"] // 2 if c["max_length"] > 0 else -1)
        data["children"][-1]["calc"]["items"][0]["part_desc"] = "中" * 14
        with self.assertRaises(SchemaError):
            self.save("batch", frozen=freeze_preview(data))
        self.assertEqual(self.db.connections[-1].inserts, 0)
        self.save("batch")  # short Chinese fixture fits; bridge checks every Unicode bind

    def test_schema_failure_mixed_numeric_unknown_codepage_default_schema_and_permissions(self):
        mutations = [lambda db: column(db.catalog.rows, "ordqdt_ai", "qty").update(type_name="real", max_length=4, precision=24),
                     lambda db: column(db.catalog.rows, "ordqdt_ai", "part_desc").update(type_name="varchar", max_length=40, codepage=932),
                     lambda db: db.catalog.context.update(default_schema="private_fixture"),
                     lambda db: db.catalog.context.update(can_view=0)]
        for kind in ("dynamic", "batch"):
            for mutate in mutations:
                db = BridgeDatabase(self.engine)
                mutate(db)
                with self.subTest(kind=kind), self.assertRaises(SchemaError):
                    self.save(kind, db=db)
                self.assertEqual(db.connections[-1].inserts, 0)
                self.assertEqual(db.connections[-1].events[-3:], ["ROLLBACK", "RELEASE", "ROLLBACK"])
                self.assertEqual(self.counts(), (0, 0))

    def test_lock_failure_precedes_schema_transaction_and_writes(self):
        for kind in ("dynamic", "batch"):
            self.db.acquire = -1
            with self.assertRaises(LockTimeout):
                self.save(kind)
            self.assertEqual(self.db.connections[-1].events, ["ACQUIRE", "ROLLBACK"])
            self.assertEqual(self.counts(), (0, 0))

    def test_interrupt_after_insert_rolls_back_and_releases_for_both_stores(self):
        for kind, cls in (("dynamic", DynamicStore), ("batch", BatchStore)):
            for error in (KeyboardInterrupt, SystemExit):
                original = cls._insert_items

                def interrupted(store, conn, doc):
                    original(store, conn, doc)
                    raise error()

                with patch.object(cls, "_insert_items", interrupted), self.assertRaises(error):
                    self.save(kind)
                self.assertEqual(self.counts(), (0, 0))
                self.assertEqual(self.db.connections[-1].events[-3:], ["ROLLBACK", "RELEASE", "ROLLBACK"])

    def test_cross_workgroup_read_list_save_and_global_number_collision(self):
        other = BridgeDatabase(self.engine, workgroup="104")
        for kind in ("dynamic", "batch"):
            saved = self.save(kind)
            with self.assertRaises(StorageError):
                self.save(kind, db=other)
            store = DynamicStore(other) if kind == "dynamic" else BatchStore(other)
            self.assertEqual(store.list_saved(), [])
            if kind == "dynamic":
                with self.assertRaises(StorageError):
                    store.read(self.frozen.confirmation_id)
            else:
                self.assertIsNone(store.read(saved["batch_id"]))
            self.clear()
        # Occupied number from another group must be seen by global allocation.
        ref = "DQ" + STAMP.strftime("%Y%m%d%H%M%S") + "_001"
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO quote_snapshot_document VALUES (:wg,:id,:ref,:payload)"),
                         dict(wg="104", id="f" * 32, ref=ref, payload="legacy"))
        self.assertTrue(self.save()["ref_no"].endswith("_002"))
        self.clear()
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO quote_snapshot_document VALUES (:wg,:id,:ref,:payload)"),
                         dict(wg="104", id="f" * 32, ref=STAMP.strftime("%Y%m%d%H%M%S") + "_001", payload="legacy"))
        with patch.object(storage.time, "monotonic", side_effect=[0, 6]), self.assertRaises(StorageError):
            self.save("batch")
        self.assertEqual(self.counts(), (1, 0))

    def test_read_and_list_recheck_context_every_time(self):
        self.save()
        for store, identity in ((DynamicStore(self.db), self.frozen.confirmation_id),
                                (BatchStore(self.db), "a" * 32)):
            for context in (dict(default_schema="private_fixture", can_view=1),
                            dict(default_schema="dbo", can_view=0)):
                self.db.catalog.context = context
                for action in (lambda: store.read(identity), store.list_saved):
                    with self.assertRaises(StorageError):
                        action()

    def seed_masters(self, category="CMT1"):
        with self.engine.begin() as conn:
            for code, wg in ((category, "103"), ("OTHER", "103"), (category, "104")):
                conn.execute(self.metadata.tables["invdoc"].insert(),
                             dict(workgroup=wg, prodkind=code, codsc="測試", ordkind="1", quo_rate=1.3))
                for table, rows in fixture_tables(code, wg).items():
                    if table == "ordspd" and code == "OTHER":
                        continue
                    conn.execute(self.metadata.tables[table].insert(), rows)

    def test_master_padded_nchar_results_are_normalized_before_catalog(self):
        self.seed_masters()
        master = MasterSource(self.db)
        expected = master.catalog("CMT1")
        original = BridgeConnection.execute

        def padded(conn, statement, params=None):
            result = original(conn, statement, params)
            if any("FROM " + table + " " in statement.text
                   for table in ("invdoc", "ordstr", "ordspd", "ordspe", "ordqty")):
                rows = [{k: v + "   " if isinstance(v, str) else v for k, v in r.items()}
                        for r in result.mappings().all()]
                result = Mock()
                result.mappings.return_value.all.return_value = rows
            return result

        with patch.object(BridgeConnection, "execute", padded):
            self.assertEqual(master.categories()[0]["prodkind"], "CMT1")
            self.assertEqual(master.catalog("CMT1"), expected)
        self.assertEqual(master._master_row({"name": "  內 部\t "})["name"], "  內 部\t")

    def test_cp950_write_preflight_and_exact_readback_for_both_stores(self):
        for kind in ("dynamic", "batch"):
            db = BridgeDatabase(self.engine)
            for c in db.catalog.rows["columns"]:
                if c["table_name"] in backend.WRITE_TABLES and c["type_name"] == "nvarchar":
                    c.update(type_name="varchar", codepage=950,
                             max_length=c["max_length"] // 2 if c["max_length"] > 0 else -1)
            first = self.save(kind, db=db)
            self.assertEqual(self.save(kind, db=db), first)
            self.clear()
        data = json.loads(self.old.payload)
        data["children"][-1]["calc"]["items"][0]["part_desc"] = "😀"
        with self.assertRaises(SchemaError):
            self.save("batch", db=db, frozen=freeze_preview(data))
        self.assertEqual(db.connections[-1].inserts, 0)
        self.assertEqual(self.counts(), (0, 0))

    def test_master_catalog_prefix_parameter_group_isolation_and_top(self):
        category = "C_'%"
        self.seed_masters(category)
        master = MasterSource(self.db)
        self.assertEqual(len(master.categories()), 2)
        cat = master.catalog(category)
        self.assertEqual(cat["workgroup"], "103")
        self.assertTrue(all(p.startswith(category + "\\") for p in cat["nodes"]))
        queries = self.db.connections[-1].statements
        selected = [(s, p) for s, p, _ in queries if "TOP 20001" in s]
        self.assertEqual(len(selected), 4)
        for sql, params in selected:
            self.assertNotIn(category, sql)
            self.assertEqual(params["wg"], "103")
            self.assertEqual(params["prefix"], category + "\\")
            self.assertEqual(params["size"], len(category) + 1)
            if "FROM ordspd" not in sql:
                self.assertIn("SUBSTRING(", sql)

    def test_master_20000_boundary_and_20001_reject_not_truncate(self):
        self.seed_masters()
        table = self.metadata.tables["ordspe"]
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM ordspe WHERE workgroup='103' AND path LIKE 'CMT1%'"))
            conn.execute(table.insert(), [dict(workgroup="103", path="CMT1\\A001", code=f"X{i:05}",
                                              codsc="合成規格", compri=1) for i in range(20000)])
        # Inspect detached rows, avoiding unrelated duplicate/business validation.
        with patch.object(business, "build_catalog", side_effect=lambda wg, cat, tables, **kw: tables):
            tables = MasterSource(self.db).catalog("CMT1")
            self.assertEqual(len(tables["ordspe"]), 20000)
            with self.engine.begin() as conn:
                conn.execute(table.insert(), dict(workgroup="103", path="CMT1\\A001", code="OVER", codsc="超量", compri=1))
            with self.assertRaisesRegex(ValueError, "上限"):
                MasterSource(self.db).catalog("CMT1")

    def test_master_errors_hide_driver_credentials_and_payload(self):
        for action in (MasterSource(self.db).categories, lambda: MasterSource(self.db).catalog("CMT1")):
            with patch.object(self.db, "read", side_effect=RuntimeError(CANARY)), self.assertRaises(StorageError) as caught:
                action()
            self.assertNotIn(CANARY, str(caught.exception))

    def test_history_keyset_pages_top100_with_mixed_documents(self):
        self.save()
        old = self.save("batch")
        with self.engine.begin() as conn:
            conn.execute(self.metadata.tables["quote_snapshot_document"].insert(),
                         [dict(workgroup="103", preview_id=f"{i:032x}", ref_no=f"ZZ{i:018}", payload="legacy")
                          for i in range(105)])
        self.assertEqual(len(DynamicStore(self.db).list_saved()), 1)
        self.assertEqual(BatchStore(self.db).list_saved()[0]["batch_id"], old["batch_id"])
        for c in self.db.connections[-2:]:
            pages = [(s, p) for s, p, _ in c.statements if "TOP 100 " in s]
            self.assertGreaterEqual(len(pages), 2)
            self.assertTrue(all("ORDER BY ref_no DESC" in s for s, _ in pages))
            self.assertTrue(any(p["cursor"] is not None for _, p in pages))


class LockConnection:
    def __init__(self, acquire=0, release=0, fail_rollback=None):
        self.acquire, self.release, self.fail_rollback = acquire, release, fail_rollback
        self.invalidated, self.events, self.rollbacks = False, [], 0

    def execute(self, statement, params):
        token = "ACQUIRE" if "sys.sp_getapplock" in str(statement) else "RELEASE"
        self.events.append(token)
        value = self.acquire if token == "ACQUIRE" else self.release
        if isinstance(value, BaseException):
            raise value
        return scalar(value)

    def rollback(self):
        self.events.append("ROLLBACK")
        self.rollbacks += 1
        if self.rollbacks == self.fail_rollback:
            raise RuntimeError(CANARY)

    def invalidate(self):
        self.invalidated = True
        self.events.append("INVALIDATE")


class MSSQLLockTests(unittest.TestCase):
    def test_all_nonnegative_acquisition_and_release_codes(self):
        for result in (0, 1, 7):
            conn = LockConnection(acquire=result, release=result)
            with storage._mssql_lock(conn):
                conn.events.append("COMMIT")
            self.assertEqual(conn.events, ["ACQUIRE", "ROLLBACK", "COMMIT", "ROLLBACK", "RELEASE", "ROLLBACK"])

    def test_timeout_other_negative_and_unknown_results(self):
        for result in (-1, -2, -3, -999, -4, None, "0", True):
            conn = LockConnection(acquire=result)
            with self.subTest(result=result), self.assertRaises(LockTimeout if result == -1 else StorageError):
                with storage._mssql_lock(conn):
                    self.fail("lock was not acquired")
            self.assertNotIn("RELEASE", conn.events)
            self.assertEqual(conn.invalidated, type(result) is not int or result not in (-1, -2, -3, -999))

    def test_unknown_acquisition_exception_invalidates_even_on_interrupt(self):
        for error in (RuntimeError(CANARY), KeyboardInterrupt(), SystemExit()):
            conn = LockConnection(acquire=error)
            with self.assertRaises(StorageError if isinstance(error, Exception) else type(error)) as caught:
                with storage._mssql_lock(conn):
                    self.fail("unreachable")
            self.assertNotIn(CANARY, str(caught.exception))
            self.assertEqual(conn.events, ["ACQUIRE", "INVALIDATE"])

    def test_body_failure_and_interrupt_always_cleanup(self):
        for error in (ValueError, KeyboardInterrupt, SystemExit):
            conn = LockConnection()
            with self.assertRaises(error), storage._mssql_lock(conn):
                raise error()
            self.assertEqual(conn.events[-3:], ["ROLLBACK", "RELEASE", "ROLLBACK"])

    def test_release_failure_invalidates_and_never_reports_success(self):
        for result in (-1, -999, None, "0", RuntimeError(CANARY), KeyboardInterrupt(), SystemExit()):
            conn = LockConnection(release=result)
            expected = type(result) if isinstance(result, BaseException) and not isinstance(result, Exception) else StorageError
            with self.assertRaises(expected) as caught:
                with storage._mssql_lock(conn):
                    pass
            self.assertNotIn(CANARY, str(caught.exception))
            self.assertTrue(conn.invalidated)
            self.assertEqual(conn.events[-3:], ["ROLLBACK", "RELEASE", "INVALIDATE"])

    def test_rollback_failure_at_each_stage_invalidates(self):
        for stage in (1, 2, 3):
            conn = LockConnection(fail_rollback=stage)
            with self.assertRaises(StorageError) as caught:
                with storage._mssql_lock(conn):
                    pass
            self.assertNotIn(CANARY, str(caught.exception))
            self.assertTrue(conn.invalidated)
            self.assertEqual(conn.events[-1], "INVALIDATE")


if __name__ == "__main__":
    unittest.main()
