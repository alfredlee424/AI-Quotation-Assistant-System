"""T29 開發快照同版原子保存／完整查回。無 UI、模型、主檔或正式保存依賴。"""
from dataclasses import asdict
from datetime import datetime, timezone
import math
import sqlite3
import time

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, OperationalError

from database.development_atomic_schema import development_atomic_schema
from engine.development_atomic_contract import (
    DOCUMENT_TYPE, SCHEMA_VERSION, MAX_DOCUMENT_BYTES, build_development_reservation_request, canonical,
    decode_request, digest, fixed_object, identity_of, validate_identity, validate_request,
)
from engine.quote_number_reservation import (
    QuoteNumberReservationService, ReservationConflict, _validate as validate_reservation,
)


class AtomicSnapshotConflict(ValueError):
    """身份衝突、registry 不一致或已保存內容受損；不得補寫或修復。"""


class AtomicSnapshotTimeout(TimeoutError):
    """有限 busy/已知身份競爭重試已用盡；可重送原請求查回。"""


class _IdentityRace(Exception):
    pass


def _flags():
    return dict(document_type=DOCUMENT_TYPE, can_quote=False, can_confirm=False, can_save=False)


class DevelopmentAtomicSnapshotService:
    def __init__(self, engine, *, clock=lambda: datetime.now(timezone.utc),
                 monotonic=time.monotonic, sleep=time.sleep, timeout=2.0,
                 max_attempts=50, poll_interval=0.05):
        # 重用 T47 的引擎／pool／方言／有限重試參數安全檢查；永遠不呼叫 reserve。
        self._registry = QuoteNumberReservationService(
            engine, monotonic=monotonic, sleep=sleep, timeout=timeout,
            max_attempts=max_attempts, poll_interval=poll_interval)
        self.engine, self.clock = engine, clock
        self.monotonic, self.sleep = monotonic, sleep
        self.timeout, self.max_attempts, self.poll_interval = timeout, max_attempts, poll_interval
        self.metadata, self.batch, self.child, self.link, self.cost = development_atomic_schema()

    def _run(self, operation):
        started = self.monotonic()
        if not math.isfinite(started):
            raise ValueError("monotonic 非有限值")
        deadline, last = started + self.timeout, started
        for attempt in range(self.max_attempts):
            tick = self.monotonic()
            if not math.isfinite(tick) or tick < last:
                raise ValueError("monotonic 倒退／非有限值")
            last = tick
            if tick >= deadline:
                break
            try:
                with self.engine.connect() as connection:
                    if connection.in_transaction():
                        raise ValueError("不得使用外部交易")
                    driver = connection.connection.driver_connection
                    if (driver.isolation_level is None or
                            getattr(driver, "autocommit", sqlite3.LEGACY_TRANSACTION_CONTROL) != sqlite3.LEGACY_TRANSACTION_CONTROL):
                        raise ValueError("僅支援 SQLite legacy 受控交易模式；拒絕 autocommit 或自動持有交易")
                    connection.exec_driver_sql("PRAGMA busy_timeout=0")
                    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
                        raise ValueError("隔離引擎須明確啟用 SQLite foreign_keys")
                    connection.rollback()
                    return operation(connection)
            except _IdentityRace:
                pass
            except OperationalError as error:
                # 不吞 FK/CHECK/row UNIQUE，也不吞其他 operational error。
                if getattr(error.orig, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY:
                    raise
            tick = self.monotonic()
            if not math.isfinite(tick) or tick < last:
                raise ValueError("monotonic 倒退／非有限值")
            last = tick
            if attempt + 1 == self.max_attempts or tick >= deadline:
                break
            # 已關閉連線並 rollback；不在持鎖期間 sleep。
            self.sleep(min(self.poll_interval, deadline - tick))
        raise AtomicSnapshotTimeout("開發快照等待超時／達重試上限；請以原 request 查回，不重新配號")

    def _find(self, connection, identity):
        table = self.batch
        found = connection.execute(select(table).where(or_(*(
            getattr(table.c, key) == getattr(identity, key)
            for key in ("batch_id", "request_id", "preview_identity", "save_id"))))).mappings().all()
        if len(found) > 1 or (found and any(found[0][key] != value for key, value in asdict(identity).items())):
            raise AtomicSnapshotConflict("已有身份對應不同批次／請求／預覽／版本")
        return dict(found[0]) if found else None

    def _reservation(self, connection, reservation_request, reservation_payload):
        try:
            result = self._registry._existing(connection, reservation_request, reservation_payload)
        except (ReservationConflict, ValueError, TypeError) as error:
            raise AtomicSnapshotConflict("持久保留內容不一致或遭修改，禁止保存／修補") from error
        if result is None:
            raise AtomicSnapshotConflict("完整固定內容尚未在同一 registry 保留")
        return result

    @staticmethod
    def _document(request, payload, reservation, created_at):
        try:
            stamp = datetime.fromisoformat(created_at)
            if stamp.tzinfo is None or stamp.utcoffset() is None or len(created_at) > 32:
                raise ValueError("建立時間須為含時區且最多32字")
        except (ValueError, TypeError) as error:
            raise AtomicSnapshotConflict("開發快照時間錯誤") from error
        fixed_digest = digest(payload)
        children = []
        for child, reserved in zip(request.children, reservation["children"], strict=True):
            children.append({**_flags(), "schema_version": SCHEMA_VERSION,
                             "batch_id": request.batch_id, "workgroup": request.workgroup,
                             "preview_identity": request.preview_identity, "fixed_digest": fixed_digest,
                             "ref_no": reserved["reserved_ref_no"], "suffix": reserved["suffix"],
                             "fixed_child": asdict(child)})
        result = {**_flags(), "schema_version": SCHEMA_VERSION, "state": "DEVELOPMENT_ONLY",
                  "warning": "保存的是開發快照，不是正式報價；不認證身份或工程核准，不得交付客戶",
                  "identity": asdict(identity_of(request)), "created_at": created_at,
                  "fixed_digest": fixed_digest, "fixed_request": fixed_object(payload),
                  "reservation": reservation, "children": children}
        # 含完整固定內容、持久整組號碼、所有對應及建立時間；不是授權簽章。
        result["final_digest"] = digest(canonical(result))
        if len(canonical(result).encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise ValueError("最終開發文件超過 128 MiB；拒絕、不截斷")
        return result

    @staticmethod
    def _rows(request, document):
        prefix = document["reservation"]["reservation"]["prefix"]
        header = {**asdict(identity_of(request)), "prefix": prefix, "state": "DEVELOPMENT_ONLY",
                  "created_at": document["created_at"], "child_count": len(request.children),
                  "fixed_digest": document["fixed_digest"], "final_digest": document["final_digest"],
                  "payload": canonical(document)}
        children, links, costs = [], [], []
        for child, child_document in zip(request.children, document["children"], strict=True):
            owner = dict(batch_id=request.batch_id, child_quote_id=child.child_quote_id)
            children.append({**owner, "workgroup": request.workgroup, "prefix": prefix,
                             "ref_no": child_document["ref_no"], "position": child.position,
                             "row_count": len(child.rows), "payload": canonical(child_document)})
            # 全部作用範圍已固定在 batch；link 只投影該張的來源，避免平方級重複。
            source_lines = [dict(line_no=line.line_no, text=line.text, kind=line.kind) for line in request.source.lines
                            if child.source_item_id in line.applies_to]
            links.append({**owner, "source_batch_id": request.source.source_batch_id,
                          "source_order_id": request.source.source_order_id,
                          "source_item_id": child.source_item_id,
                          "configuration_line_id": child.configuration_line_id,
                          "source_line_no": child.source_line_no, "position": child.position,
                          "product_qty": child.product_qty,
                          "payload": canonical({**_flags(), "source_lines": source_lines})})
            for position, row in enumerate(child.rows, 1):
                costs.append({**owner, "seq_no": row.seq_no, "position": position, "path": row.path,
                              "payload": canonical({**_flags(), **owner, "ref_no": child_document["ref_no"],
                                                    "fixed_row": asdict(row)})})
        return header, children, links, costs

    def _verify(self, connection, header, expected_request=None):
        try:
            document = fixed_object(header["payload"], capacity=MAX_DOCUMENT_BYTES)
            payload = canonical(document["fixed_request"])
            request = decode_request(payload)
            if expected_request is not None and payload != expected_request:
                raise AtomicSnapshotConflict("同一身份已有不同固定內容，禁止覆寫")
            reservation_request = build_development_reservation_request(request)
            reservation = self._reservation(connection, reservation_request, validate_reservation(reservation_request))
            expected = self._document(request, payload, reservation, header["created_at"])
            rows = self._rows(request, expected)
            if header != rows[0]:
                raise AtomicSnapshotConflict("已保存批次摘要／payload／版本遭修改")
            for table, expected_rows, order in (
                    (self.child, rows[1], (self.child.c.position,)),
                    (self.link, rows[2], (self.link.c.position,)),
                    (self.cost, rows[3], (self.cost.c.child_quote_id, self.cost.c.position))):
                stored = connection.execute(select(table).where(table.c.batch_id == request.batch_id).order_by(*order)).mappings().all()
                if table is self.cost:
                    expected_rows = sorted(expected_rows, key=lambda r: (r["child_quote_id"], r["position"]))
                if [dict(row) for row in stored] != expected_rows:
                    raise AtomicSnapshotConflict("已保存子文件／來源關聯／成本列缺漏、多列或遭修改；不修補")
            # canonical round trip 保證 save/read 的巢狀 tuple/list 與精確字串完全一致。
            return fixed_object(canonical(expected), capacity=MAX_DOCUMENT_BYTES)
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, AtomicSnapshotConflict):
                raise
            raise AtomicSnapshotConflict("持久開發快照不完整或版本錯誤") from error

    @staticmethod
    def _identity_collision(error):
        return (getattr(error.orig, "sqlite_errorcode", None) in
                (sqlite3.SQLITE_CONSTRAINT_UNIQUE, sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY)
                and str(error.orig) in {f"UNIQUE constraint failed: development_atomic_batch.{key}"
                                        for key in ("batch_id", "request_id", "preview_identity", "save_id")})

    def save(self, request):
        payload = validate_request(request)
        reservation_request = build_development_reservation_request(request)
        reservation_payload = validate_reservation(reservation_request)
        identity = identity_of(request)

        def operation(connection):
            # SQLite legacy driver 的 SELECT 不會自動開始實體交易；明確 BEGIN 保證完整讀快照。
            connection.exec_driver_sql("BEGIN")
            existing = self._find(connection, identity)
            if existing is not None:
                return self._verify(connection, existing, payload)
            reservation = self._reservation(connection, reservation_request, reservation_payload)
            connection.rollback()  # 不將 deferred read 升級寫交易；讓唯一鍵處理真正競爭。
            stamp = self.clock()
            if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("建立時間須為 aware datetime")
            document = self._document(request, payload, reservation, stamp.astimezone(timezone.utc).isoformat())
            header, children, links, costs = self._rows(request, document)
            with connection.begin():
                connection.exec_driver_sql("BEGIN")  # owned 寫交易也明確開始實體 SQLite 交易。
                try:
                    connection.execute(self.batch.insert().values(**header))
                except IntegrityError as error:
                    if self._identity_collision(error):
                        raise _IdentityRace() from error
                    raise
                # INSERT 已取得寫交易；再用同一連線核驗持久 registry，不能信 caller dict。
                locked = self._reservation(connection, reservation_request, reservation_payload)
                if locked != reservation:
                    raise AtomicSnapshotConflict("保留在保存前已變更")
                for table, records in ((self.child, children), (self.link, links), (self.cost, costs)):
                    for record in records:
                        connection.execute(table.insert().values(**record))
                # 連觸發器造成的內容改寫也不回傳假成功。
                persisted = self._find(connection, identity)
                result = self._verify(connection, persisted, payload)
            return result

        return self._run(operation)

    def read(self, identity):
        """按完整身份與版本讀取、核驗所有表和 registry；沒有部分子張或舊文件混讀。"""
        validate_identity(identity)

        def operation(connection):
            connection.exec_driver_sql("BEGIN")
            existing = self._find(connection, identity)
            if existing is None:
                return None
            return self._verify(connection, existing)

        return self._run(operation)
