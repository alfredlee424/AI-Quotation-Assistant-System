"""本機合成驗收持久控制器；不提供正式核准或文件匯入。

目前 UI 使用 prepare/generate/complete_test：一次確認並測試保存。
舊 initialize/create/confirm/save 契約保留給歷史；不遷移、不假造兩次事件。
create 只收來源及操作設定；context() 建立本輪完整上下文，UI 每次編輯須
synchronize_context，失效不可復活，修改須 create(new_version_of=舊 id)。
preview 的 document 必須完整展示；以 next_stage/token 分開呼叫 confirm。
所有公開回傳物件都是展示資料，不能拿來 create 或 save。

專用目錄鎖與 workflow 的 BEGIN IMMEDIATE 串行化本模式寫入及確認 CAS；
身份先獨立提交，才進入 registry。跨庫不是同一交易，重啟按原身份核驗／補記結果。
摘要、全表根摘要與不可變觸發器偵測損壞，不是抵抗同 UID 重寫整庫的簽章。
兩次確認是本機人工作業協定；UI 須分開展示完整預覽並送出操作，不是假冒
正式身份認證，也無法判定呼叫端是否真的由人閱讀過畫面。
頂層只使用標準庫，必須在 bootstrap 隔離後才載入核心。
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import time


VERSION = "four-quote-workflow-v1"
_SCHEMA = (
    "CREATE TABLE acceptance_workflow_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version TEXT NOT NULL, count INTEGER NOT NULL, root TEXT NOT NULL)",
    "CREATE TABLE acceptance_workflow_batch (id TEXT PRIMARY KEY, context_hash TEXT NOT NULL, predecessor TEXT UNIQUE REFERENCES acceptance_workflow_batch(id), immutable TEXT NOT NULL, immutable_digest TEXT NOT NULL, state TEXT NOT NULL, state_digest TEXT NOT NULL)",
    "CREATE TRIGGER acceptance_workflow_immutable BEFORE UPDATE OF id, context_hash, predecessor, immutable, immutable_digest ON acceptance_workflow_batch BEGIN SELECT RAISE(ABORT, 'immutable workflow identity'); END",
    "CREATE TRIGGER acceptance_workflow_no_delete BEFORE DELETE ON acceptance_workflow_batch BEGIN SELECT RAISE(ABORT, 'workflow history cannot be deleted'); END",
)


class WorkflowError(ValueError):
    """未知、損壞、不合時序或失效；不得默默另建身份。"""


class WorkflowOperationError(RuntimeError):
    """服務失敗；batch_id 已持久提交，使用 resume/save 原身份重試。"""

    def __init__(self, batch_id, phase):
        self.batch_id, self.phase = batch_id, phase
        super().__init__(f"{phase}；請以原批次 {batch_id} 查回／重試，不重新建請求")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _file_lock(directory):
    """鎖已識別目錄，不受 bootstrap 的資料庫 header fd.close 影響。

    SQLite 的 POSIX record lock 可能因同程序關閉同 inode 的另一 fd 被釋放；
    因此不能只靠 BEGIN IMMEDIATE。macOS flock 又與同檔 SQLite 鎖衝突，
    故鎖目錄 inode。所有控制器操作（含初始化）共用此鎖，不新增鎖檔、
    不鎖正式庫；OS 在程序退出時自動釋放。限定本版 macOS/POSIX。
    """
    from acceptance.bootstrap import isolate_process, verify_directory
    verify_directory(isolate_process(directory))
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("流程鎖等待超時；請以原身份／輸入重試")
                time.sleep(0.02)
        verify_directory(isolate_process(directory))
        info = os.fstat(fd)
        current = directory.stat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise WorkflowError("流程目錄在取得鎖時被替換")
        yield
    finally:
        os.close(fd)


@dataclass(frozen=True)
class InputContext:
    """只描述目前輸入，不是請求、身份、預覽或授權。保留精確輸入差異。"""

    raw_text: str
    actor: str
    accepted_assumptions: tuple[str, ...]
    source_confirmed: bool
    discount_rate: str = "0"
    business_version: str = "four-quote-business-v1"
    master_version: str = "synthetic-four-catalog-v1"


def _context_json(context):
    if (type(context) is not InputContext or type(context.raw_text) is not str
            or type(context.actor) is not str or not context.actor.strip()
            or len(context.actor) > 80 or type(context.accepted_assumptions) is not tuple
            or any(type(key) is not str for key in context.accepted_assumptions)
            or len(set(context.accepted_assumptions)) != len(context.accepted_assumptions)
            or type(context.source_confirmed) is not bool
            or type(context.discount_rate) is not str
            or type(context.business_version) is not str or type(context.master_version) is not str):
        raise WorkflowError("必須提供本輪完整 InputContext；不接受文件 dict 或省略來源")
    return _json(asdict(context))


class Workflow:
    @staticmethod
    def _mode(fixed):
        from acceptance import business
        return (business.TEST_CONFIRMATION if fixed["context"]["business_version"] == business.TEST_VERSION
                else business.LEGACY_CONFIRMATION)

    @staticmethod
    def test_context(raw, discount_rate="0"):
        from acceptance import business
        return InputContext(raw, "本機測試", (), False, discount_rate,
                            business.TEST_VERSION, business.MASTER_VERSION)

    @classmethod
    def prepare(cls, directory):
        """只於產生操作準備已識別的空庫；存在結構不覆寫、不修復。"""
        from acceptance import business
        from acceptance.bootstrap import isolate_process, verify_directory
        directory = verify_directory(isolate_process(directory))
        with _file_lock(directory):
            with closing(sqlite3.connect((directory / "master.sqlite").as_uri() + "?mode=rw", uri=True)) as db:
                objects = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
                if objects and objects != {"acceptance_synthetic_catalog"}:
                    raise WorkflowError("合成主檔結構不符；拒絕覆寫或修復。")
            if not objects:
                business.initialize_master(directory)
            with closing(sqlite3.connect((directory / "workflow.sqlite").as_uri() + "?mode=rw", uri=True)) as db:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    if not db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                        for sql in _SCHEMA:
                            db.execute(sql)
                        db.execute("INSERT INTO acceptance_workflow_meta VALUES (1, ?, 0, ?)", (VERSION, _hash("[]")))
        return cls(directory)

    @staticmethod
    def context(raw, *, actor, accepted_assumptions, source_confirmed, discount_rate="0"):
        """不查價；包含來源順序、操作者、設定接受狀態、折扣與設定版本。"""
        from acceptance import business
        if type(accepted_assumptions) not in (list, tuple, set, frozenset):
            raise WorkflowError("設定須為 id 清單")
        if any(type(key) is not str for key in accepted_assumptions):
            raise WorkflowError("設定 id 須為字串")
        result = InputContext(raw, actor, tuple(sorted(accepted_assumptions)), source_confirmed,
                              discount_rate, business.VERSION, business.MASTER_VERSION)
        _context_json(result)
        return result

    @classmethod
    def initialize(cls, directory):
        """只在 bootstrap 已識別的空 workflow 庫明確建表；不改 pragma 身份。"""
        from acceptance.bootstrap import isolate_process, verify_directory
        directory = verify_directory(isolate_process(directory))
        with _file_lock(directory), closing(sqlite3.connect((directory / "workflow.sqlite").as_uri() + "?mode=rw", uri=True)) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                    raise WorkflowError("流程庫已初始化或有未知結構；不覆寫／修復")
                for sql in _SCHEMA:
                    db.execute(sql)
                db.execute("INSERT INTO acceptance_workflow_meta VALUES (1, ?, 0, ?)", (VERSION, _hash("[]")))
        return cls(directory)

    def __init__(self, directory):
        from acceptance.bootstrap import isolate_process, verify_directory, snapshot_engine
        self.directory = verify_directory(isolate_process(directory))
        with self._transaction():
            pass
        self._engine = snapshot_engine(self.directory)
        from engine.development_atomic_snapshot import DevelopmentAtomicSnapshotService
        from engine.quote_number_reservation import QuoteNumberReservationService
        self._snapshots = DevelopmentAtomicSnapshotService(self._engine)
        self._registry = QuoteNumberReservationService(self._engine)

    def close(self):
        self._engine.dispose()

    @contextmanager
    def _transaction(self):
        from acceptance.bootstrap import isolate_process, verify_directory
        verify_directory(isolate_process(self.directory))
        with _file_lock(self.directory), closing(sqlite3.connect((self.directory / "workflow.sqlite").as_uri() + "?mode=rw",
                                    uri=True, timeout=15)) as db:
            db.row_factory = sqlite3.Row
            with db:
                db.execute("BEGIN IMMEDIATE")
                self._check_store(db)
                yield db

    @staticmethod
    def _rows(db):
        return [dict(row) for row in db.execute("SELECT * FROM acceptance_workflow_batch ORDER BY id")]

    def _check_store(self, db):
        schema = {row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}
        if schema != set(_SCHEMA):
            raise WorkflowError("流程表未初始化、版本不符或結構受損；不自動補表")
        rows = self._rows(db)
        meta = [tuple(row) for row in db.execute("SELECT singleton, version, count, root FROM acceptance_workflow_meta")]
        if meta != [(1, VERSION, len(rows), _hash(_json(rows)))]:
            raise WorkflowError("流程身份／內容／狀態清冊受損；不補身份")
        records = {row["id"]: self._decode(row) for row in rows}
        for record in records.values():
            fixed = record["fixed"]
            previous = fixed["predecessor"]
            if previous is None:
                if fixed["version"] != 0:
                    raise WorkflowError("初版版本錯誤")
            else:
                old = records.get(previous)
                if (old is None or not old["state"]["invalidated"]
                        or fixed["version"] != old["fixed"]["version"] + 1
                        or fixed["predecessor_request_id"] != old["request"].request_id):
                    raise WorkflowError("前版追溯或失效紀錄受損")

    @staticmethod
    def _decode(row):
        from engine.development_atomic_contract import decode_request, identity_of
        try:
            if (_hash(row["immutable"]) != row["immutable_digest"]
                    or _hash(row["state"]) != row["state_digest"]):
                raise WorkflowError("流程摘要錯誤")
            fixed, state = json.loads(row["immutable"]), json.loads(row["state"])
            from acceptance import business
            single = Workflow._mode(fixed) == business.TEST_CONFIRMATION
            request = decode_request(fixed["payload"])
            history = json.loads(request.source.history_json)
            context = fixed["context"]
            expected_input = dict(context, revision=fixed["version"], predecessor=fixed["predecessor_request_id"],
                                  actor=context["actor"].strip(), discount_rate=json.loads(request.policy_json)["discount_rate"],
                                  master_digest=history["input_context"]["master_digest"])
            if (fixed["format"] != VERSION or row["id"] != request.batch_id
                    or fixed["identity"] != asdict(identity_of(request))
                    or fixed["payload_digest"] != _hash(fixed["payload"])
                    or fixed["context_hash"] != row["context_hash"]
                    or row["context_hash"] != _hash(_json(context))
                    or row["predecessor"] != fixed["predecessor"]
                    or request.revision != fixed["version"]
                    or history["input_context"] != expected_input
                    or fixed["created_at"] != history["created_at"]
                    or request.source.raw_text != context["raw_text"]):
                raise WorkflowError("持久身份、版本、來源或上下文不一致")
            if (state["phase"] not in {"RESERVING", "RESERVATION_FAILED", "PREVIEW", "SAVE_FAILED", "SAVED"}
                    or type(state["invalidated"]) is not bool
                    or state["stage"] != len(state["confirmations"])
                    or state["stage"] not in (0, 1, 2)
                    or (single and state["stage"] > 1)
                    or (state["invalidated"] and (state["stage"] or state["token"] is not None))):
                raise WorkflowError("確認狀態錯誤")
            preview = state["preview"]
            if preview is not None and (_hash(_json(preview)) != state["preview_digest"]
                                        or preview != Workflow._document(fixed, preview["reservation"])):
                raise WorkflowError("含號預覽內容受損")
            for index, receipt in enumerate(state["confirmations"], 1):
                if (preview is None or receipt["stage"] != index
                        or (single and receipt.get("event") != "確認並測試保存")
                        or receipt["context_hash"] != row["context_hash"]
                        or receipt["preview_digest"] != state["preview_digest"]
                        or receipt["actor"] != context["actor"]):
                    raise WorkflowError("兩次確認未綁同一完整預覽")
            return {"fixed": fixed, "state": state, "request": request}
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowError("持久流程內容不完整或遭修改；拒絕補寫") from exc

    def _load(self, db, batch_id):
        if type(batch_id) is not str:
            raise WorkflowError("只接受本模式持久 batch id")
        row = db.execute("SELECT * FROM acceptance_workflow_batch WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise WorkflowError("未知批次；不另配身份")
        return self._decode(dict(row))

    @staticmethod
    def _seal(db):
        rows = Workflow._rows(db)
        db.execute("UPDATE acceptance_workflow_meta SET count=?, root=? WHERE singleton=1", (len(rows), _hash(_json(rows))))

    def _write_state(self, db, record):
        value = _json(record["state"])
        db.execute("UPDATE acceptance_workflow_batch SET state=?, state_digest=? WHERE id=?",
                   (value, _hash(value), record["request"].batch_id))
        self._seal(db)

    def _invalidate(self, db, record, observed):
        state = record["state"]
        if not state["invalidated"]:
            state["invalidated"] = True
            state["invalidated_at"] = _now()
            state["observed_context_hash"] = observed
            state["revoked_confirmations"] = state["confirmations"]
            state["confirmations"], state["stage"], state["token"] = [], 0, None
            self._write_state(db, record)

    def create(self, raw, *, actor, accepted_assumptions, source_confirmed, discount_rate="0", new_version_of=None,
               confirmation_mode="legacy-two-step"):
        """同輸入重試只取持久內容；明確新版一個前版只能產生一個直接後繼。"""
        context = self.context(raw, actor=actor, accepted_assumptions=accepted_assumptions,
                               source_confirmed=source_confirmed, discount_rate=discount_rate)
        from acceptance import business
        if confirmation_mode == business.TEST_CONFIRMATION:
            if actor != "本機測試" or accepted_assumptions or source_confirmed is not False:
                raise WorkflowError("單次測試使用預設設定，不記錄逐項人工接受")
            context = self.test_context(raw, discount_rate)
        elif confirmation_mode != business.LEGACY_CONFIRMATION:
            raise WorkflowError("未知確認契約")
        context_json = _context_json(context)
        context_hash = _hash(context_json)
        if new_version_of is not None:
            # 即使新內容解析／計算失敗，既已宣告修改，舊確認亦不得復活。
            with self._transaction() as db:
                old = self._load(db, new_version_of)
                self._recover(db, old)
                self._invalidate(db, old, context_hash)
        with self._transaction() as db:
            if new_version_of is None:
                matches = db.execute("SELECT id FROM acceptance_workflow_batch WHERE context_hash=? ORDER BY rowid DESC", (context_hash,)).fetchall()
            else:
                matches = db.execute("SELECT id FROM acceptance_workflow_batch WHERE predecessor=?", (new_version_of,)).fetchall()
            record = None
            for match in matches:
                candidate = self._load(db, match[0])
                if candidate["fixed"]["context_hash"] != context_hash:
                    raise WorkflowError("此前版已有不同後繼；請從最新版本明確建新版")
                if not candidate["state"]["invalidated"]:
                    record = candidate
                    break
            if matches and record is None:
                raise WorkflowError("同內容舊版已失效；不可回切，須明確 new_version_of")
            if record is None:
                from acceptance import business
                from engine.development_atomic_contract import validate_request, identity_of
                old = self._load(db, new_version_of) if new_version_of is not None else None
                version = old["fixed"]["version"] + 1 if old else 0
                predecessor_request = old["request"].request_id if old else None
                request = business.build_request(self.directory, raw, actor=actor,
                    accepted_assumptions=context.accepted_assumptions, source_confirmed=source_confirmed,
                    discount_rate=discount_rate, revision=version, predecessor=predecessor_request,
                    confirmation_mode=confirmation_mode)
                payload = validate_request(request)
                fixed = {"format": VERSION, "identity": asdict(identity_of(request)), "version": version,
                         "predecessor": new_version_of, "predecessor_request_id": predecessor_request,
                         "context": json.loads(context_json), "context_hash": context_hash,
                         "payload": payload, "payload_digest": _hash(payload),
                         "created_at": json.loads(request.source.history_json)["created_at"]}
                state = {"phase": "RESERVING", "invalidated": False, "invalidated_at": None,
                         "observed_context_hash": None, "stage": 0, "confirmations": [],
                         "revoked_confirmations": [], "token": None, "preview": None,
                         "preview_digest": None, "saved_digest": None, "last_error": None}
                fixed_json, state_json = _json(fixed), _json(state)
                db.execute("INSERT INTO acceptance_workflow_batch VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (request.batch_id, context_hash, new_version_of, fixed_json, _hash(fixed_json), state_json, _hash(state_json)))
                self._seal(db)
                record = {"fixed": fixed, "state": state, "request": request}
            batch_id = record["request"].batch_id
        # 到這裡身份已 COMMIT。服務回應遺失仍能由 context 或 id 找回。
        return self.resume(batch_id)

    def generate(self, raw, *, discount_rate="0", active_batch=None):
        """同內容重用原組；變更或已失效時自動接續最新測試版本。"""
        from acceptance import business
        context = self.test_context(raw, discount_rate)
        context_hash = _hash(_context_json(context))
        reuse, predecessor = None, None
        with self._transaction() as db:
            if active_batch is None:
                match = db.execute("SELECT id FROM acceptance_workflow_batch WHERE context_hash=? ORDER BY rowid DESC LIMIT 1",
                                   (context_hash,)).fetchone()
                active_batch = match[0] if match else None
            if active_batch is not None:
                record = self._load(db, active_batch)
                while True:
                    child = db.execute("SELECT id FROM acceptance_workflow_batch WHERE predecessor=?",
                                       (record["request"].batch_id,)).fetchone()
                    if child is None:
                        break
                    record = self._load(db, child[0])
                if record["fixed"]["context_hash"] == context_hash and not record["state"]["invalidated"]:
                    reuse = record["request"].batch_id
                else:
                    predecessor = record["request"].batch_id
                    self._recover(db, record)
                    self._invalidate(db, record, context_hash)
        if reuse is not None:
            return self.resume(reuse)
        return self.create(raw, actor="本機測試", accepted_assumptions=(), source_confirmed=False,
                           discount_rate=discount_rate, new_version_of=predecessor,
                           confirmation_mode=business.TEST_CONFIRMATION)

    @staticmethod
    def _document(fixed, reservation):
        from acceptance.business import WARNING
        request = json.loads(fixed["payload"])
        return {"document_type": "four-quote-numbered-preview-only", "warning": WARNING,
                "formal_approved": False, "synthetic_only": True, "can_quote": False,
                "can_confirm": False, "can_save": False, "importable": False,
                "identity": fixed["identity"], "version": fixed["version"],
                "predecessor": fixed["predecessor"], "created_at": fixed["created_at"],
                "context": fixed["context"], "context_hash": fixed["context_hash"],
                "fixed_digest": fixed["payload_digest"], "fixed_request": request,
                "reservation": reservation,
                "children": [{"ref_no": reserved["reserved_ref_no"], "suffix": reserved["suffix"],
                              "fixed_child": child} for child, reserved in
                             zip(request["children"], reservation["children"], strict=True)]}

    def _existing_reservation(self, request):
        # 舊 registry 無公開唯讀 API；使用其既有完整驗證器，不自己解讀／補號。
        from engine.development_atomic_contract import build_development_reservation_request
        from engine.quote_number_reservation import _validate
        value = build_development_reservation_request(request)
        with self._engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            return self._registry._existing(connection, value, _validate(value))

    def _recover(self, db, record):
        from engine.development_atomic_contract import identity_of
        from acceptance import business
        state, fixed = record["state"], record["fixed"]
        reservation = self._existing_reservation(record["request"])
        if state["preview"] is not None and reservation != state["preview"]["reservation"]:
            raise WorkflowError("原配號缺漏或已改動；禁止補配")
        if reservation is not None and state["preview"] is None:
            state["preview"] = self._document(fixed, reservation)
            state["preview_digest"] = _hash(_json(state["preview"]))
            state["phase"] = "PREVIEW"
            state["token"] = None if state["invalidated"] else secrets.token_hex(32)
            state["last_error"] = None
            self._write_state(db, record)
        saved = self._snapshots.read(identity_of(record["request"]))
        if saved is None:
            if state["saved_digest"] is not None:
                raise WorkflowError("已保存快照消失；禁止補存")
        else:
            if (saved["fixed_request"] != json.loads(fixed["payload"])
                    or saved["reservation"] != reservation
                    or (state["saved_digest"] is not None and state["saved_digest"] != saved["final_digest"])
                    or (state["saved_digest"] is None and state["stage"] != (
                        1 if self._mode(fixed) == business.TEST_CONFIRMATION else 2))):
                raise WorkflowError("查回身份／固定內容／兩次核對不一致")
            if state["saved_digest"] is None:
                state["saved_digest"], state["phase"], state["last_error"] = saved["final_digest"], "SAVED", None
                state["token"] = None
                self._write_state(db, record)
        return saved

    @staticmethod
    def _view(record, saved=None):
        fixed, state = record["fixed"], record["state"]
        context = dict(fixed["context"])
        context["accepted_assumptions"] = tuple(context["accepted_assumptions"])
        return {"id": fixed["identity"]["batch_id"], "identity": fixed["identity"],
                "confirmation_mode": Workflow._mode(fixed),
                "version": fixed["version"], "predecessor": fixed["predecessor"],
                "created_at": fixed["created_at"], "phase": state["phase"],
                "invalidated": state["invalidated"], "context": InputContext(**context),
                "stage": state["stage"], "next_stage": state["stage"] + 1 if state["token"] and state["stage"] < 2
                and Workflow._mode(fixed) == "legacy-two-step" else None,
                "token": state["token"], "preview_digest": state["preview_digest"],
                "confirmations": state["confirmations"], "revoked_confirmations": state["revoked_confirmations"],
                "invalidated_at": state["invalidated_at"], "observed_context_hash": state["observed_context_hash"],
                "last_error": state["last_error"], "document": state["preview"], "saved": saved}

    def resume(self, batch_id):
        """唯一配號重試入口；永遠重用原 typed request，不重新查價。"""
        failure = None
        with self._transaction() as db:
            record = self._load(db, batch_id)
            saved = self._recover(db, record)
            if record["state"]["preview"] is None and not record["state"]["invalidated"]:
                from engine.development_atomic_contract import build_development_reservation_request
                try:
                    self._registry.reserve(build_development_reservation_request(record["request"]))
                    saved = self._recover(db, record)
                except Exception as exc:
                    record["state"]["phase"] = "RESERVATION_FAILED"
                    record["state"]["last_error"] = "RESERVATION_FAILED"  # 不保存原文／SQL／例外訊息
                    self._write_state(db, record)
                    failure = exc
            result = self._view(record, saved)
        if failure is not None:
            raise WorkflowOperationError(batch_id, "RESERVATION_FAILED") from failure
        return result

    def retrieve(self, batch_id):
        """完整驗證查回，可補記跨庫提交結果，但絕不配號／保存／計價。"""
        with self._transaction() as db:
            record = self._load(db, batch_id)
            return self._view(record, self._recover(db, record))

    def preview(self, batch_id):
        result = self.retrieve(batch_id)
        if result["document"] is None:
            raise WorkflowError("尚無完整含號預覽；請明確 resume 原身份")
        return result

    def list_batches(self):
        """列出全部本模式批次，含待保留、失敗、失效及已保存；不觸發配號。"""
        with self._transaction() as db:
            result = []
            for row in db.execute("SELECT id FROM acceptance_workflow_batch ORDER BY rowid").fetchall():
                record = self._load(db, row[0])
                view = self._view(record, self._recover(db, record))
                result.append({key: view[key] for key in ("id", "identity", "version", "predecessor", "created_at", "phase", "invalidated", "stage", "last_error", "confirmation_mode")})
            return result

    def synchronize_context(self, batch_id, context):
        """任何差異持久撤銷兩次確認；回切原內容亦不能恢復。"""
        observed = _hash(_context_json(context))
        with self._transaction() as db:
            record = self._load(db, batch_id)
            self._recover(db, record)
            if observed != record["fixed"]["context_hash"]:
                self._invalidate(db, record, observed)
            return not record["state"]["invalidated"]

    def confirm(self, batch_id, stage, token, context):
        """stage 必須顯式為 1 或 2；同 stage/token 的雙點僅一次成功。"""
        if not self.synchronize_context(batch_id, context):
            raise WorkflowError("上下文已失效；須明確建立新版本")
        with self._transaction() as db:
            record = self._load(db, batch_id)
            saved = self._recover(db, record)
            state = record["state"]
            if (self._mode(record["fixed"]) != "legacy-two-step"
                    or type(stage) is not int or stage not in (1, 2) or saved is not None
                    or state["invalidated"] or state["preview"] is None
                    or stage != state["stage"] + 1 or type(token) is not str
                    or not secrets.compare_digest(token, state["token"] or "")):
                raise WorkflowError("確認 CAS 失敗／舊 token／錯誤階段；須分開核對完整含號預覽")
            old_state = _hash(_json(state))
            state["confirmations"].append({"stage": stage, "token": token, "at": _now(),
                "actor": context.actor, "context_hash": record["fixed"]["context_hash"],
                "preview_digest": state["preview_digest"]})
            state["stage"] = stage
            state["token"] = secrets.token_hex(32) if stage == 1 else None
            value = _json(state)
            changed = db.execute("UPDATE acceptance_workflow_batch SET state=?, state_digest=? WHERE id=? AND state_digest=?",
                                 (value, _hash(value), batch_id, old_state)).rowcount
            if changed != 1:
                raise WorkflowError("確認 CAS 已被其他操作消耗")
            self._seal(db)
            return self._view(record)

    def save(self, batch_id, context):
        """僅按持久身份保存；鎖內核對兩次 receipt，委派既有原子服務。"""
        if not self.synchronize_context(batch_id, context):
            raise WorkflowError("上下文已失效；只能查歷史，不得保存舊版")
        failure = None
        with self._transaction() as db:
            record = self._load(db, batch_id)
            saved = self._recover(db, record)
            state = record["state"]
            if (self._mode(record["fixed"]) != "legacy-two-step"
                    or state["invalidated"] or state["stage"] != 2 or state["preview"] is None):
                raise WorkflowError("必須兩次分開人工核對同一完整含號預覽")
            if saved is None:
                try:
                    self._snapshots.save(record["request"])
                    saved = self._recover(db, record)
                except Exception as exc:
                    state["phase"], state["last_error"] = "SAVE_FAILED", "SAVE_FAILED"
                    self._write_state(db, record)
                    failure = exc
            result = self._view(record, saved)
        if failure is not None:
            raise WorkflowOperationError(batch_id, "SAVE_FAILED") from failure
        return result

    def complete_test(self, batch_id, token, context):
        """一次人工確認事件，原四張原子保存；重試／並行不追加事件或重算。

        事件先提交，再進入既有跨庫保存。程序中止後可依原事件及固定文件續存。
        """
        from acceptance import business
        if not self.synchronize_context(batch_id, context):
            raise WorkflowError("來源或折扣已修改，請重新產生四張成本明細")
        with self._transaction() as db:
            record = self._load(db, batch_id)
            self._recover(db, record)
            state = record["state"]
            if (self._mode(record["fixed"]) != business.TEST_CONFIRMATION or state["invalidated"]
                    or state["preview"] is None or type(token) is not str):
                raise WorkflowError("請先產生本次四張成本明細")
            expected = state["confirmations"][0]["token"] if state["confirmations"] else state["token"]
            if not secrets.compare_digest(token, expected or ""):
                raise WorkflowError("成本明細已變更，請以目前四張重新確認")
            if not state["confirmations"]:
                state["confirmations"].append({"stage": 1, "event": "確認並測試保存", "token": token,
                    "at": _now(), "actor": context.actor, "context_hash": record["fixed"]["context_hash"],
                    "preview_digest": state["preview_digest"]})
                state["stage"], state["token"] = 1, None
                self._write_state(db, record)
        failure = None
        with self._transaction() as db:
            record = self._load(db, batch_id)
            saved = self._recover(db, record)
            state = record["state"]
            if state["invalidated"] or state["stage"] != 1:
                raise WorkflowError("來源或折扣已修改，請重新產生四張成本明細")
            if saved is None:
                try:
                    self._snapshots.save(record["request"])
                    saved = self._recover(db, record)
                except Exception as exc:
                    state["phase"], state["last_error"] = "SAVE_FAILED", "SAVE_FAILED"
                    self._write_state(db, record)
                    failure = exc
            result = self._view(record, saved)
        if failure is not None:
            raise WorkflowOperationError(batch_id, "SAVE_FAILED") from failure
        return result

    def child_documents(self, batch_id):
        """每張自足全文及結構化展示文件；保留完整來源／證據，永不成為匯入請求。"""
        view = self.preview(batch_id)
        preview = view["document"]
        request = preview["fixed_request"]
        result = []
        for child in preview["children"]:
            document = {"document_type": "four-quote-child-download-only", "warning": preview["warning"],
                "synthetic_only": True, "formal_approved": False, "importable": False,
                "can_quote": False, "can_confirm": False, "can_save": False,
                "identity": preview["identity"], "version": view["version"], "predecessor": view["predecessor"],
                "created_at": view["created_at"], "snapshot_created_at": view["saved"]["created_at"] if view["saved"] else None,
                "invalidated": view["invalidated"], "phase": view["phase"],
                "preview_digest": view["preview_digest"], "context": preview["context"],
                "source": request["source"], "source_history": json.loads(request["source"]["history_json"]),
                "policy": json.loads(request["policy_json"]), "batch_totals": json.loads(request["totals_json"]),
                **child}
            # 文字版完整展開所有內嵌 JSON 證據，不裁切成本列或只提供摘要。
            def expanded(value):
                if isinstance(value, dict):
                    return {key: expanded(json.loads(item) if key.endswith("_json") else item) for key, item in value.items()}
                if isinstance(value, list):
                    return [expanded(item) for item in value]
                return value
            text = (preview["warning"] + "\n驗收號碼：" + child["ref_no"] + "\n完整來源：\n"
                    + request["source"]["raw_text"] + "\n完整配置、成本、缺口及金額：\n"
                    + json.dumps(expanded(document), ensure_ascii=False, indent=2, allow_nan=False))
            result.append({"ref_no": child["ref_no"], "text": text, "structured": document})
        return result
