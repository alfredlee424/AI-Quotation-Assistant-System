"""T47 開發保留契約；不是正式 preview、報價或授權服務。

只接受明確 Engine，自己取得新連線並持有完整交易。沒有建表、正式庫預設、
UI、模型工具或 trial 轉接。SQLite 以外 runtime 刻意拒絕。
"""
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.pool import NullPool, QueuePool

from database.quote_number_reservation_schema import (
    REQUEST_VERSION, SCHEMA_VERSION, reservation_schema,
)


TAIPEI = ZoneInfo("Asia/Taipei")
DOCUMENT_TYPE = "quote-number-reservation-development"


class ReservationConflict(ValueError):
    """同一身份已有不可變保留，不可改內容、版本、順序或換 preview。"""


class ReservationTimeout(TimeoutError):
    """在有界等待內未取得整組保留；可使用相同 request 重試。"""


class UnsafeClock(ValueError):
    """時鐘無時區或倒退，不能偽造未來時間。"""


@dataclass(frozen=True)
class DevelopmentReservationChild:
    workgroup: str
    batch_id: str
    source_order_id: str
    child_quote_id: str
    source_item_id: str
    configuration_line_id: str
    product_qty: int
    fixed_content_json: str


@dataclass(frozen=True)
class DevelopmentReservationRequest:
    workgroup: str
    batch_id: str
    request_id: str
    preview_identity: str
    source_batch_id: str
    source_order_id: str
    revision: int
    origin: str
    fixed_context_json: str
    children: tuple[DevelopmentReservationChild, ...]
    request_version: str = REQUEST_VERSION
    schema_version: str = SCHEMA_VERSION


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _fixed_object(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("固定內容不得有重複 JSON key")
            result[key] = item
        return result

    if type(value) is not str:
        raise ValueError("固定內容必須為完整 JSON object 字串")
    result = json.loads(value, object_pairs_hook=pairs)
    if type(result) is not dict or not result:
        raise ValueError("固定內容不得為空或非 object")
    _json(result)  # 拒絕 NaN/Infinity，不改寫原本固定字串。


def _validate(request):
    if type(request) is not DevelopmentReservationRequest:
        raise ValueError("僅接受獨立 typed 開發保留 request，不接受試算／預覽文件")
    if (request.request_version != REQUEST_VERSION or request.schema_version != SCHEMA_VERSION):
        raise ReservationConflict("保留契約版本不相容")
    if type(request.workgroup) is not str or not re.fullmatch(r"[0-9]{3}", request.workgroup):
        raise ValueError("事業別必須為三位數字")
    for field in ("batch_id", "request_id", "preview_identity", "source_batch_id", "source_order_id"):
        _identifier(getattr(request, field))
    if len({request.batch_id, request.request_id, request.preview_identity}) != 3:
        raise ValueError("批次、request、preview 識別必須分離")
    if type(request.revision) is not int or request.revision < 0:
        raise ValueError("修訂版必須為非負整數")
    if type(request.origin) is not str or not request.origin.strip() or len(request.origin) > 80:
        raise ValueError("保留來源必填，最多 80 字")
    _fixed_object(request.fixed_context_json)
    if type(request.children) is not tuple or not 1 <= len(request.children) <= 999:
        raise ValueError("整批必須為固定 tuple，1 至 999 品項")
    seen = {key: set() for key in ("child_quote_id", "source_item_id", "configuration_line_id")}
    for child in request.children:
        if type(child) is not DevelopmentReservationChild:
            raise ValueError("子報價必須為開發保留型別")
        if (child.workgroup, child.batch_id, child.source_order_id) != (
                request.workgroup, request.batch_id, request.source_order_id):
            raise ValueError("子報價跨事業別／批次／來源工單")
        for key, values in seen.items():
            value = getattr(child, key)
            _identifier(value)
            if value in values:
                raise ValueError("子報價／來源品項／配置識別重複")
            values.add(value)
        if type(child.product_qty) is not int or not 1 <= child.product_qty <= 999999999:
            raise ValueError("成品數必須為正整數且最多九位")
        _fixed_object(child.fixed_content_json)
    return _json(asdict(request))


def _identifier(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("識別必須為 32 位小寫十六進位")


def taiwan_prefix(timestamp):
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise UnsafeClock("時鐘必須提供 aware datetime")
    local = timestamp.astimezone(TAIPEI)
    return (f"{local.year:04d}{local.month:02d}{local.day:02d}"
            f"{local.hour:02d}{local.minute:02d}{local.second:02d}")


def _child_rows(request, prefix):
    return [dict(batch_id=request.batch_id, workgroup=request.workgroup, prefix=prefix,
                 child_quote_id=child.child_quote_id, source_item_id=child.source_item_id,
                 configuration_line_id=child.configuration_line_id, position=index,
                 suffix=f"{index:03d}", reserved_ref_no=f"{prefix}_{index:03d}")
            for index, child in enumerate(request.children, 1)]


def _result(header, children):
    flags = dict(document_type=DOCUMENT_TYPE, can_confirm=False, can_quote=False, can_save=False)
    return dict(document_type=DOCUMENT_TYPE, can_confirm=False, can_quote=False, can_save=False,
                warning="僅配號保留，不是已開立報價或正式預覽，不得交付客戶",
                reservation={**dict(header), **flags},
                children=[{**dict(row), **flags} for row in children])


class QuoteNumberReservationService:
    def __init__(self, engine, *, clock=lambda: datetime.now(TAIPEI),
                 monotonic=time.monotonic, sleep=time.sleep,
                 timeout=2.0, max_attempts=50, poll_interval=0.05):
        if not isinstance(engine, Engine):
            raise ValueError("僅接受明確 Engine；不接受外部 Connection／Session／交易")
        if engine.dialect.name != "sqlite":
            raise NotImplementedError("僅驗證 SQLite runtime；MySQL/MSSQL 待 DBA 與併發驗收")
        if (not isinstance(engine.pool, (QueuePool, NullPool)) or
                engine.url.database in (None, "", ":memory:") or engine.url.query.get("mode") == "memory"):
            raise ValueError("須注入檔案型 SQLite 與獨立 checkout 連線池；拒絕共享單一連線")
        if (type(max_attempts) is not int or not 1 <= max_attempts <= 10000 or
                any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                    for v in (timeout, poll_interval))):
            raise ValueError("必須設定有限正值 deadline、poll 與 attempt 上限")
        self.engine, self.clock, self.monotonic, self.sleep = engine, clock, monotonic, sleep
        self.timeout, self.max_attempts, self.poll_interval = timeout, max_attempts, poll_interval
        _, self.header, self.children = reservation_schema()

    def _existing(self, connection, request, payload):
        h = self.header
        found = connection.execute(select(h).where(or_(
            h.c.batch_id == request.batch_id, h.c.request_id == request.request_id,
            h.c.preview_identity == request.preview_identity))).mappings().all()
        if not found:
            return None
        if len(found) != 1 or found[0]["payload"] != payload:
            raise ReservationConflict("身份已有不同內容／順序／preview 的不可變保留；須新批識別")
        row = found[0]
        expected_header = self._header_row(request, payload, row["prefix"], row["reserved_at"])
        expected_children = _child_rows(request, row["prefix"])
        stored = connection.execute(select(self.children).where(
            self.children.c.batch_id == request.batch_id).order_by(self.children.c.position)).mappings().all()
        if dict(row) != expected_header or [dict(item) for item in stored] != expected_children:
            raise ReservationConflict("保留 registry 不完整或遭修改，禁止補配／回收")
        if taiwan_prefix(datetime.fromisoformat(row["reserved_at"])) != row["prefix"]:
            raise ReservationConflict("保留時間與前綴不一致")
        return _result(row, stored)

    @staticmethod
    def _header_row(request, payload, prefix, reserved_at):
        return dict(batch_id=request.batch_id, workgroup=request.workgroup,
                    request_id=request.request_id, preview_identity=request.preview_identity,
                    prefix=prefix, reserved_at=reserved_at, schema_version=SCHEMA_VERSION,
                    request_version=REQUEST_VERSION, state="RESERVED", origin=request.origin,
                    child_count=len(request.children), content_digest=hashlib.sha256(payload.encode()).hexdigest(),
                    payload=payload)

    @staticmethod
    def _header_collision(error):
        # 只把已知 header 身份／prefix 唯一鍵當競爭；child、CHECK、FK 及其他錯誤原樣拋出。
        return (getattr(error.orig, "sqlite_errorcode", None) in
                (sqlite3.SQLITE_CONSTRAINT_UNIQUE, sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY) and
                str(error.orig) in {f"UNIQUE constraint failed: quote_number_reservation.{key}"
                                    for key in ("batch_id", "request_id", "preview_identity", "prefix")})

    def reserve(self, request):
        payload = _validate(request)
        started = self.monotonic()
        if not math.isfinite(started):
            raise UnsafeClock("monotonic 非有限值")
        deadline, last_tick, last_time, collided = started + self.timeout, started, None, None
        for attempt in range(self.max_attempts):
            tick = self.monotonic()
            if not math.isfinite(tick) or tick < last_tick:
                raise UnsafeClock("monotonic 時鐘倒退／無效")
            last_tick = tick
            if tick >= deadline:
                break
            try:
                with self.engine.connect() as connection:
                    if connection.in_transaction():
                        raise ValueError("服務不得參與呼叫端交易")
                    # 不讓 SQLite driver 在鎖內等待；全部重試於釋放連線後有界進行。
                    connection.exec_driver_sql("PRAGMA busy_timeout=0")
                    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
                        raise ValueError("隔離引擎須明確啟用 SQLite foreign_keys")
                    existing = self._existing(connection, request, payload)
                    if existing is not None:
                        return existing  # 跨日／重啟仍優先回原整組，不讀 wall clock。
                    high_water = connection.execute(select(func.max(self.header.c.prefix))).scalar()
                    timestamp = self.clock()
                    prefix = taiwan_prefix(timestamp)
                    if last_time is not None and timestamp < last_time:
                        raise UnsafeClock("wall clock 倒退")
                    last_time = timestamp
                    if high_water is not None and prefix < high_water:
                        raise UnsafeClock("時鐘早於 registry 已保留前綴；等待時鐘恢復後重試")
                    connection.rollback()  # 結束讀交易，避免 deferred read 升級寫入死鎖。
                    if collided is None or prefix > collided:
                        header = self._header_row(request, payload, prefix, timestamp.astimezone(TAIPEI).isoformat())
                        children = _child_rows(request, prefix)
                        with connection.begin():
                            try:
                                connection.execute(self.header.insert().values(**header))
                            except IntegrityError as error:
                                if not self._header_collision(error):
                                    raise
                                raise _HeaderRace(prefix) from error
                            for child in children:
                                connection.execute(self.children.insert().values(**child))
                        return _result(header, children)
            except _HeaderRace as race:
                collided = race.prefix
            except OperationalError as error:
                if getattr(error.orig, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY:
                    raise
            # 已退出連線／交易，sleep 失敗直接傳出，不留部分保留。
            tick = self.monotonic()
            if not math.isfinite(tick) or tick < last_tick:
                raise UnsafeClock("monotonic 時鐘倒退／無效")
            last_tick = tick
            if attempt + 1 >= self.max_attempts or tick >= deadline:
                break
            self.sleep(min(self.poll_interval, deadline - tick))
        raise ReservationTimeout("配號保留等待超時／達 attempt 上限；未取得新整組，請以原 request 重試")


class _HeaderRace(Exception):
    def __init__(self, prefix):
        self.prefix = prefix
