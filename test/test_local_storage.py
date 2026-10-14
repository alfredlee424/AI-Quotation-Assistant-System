"""Standalone: ./venv/bin/python -m unittest discover -s test -p test_local_storage.py -v

Deliberately uses unittest (no project conftest / models / application imports).
All real SQL runs against a freshly created in-memory SQLite database. MySQL
protocol tests use fakes only. No .env, live database, network, or secret output.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from sqlalchemy import (Column, Float, Integer, MetaData, String, Table, Text,
                        UniqueConstraint, create_engine, event, text)
from sqlalchemy.exc import IntegrityError

from local_quote import backend, storage
from local_quote.backend import LocalDatabase, SchemaError, Settings, SettingsError, load_settings
from local_quote.storage import BatchStore, FrozenPreview, LockTimeout, StorageError, freeze_preview


SETTINGS = Settings("mysql+pymysql://fixture:fixture@localhost/isolated_fixture", "103")
STAMP = datetime(2026, 10, 13, 11, 22, 33, 123456, tzinfo=storage.TAIPEI)


def preview():
    children = []
    for ordinal in range(1, 5):
        children.append({
            "ordinal": ordinal, "product_qty": 2, "description": f"桌子 {ordinal}",
            "source_line": ordinal, "configuration": {"selections": {"CMT1\\S001": "W001"}},
            "requirements": [], "calc": {"total_cost": "6.00", "total_price": "8.00", "items": [{
                "path": "CMT1\\S001", "optno": "S001", "optdesc": "木材", "spc_code": "W001",
                "spdsc": "橡木", "qty": "4", "product_qty": 2, "line_qty": "2", "stdqty": "1",
                "stdpar": "2", "compri": "3", "unit_price": "2", "amount": "8", "part_cost": "6",
                "source": "ordqty", "driver_path": "CMT1\\A001", "driver_code": "D001",
            }]},
        })
    return {"document_type": storage.PREVIEW_TYPE, "document_version": 1,
            "preview_id": uuid4().hex, "workgroup": "103", "created_at": STAMP.isoformat(),
            "category": {"prodkind": "CMT1", "codsc": "桌子", "quo_rate": "1.3"},
            "source": "桌子一\n桌子二\n桌子三\n桌子四", "input_digest": hashlib.sha256(b"input").hexdigest(),
            "batch_total": "32.00", "children": children}


def fixture_schema(engine):
    """Independent DDL fixture: never imported from production models or specs."""
    metadata = MetaData()
    masters = {
        "invdoc": ("workgroup prodkind", "workgroup:3 prodkind:12 codsc:30 ordkind:1", "quo_rate", ""),
        "ordstr": ("workgroup pathf pathc", "workgroup:3 pathf:100 pathc:100 optnof:10 optnoc:10 must_chose:1 has_name:1 has_inname:1", "", "seq"),
        "ordspd": ("workgroup optno", "workgroup:3 optno:10 kind:1 optdesc:30 code:10", "", ""),
        "ordspe": ("workgroup path code codsc", "workgroup:3 path:100 code:10 codsc:40", "compri", ""),
        "ordqty": ("workgroup path code", "workgroup:3 path:100 code:10 codsc:40 part_path:100", "stdqty stdpar", ""),
        "ordqdt_ai": ("workgroup ref_no seq_no", "workgroup:3 ref_no:21 seq_no:5 part_code:10 part_desc:40 path:100 opt_code:10 opt_desc:40 spc_code:10 spdsc:40 unit:5 status:1 transferred:1 adddate:10 addusrno:8 abndat:10 abntim:8 usrno:8 prgno:10", "qty stdqty stdpar compri unit_price amount", ""),
        "quote_snapshot_document": ("workgroup preview_id", "workgroup:3 preview_id:32 ref_no:21", "", ""),
    }
    for name, (pk, strings, floats, ints) in masters.items():
        required = set(pk.split())
        if name == "ordstr":
            required |= {"optnof", "optnoc"}
        if name == "ordqdt_ai":
            required |= {"part_code", "path"}
        if name == "quote_snapshot_document":
            required.add("ref_no")
        columns = []
        for spec in strings.split():
            key, length = spec.split(":")
            columns.append(Column(key, String(int(length)), nullable=key not in required, primary_key=key in pk.split()))
        columns += [Column(key, Float) for key in floats.split()]
        columns += [Column(key, Integer) for key in ints.split()]
        if name == "ordstr":
            columns.append(Column("dmark", Text))
        if name == "quote_snapshot_document":
            columns += [Column("payload", Text, nullable=False), UniqueConstraint("workgroup", "ref_no")]
        Table(name, metadata, *columns)
    metadata.create_all(engine)
    return metadata


class SettingsTests(unittest.TestCase):
    def test_only_two_file_values_no_interpolation_or_environment(self):
        with patch.object(backend, "dotenv_values", return_value={
            "DB_CONN_STR": "mysql+pymysql://u:${NOT_EXPANDED}@localhost/d", "WORKGROUP": "103",
            "API_KEY": "unused", "MODEL": "unused",
        }) as values, patch.dict(os.environ, {"WORKGROUP": "999"}):
            settings = load_settings("not-a-real-env")
        values.assert_called_once_with("not-a-real-env", interpolate=False)
        self.assertEqual(settings.workgroup, "103")
        self.assertIn("${NOT_EXPANDED}", settings.db_conn_str)
        self.assertNotIn("mysql", repr(settings))

    def test_real_dotenv_parser_temp_file_only(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.env"
            path.write_text("DB_CONN_STR='mysql+pymysql://u:${UNSET}@localhost/d'\nWORKGROUP=103\n", encoding="utf-8")
            settings = load_settings(path)
        self.assertIn("${UNSET}", settings.db_conn_str)

    def test_local_hosts(self):
        for host in ("localhost", "127.0.0.1", "[::1]"):
            with self.subTest(host=host):
                self.assertEqual(backend._validated_url(f"mysql+pymysql://u:p@{host}/d?charset=utf8mb4").query["charset"], "utf8mb4")

    def test_reject_bypass_without_echoing_value(self):
        urls = ["sqlite:///:memory:", "mysql://u:p@localhost/d", "mysql+pymysql://u:p@remote/d",
                "mysql+pymysql://u:p@localhost.evil/d", "mysql+pymysql://u:p@127.1/d",
                "mysql+pymysql://u:p@localhost/d?unix_socket=/tmp/a",
                "mysql+pymysql://u:p@localhost/d?host=remote", "mysql+pymysql://u:p@localhost/d?read_default_file=x",
                "mysql+pymysql://u:p@localhost/d?charset=utf8", "mysql+pymysql://u:p@localhost/d?charset=utf8mb4&charset=utf8mb4",
                "mysql+pymysql://u:p@localhost/d?local_infile=1", "mysql+pymysql://u:p@localhost/d?ssl=x",
                "mysql+pymysql://u:p@localhost/d#fragment", "mysql+pymysql://u:p@localhost:99999/d",
                "mysql+pymysql://u:p@localhost", None]
        for url in urls:
            with self.subTest(url_index=urls.index(url)), self.assertRaises(SettingsError) as caught:
                backend._validated_url(url)
            self.assertNotIn("u:p", str(caught.exception))

    def test_engine_requires_explicit_testing(self):
        engine = create_engine("sqlite:///:memory:")
        try:
            with self.assertRaises(SettingsError):
                LocalDatabase(SETTINGS, engine=engine)
            with self.assertRaises(SettingsError):
                LocalDatabase(SETTINGS, testing=True)
            with self.assertRaises(SettingsError):
                LocalDatabase(SETTINGS, engine=Mock(dialect=Mock(name="mysql")), testing=True)
        finally:
            engine.dispose()

    def test_imports_are_independent(self):
        import subprocess
        code = ("import local_quote.backend,local_quote.storage,sys; "
                "forbidden=('database','acceptance','app','utils.config','config','agent'); "
                "assert not any(n==f or n.startswith(f+'.') for n in sys.modules for f in forbidden)")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class FreezeTests(unittest.TestCase):
    def test_detached_immutable_and_decimal_canonical(self):
        p = preview()
        p["batch_total"] = Decimal("32.00")
        frozen = freeze_preview(p)
        p["children"][0]["description"] = "changed"
        self.assertNotIn("changed", frozen.payload)
        self.assertEqual(freeze_preview(json.loads(frozen.payload)), frozen)
        with self.assertRaises(FrozenInstanceError):
            frozen.digest = "x"

    def test_incomplete_fake_or_wrong_source(self):
        mutations = [lambda p: p.update(document_type="development-atomic"),
                     lambda p: p.update(document_version=True),
                     lambda p: p["children"].pop(), lambda p: p["children"][0]["calc"].pop("items"),
                     lambda p: p["children"][0].update(ordinal=2),
                     lambda p: p["children"][1].update(source_line=1),
                     lambda p: p["children"][0].update(source_line=0),
                     lambda p: p.update(source=""), lambda p: p.update(batch_total="31"),
                     lambda p: p["children"][0].update(configuration={}),
                     lambda p: p.update(created_at="2026-01-01T00:00:00"),
                     lambda p: p.update(preview_id="not-a-uuid")]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                p = preview()
                mutate(p)
                with self.assertRaises(StorageError):
                    freeze_preview(p)

    def test_all_string_storage_limits_reject_not_truncate(self):
        for key, length in {"path": 100, "optno": 10, "optdesc": 40, "spc_code": 10,
                            "spdsc": 40, "part_desc": 40, "driver_path": 100, "driver_code": 10}.items():
            with self.subTest(field=key):
                p = preview()
                p["children"][0]["calc"]["items"][0][key] = "界" * (length + 1)
                with self.assertRaises(StorageError):
                    freeze_preview(p)

    def test_nonfinite_negative_bool_and_out_of_range(self):
        for value in (float("nan"), float("inf"), "NaN", "Infinity", "1e100", "-1", True, None):
            for key in storage.NUMBER_FIELDS:
                with self.subTest(field=key, value_type=type(value).__name__):
                    p = preview()
                    p["children"][0]["calc"]["items"][0][key] = value
                    with self.assertRaises(StorageError):
                        freeze_preview(p)

    def test_quantity_contract_and_denominator(self):
        for key, value in (("qty", "2"), ("line_qty", "0"), ("stdpar", "0"), ("product_qty", "3")):
            p = preview()
            p["children"][0]["calc"]["items"][0][key] = value
            with self.assertRaises(StorageError):
                freeze_preview(p)

    def test_container_empty_part_code_preserved(self):
        p = preview()
        p["children"][0]["calc"]["items"][0]["spc_code"] = ""
        self.assertEqual(json.loads(freeze_preview(p).payload)["children"][0]["calc"]["items"][0]["spc_code"], "")

    def test_text_utf8_bytes_not_character_count(self):
        p = preview()
        p["children"][0]["description"] = "😀" * 17000
        with self.assertRaisesRegex(StorageError, "65535"):
            freeze_preview(p)

    def test_exact_text_byte_boundary(self):
        p = preview()
        p["children"][0]["description"] = "x"
        docs = BatchStore._documents(p, "0" * 64, storage._child_ids(p["preview_id"]),
                                     [f"20000101000000_{i:03d}" for i in range(1, 5)],
                                     "2000-01-01T00:00:00.000000+08:00")
        available = storage.TEXT_BYTES - len(storage._canonical(docs[0]).encode("utf-8"))
        p["children"][0]["description"] += "x" * available
        freeze_preview(p)  # 65535 bytes is valid, not 65535 characters.
        p["children"][0]["description"] += "x"
        with self.assertRaisesRegex(StorageError, "65535"):
            freeze_preview(p)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.engine = create_engine("sqlite:///:memory:", hide_parameters=True)
        self.addCleanup(self.engine.dispose)
        self.metadata = fixture_schema(self.engine)
        self.db = LocalDatabase(SETTINGS, engine=self.engine, testing=True)
        self.store = BatchStore(self.db)
        self.p = preview()
        self.frozen = freeze_preview(self.p)
        self.clock = patch.object(storage, "_now", return_value=STAMP)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.sql = []
        event.listen(self.engine, "before_cursor_execute", lambda c, u, s, p, x, m: self.sql.append(s))

    def save(self, frozen=None):
        return self.store.save(frozen or self.frozen, confirmed=True, input_digest=self.p["input_digest"])

    def counts(self):
        with self.engine.connect() as conn:
            return tuple(conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
                         for t in ("quote_snapshot_document", "ordqdt_ai"))

    def test_confirmation_and_input_validation_before_any_sql(self):
        for confirmed in (False, None, 1, "yes"):
            with self.assertRaises(StorageError):
                self.store.save(self.frozen, confirmed=confirmed, input_digest=self.p["input_digest"])
        with self.assertRaises(StorageError):
            self.store.save(self.frozen, confirmed=True, input_digest="0" * 64)
        bad = FrozenPreview(self.frozen.payload + " ", self.frozen.digest)
        with self.assertRaises(StorageError):
            self.save(bad)
        noncanonical = self.frozen.payload + " "
        with self.assertRaises(StorageError):
            self.save(FrozenPreview(noncanonical, storage._digest(noncanonical)))
        self.assertEqual(self.sql, [])

    def test_save_four_and_exact_mapping_single_commit(self):
        commits = []
        event.listen(self.engine, "commit", lambda c: commits.append(True))
        result = self.save()
        self.assertEqual(self.counts(), (4, 4))
        self.assertEqual(len(commits), 1)
        self.assertEqual(result["ref_nos"], [f"20261013112233_{i:03d}" for i in range(1, 5)])
        with self.engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM ordqdt_ai ORDER BY ref_no")).mappings().all()
        for row in rows:
            self.assertEqual((row["seq_no"], row["part_code"], row["part_desc"], row["qty"], row["unit"]),
                             ("00001", "W001", "橡木", 4.0, ""))
            self.assertEqual((row["opt_code"], row["compri"], row["amount"]), ("S001", 3.0, 8.0))
        self.assertEqual(result, self.store.read(self.p["preview_id"]))

    def test_idempotence_returns_original_timestamp_no_repricing(self):
        first = self.save()
        with patch.object(storage, "_now", return_value=STAMP + timedelta(days=1)):
            self.assertEqual(first, self.save())
        self.assertEqual(self.counts(), (4, 4))

    def test_same_id_different_frozen_content_rejected(self):
        self.save()
        changed = deepcopy(self.p)
        changed["children"][0]["description"] = "另一份"
        with self.assertRaisesRegex(StorageError, "不同內容"):
            self.save(freeze_preview(changed))
        self.assertEqual(self.counts(), (4, 4))

    def test_mid_batch_actual_insert_error_rolls_back_everything(self):
        inserted = []
        original = self.store._insert_items
        def fail(conn, doc):
            original(conn, doc)
            inserted.append(conn.execute(text("SELECT count(*) FROM ordqdt_ai")).scalar_one())
            if len(inserted) == 3:
                raise RuntimeError("injected failure")
        with patch.object(self.store, "_insert_items", side_effect=fail), self.assertRaises(StorageError):
            self.save()
        self.assertEqual(inserted, [1, 2, 3])
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(len(self.save()["children"]), 4)

    def test_real_integrity_error_rolls_back_previous_children(self):
        original = self.store._insert_items
        failures = []
        def duplicate(conn, doc):
            original(conn, doc)
            if doc["child"]["ordinal"] == 3:
                try:
                    original(conn, doc)
                except IntegrityError:
                    failures.append(True)
                    raise
        with patch.object(self.store, "_insert_items", side_effect=duplicate), self.assertRaises(StorageError):
            self.save()
        self.assertEqual(failures, [True])
        self.assertEqual(self.counts(), (0, 0))

    def test_external_unique_collision_after_allocation_rolls_back_first_two(self):
        refs = [f"20261013112233_{i:03d}" for i in range(1, 5)]
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO quote_snapshot_document VALUES ('103',:id,:r,'{}')"),
                         {"id": uuid4().hex, "r": refs[2]})
        # Simulate an external writer winning after the collision SELECT:
        # the database constraint, not an application check, must reject it.
        with patch.object(self.store, "_allocate_refs", return_value=refs), self.assertRaises(StorageError):
            self.save()
        self.assertEqual(self.counts(), (1, 0))

    def test_sequences_reset_per_child_and_container_mapping(self):
        p = deepcopy(self.p)
        for child in p["children"]:
            container = deepcopy(child["calc"]["items"][0])
            container.update(path="CMT1", spc_code="", spdsc="", source="zero_cost",
                             compri="0", amount="0", part_cost="0", unit_price="0")
            child["calc"]["items"].insert(0, container)
        self.save(freeze_preview(p))
        with self.engine.connect() as conn:
            rows = conn.execute(text("SELECT ref_no,seq_no,part_code,unit FROM ordqdt_ai ORDER BY ref_no,seq_no")).all()
        self.assertEqual(len(rows), 8)
        self.assertEqual([r.seq_no for r in rows], ["00001", "00002"] * 4)
        self.assertTrue(all(r.part_code == "" and r.unit == "" for r in rows[::2]))

    def test_oversized_manual_token_is_rejected_before_sql(self):
        p = deepcopy(self.p)
        p["children"][3]["description"] = "漢" * 24000
        payload = storage._canonical(p)
        with self.assertRaisesRegex(StorageError, "65535"):
            self.save(FrozenPreview(payload, storage._digest(payload)))
        self.assertEqual(self.sql, [])

    def test_partial_history_never_repaired(self):
        self.save()
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM quote_snapshot_document WHERE preview_id=:id"),
                         {"id": storage._child_ids(self.p["preview_id"])[2]})
        self.sql.clear()
        for action in (self.save, lambda: self.store.read(self.p["preview_id"]), self.store.list_saved):
            with self.assertRaises(StorageError):
                action()
        self.assertFalse(any(s.startswith("INSERT") for s in self.sql))
        self.assertEqual(self.counts(), (3, 4))

    def test_collision_in_either_table_other_workgroup_waits_real_next_second(self):
        for table in ("ordqdt_ai", "quote_snapshot_document"):
            with self.subTest(table=table):
                ref = "20261013112233_001"
                with self.engine.begin() as conn:
                    if table == "ordqdt_ai":
                        conn.execute(text("INSERT INTO ordqdt_ai (workgroup,ref_no,seq_no,part_code,path) "
                                          "VALUES ('999',:r,'00001','','x')"), {"r": ref})
                    else:
                        conn.execute(text("INSERT INTO quote_snapshot_document VALUES ('999',:id,:r,'{}')"),
                                     {"id": uuid4().hex, "r": ref})
                fresh = preview()
                with patch.object(storage, "_now", side_effect=[STAMP, STAMP + timedelta(seconds=1), STAMP]), \
                     patch.object(storage.time, "sleep") as sleep:
                    result = self.save(freeze_preview(fresh))
                self.assertEqual(result["ref_nos"][0], "20261013112234_001")
                sleep.assert_called_once()
                with self.engine.begin() as conn:
                    conn.execute(text("DELETE FROM quote_snapshot_document"))
                    conn.execute(text("DELETE FROM ordqdt_ai"))

    def test_collision_timeout_does_not_invent_reservation(self):
        self.save()
        fresh = preview()
        before = self.counts()
        with patch.object(storage.time, "monotonic", side_effect=[0, 6]), self.assertRaisesRegex(StorageError, "碰撞"):
            self.save(freeze_preview(fresh))
        self.assertEqual(self.counts(), before)

    def test_history_only_snapshot_queries_and_zero_writes(self):
        saved = self.save()
        self.sql.clear()
        self.assertEqual(saved, self.store.read(self.p["preview_id"]))
        self.assertEqual(self.store.list_saved(), [{"batch_id": self.p["preview_id"], "ref_nos": saved["ref_nos"]}])
        queries = [s for s in self.sql if not s.startswith("PRAGMA")]
        self.assertTrue(queries)
        self.assertTrue(all(s.startswith("SELECT") and "FROM quote_snapshot_document" in s for s in queries))

    def test_legacy_snapshot_ignored_and_unchanged(self):
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO quote_snapshot_document VALUES ('103',:id,'OLD','{\"document_type\":\"single-product\"}')"), {"id": uuid4().hex})
        self.assertEqual(self.store.list_saved(), [])
        self.assertEqual(self.counts(), (1, 0))

    def test_history_tamper_rejected_even_if_per_document_digest_resealed(self):
        saved = self.save()
        doc = deepcopy(saved["children"][0])
        doc["child"]["calc"]["items"][0]["compri"] = "999"
        doc["snapshot_digest"] = storage._digest(storage._canonical({k: v for k, v in doc.items() if k != "snapshot_digest"}))
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE quote_snapshot_document SET payload=:p WHERE preview_id=:id"),
                         {"p": storage._canonical(doc), "id": doc["preview_id"]})
        with self.assertRaisesRegex(StorageError, "摘要"):
            self.store.read(self.p["preview_id"])

    def test_read_absent_workgroup_isolation_and_limit_validation(self):
        self.assertIsNone(self.store.read(uuid4().hex))
        for limit in (0, 101, True, "20"):
            with self.assertRaises(StorageError):
                self.store.list_saved(limit)
        other = deepcopy(self.p)
        other["workgroup"] = "104"
        with self.assertRaises(StorageError):
            self.save(freeze_preview(other))
        self.assertEqual(self.counts(), (0, 0))

    def test_schema_checked_every_save_before_any_insert(self):
        with patch.object(self.db, "inspect_schema", side_effect=SchemaError("blocked")) as check:
            with self.assertRaises(SchemaError):
                self.save()
        check.assert_called_once()
        self.assertFalse(any(s.startswith("INSERT") for s in self.sql))

    def test_schema_type_length_pk_unique_fk_engine_charset_and_trigger(self):
        self.assertTrue(self.db.inspect_schema()["ok"])
        with self.engine.connect() as conn:
            facts = self.db._sqlite_schema(conn)
        for table in facts.values():
            table["engine"], table["collation"] = "InnoDB", "utf8mb4_unicode_ci"
            for col in table["columns"].values():
                col["charset"] = "utf8mb4"
        self.assertTrue(backend._check_schema(facts)["ok"])
        cases = [lambda f: f.pop("invdoc"),
                 lambda f: f["ordqdt_ai"]["columns"]["path"].update(length=99),
                 lambda f: f["ordqdt_ai"]["columns"]["qty"].update(type="double"),
                 lambda f: f["quote_snapshot_document"]["columns"]["payload"].update(type="longtext"),
                 lambda f: f["quote_snapshot_document"]["columns"]["payload"].update(nullable=True),
                 lambda f: f["ordqdt_ai"].update(pk=["ref_no"]),
                 lambda f: f["quote_snapshot_document"].update(unique=[]),
                 lambda f: f["ordqdt_ai"].update(fk=[True]),
                 lambda f: f["ordqdt_ai"].update(triggers=[True]),
                 lambda f: f["ordqdt_ai"].update(engine="MyISAM"),
                 lambda f: f["invdoc"].update(collation="utf8_general_ci"),
                 lambda f: f["ordqdt_ai"]["columns"]["path"].update(charset="utf8"),
                 lambda f: f["ordqdt_ai"]["columns"]["amount"].update(generated=True)]
        for index, mutate in enumerate(cases):
            with self.subTest(index=index):
                changed = deepcopy(facts)
                mutate(changed)
                with self.assertRaises(SchemaError):
                    backend._check_schema(changed)

    def test_runtime_schema_failure_and_trigger_are_readonly(self):
        with self.engine.begin() as conn:
            conn.exec_driver_sql("CREATE TRIGGER fixture_trigger AFTER INSERT ON ordqdt_ai BEGIN SELECT 1; END")
        self.sql.clear()
        with self.assertRaises(SchemaError):
            self.save()
        self.assertFalse(any(s.startswith(("INSERT", "CREATE", "UPDATE", "DELETE")) for s in self.sql))
        self.assertEqual(self.counts(), (0, 0))

    def test_actual_mysql_master_char_double_contract(self):
        with self.engine.connect() as conn:
            facts = self.db._sqlite_schema(conn)
        for name, table in facts.items():
            table["engine"], table["collation"] = "InnoDB", "utf8mb4_0900_ai_ci"
            for col in table["columns"].values():
                col["charset"] = "utf8mb4"
                if name not in backend.WRITE_TABLES:
                    if col["type"] == "varchar":
                        col["type"] = "char"
                    if name in {"ordspe", "ordqty"} and col["type"] == "float":
                        col["type"] = "double"
        self.assertTrue(backend._check_schema(facts)["ok"])
        facts["ordqdt_ai"]["columns"]["path"]["type"] = "char"
        with self.assertRaises(SchemaError):
            backend._check_schema(facts)

    def test_read_context_blocks_writes_transaction_escape_and_always_resets(self):
        for statement in ("INSERT INTO invdoc (workgroup,prodkind) VALUES ('103','X')", "COMMIT", "PRAGMA query_only=OFF"):
            with self.assertRaises(ValueError):
                with self.db.read() as conn:
                    conn.execute(text(statement))
        for method in ("commit", "rollback"):
            with self.assertRaises(ValueError):
                with self.db.read() as conn:
                    getattr(conn, method)()
        with self.assertRaises(RuntimeError):
            with self.db.read() as conn:
                self.assertEqual(conn.execute(text("SELECT 1")).scalar_one(), 1)
                raise RuntimeError("fixture")
        with self.engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("PRAGMA query_only").scalar_one(), 0)
        self.assertEqual(self.counts(), (0, 0))

    def test_mysql_read_start_transaction_and_final_rollback_runtime(self):
        # Intercept ONLY MySQL's transaction command; all SELECT/rollback work
        # executes through a real SQLAlchemy connection backed by isolated SQLite.
        statements = []
        def translate(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
            if statement == "START TRANSACTION READ ONLY":
                return "BEGIN", parameters
            return statement, parameters
        event.listen(self.engine, "before_cursor_execute", translate, retval=True)
        rollbacks = []
        event.listen(self.engine, "rollback", lambda c: rollbacks.append(True))
        self.db.testing = False
        try:
            with self.assertRaises(RuntimeError):
                with self.db.read() as conn:
                    self.assertEqual(conn.execute(text("SELECT 1")).scalar_one(), 1)
                    raise RuntimeError("fixture")
        finally:
            self.db.testing = True
            event.remove(self.engine, "before_cursor_execute", translate)
        self.assertEqual(statements[0], "START TRANSACTION READ ONLY")
        self.assertEqual(len(rollbacks), 1)

    def test_fake_mysql_lock_wraps_real_sqlite_save_and_idempotence(self):
        bridge = MySQLSQLiteEngine(self.engine)
        db = Mock(engine=bridge, workgroup="103", testing=False)
        db.inspect_schema.side_effect = lambda conn: self.db.inspect_schema(conn.base)
        store = BatchStore(db)
        first = store.save(self.frozen, confirmed=True, input_digest=self.p["input_digest"])
        second = store.save(self.frozen, confirmed=True, input_digest=self.p["input_digest"])
        self.assertEqual(first, second)
        self.assertEqual(self.counts(), (4, 4))
        for conn in bridge.connections:
            self.assertEqual(conn.events[:4], ["DATABASE", "GET_LOCK", "ROLLBACK", "BEGIN"])
            self.assertIn("COMMIT", conn.events)
            self.assertEqual(conn.events[-3:], ["ROLLBACK", "RELEASE_LOCK", "ROLLBACK"])

    def test_fake_mysql_lock_released_after_actual_partial_writes_fail(self):
        bridge = MySQLSQLiteEngine(self.engine)
        db = Mock(engine=bridge, workgroup="103", testing=False)
        db.inspect_schema.side_effect = lambda conn: self.db.inspect_schema(conn.base)
        store = BatchStore(db)
        original = store._insert_items
        inserted = []
        def fail(conn, doc):
            original(conn, doc)
            inserted.append(conn.execute(text("SELECT count(*) FROM ordqdt_ai")).scalar_one())
            if doc["child"]["ordinal"] == 3:
                raise RuntimeError("fixture")
        with patch.object(store, "_insert_items", side_effect=fail), self.assertRaises(StorageError):
            store.save(self.frozen, confirmed=True, input_digest=self.p["input_digest"])
        self.assertEqual(inserted, [1, 2, 3])
        self.assertEqual(self.counts(), (0, 0))
        self.assertNotIn("COMMIT", bridge.connections[0].events)
        self.assertEqual(bridge.connections[0].events[-3:], ["ROLLBACK", "RELEASE_LOCK", "ROLLBACK"])

    def test_fake_mysql_getlock_timeout_prevents_schema_and_writes(self):
        bridge = MySQLSQLiteEngine(self.engine, acquire=0)
        db = Mock(engine=bridge, workgroup="103", testing=False)
        with self.assertRaises(LockTimeout):
            BatchStore(db).save(self.frozen, confirmed=True, input_digest=self.p["input_digest"])
        db.inspect_schema.assert_not_called()
        self.assertNotIn("BEGIN", bridge.connections[0].events)
        self.assertEqual(self.counts(), (0, 0))

    def test_mysql_schema_normalizer_select_only_runtime(self):
        with self.engine.connect() as conn:
            facts = self.db._sqlite_schema(conn)
        rows = {}
        rows["TABLES"] = [{"TABLE_NAME": t, "ENGINE": "InnoDB", "TABLE_COLLATION": "utf8mb4_unicode_ci"}
                          for t in facts]
        rows["COLUMNS"] = [{"TABLE_NAME": t, "COLUMN_NAME": k, "DATA_TYPE": c["type"],
                            "CHARACTER_MAXIMUM_LENGTH": c["length"], "IS_NULLABLE": "YES" if c["nullable"] else "NO",
                            "CHARACTER_SET_NAME": "utf8mb4", "COLUMN_DEFAULT": None, "EXTRA": ""}
                           for t, f in facts.items() for k, c in f["columns"].items()]
        rows["STATISTICS"] = []
        for table, f in facts.items():
            indexes = [("PRIMARY", f["pk"])] + [(f"uq_{i}", cols) for i, cols in enumerate(f["unique"])]
            for name, cols in indexes:
                rows["STATISTICS"] += [{"TABLE_NAME": table, "INDEX_NAME": name, "COLUMN_NAME": col,
                                        "NON_UNIQUE": 0, "SUB_PART": None} for col in cols]
        rows["KEY_COLUMN_USAGE"], rows["TRIGGERS"] = [], []
        conn = Mock()
        queries = []
        def execute(statement):
            sql = str(statement)
            queries.append(sql)
            table = sql.split("FROM information_schema.")[1].split()[0]
            result = Mock()
            result.mappings.return_value.all.return_value = rows[table]
            return result
        conn.execute.side_effect = execute
        self.assertTrue(backend._check_schema(LocalDatabase._mysql_schema(conn))["ok"])
        self.assertEqual(len(queries), 5)
        self.assertTrue(all(q.startswith("SELECT") for q in queries))
        rows["TRIGGERS"] = [{"EVENT_OBJECT_TABLE": "ordqdt_ai"}]
        with self.assertRaises(SchemaError):
            backend._check_schema(LocalDatabase._mysql_schema(conn))


class MySQLSQLiteConnection:
    """Fake ONLY MySQL protocol; execute data SQL using a real SQLite transaction."""
    def __init__(self, base, acquire):
        self.base, self.acquire, self.events = base, acquire, []

    @property
    def invalidated(self):
        return self.base.invalidated

    def execute(self, stmt, params=None):
        sql = str(stmt)
        for token, value in (("DATABASE()", "fixture"), ("GET_LOCK", self.acquire), ("RELEASE_LOCK", 1)):
            if token in sql:
                self.events.append(token.removesuffix("()"))
                self.base.execute(text("SELECT 1"))  # creates real autobegin
                return FakeResult(value)
        if sql.endswith(" FOR UPDATE"):
            stmt = text(stmt.text.removesuffix(" FOR UPDATE")).bindparams(storage.bindparam("refs", expanding=True))
        return self.base.execute(stmt, params or {})

    def rollback(self):
        self.events.append("ROLLBACK")
        self.base.rollback()

    def invalidate(self):
        self.events.append("INVALIDATE")
        self.base.invalidate()

    @contextmanager
    def begin(self):
        self.events.append("BEGIN")
        with self.base.begin():
            yield
        self.events.append("COMMIT")


class MySQLSQLiteEngine:
    def __init__(self, engine, acquire=1):
        self.engine, self.acquire, self.connections = engine, acquire, []

    @contextmanager
    def connect(self):
        with self.engine.connect() as base:
            conn = MySQLSQLiteConnection(base, self.acquire)
            self.connections.append(conn)
            yield conn


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class FakeMySQLConnection:
    def __init__(self, acquire=1, release=1, database="isolated_fixture"):
        self.acquire, self.release, self.database = acquire, release, database
        self.calls, self.invalidated, self.active = [], False, False

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.calls.append((sql, params))
        self.active = True
        value = self.database if "DATABASE()" in sql else self.acquire if "GET_LOCK" in sql else self.release
        if isinstance(value, Exception):
            raise value
        return FakeResult(value)

    def rollback(self):
        self.calls.append(("ROLLBACK", None))
        self.active = False

    def invalidate(self):
        self.calls.append(("INVALIDATE", None))
        self.invalidated = True

    @contextmanager
    def begin(self):
        if self.active:
            raise AssertionError("autobegin not rolled back")
        self.calls.append(("BEGIN", None))
        self.active = True
        try:
            yield
        except Exception:
            self.rollback()
            raise
        else:
            self.calls.append(("COMMIT", None))
            self.active = False


class MySQLProtocolTests(unittest.TestCase):
    def test_runtime_getlock_rollback_begin_commit_release_order(self):
        conn = FakeMySQLConnection()
        with storage._mysql_lock(conn):
            with conn.begin():
                pass
        self.assertEqual([c[0] for c in conn.calls], ["SELECT DATABASE()", "SELECT GET_LOCK(:name, 5)",
                         "ROLLBACK", "BEGIN", "COMMIT", "ROLLBACK", "SELECT RELEASE_LOCK(:name)", "ROLLBACK"])
        self.assertEqual(conn.calls[1][1], conn.calls[-2][1])
        self.assertLessEqual(len(conn.calls[1][1]["name"]), 64)

    def test_lock_name_database_scoped_not_workgroup_scoped(self):
        names = []
        for db in ("same", "same", "different"):
            conn = FakeMySQLConnection(database=db)
            with storage._mysql_lock(conn):
                pass
            names.append(conn.calls[1][1]["name"])
        self.assertEqual(names[0], names[1])
        self.assertNotEqual(names[0], names[2])

    def test_timeout_or_null_never_begins_or_releases_unowned_lock(self):
        for result in (0, None):
            conn = FakeMySQLConnection(acquire=result)
            with self.assertRaises(LockTimeout), storage._mysql_lock(conn):
                self.fail("lock was not acquired")
            self.assertFalse(any("RELEASE" in c[0] or c[0] == "BEGIN" for c in conn.calls))

    def test_failure_inside_transaction_rolls_back_then_releases(self):
        conn = FakeMySQLConnection()
        with self.assertRaises(RuntimeError), storage._mysql_lock(conn):
            with conn.begin():
                raise RuntimeError("fixture failure")
        self.assertIn(("SELECT RELEASE_LOCK(:name)", conn.calls[1][1]), conn.calls)
        self.assertFalse(any(c[0] == "COMMIT" for c in conn.calls))

    def test_release_failure_or_unknown_result_invalidates(self):
        for result in (0, None, RuntimeError("fixture")):
            conn = FakeMySQLConnection(release=result)
            with storage._mysql_lock(conn):
                with conn.begin():
                    pass
            self.assertTrue(conn.invalidated)

    def test_acquisition_network_failure_invalidates_and_sanitizes(self):
        conn = FakeMySQLConnection(acquire=RuntimeError("private fixture marker"))
        with self.assertRaises(StorageError) as caught, storage._mysql_lock(conn):
            self.fail("unreachable")
        self.assertTrue(conn.invalidated)
        self.assertNotIn("private fixture marker", str(caught.exception))

    def test_acquired_lock_cleanup_even_if_initial_rollback_fails(self):
        conn = FakeMySQLConnection()
        original = conn.rollback
        count = 0
        def once():
            nonlocal count
            count += 1
            if count == 1:
                raise RuntimeError("fixture rollback failure")
            original()
        conn.rollback = once
        with self.assertRaises(RuntimeError), storage._mysql_lock(conn):
            self.fail("unreachable")
        self.assertTrue(any("RELEASE_LOCK" in c[0] for c in conn.calls))


if __name__ == "__main__":
    unittest.main()
