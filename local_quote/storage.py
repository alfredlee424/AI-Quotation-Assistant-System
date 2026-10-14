"""固定四張預覽的原子保存。沒有主檔查價、模型、開發快照或 DDL 相依。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import hmac
import json
import math
import re
import threading
import time
from uuid import UUID, uuid5
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text

from .backend import LocalDatabase, SchemaError


PREVIEW_TYPE = "local-master-four-quote"
CHILD_TYPE = "local-master-four-quote-child"
TEXT_BYTES = 65535
TAIPEI = ZoneInfo("Asia/Taipei")
FLOAT_MAX = Decimal("3.4028234663852886e38")
TOP_FIELDS = {"document_type", "document_version", "preview_id", "workgroup", "created_at",
              "category", "source", "input_digest", "batch_total", "children"}
SHARED_FIELDS = ("workgroup", "created_at", "category", "source", "input_digest", "batch_total")
NUMBER_FIELDS = ("qty", "product_qty", "line_qty", "stdqty", "stdpar", "compri",
                 "unit_price", "amount", "part_cost")
_sqlite_locks = WeakKeyDictionary()
_locks_guard = threading.Lock()


class StorageError(ValueError):
    """保存或歷史完整性錯誤；不夾帶 SQL 參數。"""


class LockTimeout(StorageError):
    pass


@dataclass(frozen=True)
class FrozenPreview:
    """可置於 UI session 的固定值；摘要是完整性檢查，不是身分簽章。"""
    payload: str
    digest: str


def _require(condition, message):
    if not condition:
        raise StorageError(message)


def _string(value, name, length=None, *, empty=True):
    _require(isinstance(value, str) and (empty or bool(value.strip())), f"{name}: 字串不完整")
    _require(length is None or len(value) <= length, f"{name}: 超出字元長度")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise StorageError(f"{name}: 無效 UTF-8") from None
    _require("\x00" not in value, f"{name}: 不允許 NUL")
    return value


def _number(value, name, *, positive=False):
    _require(isinstance(value, (str, int, float, Decimal)) and not isinstance(value, bool),
             f"{name}: 必須為有限數值")
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise StorageError(f"{name}: 必須為有限数值") from None
    _require(n.is_finite() and abs(n) <= FLOAT_MAX, f"{name}: 數值超出 FLOAT 範圍")
    _require(n > 0 if positive else n >= 0, f"{name}: 數值範圍錯誤")
    return n


def _hex(value, size, name):
    _require(isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{size}}}", value) is not None,
             f"{name}: 無效識別碼或摘要")
    return value


def _iso(value, name):
    _string(value, name, 40, empty=False)
    try:
        parsed = datetime.fromisoformat(value)
        _require(parsed.tzinfo is not None and parsed.utcoffset() is not None, f"{name}: 缺少時區")
    except (TypeError, ValueError):
        raise StorageError(f"{name}: 必須為帶時區的 ISO 時間") from None


def _json_tree(value):
    if isinstance(value, dict):
        _require(all(isinstance(k, str) for k in value), "JSON key 必須為字串")
        return {k: _json_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_tree(v) for v in value]
    if isinstance(value, Decimal):
        _require(value.is_finite(), "JSON 不允許非有限數值")
        return str(value)
    if isinstance(value, float):
        _require(math.isfinite(value), "JSON 不允許非有限數值")
    _require(value is None or isinstance(value, (str, int, float, bool)), "JSON 不支援此資料型態")
    if isinstance(value, str):
        _string(value, "JSON string")
    return value


def _canonical(value):
    try:
        return json.dumps(_json_tree(value), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (RecursionError, TypeError, ValueError):
        raise StorageError("無法建立完整 canonical JSON") from None


def _digest(payload):
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_preview(p):
    _require(isinstance(p, dict) and set(p) == TOP_FIELDS, "預覽頂層欄位不完整或版本不支援")
    _require(p["document_type"] == PREVIEW_TYPE and type(p["document_version"]) is int
             and p["document_version"] == 1, "只接受本機四張預覽版本 1")
    _hex(p["preview_id"], 32, "preview_id")
    _hex(p["input_digest"], 64, "input_digest")
    _string(p["workgroup"], "workgroup", 3, empty=False)
    _iso(p["created_at"], "created_at")
    _string(p["source"], "source", empty=False)
    category = p["category"]
    _require(isinstance(category, dict) and {"prodkind", "codsc", "quo_rate"} <= set(category),
             "類別資訊不完整")
    _string(category["prodkind"], "prodkind", 12, empty=False)
    _string(category["codsc"], "codsc", 30)
    _number(category["quo_rate"], "quo_rate", positive=True)
    _number(p["batch_total"], "batch_total")
    children = p["children"]
    _require(isinstance(children, list) and len(children) == 4, "必須包含完整四張報價")
    source_lines = set()
    for ordinal, child in enumerate(children, 1):
        _require(isinstance(child, dict) and {"ordinal", "product_qty", "description", "source_line",
                 "configuration", "requirements", "calc"} <= set(child), "子報價資訊不完整")
        _require(type(child["ordinal"]) is int and child["ordinal"] == ordinal, "四張順序必須為 1..4")
        quantity = child["product_qty"]
        _require(type(quantity) is int and quantity > 0, "產品數量必須為正整數")
        _number(quantity, "product_qty", positive=True)
        _string(child["description"], "description", empty=False)
        line = child["source_line"]
        _require(type(line) is int and line > 0 and line not in source_lines,
                 "來源行號必須為不重複的正整數")
        source_lines.add(line)
        _require(isinstance(child["configuration"], dict) and bool(child["configuration"])
                 and isinstance(child["requirements"], list), "配置與需求不完整")
        calc = child["calc"]
        _require(isinstance(calc, dict) and {"items", "total_cost", "total_price"} <= set(calc),
                 "固定成本資訊不完整")
        for key in ("total_cost", "total_price"):
            _number(calc[key], key)
        items = calc["items"]
        _require(isinstance(items, list) and 0 < len(items) <= 99999, "成本列數量超出序號範圍")
        paths = set()
        for item in items:
            required = set(NUMBER_FIELDS) | {"path", "optno", "optdesc", "spc_code", "spdsc",
                                             "source", "driver_path", "driver_code"}
            _require(isinstance(item, dict) and required <= set(item), "固定成本列欄位不完整")
            for key, maximum in (("path", 100), ("optno", 10), ("optdesc", 40),
                                 ("spc_code", 10), ("spdsc", 40),
                                 ("driver_path", 100), ("driver_code", 10)):
                _string(item[key], key, maximum, empty=key != "path")
            for key, maximum in (("part_code", 10), ("part_desc", 40), ("unit", 5)):
                if key in item:
                    _string(item[key], key, maximum)
            _string(item["source"], "item.source", empty=False)
            _require(item["path"] not in paths, "同張成本路徑不得重複")
            paths.add(item["path"])
            numbers = {k: _number(item[k], k, positive=k in {"qty", "product_qty", "line_qty", "stdpar"})
                       for k in NUMBER_FIELDS}
            _require(numbers["product_qty"] == quantity
                     and numbers["qty"] == numbers["product_qty"] * numbers["line_qty"],
                     "qty 必須為 product_qty × line_qty 的總項目數")
        # Check optional calculation amount fields too, without recomputing prices.
        for key, value in calc.items():
            if key != "items" and (key.endswith(("cost", "price", "amount", "rate"))
                                   or key in {"subtotal", "after_discount"}):
                _number(value, key)


def freeze_preview(preview: dict) -> FrozenPreview:
    """Detach/canonicalize input; reject incomplete/non-finite/overlong content."""
    payload = _canonical(preview)
    detached = json.loads(payload)
    _validate_preview(detached)
    with localcontext() as ctx:
        ctx.prec = 80
        total = sum((_number(c["calc"]["total_price"], "total_price") for c in detached["children"]), Decimal(0))
        _require(total == _number(detached["batch_total"], "batch_total"), "整批總額與四張固定總額不符")
    digest = _digest(payload)
    # Reserve the full ISO microsecond timestamp width, regardless of clock precision.
    docs = BatchStore._documents(detached, digest, _child_ids(detached["preview_id"]),
                                 [f"20000101000000_{i:03d}" for i in range(1, 5)],
                                 "2000-01-01T00:00:00.000000+08:00")
    _require(all(len(_canonical(d).encode("utf-8")) <= TEXT_BYTES for d in docs),
             "單張快照超過 TEXT 65535 UTF-8 bytes，拒絕固定")
    return FrozenPreview(payload, digest)


def _thaw(frozen):
    _require(isinstance(frozen, FrozenPreview), "必須提供 FrozenPreview")
    _hex(frozen.digest, 64, "frozen.digest")
    _string(frozen.payload, "frozen.payload", empty=False)
    _require(hmac.compare_digest(_digest(frozen.payload), frozen.digest), "固定內容摘要不符")
    try:
        p = json.loads(frozen.payload)
    except (ValueError, RecursionError):
        raise StorageError("固定內容不是有效 JSON") from None
    validated = freeze_preview(p)
    _require(validated.payload == frozen.payload, "固定內容不是 canonical JSON")
    return p


def _child_ids(batch_id):
    return [uuid5(UUID(hex=batch_id), str(i)).hex for i in range(1, 5)]


def _now():
    return datetime.now(TAIPEI)


@contextmanager
def _mysql_lock(conn):
    """The name is database-wide, never scoped by workgroup. Same connection cleanup."""
    name = None
    acquired = False
    try:
        try:
            database = conn.execute(text("SELECT DATABASE()")).scalar_one()
            _require(isinstance(database, str) and bool(database), "未選擇保存資料庫")
            name = "local-four-quote:" + hashlib.sha256(database.casefold().encode()).hexdigest()[:40]
            result = conn.execute(text("SELECT GET_LOCK(:name, 5)"), {"name": name}).scalar_one()
        except Exception:
            # A network error can leave acquisition outcome unknown.
            conn.invalidate()
            raise StorageError("無法確認配號鎖狀態，連線已失效") from None
        if result != 1:
            raise LockTimeout("無法在 5 秒內取得配號鎖")
        acquired = True
        conn.rollback()  # GET_LOCK's autobegin must end before explicit begin.
        yield
    finally:
        if acquired:
            try:
                conn.rollback()
                released = conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name}).scalar_one()
                if released != 1:
                    conn.invalidate()
                else:
                    conn.rollback()
            except Exception:
                conn.invalidate()
        elif not getattr(conn, "invalidated", False):
            conn.rollback()


@contextmanager
def _sqlite_lock(engine):
    # Only an explicit isolated testing engine reaches this branch.
    with _locks_guard:
        lock = _sqlite_locks.setdefault(engine, threading.Lock())
    if not lock.acquire(timeout=5):
        raise LockTimeout("測試配號鎖逾時")
    try:
        yield
    finally:
        lock.release()


def _select_documents(conn, workgroup, ids):
    statement = text("SELECT workgroup, preview_id, ref_no, payload FROM quote_snapshot_document "
                     "WHERE workgroup=:wg AND preview_id IN :ids").bindparams(bindparam("ids", expanding=True))
    return conn.execute(statement, {"wg": workgroup, "ids": ids}).mappings().all()


def _decode_history(rows, batch_id, workgroup):
    _require(len(rows) == 4, "歷史批次不完整；拒絕補存或回傳部分結果")
    ids = _child_ids(batch_id)
    by_id = {r["preview_id"]: r for r in rows}
    _require(set(by_id) == set(ids), "歷史子張識別碼不符")
    docs = []
    for child_id in ids:
        row = by_id[child_id]
        _require(isinstance(row["payload"], str) and len(row["payload"].encode("utf-8")) <= TEXT_BYTES,
                 "歷史快照容量不符")
        try:
            doc = json.loads(row["payload"])
        except (TypeError, ValueError):
            raise StorageError("歷史快照 JSON 損毀") from None
        _require(isinstance(doc, dict) and doc.get("document_type") == CHILD_TYPE
                 and type(doc.get("document_version")) is int and doc["document_version"] == 1,
                 "不是本機四張子快照版本 1")
        _require(doc.get("batch_id") == batch_id and doc.get("preview_id") == child_id
                 and doc.get("workgroup") == workgroup and row["workgroup"] == workgroup
                 and doc.get("ref_no") == row["ref_no"], "歷史快照識別不一致")
        integrity = doc.get("snapshot_digest")
        _hex(integrity, 64, "snapshot_digest")
        _require(hmac.compare_digest(integrity, _digest(_canonical(
            {k: v for k, v in doc.items() if k != "snapshot_digest"}))), "歷史快照摘要不符")
        docs.append(doc)
    first = docs[0]
    common = (*SHARED_FIELDS, "batch_id", "frozen_digest", "confirmed_at", "ref_nos")
    _require(all(all(k in d and d[k] == first.get(k) for k in common) for d in docs),
             "四張共用批次資訊不一致")
    _iso(first["confirmed_at"], "confirmed_at")
    refs = first["ref_nos"]
    _require(isinstance(refs, list) and len(refs) == 4 and all(isinstance(r, str) for r in refs)
             and len(set(refs)) == 4 and refs == [d["ref_no"] for d in docs], "歷史單號集合不符")
    _require(all(re.fullmatch(r"\d{14}_00[1-4]", r) for r in refs)
             and len({r[:14] for r in refs}) == 1
             and [r[-3:] for r in refs] == ["001", "002", "003", "004"], "歷史單號格式不符")
    _require(all("child" in d for d in docs), "缺少完整子報價")
    preview = {"document_type": PREVIEW_TYPE, "document_version": 1, "preview_id": batch_id,
               **{k: first[k] for k in SHARED_FIELDS}, "children": [d["child"] for d in docs]}
    frozen = freeze_preview(preview)
    _require(frozen.digest == first["frozen_digest"], "歷史固定預覽摘要不符")
    return {"batch_id": batch_id, "ref_nos": refs, "confirmed_at": first["confirmed_at"],
            "children": docs}, frozen


class BatchStore:
    def __init__(self, db: LocalDatabase):
        self.db = db

    def save(self, frozen: FrozenPreview, *, confirmed: bool, input_digest: str):
        _require(confirmed is True, "必須明確確認後才可保存")
        preview = _thaw(frozen)
        _hex(input_digest, 64, "input_digest")
        _require(hmac.compare_digest(input_digest, preview["input_digest"]), "輸入已變更，請重新預覽")
        _require(preview["workgroup"] == self.db.workgroup, "工作組不符")
        try:
            with self.db.engine.connect() as conn:
                lock = _sqlite_lock(self.db.engine) if self.db.testing else _mysql_lock(conn)
                with lock:
                    with conn.begin():
                        self.db.inspect_schema(conn)
                        ids = _child_ids(preview["preview_id"])
                        existing = _select_documents(conn, self.db.workgroup, ids)
                        if existing:
                            result, old = _decode_history(existing, preview["preview_id"], self.db.workgroup)
                            _require(old == frozen, "相同批次 ID 已保存不同內容")
                            return result
                        refs = self._allocate_refs(conn)
                        confirmed_at = _now().isoformat()
                        documents = self._documents(preview, frozen.digest, ids, refs, confirmed_at)
                        # Validate every child's byte capacity BEFORE the first insert.
                        payloads = [_canonical(d) for d in documents]
                        _require(all(len(p.encode("utf-8")) <= TEXT_BYTES for p in payloads),
                                 "單張快照超過 TEXT 65535 UTF-8 bytes，拒絕保存")
                        for doc, payload in zip(documents, payloads):
                            conn.execute(text(
                                "INSERT INTO quote_snapshot_document (workgroup,preview_id,ref_no,payload) "
                                "VALUES (:workgroup,:preview_id,:ref_no,:payload)"),
                                {"workgroup": self.db.workgroup, "preview_id": doc["preview_id"],
                                 "ref_no": doc["ref_no"], "payload": payload})
                            self._insert_items(conn, doc)
                        return {"batch_id": preview["preview_id"], "ref_nos": refs,
                                "confirmed_at": confirmed_at, "children": documents}
        except (StorageError, SchemaError):
            raise
        except Exception:
            # SQLAlchemy/driver exception strings may contain credentials or payloads.
            raise StorageError("整批保存未完成；請以原固定預覽重試查核，不另建批次") from None

    def _allocate_refs(self, conn):
        deadline = time.monotonic() + 5
        attempted = None
        for _ in range(120):  # Bounded even if a test clock is accidentally frozen.
            stamp = _now().strftime("%Y%m%d%H%M%S")
            if stamp != attempted:
                refs = [f"{stamp}_{i:03d}" for i in range(1, 5)]
                collision = False
                for table in ("ordqdt_ai", "quote_snapshot_document"):
                    # Global, not workgroup-scoped, collision check. Locking reads see
                    # external commits even at MySQL REPEATABLE READ isolation.
                    suffix = "" if self.db.testing else " FOR UPDATE"
                    stmt = text(f"SELECT ref_no FROM {table} WHERE ref_no IN :refs LIMIT 1{suffix}")
                    stmt = stmt.bindparams(bindparam("refs", expanding=True))
                    if conn.execute(stmt, {"refs": refs}).first() is not None:
                        collision = True
                if not collision:
                    return refs
                attempted = stamp
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        raise StorageError("配號碰撞等待逾時；沒有保留或虛構任何單號")

    @staticmethod
    def _documents(preview, digest, ids, refs, confirmed_at):
        documents = []
        for child, child_id, ref in zip(preview["children"], ids, refs):
            doc = {"document_type": CHILD_TYPE, "document_version": 1,
                   "batch_id": preview["preview_id"], "preview_id": child_id,
                   "frozen_digest": digest, "confirmed_at": confirmed_at,
                   "ref_nos": refs, "ref_no": ref, "child": child,
                   **{k: preview[k] for k in SHARED_FIELDS}}
            doc["snapshot_digest"] = _digest(_canonical(doc))
            documents.append(doc)
        return documents

    @staticmethod
    def _insert_items(conn, doc):
        rows = []
        for seq, item in enumerate(doc["child"]["calc"]["items"], 1):
            rows.append({
                "workgroup": doc["workgroup"], "ref_no": doc["ref_no"], "seq_no": f"{seq:05d}",
                "part_code": item["spc_code"], "part_desc": item.get("part_desc", item["spdsc"]),
                "path": item["path"], "opt_code": item["optno"], "opt_desc": item["optdesc"],
                "spc_code": item["spc_code"], "spdsc": item["spdsc"],
                **{k: float(Decimal(str(item[k]))) for k in
                   ("qty", "stdqty", "stdpar", "compri", "unit_price", "amount")},
                "unit": "", "status": "C", "transferred": "N",
                "adddate": doc["confirmed_at"][:10], "addusrno": "", "prgno": "LOCAL4",
            })
        names = tuple(rows[0])
        stmt = text("INSERT INTO ordqdt_ai (" + ",".join(names) + ") VALUES ("
                    + ",".join(":" + k for k in names) + ")")
        conn.execute(stmt, rows)

    def read(self, batch_id: str):
        """Snapshot-only read; never inspect or query master/pricing tables."""
        _hex(batch_id, 32, "batch_id")
        try:
            with self.db.read() as conn:
                rows = _select_documents(conn, self.db.workgroup, _child_ids(batch_id))
                if not rows:
                    return None
                return _decode_history(rows, batch_id, self.db.workgroup)[0]
        except StorageError:
            raise
        except Exception:
            raise StorageError("無法完整讀取歷史快照") from None

    def list_saved(self, limit=20):
        _require(type(limit) is int and 1 <= limit <= 100, "limit 必須介於 1 與 100")
        try:
            with self.db.read() as conn:
                # Keyset pages avoid loading old single-product documents into RAM.
                cursor = None
                result, seen = [], set()
                while len(result) < limit:
                    where = "workgroup=:wg" + (" AND ref_no < :cursor" if cursor else "")
                    rows = conn.execute(text(
                        "SELECT preview_id, ref_no, payload FROM quote_snapshot_document WHERE "
                        + where + " ORDER BY ref_no DESC LIMIT 100"),
                        {"wg": self.db.workgroup, "cursor": cursor}).mappings().all()
                    if not rows:
                        break
                    for row in rows:
                        try:
                            doc = json.loads(row["payload"])
                        except (TypeError, ValueError):
                            continue  # Legacy documents are not interpreted or modified.
                        if (not isinstance(doc, dict) or doc.get("document_type") != CHILD_TYPE
                                or doc.get("document_version") != 1):
                            continue
                        batch_id = _hex(doc.get("batch_id"), 32, "batch_id")
                        if batch_id in seen:
                            continue
                        seen.add(batch_id)
                        history, _ = _decode_history(
                            _select_documents(conn, self.db.workgroup, _child_ids(batch_id)),
                            batch_id, self.db.workgroup)
                        result.append({"batch_id": batch_id, "ref_nos": history["ref_nos"]})
                        if len(result) == limit:
                            break
                    cursor = rows[-1]["ref_no"]
                return result
        except StorageError:
            raise
        except Exception:
            raise StorageError("無法完整列出歷史快照") from None
