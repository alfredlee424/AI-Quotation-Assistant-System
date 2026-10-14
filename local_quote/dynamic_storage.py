"""新版逐張原子保存；不查價、不寫主檔、不 DDL、不套用四張 payload。

prepare_document(dynamic.freeze(...)) -> FrozenDynamicQuote（固定 UUID，重試勿重建）
DynamicStore.save(frozen, confirmed=True, current=可信最新分析,
                  trusted_catalog_digest=可信主檔摘要) -> receipt
DynamicStore.read(confirmation_id) / list_saved(limit=20) -> receipt / receipts

receipt 包含 confirmation_id、child_id、ref_no、confirmed_at、document。
current / digest 必須由應用層保管，驗證不是 authentication。人工 actor
為 1..8 字元（不可前後空白／控制字元），不會截短或冒用登入身份。

容量策略刻意 fail closed：保留完整 catalog（含未選候選、必要結構與
ordqty 規則）及來源。核心的語意候選可跨所有部位，直接只留已選規格
會改變候選、revision 及結構驗證，故不作有損裁剪。邏輯快照上限
8 MiB；超過 TEXT 65535 bytes 才用 V2 zlib+base64 無損封套，封套仍須
符合 TEXT 上限。V1 可讀；沒有 DDL 或隱藏外部來源依賴。

MySQL FLOAT 為 binary32、SQLite REAL 為 binary64。JSON 保留原 Decimal
字串；明細有明確投影政策，核對 SELECT 提升至 DOUBLE 後的完整數值，
不以 MySQL FLOAT 文字協定的六位顯示數字或任意 epsilon 比較。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, ROUND_HALF_UP, localcontext
import base64
import binascii
import json
import math
import re
import struct
from uuid import UUID, uuid4
import zlib

from sqlalchemy import text

from . import dynamic
from .backend import LocalDatabase, SchemaError
from .storage import (StorageError, TEXT_BYTES, _canonical, _digest, _hex, _iso,
                      _mysql_lock, _now, _number, _require, _sqlite_lock, _string)


DOCUMENT_TYPE = "local_dynamic_saved_child"
MAX_LOGICAL_BYTES = 8 * 1024 * 1024
COMPRESSION = "zlib+base64"
_ENVELOPE_FIELDS = {"document_type", "document_version", "algorithm", "logical_bytes",
                    "compressed_bytes", "data", "snapshot_digest"}
NUMERIC_FIELDS = ("qty", "stdqty", "stdpar", "compri", "unit_price", "amount")
ROW_FIELDS = ("workgroup", "ref_no", "seq_no", "part_code", "part_desc", "path",
              "opt_code", "opt_desc", "spc_code", "spdsc", *NUMERIC_FIELDS,
              "unit", "status", "transferred", "adddate", "addusrno", "abndat",
              "abntim", "usrno", "prgno")
# logs/ss.txt 的原始 13 欄：群組、單號、路徑、數量、規格、說明、用量、
# 基準量、成本、日期、時間、人員、程式。新增日期另保存在 adddate。
LEGACY_FIELDS = ("workgroup", "ref_no", "path", "qty", "spc_code", "spdsc",
                 "stdqty", "stdpar", "compri", "abndat", "abntim", "usrno", "prgno")
_REF = re.compile(r"DQ\d{14}_[0-9]{3}\Z")
_FIELDS = {"document_type", "document_version", "confirmation_id", "workgroup",
           "ref_no", "confirmed_at", "document", "request_digest", "numeric_storage",
           "snapshot_digest"}


@dataclass(frozen=True)
class FrozenDynamicQuote:
    """可序列化、跨程序重試的固定請求；三欄均須原樣保留。"""
    confirmation_id: str
    payload: str
    digest: str


def _uuid(value):
    _hex(value, 32, "confirmation_id")
    _require(UUID(hex=value).version == 4, "confirmation_id 必須是 UUID4 hex")
    return value


def validate_actor(actor):
    """只檢查 schema 可容納的人工代號，不代表驗證登入身份或授權。"""
    _string(actor, "actor（1 至 8 字元）", 8, empty=False)
    _require(actor == actor.strip() and not any(ord(c) < 32 or ord(c) == 127 for c in actor),
             "actor 不得含前後空白或控制字元")
    return actor


def _validate_audits(document):
    validate_actor(document["frozen_audit"]["actor"])
    for name in ("customs", "overrides", "resolutions"):
        for values in document["context"]["inputs"][name].values():
            for value in values.values():
                validate_actor(value["actor"])


def _request_digest(confirmation_id, document):
    return _digest(_canonical({"confirmation_id": confirmation_id, "document": document}))


def _snapshot(frozen, document, workgroup, ref_no, confirmed_at, numeric_storage):
    result = dict(document_type=DOCUMENT_TYPE, document_version=1,
                  confirmation_id=frozen.confirmation_id, workgroup=workgroup, ref_no=ref_no,
                  confirmed_at=confirmed_at, document=document, request_digest=frozen.digest,
                  numeric_storage=numeric_storage)
    result["snapshot_digest"] = _digest(_canonical(result))
    return result


def _utf8(value, limit, label):
    # 先檢查字元數，避免外來超大字串再配置一份無界 bytes。
    _require(isinstance(value, str) and len(value) <= limit, f"{label}超過 {limit} bytes")
    try:
        raw = value.encode("utf-8")
    except UnicodeError:
        raise StorageError(f"{label}不是合法 UTF-8") from None
    _require(len(raw) <= limit, f"{label}超過 {limit} UTF-8 bytes；拒絕、不截斷")
    return raw


def _capacity(snapshot):
    """prepare/save 共用：限制完整邏輯快照，僅必要時壓縮，限制最終封套。"""
    payload = _canonical(snapshot)
    raw = _utf8(payload, MAX_LOGICAL_BYTES, "完整邏輯快照（8 MiB）")
    if len(raw) <= TEXT_BYTES:
        return payload
    compressed = zlib.compress(raw, level=9)
    envelope = dict(document_type=DOCUMENT_TYPE, document_version=2, algorithm=COMPRESSION,
                    logical_bytes=len(raw), compressed_bytes=len(compressed),
                    data=base64.b64encode(compressed).decode("ascii"),
                    snapshot_digest=snapshot["snapshot_digest"])
    encoded = _canonical(envelope)
    _utf8(encoded, TEXT_BYTES, f"單張壓縮封套（logical={len(raw)} bytes；TEXT 65535）")
    return encoded


def _json(payload):
    try:
        result = json.loads(payload)
    except (TypeError, ValueError, RecursionError):
        raise StorageError("新版歷史 JSON 損毀") from None
    # 同時拒絕重複鍵、非有限值、非 canonical 格式。
    _require(_canonical(result) == payload, "歷史 JSON 不是 canonical 內容")
    return result


def _load_snapshot(payload):
    """只解碼自含儲存文件，不接受外部參照或遞迴封套。摘要不依賴壓縮位元組。"""
    _utf8(payload, TEXT_BYTES, "歷史儲存容量（TEXT 65535）")
    stored = _json(payload)
    _require(isinstance(stored, dict) and stored.get("document_type") == DOCUMENT_TYPE
             and type(stored.get("document_version")) is int
             and stored["document_version"] in (1, 2),
             "不是支援的新版單張歷史；舊歷史請用 BatchStore")
    snapshot = stored
    if stored["document_version"] == 2:
        _require(set(stored) == _ENVELOPE_FIELDS and stored["algorithm"] == COMPRESSION,
                 "壓縮封套欄位／演算法不符")
        size, compressed_size = stored["logical_bytes"], stored["compressed_bytes"]
        _require(type(size) is int and 0 < size <= MAX_LOGICAL_BYTES,
                 "壓縮封套 logical_bytes 超出 8 MiB 上限")
        _require(type(compressed_size) is int and 0 < compressed_size <= TEXT_BYTES,
                 "壓縮封套 compressed_bytes 超出上限")
        data = stored["data"]
        _require(isinstance(data, str) and len(data) == 4 * ((compressed_size + 2) // 3),
                 "壓縮封套 base64 長度不符")
        _hex(stored["snapshot_digest"], 64, "snapshot_digest")
        try:
            compressed = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            raise StorageError("壓縮封套 base64 損毀") from None
        _require(len(compressed) == compressed_size and base64.b64encode(compressed).decode("ascii") == data,
                 "壓縮封套 base64 非 canonical 或長度不符")
        try:
            inflater = zlib.decompressobj()
            # 最多宣告長度 + 1，絕不無界 decompress/flush。+1 用來偵測偽報長度／bomb。
            raw = inflater.decompress(compressed, size + 1)
        except zlib.error:
            raise StorageError("壓縮封套 zlib 損毀") from None
        _require(len(raw) == size and inflater.eof and not inflater.unused_data
                 and not inflater.unconsumed_tail,
                 "壓縮封套 zlib 長度、截斷、尾端、多流或解壓上限不符")
        try:
            logical = raw.decode("utf-8")
        except UnicodeError:
            raise StorageError("壓縮快照不是合法 UTF-8") from None
        snapshot = _json(logical)
    _require(isinstance(snapshot, dict) and set(snapshot) == _FIELDS
             and snapshot["document_type"] == DOCUMENT_TYPE
             and type(snapshot["document_version"]) is int and snapshot["document_version"] == 1,
             "邏輯快照欄位／版本不符")
    _require(snapshot["snapshot_digest"] == _digest(_canonical(
        {k: v for k, v in snapshot.items() if k != "snapshot_digest"})), "歷史摘要不符")
    _require(stored["snapshot_digest"] == snapshot["snapshot_digest"], "壓縮封套與邏輯快照摘要不符")
    return snapshot


def prepare_document(document, *, confirmation_id=None):
    """重建驗證、schema／容量預檢並配置 UUID；不連庫。不可於重試時重建。"""
    payload = _canonical(document)
    _utf8(payload, MAX_LOGICAL_BYTES, "固定文件（完整邏輯快照上限 8 MiB）")
    detached = json.loads(payload)
    dynamic.validate_document(detached)
    _validate_audits(detached)
    identity = _uuid(confirmation_id if confirmation_id is not None else uuid4().hex)
    frozen = FrozenDynamicQuote(identity, payload, _request_digest(identity, detached))
    wg = detached["context"]["catalog_snapshot"]["workgroup"]
    preview = _snapshot(frozen, detached, wg, "DQ20000101000000_001",
                        "2000-01-01T00:00:00.000000+08:00", "binary64")
    _detail_rows(preview)
    _capacity(preview)
    return frozen


def _thaw(frozen):
    _require(isinstance(frozen, FrozenDynamicQuote), "必須提供 FrozenDynamicQuote")
    _uuid(frozen.confirmation_id)
    _hex(frozen.digest, 64, "digest")
    _utf8(frozen.payload, MAX_LOGICAL_BYTES, "固定文件（完整邏輯快照上限 8 MiB）")
    try:
        document = json.loads(frozen.payload)
    except (TypeError, ValueError, RecursionError):
        raise StorageError("固定內容不是完整 JSON") from None
    _require(_canonical(document) == frozen.payload, "固定內容不是 canonical JSON")
    _require(_request_digest(frozen.confirmation_id, document) == frozen.digest, "固定請求摘要不符")
    dynamic.validate_document(document)
    _validate_audits(document)
    return document


def _detail_rows(snapshot):
    """13 欄語意及現行 schema 的精確字串投影；零結構 NULL 用量不捏造 1。"""
    doc = snapshot["document"]
    cat, child = doc["context"]["catalog_snapshot"], doc["child"]
    actor = validate_actor(doc["frozen_audit"]["actor"])
    _string(snapshot["workgroup"], "workgroup", 3, empty=False)
    _string(snapshot["ref_no"], "ref_no", 21, empty=False)
    _iso(snapshot["confirmed_at"], "confirmed_at")
    instant = datetime.fromisoformat(snapshot["confirmed_at"])
    items = child["items"]
    _require(0 < len(items) <= 99999, "成本列數量超出五位序號範圍")
    _require(len({i["path"] for i in items}) == len(items), "同張路徑不可重複")
    rows = []
    with localcontext(Context(prec=80, rounding=ROUND_HALF_UP)):
        for seq, item in enumerate(items, 1):
            # 成本金額尚未逐列捨入，unit_price 為每實際項目稅前報價。
            unit_price = Decimal(item["raw_cost"]) / Decimal(item["qty"]) * Decimal(child["calc"]["quote_rate"])
            row = dict(workgroup=snapshot["workgroup"], ref_no=snapshot["ref_no"], seq_no=f"{seq:05d}",
                       part_code=item["code"], part_desc=item["codsc"], path=item["path"],
                       opt_code=cat["nodes"][item["path"]]["optnoc"], opt_desc=item["label"],
                       spc_code=item["code"], spdsc=item["codsc"],
                       **{k: item[k] for k in ("qty", "stdqty", "stdpar", "compri", "amount")},
                       unit_price=str(unit_price), unit="", status="C", transferred="N",
                       adddate=instant.strftime("%Y-%m-%d"), addusrno=actor,
                       abndat=instant.strftime("%Y.%m.%d"), abntim=instant.strftime("%H:%M:%S"),
                       usrno=actor, prgno="DYNAMIC1")
            for key, length in (("path", 100), ("part_code", 10), ("part_desc", 40),
                                ("opt_code", 10), ("opt_desc", 40), ("spc_code", 10), ("spdsc", 40)):
                _string(row[key], key, length, empty=key != "path")
            for key in NUMERIC_FIELDS:
                if row[key] is None:
                    _require(key in {"stdqty", "stdpar"} and item["sources"].get("zero_cost") ==
                             "explicit_zero_structural_master", "只有明確零成本結構允許空用量")
                else:
                    _number(row[key], key, positive=key in {"qty", "stdpar"})
            rows.append(row)
    return rows


def _stored_number(value, profile):
    if value is None:
        return None
    number = float(_number(value, "明細數值"))
    if profile == "binary32":
        number = struct.unpack("!f", struct.pack("!f", number))[0]
    _require(math.isfinite(number), "明細數值超出儲存範圍")
    return number


def _database_rows(snapshot):
    return [{k: _stored_number(v, snapshot["numeric_storage"]) if k in NUMERIC_FIELDS else v
             for k, v in row.items()} for row in _detail_rows(snapshot)]


def _receipt(snapshot):
    return dict(confirmation_id=snapshot["confirmation_id"], child_id=snapshot["document"]["child"]["id"],
                ref_no=snapshot["ref_no"], confirmed_at=snapshot["confirmed_at"],
                document=snapshot["document"])


class DynamicStore:
    def __init__(self, db: LocalDatabase):
        self.db = db

    @property
    def _profile(self):
        return "binary64" if self.db.testing else "binary32"

    def _fetch(self, conn, confirmation_id):
        # 同一 SELECT 擷取文件＋全部明細：SQLite legacy transaction mode 與
        # MySQL READ COMMITTED 亦不會在兩次查詢間拼湊不同時點。
        columns = [f"q.{k} + 0e0 AS {k}" if k in NUMERIC_FIELDS else f"q.{k} AS {k}" for k in ROW_FIELDS]
        return conn.execute(text(
            "SELECT d.workgroup AS doc_workgroup, d.preview_id AS doc_id, d.ref_no AS doc_ref, "
            "d.payload AS doc_payload, " + ",".join(columns) +
            " FROM quote_snapshot_document d LEFT JOIN ordqdt_ai q "
            "ON q.workgroup=d.workgroup AND q.ref_no=d.ref_no "
            "WHERE d.preview_id=:id ORDER BY d.workgroup,q.seq_no"),
            {"id": confirmation_id}).mappings().all()

    def _decode(self, rows, confirmation_id):
        _require(bool(rows), "歷史文件不存在")
        row = rows[0]
        payload = row["doc_payload"]
        snapshot = _load_snapshot(payload)
        _require(snapshot["confirmation_id"] == _uuid(confirmation_id) and
                 snapshot["workgroup"] == self.db.workgroup and snapshot["numeric_storage"] == self._profile,
                 "歷史 UUID／工作群／數值儲存規格不符")
        _require(isinstance(snapshot["ref_no"], str) and _REF.fullmatch(snapshot["ref_no"]) is not None
                 and snapshot["ref_no"][-3:] != "000", "新版單號不符")
        _require(all(r["doc_id"] == confirmation_id and r["doc_workgroup"] == self.db.workgroup
                     and r["doc_ref"] == snapshot["ref_no"] and r["doc_payload"] == payload for r in rows),
                 "歷史文件識別衝突")
        doc = snapshot["document"]
        frozen = FrozenDynamicQuote(confirmation_id, _canonical(doc), snapshot["request_digest"])
        _thaw(frozen)
        _require(doc["context"]["catalog_snapshot"]["workgroup"] == self.db.workgroup, "歷史主檔工作群不符")
        expected = _database_rows(snapshot)
        _require(len(rows) == len(expected), "歷史明細缺列、額外列或 UUID 衝突")
        for actual, wanted in zip(rows, expected):
            # +0e0 讓 MySQL 以 DOUBLE 傳回 binary32 的確切值；不把 actual 再
            # 截成 FLOAT，也不接受 arbitrary tolerance（避免篡改被吞掉）。
            _require(all(actual[k] == wanted[k] for k in ROW_FIELDS), "歷史快照與 ordqdt_ai 明細不符")
        return snapshot

    def _allocate_ref(self, conn):
        prefix = "DQ" + _now().strftime("%Y%m%d%H%M%S") + "_"
        used = set()
        for table in ("ordqdt_ai", "quote_snapshot_document"):
            suffix = "" if self.db.testing else " FOR UPDATE"
            # range 而非 LIKE：底線不是萬用字元，且跨 workgroup 查碰撞。
            used.update(conn.execute(text(f"SELECT ref_no FROM {table} "
                                          "WHERE ref_no>=:lo AND ref_no<=:hi" + suffix),
                                     {"lo": prefix + "001", "hi": prefix + "999"}).scalars())
        for seq in range(1, 1000):
            candidate = prefix + f"{seq:03d}"
            if candidate not in used:
                return candidate
        raise StorageError("本秒單張配號已滿；請保留同一固定 UUID 稍後重試")

    def _insert_items(self, conn, snapshot):
        conn.execute(text("INSERT INTO ordqdt_ai (" + ",".join(ROW_FIELDS) + ") VALUES (" +
                          ",".join(":" + k for k in ROW_FIELDS) + ")"), _database_rows(snapshot))

    def save(self, frozen, *, confirmed, current, trusted_catalog_digest):
        """同一 UUID＋相同內容回原 receipt；任何衝突／殘缺都拒絕補存。"""
        _require(confirmed is True, "必須人工明確確認才可保存")
        _require(isinstance(current, dict), "保存需要可信的最新 analysis")
        _hex(trusted_catalog_digest, 64, "trusted_catalog_digest")
        document = _thaw(frozen)
        dynamic.validate_document(document, current=current, trusted_catalog_digest=trusted_catalog_digest)
        _require(document["context"]["catalog_snapshot"]["workgroup"] == self.db.workgroup, "工作群不符")
        try:
            with self.db.engine.connect() as conn:
                lock = _sqlite_lock(self.db.engine) if self.db.testing else _mysql_lock(conn)
                with lock:
                    # BEGIN IMMEDIATE 跨 engine／跨程序亦串行配號，不只 Python lock。
                    if self.db.testing:
                        conn.exec_driver_sql("BEGIN IMMEDIATE")
                    else:
                        conn.begin()
                    try:
                        self.db.inspect_schema(conn)
                        existing = self._fetch(conn, frozen.confirmation_id)
                        if existing:
                            snapshot = self._decode(existing, frozen.confirmation_id)
                            _require(snapshot["request_digest"] == frozen.digest and snapshot["document"] == document,
                                     "相同確認 UUID 已保存不同內容")
                        else:
                            snapshot = _snapshot(frozen, document, self.db.workgroup, self._allocate_ref(conn),
                                                 _now().isoformat(timespec="microseconds"), self._profile)
                            payload = _capacity(snapshot)
                            _database_rows(snapshot)  # schema/數值預檢在任何 INSERT 之前
                            conn.execute(text("INSERT INTO quote_snapshot_document "
                                              "(workgroup,preview_id,ref_no,payload) VALUES (:wg,:id,:ref,:payload)"),
                                         dict(wg=self.db.workgroup, id=frozen.confirmation_id,
                                              ref=snapshot["ref_no"], payload=payload))
                            self._insert_items(conn, snapshot)
                            verified = self._decode(self._fetch(conn, frozen.confirmation_id), frozen.confirmation_id)
                            _require(verified == snapshot, "保存讀回的完整邏輯快照不符")
                            snapshot = verified
                        conn.commit()
                        return _receipt(snapshot)
                    except BaseException:
                        conn.rollback()
                        raise
        except (StorageError, SchemaError):
            raise
        except Exception:
            raise StorageError("單張保存未完成；請保留原固定 UUID 重試查核，不另建確認") from None

    def read(self, confirmation_id):
        """唯讀快照＋所有明細，不重讀主檔；拒絕舊型別、損毀或跨群組 UUID。"""
        _uuid(confirmation_id)
        try:
            with self.db.read() as conn:
                rows = self._fetch(conn, confirmation_id)
                return _receipt(self._decode(rows, confirmation_id)) if rows else None
        except StorageError:
            raise
        except Exception:
            raise StorageError("無法完整核驗新版單張歷史") from None

    def list_saved(self, limit=20):
        """只列新版，每張均核驗；保留 DQ 號前綴使壞 JSON 也不被默默略過。"""
        _require(type(limit) is int and 1 <= limit <= 100, "limit 必須介於 1 至 100")
        try:
            with self.db.read() as conn:
                result, cursor = [], None
                while len(result) < limit:
                    where = "workgroup=:wg" + (" AND ref_no<:cursor" if cursor else "")
                    rows = conn.execute(text("SELECT preview_id,ref_no,payload FROM quote_snapshot_document WHERE " +
                                             where + " ORDER BY ref_no DESC LIMIT 100"),
                                        {"wg": self.db.workgroup, "cursor": cursor}).mappings().all()
                    if not rows:
                        break
                    for row in rows:
                        try:
                            _utf8(row["payload"], TEXT_BYTES, "歷史儲存容量")
                            doc = json.loads(row["payload"])
                        except (TypeError, ValueError, RecursionError):
                            doc = None
                        if (row["ref_no"].startswith("DQ") or
                                isinstance(doc, dict) and doc.get("document_type") == DOCUMENT_TYPE):
                            result.append(_receipt(self._decode(self._fetch(conn, row["preview_id"]), row["preview_id"])))
                            if len(result) == limit:
                                break
                    cursor = rows[-1]["ref_no"]
                return result
        except StorageError:
            raise
        except Exception:
            raise StorageError("無法完整列出新版單張歷史") from None
