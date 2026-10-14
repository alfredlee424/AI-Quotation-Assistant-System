"""離線 unittest；SQLite 合成 schema、網路硬阻擋，無 .env／模型／真 MySQL。

python -B -m unittest discover -s test -p 'test_local_dynamic*.py' -v
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, localcontext
import base64
import json
import random
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
import zlib

from sqlalchemy import create_engine, event, text

from local_quote import dynamic, dynamic_storage as storage
from local_quote.backend import LocalDatabase, Settings
from local_quote.dynamic_storage import DynamicStore, prepare_document
from local_quote.storage import BatchStore, LockTimeout, StorageError, TAIPEI, freeze_preview
from test_local_dynamic import BOARD, BOX, ROOT, SOURCE, fixture
from test_local_storage import fixture_schema, preview


AUDIT = dict(actor="tester", reason="人工核對合成資料", time="2026-10-14T08:00:00+08:00")
SETTINGS = Settings("mysql+pymysql://fixture:fixture@localhost/isolated_fixture", "103")
NOW = datetime(2026, 10, 14, 9, 8, 7, 123456, tzinfo=TAIPEI)


def request(source=SOURCE, catalog=None, **inputs):
    analysis = dynamic.analyze(source, fixture() if catalog is None else catalog, **inputs)
    document = dynamic.freeze(analysis, analysis["children"][0]["id"], **AUDIT)
    return analysis, prepare_document(document)


def large_catalog():
    cat = fixture()
    for index in range(1500):
        cat["options"][BOX].append(dict(path=BOX, code=f"X{index:04}", codsc=f"合成未選規格{index}", compri="100"))
    return cat


def envelope(raw, *, compressed=None, digest="0" * 64):
    compressed = zlib.compress(raw, 9) if compressed is None else compressed
    return dict(document_type=storage.DOCUMENT_TYPE, document_version=2, algorithm=storage.COMPRESSION,
                logical_bytes=len(raw), compressed_bytes=len(compressed),
                data=base64.b64encode(compressed).decode("ascii"), snapshot_digest=digest)


class DynamicStorageTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("禁止網路"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.engine = create_engine("sqlite:///:memory:")
        fixture_schema(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = LocalDatabase(SETTINGS, engine=self.engine, testing=True)
        self.store = DynamicStore(self.db)
        self.analysis, self.frozen = request()

    def save(self, frozen=None, analysis=None, store=None):
        analysis = self.analysis if analysis is None else analysis
        return (store or self.store).save(self.frozen if frozen is None else frozen, confirmed=True,
                                         current=analysis, trusted_catalog_digest=analysis["catalog_digest"])

    def counts(self):
        with self.engine.connect() as conn:
            return tuple(conn.execute(text("SELECT count(*) FROM " + t)).scalar_one()
                         for t in ("quote_snapshot_document", "ordqdt_ai"))

    def test_atomic_one_document_and_all_13_column_details(self):
        with patch.object(storage, "_now", return_value=NOW):
            receipt = self.save()
        self.assertEqual(receipt["ref_no"], "DQ20261014090807_001")
        self.assertEqual(receipt["confirmation_id"], self.frozen.confirmation_id)
        self.assertEqual(receipt["document"], json.loads(self.frozen.payload))
        self.assertEqual(self.counts(), (1, 3))
        self.assertEqual(self.store.read(self.frozen.confirmation_id), receipt)
        self.assertEqual(self.store.list_saved(), [receipt])
        with self.engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM ordqdt_ai ORDER BY seq_no")).mappings().all()
        board = next(row for row in rows if row["path"] == BOARD)
        self.assertEqual(len(storage.LEGACY_FIELDS), 13)
        self.assertEqual(tuple(board[k] for k in storage.LEGACY_FIELDS),
                         ("103", receipt["ref_no"], BOARD, 2.0, "BOARD", "25mm MDF", 4.5, 24.0, 700.0,
                          "2026.10.14", "09:08:07", "tester", "DYNAMIC1"))
        self.assertEqual(board["unit_price"], 170.625)
        self.assertEqual(board["amount"], 341.25)
        self.assertIsNone(rows[0]["stdqty"])
        self.assertIsNone(rows[0]["stdpar"])

    def test_uuid_persists_roundtrip_new_store_idempotence(self):
        receipt = self.save()
        frozen = storage.FrozenDynamicQuote(**json.loads(json.dumps(self.frozen.__dict__)))
        self.assertEqual(self.save(frozen=frozen, store=DynamicStore(self.db)), receipt)
        self.assertEqual(self.counts(), (1, 3))

    def test_fresh_process_retries_persisted_uuid_without_private_config_or_network(self):
        self.analysis, self.frozen = request(catalog=large_catalog())
        with TemporaryDirectory() as directory:
            url = "sqlite:///" + directory + "/isolated.db"
            engine = create_engine(url)
            try:
                fixture_schema(engine)
                store = DynamicStore(LocalDatabase(SETTINGS, engine=engine, testing=True))
                receipt = self.save(store=store)
            finally:
                engine.dispose()
            code = '''
import json, socket, sys
def denied(*args, **kwargs):
    raise AssertionError("forbidden network/config")
socket.socket.connect = socket.create_connection = denied
from sqlalchemy import create_engine, text
from local_quote import dynamic
from local_quote.backend import LocalDatabase, Settings
from local_quote.dynamic_storage import DynamicStore, FrozenDynamicQuote
from local_quote import backend
backend.dotenv_values = denied
frozen = FrozenDynamicQuote(**json.loads(sys.stdin.read()))
document = json.loads(frozen.payload)
context = document["context"]
current = dynamic.analyze(context["source"], context["catalog_snapshot"], **context["inputs"])
engine = create_engine(sys.argv[1])
try:
    db = LocalDatabase(Settings("mysql+pymysql://fixture:fixture@localhost/isolated_fixture", "103"), engine=engine, testing=True)
    receipt = DynamicStore(db).save(frozen, confirmed=True, current=current, trusted_catalog_digest=current["catalog_digest"])
    assert DynamicStore(db).read(frozen.confirmation_id) == receipt
    assert DynamicStore(db).list_saved() == [receipt]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM quote_snapshot_document")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM ordqdt_ai")).scalar_one() == 3
        assert json.loads(conn.execute(text("SELECT payload FROM quote_snapshot_document")).scalar_one())["document_version"] == 2
    print(json.dumps(receipt))
finally:
    engine.dispose()
assert not any(n == "database" or n.startswith("database.") for n in sys.modules)
'''
            result = subprocess.run([sys.executable, "-B", "-c", code, url],
                                    input=json.dumps(self.frozen.__dict__), capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), receipt)

    def test_one_ready_child_saves_while_other_child_is_incomplete(self):
        source = SOURCE + "\n平直70X140-----3"
        analysis, frozen = request(source)
        self.assertFalse(analysis["children"][1]["ready"])
        self.save(frozen, analysis)
        self.assertEqual(self.counts(), (1, 3))

    def test_unknown_history_id_and_limit_validation(self):
        self.assertIsNone(self.store.read(uuid4().hex))
        for limit in (True, 0, 101, "20"):
            with self.assertRaises(StorageError):
                self.store.list_saved(limit)

    def test_master_tables_remain_byte_for_byte_and_history_survives_price_change(self):
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO ordspe (workgroup,path,code,codsc,compri) "
                              "VALUES ('103',:path,'BOARD','fixture',999)"), {"path": BOARD})
            before = conn.execute(text("SELECT * FROM ordspe")).all()
        receipt = self.save()
        with self.engine.begin() as conn:
            self.assertEqual(conn.execute(text("SELECT * FROM ordspe")).all(), before)
            conn.execute(text("UPDATE ordspe SET compri=1"))  # 只測試 fixture 模擬別人更新主檔
        self.assertEqual(self.store.read(self.frozen.confirmation_id), receipt)
        self.assertEqual(self.save(), receipt)

    def test_same_uuid_different_document_is_conflict_not_replacement(self):
        first = self.save()
        doc = json.loads(self.frozen.payload)
        doc["frozen_audit"]["reason"] = "異內容"
        conflicting = prepare_document(doc, confirmation_id=self.frozen.confirmation_id)
        with self.assertRaisesRegex(StorageError, "不同內容"):
            self.save(frozen=conflicting)
        self.assertEqual(self.store.read(self.frozen.confirmation_id), first)

    def test_confirmed_trusted_current_and_digest_are_required(self):
        for kwargs in (dict(confirmed=False), dict(confirmed=1), dict(current=None),
                       dict(trusted_catalog_digest="0" * 64)):
            arguments = dict(confirmed=True, current=self.analysis,
                             trusted_catalog_digest=self.analysis["catalog_digest"])
            arguments.update(kwargs)
            with self.assertRaises(ValueError):
                self.store.save(self.frozen, **arguments)
        self.assertEqual(self.counts(), (0, 0))

    def test_stale_source_or_child_edit_rejected_even_after_first_commit(self):
        self.save()
        cid = self.analysis["children"][0]["id"]
        for current in (dynamic.analyze(SOURCE + "\n", fixture()),
                        dynamic.analyze(SOURCE, fixture(), overrides={cid: {BOARD: dict(AUDIT, values={"compri": "701"})}})):
            with self.assertRaisesRegex(ValueError, "變更"):
                self.save(analysis=current)
        self.assertEqual(self.counts(), (1, 3))

    def test_not_just_ready_rebuild_rejects_missing_structure_and_cost(self):
        for mode in ("structure", "cost"):
            cat = fixture()
            if mode == "structure":
                cat["options"][ROOT] = []
            else:
                cat["options"][BOARD][0]["compri"] = None
            analysis = dynamic.analyze(SOURCE, cat)
            self.assertFalse(analysis["children"][0]["ready"])
            # 模擬不可信客戶端把 ready 換成 True、套上舊金額與重算 hash。
            doc = json.loads(self.frozen.payload)
            doc["context"] = analysis["context"]
            doc["child"] = deepcopy(analysis["children"][0])
            doc["child"]["ready"] = True
            doc["frozen_digest"] = dynamic._digest(doc["child"])
            payload = storage._canonical(doc)
            bad = replace(self.frozen, payload=payload, digest=storage._request_digest(self.frozen.confirmation_id, doc))
            with self.assertRaises(ValueError):
                self.save(frozen=bad, analysis=analysis)
        self.assertEqual(self.counts(), (0, 0))

    def test_tampered_request_envelope_and_derived_values(self):
        for value in (replace(self.frozen, confirmation_id="0" * 32), replace(self.frozen, digest="0" * 64),
                      replace(self.frozen, payload="{}")):
            with self.assertRaises(ValueError):
                self.save(frozen=value)
        doc = json.loads(self.frozen.payload)
        doc["child"]["items"][1]["qty"] = "100"
        doc["frozen_digest"] = dynamic._digest(doc["child"])
        with self.assertRaises(ValueError):
            prepare_document(doc)

    def test_actor_schema_is_explicit_not_truncated_including_manual_audit(self):
        for actor in ("", "123456789", " actor", "actor\n", "a\x7f", "a\x00"):
            doc = json.loads(self.frozen.payload)
            doc["frozen_audit"]["actor"] = actor
            with self.assertRaises(ValueError):
                prepare_document(doc)
        doc = json.loads(self.frozen.payload)
        doc["frozen_audit"]["actor"] = "人工代號八個字元"
        self.assertEqual(len(doc["frozen_audit"]["actor"]), 8)
        self.save(frozen=prepare_document(doc))
        cid = self.analysis["children"][0]["id"]
        analysis = dynamic.analyze(SOURCE, fixture(), overrides={cid: {
            BOARD: dict(AUDIT, actor="long-operator", values={"compri": "1"})}})
        with self.assertRaisesRegex(StorageError, "actor"):
            prepare_document(dynamic.freeze(analysis, cid, **AUDIT))

    def test_schema_string_capacity_rejects_not_truncate(self):
        cat = fixture()
        cat["options"][BOARD][0]["codsc"] = "板" * 41
        with self.assertRaisesRegex(StorageError, "長度"):
            request(catalog=cat)
        self.assertEqual(self.counts(), (0, 0))

    def test_realistic_large_catalog_lossless_save_read_list_and_retry(self):
        analysis, frozen = request(catalog=large_catalog())
        frozen = prepare_document(json.loads(frozen.payload), confirmation_id="0123456789ab4def8123456789abcdef")
        with patch.object(storage, "_now", return_value=NOW):
            receipt = self.save(frozen, analysis)
        with self.engine.connect() as conn:
            payload = conn.execute(text("SELECT payload FROM quote_snapshot_document")).scalar_one()
        stored = json.loads(payload)
        logical = storage._load_snapshot(payload)
        self.assertEqual(stored["document_version"], 2)
        self.assertGreater(stored["logical_bytes"], storage.TEXT_BYTES)
        self.assertLessEqual(len(payload.encode()), storage.TEXT_BYTES)
        self.assertEqual(stored["logical_bytes"], len(storage._canonical(logical).encode()))
        self.assertEqual(receipt["document"], json.loads(frozen.payload))
        self.assertEqual(receipt["document"]["context"]["catalog_snapshot"], analysis["context"]["catalog_snapshot"])
        self.assertEqual(self.store.read(frozen.confirmation_id), receipt)
        self.assertEqual(self.store.list_saved(), [receipt])
        self.assertEqual(self.save(frozen, analysis), receipt)
        self.assertEqual(self.counts(), (1, 3))
        print(f"\n1500-candidate fixture: logical={stored['logical_bytes']} encoded={len(payload.encode())} bytes")

    def test_encoded_and_decoded_capacity_prepare_and_bypassed_save_no_writes(self):
        # 一個可壓縮但 decoded 超量，一個 decoded 合法但 encoded 超量。
        for padding, message in (("x" * storage.MAX_LOGICAL_BYTES, "8 MiB"),
                                 (base64.b64encode(random.Random(90210).randbytes(100000)).decode(), "65535")):
            with self.subTest(limit=message):
                cat = fixture()
                cat["nodes"][ROOT]["dmark"] = padding
                analysis = dynamic.analyze(SOURCE, cat)
                doc = dynamic.freeze(analysis, analysis["children"][0]["id"], **AUDIT)
                with self.assertRaisesRegex(StorageError, message):
                    prepare_document(doc)
                identity = uuid4().hex
                frozen = storage.FrozenDynamicQuote(identity, storage._canonical(doc), storage._request_digest(identity, doc))
                with self.assertRaisesRegex(StorageError, message):
                    self.save(frozen=frozen, analysis=analysis)
                self.assertEqual(self.counts(), (0, 0))

    def test_utf8_capacity_exact_boundary(self):
        snapshot = {"text": "", "snapshot_digest": "0" * 64}
        overhead = len(storage._canonical(snapshot).encode())
        snapshot["text"] = "a" * (65535 - overhead - 3) + "板"
        self.assertEqual(len(storage._capacity(snapshot).encode()), 65535)
        snapshot["text"] += "a"
        self.assertEqual(json.loads(storage._capacity(snapshot))["document_version"], 2)
        snapshot["text"] = "a" * (storage.MAX_LOGICAL_BYTES - overhead)
        encoded = storage._capacity(snapshot)
        self.assertEqual(json.loads(encoded)["logical_bytes"], storage.MAX_LOGICAL_BYTES)
        self.assertLessEqual(len(encoded.encode()), storage.TEXT_BYTES)
        snapshot["text"] += "a"
        with self.assertRaises(StorageError):
            storage._capacity(snapshot)

    def test_encoded_envelope_exact_cap_checked_by_prepare_and_save_codec(self):
        snapshot = storage._snapshot(self.frozen, json.loads(self.frozen.payload), "103",
                                     "DQ20261014090807_001", NOW.isoformat(), "binary64")
        snapshot["document"]["frozen_audit"]["reason"] = "x" * 70000
        payload = storage._capacity(snapshot)
        size = len(payload.encode())
        with patch.object(storage, "TEXT_BYTES", size):
            self.assertEqual(storage._capacity(snapshot), payload)
        with patch.object(storage, "TEXT_BYTES", size - 1), self.assertRaises(StorageError):
            storage._capacity(snapshot)

    def test_complete_logical_snapshot_at_8_mib_roundtrips_and_one_byte_over_rejected(self):
        snapshot = storage._snapshot(self.frozen, json.loads(self.frozen.payload), "103",
                                     "DQ20261014090807_001", NOW.isoformat(), "binary64")
        audit = snapshot["document"]["frozen_audit"]
        audit["reason"] = ""
        overhead = len(storage._canonical(snapshot).encode())
        audit["reason"] = "x" * (storage.MAX_LOGICAL_BYTES - overhead - 3) + "板"
        snapshot["snapshot_digest"] = storage._digest(storage._canonical(
            {k: v for k, v in snapshot.items() if k != "snapshot_digest"}))
        encoded = storage._capacity(snapshot)
        self.assertEqual(json.loads(encoded)["logical_bytes"], storage.MAX_LOGICAL_BYTES)
        self.assertEqual(storage._load_snapshot(encoded), snapshot)
        audit["reason"] += "a"
        with self.assertRaisesRegex(StorageError, "8 MiB"):
            storage._capacity(snapshot)

    def test_v1_unchanged_and_v2_digest_independent_of_compression(self):
        receipt = self.save()
        with self.engine.connect() as conn:
            payload = conn.execute(text("SELECT payload FROM quote_snapshot_document")).scalar_one()
        snapshot = json.loads(payload)
        self.assertEqual(snapshot["document_version"], 1)
        self.assertEqual(storage._capacity(snapshot), payload)
        for level in (1, 9):
            compressed = envelope(payload.encode(), compressed=zlib.compress(payload.encode(), level),
                                  digest=snapshot["snapshot_digest"])
            with self.engine.begin() as conn:
                conn.execute(text("UPDATE quote_snapshot_document SET payload=:p"), {"p": storage._canonical(compressed)})
            self.assertEqual(self.store.read(self.frozen.confirmation_id), receipt)
            self.assertEqual(self.store.list_saved(), [receipt])
            self.assertEqual(self.save(), receipt)
        # 恢復舊版，無遷移亦能讀及重試。
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE quote_snapshot_document SET payload=:p"), {"p": payload})
        self.assertEqual(self.save(), receipt)

    def test_v2_corruption_rejected_by_read_list_and_retry(self):
        self.analysis, self.frozen = request(catalog=large_catalog())
        self.save()
        with self.engine.connect() as conn:
            payload = conn.execute(text("SELECT payload FROM quote_snapshot_document")).scalar_one()
        original = json.loads(payload)
        compressed = base64.b64decode(original["data"])
        raw = zlib.decompress(compressed)
        cases = []
        for key, values in {
            "document_version": [True, 2.0, 3, "2", 1],
            "document_type": ["external_document"],
            "algorithm": ["gzip+base64", "zlib", "ZLIB+BASE64", None],
            "logical_bytes": [True, 1.0, 0, -1, storage.MAX_LOGICAL_BYTES + 1, len(raw) - 1, len(raw) + 1],
            "compressed_bytes": [True, 0, -1, storage.TEXT_BYTES + 1, len(compressed) - 1],
            "data": [None, "!" + original["data"][1:], original["data"] + "\n", original["data"] + "===="],
            "snapshot_digest": ["0" * 64, None],
        }.items():
            cases.extend(dict(original, **{key: value}) for value in values)
        missing = dict(original)
        del missing["data"]
        cases.extend([missing, dict(original, url="file:///must-not-read"), dict(original, extra=1)])
        for bad in (compressed[:-1], compressed + b"tail", compressed + zlib.compress(b"second"),
                    compressed[:-1] + bytes([compressed[-1] ^ 1]),
                    zlib.compress(raw, wbits=-15), zlib.compress(raw, wbits=31)):
            cases.append(envelope(raw, compressed=bad, digest=original["snapshot_digest"]))
        for bad in (b"\xff", b"{}", storage._canonical(original).encode(), raw + b" ",
                    b'{"duplicate":1,"duplicate":2}', b'{"n":NaN}'):
            cases.append(envelope(bad, digest=original["snapshot_digest"]))
        logical = json.loads(raw)
        logical["document"]["frozen_audit"]["reason"] += "tampered"
        cases.append(envelope(storage._canonical(logical).encode(), digest=original["snapshot_digest"]))
        payloads = [storage._canonical(case) for case in cases]
        payloads.extend([payload + " ", payload[:-1] + ',"document_version":2}',
                         "[" * 2000 + "]" * 2000, "x" * (storage.TEXT_BYTES + 1)])
        for index, bad in enumerate(payloads):
            with self.subTest(case=index):
                with self.engine.begin() as conn:
                    conn.execute(text("UPDATE quote_snapshot_document SET payload=:p"), {"p": bad})
                for action in (lambda: self.store.read(self.frozen.confirmation_id), self.store.list_saved, self.save):
                    with self.assertRaises(StorageError):
                        action()
                self.assertEqual(self.counts(), (1, 3))

    def test_bomb_bounded_decompression_and_noncanonical_base64(self):
        for declared in (32, storage.MAX_LOGICAL_BYTES):
            bomb = envelope(b"x" * (storage.MAX_LOGICAL_BYTES + 1))
            bomb["logical_bytes"] = declared
            real = zlib.decompressobj()
            class TrackedInflater:
                def decompress(inner, data, max_length):
                    self.assertEqual(max_length, declared + 1)
                    return real.decompress(data, max_length)
                def __getattr__(inner, name):
                    self.assertNotEqual(name, "flush")
                    return getattr(real, name)
            with patch.object(storage.zlib, "decompressobj", return_value=TrackedInflater()), \
                    self.assertRaisesRegex(StorageError, "解壓上限"):
                storage._load_snapshot(storage._canonical(bomb))
        # 非零 padding bits：標準解碼可能接受，但 canonical base64 必須拒絕。
        bad = envelope(b"x", compressed=b"x")
        bad["data"] = "eB=="
        with self.assertRaisesRegex(StorageError, "base64 非 canonical"):
            storage._load_snapshot(storage._canonical(bad))

    def test_v2_readback_corruption_and_over_capacity_roll_back_complete_transaction(self):
        self.analysis, self.frozen = request(catalog=large_catalog())
        original = self.store._insert_items
        for mode in ("detail", "encoded", "decoded", "tail", "resealed_logical"):
            def tamper(conn, snapshot):
                original(conn, snapshot)
                if mode == "detail":
                    conn.execute(text("UPDATE ordqdt_ai SET qty=9 WHERE seq_no='00001'"))
                    return
                payload = conn.execute(text("SELECT payload FROM quote_snapshot_document")).scalar_one()
                value = json.loads(payload)
                if mode == "encoded":
                    payload += " " * storage.TEXT_BYTES
                elif mode == "decoded":
                    value["logical_bytes"] = storage.MAX_LOGICAL_BYTES + 1
                    payload = storage._canonical(value)
                elif mode == "resealed_logical":
                    value = storage._load_snapshot(payload)
                    value["document"]["frozen_audit"]["reason"] += "changed"
                    value["request_digest"] = storage._request_digest(self.frozen.confirmation_id, value["document"])
                    value["snapshot_digest"] = storage._digest(storage._canonical(
                        {k: v for k, v in value.items() if k != "snapshot_digest"}))
                    payload = storage._capacity(value)
                else:
                    raw = base64.b64decode(value["data"]) + b"tail"
                    value.update(data=base64.b64encode(raw).decode(), compressed_bytes=len(raw))
                    payload = storage._canonical(value)
                conn.execute(text("UPDATE quote_snapshot_document SET payload=:p"), {"p": payload})
            with self.subTest(mode=mode), patch.object(self.store, "_insert_items", side_effect=tamper), \
                    self.assertRaises(StorageError):
                self.save()
            self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.save()["confirmation_id"], self.frozen.confirmation_id)

    def test_v2_list_detects_envelope_even_with_corrupt_non_dq_reference(self):
        self.analysis, self.frozen = request(catalog=large_catalog())
        self.save()
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE quote_snapshot_document SET ref_no='NOT-DQ'"))
        with self.assertRaises(StorageError):
            self.store.list_saved()

    def test_total_accessory_qty_no_product_multiplication_or_duplicate_paths(self):
        source = SOURCE + "\n配件：銀線盒共3個\n配件：銀線盒共3個"
        analysis, frozen = request(source)
        receipt = self.save(frozen, analysis)
        with self.engine.connect() as conn:
            rows = conn.execute(text("SELECT qty,amount FROM ordqdt_ai WHERE path=:path"), {"path": BOX}).all()
        self.assertEqual(rows, [(3.0, 468.0)])
        self.assertEqual(self.store.read(frozen.confirmation_id), receipt)

    def test_arbitrary_nine_children_across_source_groups(self):
        source = "工單：甲\n" + "\n".join([SOURCE] * 5) + "\n工單：乙\n" + "\n".join([SOURCE] * 4)
        analysis = dynamic.analyze(source, fixture())
        refs = []
        with patch.object(storage, "_now", return_value=NOW):
            for child in analysis["children"]:
                frozen = prepare_document(dynamic.freeze(analysis, child["id"], **AUDIT))
                refs.append(self.save(frozen, analysis)["ref_no"])
        self.assertEqual(len(set(refs)), 9)
        self.assertEqual(refs[-1], "DQ20261014090807_009")
        self.assertEqual(self.counts(), (9, 27))
        self.assertEqual(len(self.store.list_saved()), 9)

    def test_workgroup_mismatch_rejected_and_global_uuid_collision(self):
        other = DynamicStore(LocalDatabase(Settings(SETTINGS.db_conn_str, "999"), engine=self.engine, testing=True))
        with self.assertRaisesRegex(StorageError, "工作群"):
            self.save(store=other)
        self.save()
        with self.assertRaisesRegex(StorageError, "工作群"):
            other.read(self.frozen.confirmation_id)
        cat = fixture()
        cat["workgroup"] = "999"
        for node in cat["nodes"].values():
            node["workgroup"] = "999"
        for name in ("options", "rules"):
            for options in cat[name].values():
                for option in options:
                    option["workgroup"] = "999"
        analysis = dynamic.analyze(SOURCE, cat)
        doc = dynamic.freeze(analysis, analysis["children"][0]["id"], **AUDIT)
        frozen = prepare_document(doc, confirmation_id=self.frozen.confirmation_id)
        with self.assertRaises(StorageError):
            self.save(frozen, analysis, other)
        self.assertEqual(self.counts(), (1, 3))

    def test_global_number_collisions_check_both_tables(self):
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO quote_snapshot_document VALUES ('999',:id,'DQ20261014090807_001','{}')"), {"id": uuid4().hex})
            conn.execute(text("INSERT INTO ordqdt_ai (workgroup,ref_no,seq_no,part_code,path) "
                              "VALUES ('998','DQ20261014090807_002','00001','','fixture')"))
        with patch.object(storage, "_now", return_value=NOW):
            self.assertEqual(self.save()["ref_no"], "DQ20261014090807_003")

    def test_all_numbers_occupied_explicit_retry_no_infinite_wait(self):
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO quote_snapshot_document VALUES ('999',:id,:ref,'{}')"),
                         [{"id": uuid4().hex, "ref": f"DQ20261014090807_{i:03d}"} for i in range(1, 1000)])
        with patch.object(storage, "_now", return_value=NOW), self.assertRaisesRegex(StorageError, "配號已滿"):
            self.save()
        self.assertEqual(self.counts(), (999, 0))

    def test_partial_detail_failure_full_rollback_and_same_uuid_retry(self):
        original = self.store._insert_items

        def fail_after_first(conn, snapshot):
            original(conn, snapshot)
            raise RuntimeError("private SQL values must not escape")

        with patch.object(self.store, "_insert_items", side_effect=fail_after_first):
            with self.assertRaises(StorageError) as caught:
                self.save()
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.save()["confirmation_id"], self.frozen.confirmation_id)

    def test_transaction_readback_mismatch_also_rolls_back(self):
        original = self.store._insert_items

        def tamper(conn, snapshot):
            original(conn, snapshot)
            conn.execute(text("UPDATE ordqdt_ai SET qty=9 WHERE seq_no='00001'"))

        with patch.object(self.store, "_insert_items", side_effect=tamper), self.assertRaises(StorageError):
            self.save()
        self.assertEqual(self.counts(), (0, 0))

    def test_read_rejects_corruption_every_persisted_detail_column(self):
        self.analysis, self.frozen = request(catalog=large_catalog())
        self.save()
        for key in storage.ROW_FIELDS:
            with self.subTest(column=key):
                with self.engine.connect() as conn:
                    transaction = conn.begin()
                    value = 987.123 if key in storage.NUMERIC_FIELDS else "X"
                    conn.execute(text(f"UPDATE ordqdt_ai SET {key}=:v WHERE seq_no='00001'"), {"v": value})
                    # 此測試 SQLite 單連線池，查閱看得到合成交易；不涉及真 DB。
                    rows = self.store._fetch(conn, self.frozen.confirmation_id)
                    with self.assertRaises(StorageError):
                        self.store._decode(rows, self.frozen.confirmation_id)
                    transaction.rollback()

    def test_missing_extra_rows_corrupt_json_and_idempotent_retry_refuse(self):
        self.save()
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM ordqdt_ai WHERE seq_no='00001'"))
        for action in (lambda: self.store.read(self.frozen.confirmation_id), self.store.list_saved, self.save):
            with self.assertRaises(StorageError):
                action()
        self.assertEqual(self.counts(), (1, 2))
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE quote_snapshot_document SET payload='not JSON'"))
        with self.assertRaises(StorageError):
            self.store.list_saved()

    def test_extra_row_is_not_ignored(self):
        receipt = self.save()
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO ordqdt_ai(workgroup,ref_no,seq_no,part_code,path) "
                              "VALUES ('103',:ref,'99999','','EXTRA')"), {"ref": receipt["ref_no"]})
        with self.assertRaises(StorageError):
            self.store.read(self.frozen.confirmation_id)

    def test_snapshot_recomputed_hash_does_not_bypass_dynamic_rebuild(self):
        self.analysis, self.frozen = request(catalog=large_catalog())
        self.save()
        with self.engine.begin() as conn:
            doc = storage._load_snapshot(conn.execute(text("SELECT payload FROM quote_snapshot_document")).scalar_one())
            doc["document"]["child"]["calc"]["total_price"] = "0"
            doc["document"]["frozen_digest"] = dynamic._digest(doc["document"]["child"])
            doc["request_digest"] = storage._request_digest(self.frozen.confirmation_id, doc["document"])
            doc["snapshot_digest"] = storage._digest(storage._canonical({k: v for k, v in doc.items() if k != "snapshot_digest"}))
            conn.execute(text("UPDATE quote_snapshot_document SET payload=:p"), {"p": storage._capacity(doc)})
        with self.assertRaises(StorageError):
            self.store.read(self.frozen.confirmation_id)

    def test_old_batch_preserved_not_interpreted_or_listed(self):
        old = freeze_preview(preview())
        old_receipt = BatchStore(self.db).save(old, confirmed=True, input_digest=json.loads(old.payload)["input_digest"])
        new = self.save()
        self.assertEqual(self.store.list_saved(), [new])
        self.assertEqual(BatchStore(self.db).read(old_receipt["batch_id"]), old_receipt)
        with self.assertRaises(StorageError):
            self.store.read(old_receipt["children"][0]["preview_id"])

    def test_save_and_history_never_query_price_or_write_master_or_ddl(self):
        self.analysis, self.frozen = request(catalog=large_catalog())
        statements = []

        def audit(conn, cursor, statement, parameters, context, executemany):
            sql = statement.upper().strip()
            statements.append(sql)
            if sql.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP")):
                self.assertTrue(sql.startswith(("INSERT INTO ORDQDT_AI", "INSERT INTO QUOTE_SNAPSHOT_DOCUMENT")), sql)
            if sql.startswith("SELECT"):
                for table in ("INVDOC", "ORDSTR", "ORDSPD", "ORDSPE", "ORDQTY"):
                    self.assertNotIn("FROM " + table + " ", sql)

        event.listen(self.engine, "before_cursor_execute", audit)
        try:
            self.save()
            with patch.object(self.db, "inspect_schema", side_effect=AssertionError("歷史不得再檢查主檔")):
                self.store.read(self.frozen.confirmation_id)
                self.store.list_saved()
        finally:
            event.remove(self.engine, "before_cursor_execute", audit)
        self.assertTrue(statements)

    def test_decimal_context_and_many_significant_digits_no_false_difference(self):
        cat = fixture()
        cat["options"][BOARD][0]["compri"] = "12345.6789012345"
        analysis, frozen = request(catalog=cat)
        with localcontext() as ctx:
            ctx.prec = 3
            receipt = self.save(frozen, analysis)
            self.assertEqual(self.store.read(frozen.confirmation_id), receipt)
        board = next(i for i in receipt["document"]["child"]["items"] if i["path"] == BOARD)
        self.assertEqual(board["compri"], "12345.6789012345")

    def test_binary32_projection_fake_mysql_protocol_on_sqlite_only(self):
        # 不啟用 MySQL engine：只把儲存欄位量化成 binary32，測試提升查詢精確回讀。
        class Binary32Store(DynamicStore):
            @property
            def _profile(self):
                return "binary32"

        store = Binary32Store(self.db)
        cat = fixture()
        cat["options"][BOARD][0]["compri"] = "12345.6789012345"
        analysis, frozen = request(catalog=cat)
        receipt = self.save(frozen, analysis, store)
        self.assertEqual(store.read(frozen.confirmation_id), receipt)
        with self.engine.begin() as conn:
            # 不接受 MySQL 常見六位顯示字串取代確切 FLOAT 值。
            conn.execute(text("UPDATE ordqdt_ai SET compri=12345.7 WHERE path=:p"), {"p": BOARD})
        with self.assertRaises(StorageError):
            store.read(frozen.confirmation_id)

    def mysql_bridge(self, *, acquire=1):
        """只攔 MySQL lock／FOR UPDATE 語法，其餘 INSERT/SELECT/交易皆真 SQLite。"""
        connections = []

        class Connection:
            def __init__(self, base):
                self.base = base
                self.events = []

            @property
            def invalidated(self):
                return self.base.invalidated

            def invalidate(self):
                self.events.append("INVALIDATE")
                self.base.invalidate()

            def execute(self, stmt, params=None):
                sql = str(stmt)
                for token, value in (("DATABASE()", "isolated_fixture"), ("GET_LOCK", acquire), ("RELEASE_LOCK", 1)):
                    if token in sql:
                        self.events.append(token)
                        self.base.execute(text("SELECT 1"))
                        result = Mock()
                        result.scalar_one.return_value = value
                        return result
                self.events.append(sql)
                if sql.endswith(" FOR UPDATE"):
                    stmt = text(sql.removesuffix(" FOR UPDATE"))
                return self.base.execute(stmt, params or {})

            def begin(self):
                self.events.append("BEGIN")
                return self.base.begin()

            def commit(self):
                self.events.append("COMMIT")
                self.base.commit()

            def rollback(self):
                self.events.append("ROLLBACK")
                self.base.rollback()

        @contextmanager
        def connect():
            with self.engine.connect() as base:
                conn = Connection(base)
                connections.append(conn)
                yield conn

        db = Mock(workgroup="103", testing=False)
        db.engine.connect = connect
        db.inspect_schema.side_effect = lambda conn: self.db.inspect_schema(conn.base)
        return DynamicStore(db), db, connections

    def test_mysql_lock_transaction_same_connection_and_idempotence_using_fake_only(self):
        store, db, connections = self.mysql_bridge()
        first = self.save(store=store)
        self.assertEqual(self.save(store=store), first)
        self.assertEqual(self.counts(), (1, 3))
        for conn in connections:
            self.assertEqual(conn.events[:4], ["DATABASE()", "GET_LOCK", "ROLLBACK", "BEGIN"])
            self.assertIn("COMMIT", conn.events)
            self.assertEqual(conn.events[-3:], ["ROLLBACK", "RELEASE_LOCK", "ROLLBACK"])
            self.assertTrue(any("q.compri + 0e0" in sql for sql in conn.events))
        self.assertTrue(any(sql.endswith(" FOR UPDATE") for sql in connections[0].events))

    def test_mysql_lock_timeout_has_no_transaction_schema_or_writes(self):
        store, db, connections = self.mysql_bridge(acquire=0)
        with self.assertRaises(LockTimeout):
            self.save(store=store)
        db.inspect_schema.assert_not_called()
        self.assertNotIn("BEGIN", connections[0].events)
        self.assertNotIn("RELEASE_LOCK", connections[0].events)
        self.assertEqual(self.counts(), (0, 0))

    def test_mysql_failed_write_rolls_back_before_lock_release(self):
        store, db, connections = self.mysql_bridge()
        original = store._insert_items

        def fail(conn, snapshot):
            original(conn, snapshot)
            raise RuntimeError("synthetic failure")

        with patch.object(store, "_insert_items", side_effect=fail), self.assertRaises(StorageError):
            self.save(store=store)
        self.assertEqual(self.counts(), (0, 0))
        self.assertNotIn("COMMIT", connections[0].events)
        self.assertEqual(connections[0].events[-3:], ["ROLLBACK", "RELEASE_LOCK", "ROLLBACK"])

    def test_parallel_separate_engines_and_same_uuid_exactly_once(self):
        with TemporaryDirectory() as directory:
            url = "sqlite:///" + directory + "/isolated.db"
            engines = [create_engine(url) for _ in range(2)]
            try:
                fixture_schema(engines[0])
                stores = [DynamicStore(LocalDatabase(SETTINGS, engine=e, testing=True)) for e in engines]
                with ThreadPoolExecutor(max_workers=2) as executor:
                    receipts = list(executor.map(lambda s: self.save(store=s), stores))
                self.assertEqual(receipts[0], receipts[1])
                with engines[0].connect() as conn:
                    self.assertEqual(conn.execute(text("SELECT count(*) FROM quote_snapshot_document")).scalar_one(), 1)
            finally:
                for engine in engines:
                    engine.dispose()


if __name__ == "__main__":
    unittest.main()
