"""不拆單合併報價的原子保存：一份工單＝一個單號，所有區塊明細連續序號。

prepare_document(combined.freeze(...)) -> FrozenCombinedQuote（固定 UUID，重試勿重建）
CombinedStore.save(frozen, confirmed=True, current=可信最新分析,
                   trusted_catalog_digest=可信主檔摘要) -> receipt
CombinedStore.read(confirmation_id) / list_saved(limit=20)

單號前綴 DM（與逐張 DQ 分開，互不誤讀歷史）。ordqdt_ai 主鍵為
(workgroup, ref_no, seq_no)，同一路徑（例：多組 CMT1\\A001）可在同單
重複出現，依品項→區塊→主檔階層順序配 seq_no。不 DDL、不寫主檔。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, ROUND_HALF_UP, localcontext
import json
import re
from uuid import uuid4

from sqlalchemy import text

from . import combined
from .backend import LocalDatabase, SchemaError
from .dynamic_storage import (MAX_LOGICAL_BYTES, NUMERIC_FIELDS, ROW_FIELDS, DynamicStore,
                              _capacity, _load_snapshot, _stored_number, _utf8, _uuid, validate_actor)
from .sql_dialect import insert_statement, is_mssql, limited_select, lock_suffix, lock_table
from .storage import (StorageError, TEXT_BYTES, _canonical, _digest, _hex, _iso,
                      _mssql_lock, _mysql_lock, _now, _number, _require, _sqlite_lock, _string)

DOCUMENT_TYPE = "local_dynamic_saved_combined"
PREFIX = "DM"
PROGRAM = "COMBINE1"
_REF = re.compile(PREFIX + r"\d{14}_[0-9]{3}\Z")


@dataclass(frozen=True)
class FrozenCombinedQuote:
    """可序列化、跨程序重試的固定請求；三欄均須原樣保留。"""
    confirmation_id: str
    payload: str
    digest: str


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


def _detail_rows(snapshot):
    """所有品項的所有區塊依序投影；同路徑可重複，seq_no 全單連續。"""
    doc = snapshot["document"]
    cat = doc["context"]["catalog_snapshot"]
    actor = validate_actor(doc["frozen_audit"]["actor"])
    _string(snapshot["workgroup"], "workgroup", 3, empty=False)
    _string(snapshot["ref_no"], "ref_no", 21, empty=False)
    _iso(snapshot["confirmed_at"], "confirmed_at")
    instant = datetime.fromisoformat(snapshot["confirmed_at"])
    items = [(child, item) for child in doc["children"] for item in child["items"]]
    _require(0 < len(items) <= 99999, "成本列數量超出五位序號範圍")
    rows = []
    with localcontext(Context(prec=80, rounding=ROUND_HALF_UP)):
        for seq, (child, item) in enumerate(items, 1):
            unit_price = Decimal(item["raw_cost"]) / Decimal(item["qty"]) * Decimal(child["calc"]["quote_rate"])
            row = dict(workgroup=snapshot["workgroup"], ref_no=snapshot["ref_no"], seq_no=f"{seq:05d}",
                       part_code=item["code"], part_desc=item["codsc"], path=item["path"],
                       opt_code=cat["nodes"][item["path"]]["optnoc"], opt_desc=item["label"],
                       spc_code=item["code"], spdsc=item["codsc"],
                       **{k: item[k] for k in ("qty", "stdqty", "stdpar", "compri", "amount")},
                       unit_price=str(unit_price), unit="", status="C", transferred="N",
                       adddate=instant.strftime("%Y-%m-%d"), addusrno=actor,
                       abndat=instant.strftime("%Y.%m.%d"), abntim=instant.strftime("%H:%M:%S"),
                       usrno=actor, prgno=PROGRAM)
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


def _database_rows(snapshot):
    return [{k: _stored_number(v, snapshot["numeric_storage"]) if k in NUMERIC_FIELDS else v
             for k, v in row.items()} for row in _detail_rows(snapshot)]


def prepare_document(document, *, confirmation_id=None):
    """重建驗證、容量預檢並配置 UUID；不連庫。重試時不可重建。"""
    payload = _canonical(document)
    _utf8(payload, MAX_LOGICAL_BYTES, "固定文件（完整邏輯快照上限 8 MiB）")
    detached = json.loads(payload)
    combined.validate_document(detached)
    _validate_audits(detached)
    identity = _uuid(confirmation_id if confirmation_id is not None else uuid4().hex)
    frozen = FrozenCombinedQuote(identity, payload, _request_digest(identity, detached))
    wg = detached["context"]["catalog_snapshot"]["workgroup"]
    preview = _snapshot(frozen, detached, wg, PREFIX + "20000101000000_001",
                        "2000-01-01T00:00:00.000000+08:00", "binary64")
    _detail_rows(preview)
    _capacity(preview, DOCUMENT_TYPE)
    return frozen


def _thaw(frozen):
    _require(isinstance(frozen, FrozenCombinedQuote), "必須提供 FrozenCombinedQuote")
    _uuid(frozen.confirmation_id)
    _hex(frozen.digest, 64, "digest")
    _utf8(frozen.payload, MAX_LOGICAL_BYTES, "固定文件（完整邏輯快照上限 8 MiB）")
    try:
        document = json.loads(frozen.payload)
    except (TypeError, ValueError, RecursionError):
        raise StorageError("固定內容不是完整 JSON") from None
    _require(_canonical(document) == frozen.payload, "固定內容不是 canonical JSON")
    _require(_request_digest(frozen.confirmation_id, document) == frozen.digest, "固定請求摘要不符")
    combined.validate_document(document)
    _validate_audits(document)
    return document


def _receipt(snapshot):
    return dict(confirmation_id=snapshot["confirmation_id"], ref_no=snapshot["ref_no"],
                child_ids=[c["id"] for c in snapshot["document"]["children"]],
                confirmed_at=snapshot["confirmed_at"], document=snapshot["document"])


class CombinedStore:
    def __init__(self, db: LocalDatabase):
        self.db = db
        self._rows = DynamicStore(db)  # 只借用相同 SELECT／數值儲存規格

    @property
    def _profile(self):
        return self._rows._profile

    def _fetch(self, conn, confirmation_id):
        return self._rows._fetch(conn, confirmation_id)

    def _decode(self, rows, confirmation_id):
        _require(bool(rows), "歷史文件不存在")
        payload = rows[0]["doc_payload"]
        snapshot = _load_snapshot(payload, DOCUMENT_TYPE)
        valid_profile = (snapshot["numeric_storage"] in ("binary32", "binary64") if is_mssql(self.db)
                         else snapshot["numeric_storage"] == self._profile)
        _require(snapshot["confirmation_id"] == _uuid(confirmation_id) and
                 snapshot["workgroup"] == self.db.workgroup and valid_profile,
                 "歷史 UUID／工作群／數值儲存規格不符")
        _require(isinstance(snapshot["ref_no"], str) and _REF.fullmatch(snapshot["ref_no"]) is not None
                 and snapshot["ref_no"][-3:] != "000", "合併單號不符")
        _require(all(r["doc_id"] == confirmation_id and r["doc_workgroup"] == self.db.workgroup
                     and r["doc_ref"] == snapshot["ref_no"] and r["doc_payload"] == payload for r in rows),
                 "歷史文件識別衝突")
        doc = snapshot["document"]
        _thaw(FrozenCombinedQuote(confirmation_id, _canonical(doc), snapshot["request_digest"]))
        _require(doc["context"]["catalog_snapshot"]["workgroup"] == self.db.workgroup, "歷史主檔工作群不符")
        expected = _database_rows(snapshot)
        _require(len(rows) == len(expected), "歷史明細缺列、額外列或 UUID 衝突")
        for actual, wanted in zip(rows, expected):
            _require(all(actual[k] == wanted[k] for k in ROW_FIELDS), "歷史快照與 ordqdt_ai 明細不符")
        return snapshot

    def _allocate_ref(self, conn):
        prefix = PREFIX + _now().strftime("%Y%m%d%H%M%S") + "_"
        used = set()
        for table in ("ordqdt_ai", "quote_snapshot_document"):
            used.update(conn.execute(text(f"SELECT ref_no FROM {lock_table(self.db, table)} "
                                          "WHERE ref_no>=:lo AND ref_no<=:hi" + lock_suffix(self.db)),
                                     {"lo": prefix + "001", "hi": prefix + "999"}).scalars())
        for seq in range(1, 1000):
            candidate = prefix + f"{seq:03d}"
            if candidate not in used:
                return candidate
        raise StorageError("本秒合併單號已滿；請保留同一固定 UUID 稍後重試")

    def save(self, frozen, *, confirmed, current, trusted_catalog_digest):
        """同一 UUID＋相同內容回原 receipt；任何衝突／殘缺都拒絕補存。"""
        _require(confirmed is True, "必須人工明確確認才可保存")
        _require(isinstance(current, dict), "保存需要可信的最新 analysis")
        _hex(trusted_catalog_digest, 64, "trusted_catalog_digest")
        document = _thaw(frozen)
        combined.validate_document(document, current=current, trusted_catalog_digest=trusted_catalog_digest)
        _require(document["context"]["catalog_snapshot"]["workgroup"] == self.db.workgroup, "工作群不符")
        try:
            with self.db.engine.connect() as conn:
                lock = (_mssql_lock(conn) if is_mssql(self.db) else
                        _sqlite_lock(self.db.engine) if self.db.testing else _mysql_lock(conn))
                with lock:
                    if self.db.testing and not is_mssql(self.db):
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
                            payload = _capacity(snapshot, DOCUMENT_TYPE)
                            rows = _database_rows(snapshot)  # 全部預檢在任何 INSERT 之前
                            header = dict(workgroup=self.db.workgroup, preview_id=frozen.confirmation_id,
                                          ref_no=snapshot["ref_no"], payload=payload)
                            if is_mssql(self.db):
                                self.db.validate_write_values("quote_snapshot_document", header)
                                for row in rows:
                                    self.db.validate_write_values("ordqdt_ai", row)
                            conn.execute(insert_statement(self.db, "quote_snapshot_document", header), header)
                            conn.execute(insert_statement(self.db, "ordqdt_ai", rows[0]), rows)
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
            raise StorageError("合併報價保存未完成；請保留原固定 UUID 重試查核，不另建確認") from None

    def read(self, confirmation_id):
        _uuid(confirmation_id)
        try:
            with self.db.read() as conn:
                rows = self._fetch(conn, confirmation_id)
                return _receipt(self._decode(rows, confirmation_id)) if rows else None
        except StorageError:
            raise
        except Exception:
            raise StorageError("無法完整核驗合併報價歷史") from None

    def list_saved(self, limit=20):
        """只列 DM 合併單，每張均核驗；壞 JSON 也不默默略過。"""
        _require(type(limit) is int and 1 <= limit <= 100, "limit 必須介於 1 至 100")
        try:
            with self.db.read() as conn:
                result, cursor = [], None
                while len(result) < limit:
                    where = "workgroup=:wg" + (" AND ref_no<:cursor" if cursor else "")
                    rows = conn.execute(text(limited_select(self.db, "preview_id,ref_no,payload",
                                             "quote_snapshot_document WHERE " + where + " ORDER BY ref_no DESC", 100)),
                                        {"wg": self.db.workgroup, "cursor": cursor}).mappings().all()
                    if not rows:
                        break
                    for row in rows:
                        try:
                            _utf8(row["payload"], TEXT_BYTES, "歷史儲存容量")
                            doc = json.loads(row["payload"])
                        except (TypeError, ValueError, RecursionError):
                            doc = None
                        if (row["ref_no"].startswith(PREFIX) or
                                isinstance(doc, dict) and doc.get("document_type") == DOCUMENT_TYPE):
                            result.append(_receipt(self._decode(self._fetch(conn, row["preview_id"]), row["preview_id"])))
                            if len(result) == limit:
                                break
                    cursor = rows[-1]["ref_no"]
                return result
        except StorageError:
            raise
        except Exception:
            raise StorageError("無法完整列出合併報價歷史") from None
