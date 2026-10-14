"""驗收程序及本機資料目錄邊界（頂層僅使用標準庫）。

這是可信本機程式的防誤接保護，不是抵抗同 UID 惡意程式的 OS 沙箱。
識別綁定包含檔案 inode；不得以複製／搬移資料庫冒充原目錄。
初始化失敗保留現場，不刪除、不覆寫、不自動修復。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
from contextlib import closing
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import threading
from urllib.parse import parse_qs, unquote, urlsplit


MARKER = ".four-quote-acceptance"
MANIFEST = "manifest.json"
DATABASES = ("master.sqlite", "snapshot.sqlite", "workflow.sqlite")
APPLICATION_ID = 0x46514131
FORMAT = "four-quote-acceptance-v1"
SNAPSHOT_TABLES = frozenset({
    "quote_number_reservation", "quote_number_reserved_child",
    "development_atomic_batch", "development_atomic_child",
    "development_atomic_source_link", "development_atomic_cost",
})
_directory: Path | None = None
_lock = threading.RLock()
_raw_connect = sqlite3.connect


class BootstrapError(RuntimeError):
    """不安全的程序、目錄或資料庫；不得自動修復後繼續。"""


def _path(value) -> Path:
    path = Path(os.path.abspath(os.fspath(value)))
    # 不使用 resolve() 接受原本指向別處的符號連結。
    original = Path(value).absolute()
    for candidate in (original, *original.parents, path, *path.parents):
        if candidate.is_symlink():
            raise BootstrapError("拒絕符號連結路徑")
    return path


def _private(path: Path, *, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or (not directory and info.st_nlink != 1)):
        raise BootstrapError("檔案必須由目前使用者持有、私有且不是連結")
    return info


def _read(path: Path, limit: int) -> bytes:
    before = _private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise BootstrapError("檔案已被替換")
        return stream.read(limit)


def _identity(info):
    return [info.st_dev, info.st_ino]


def _manifest(path) -> tuple[Path, dict]:
    directory = _path(path)
    info = _private(directory, directory=True)
    try:
        data = json.loads(_read(directory / MANIFEST, 16385))
        if (set(data) != {"format", "token", "directory", "files"}
                or data["format"] != FORMAT
                or data["directory"] != _identity(info)
                or not isinstance(data["token"], str)
                or len(data["token"]) != 64
                or _read(directory / MARKER, 100) != data["token"].encode("ascii")
                or set(data["files"]) != set(DATABASES)):
            raise BootstrapError("驗收目錄識別不符")
        for name in DATABASES:
            file = directory / name
            record = data["files"][name]
            header = _read(file, 100)
            if (set(record) != {"identity", "version"}
                    or record["identity"] != _identity(_private(file))
                    or len(header) != 100 or header[:16] != b"SQLite format 3\x00"
                    or int.from_bytes(header[68:72], "big") != APPLICATION_ID
                    or int.from_bytes(header[60:64], "big") != record["version"]):
                raise BootstrapError("資料庫識別與 manifest 不符")
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = directory / (name + suffix)
                if os.path.lexists(sidecar):
                    _private(sidecar)
        # 不讓額外 symlink／非私有檔案成為 UI 的輸入或輸出位置。
        for child in directory.iterdir():
            _private(child, directory=child.is_dir() and not child.is_symlink())
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise BootstrapError("驗收目錄不存在、損壞或權限不符") from exc
    return directory, data


def verify_directory(path) -> Path:
    """唯讀驗證識別、私有權限及保存六表；不建檔、不補表。"""
    directory, _ = _manifest(path)
    with closing(_raw_connect((directory / "snapshot.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables != SNAPSHOT_TABLES:
            raise BootstrapError("保存庫必須恰有六張開發表")
    return directory


def _exclusive(path: Path, content: bytes = b""):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _fresh_process():
    if _directory is not None or any(name in sys.modules for name in ("config", "database.connection")):
        raise BootstrapError("必須在尚未匯入設定／正式連線的新程序操作")


def initialize_directory(path) -> Path:
    """只接受全新目錄；建立三個識別庫，僅 snapshot 建立六表。

    master/workflow 保留為有識別但無業務表的庫，由主流程建立其 schema。
    此操作會隔離目前程序；一個程序只允許管理一個驗收目錄。
    """
    _fresh_process()
    directory = _path(path)
    os.umask(0o077)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    token = secrets.token_hex(32)
    data = {"format": FORMAT, "token": token,
            "directory": _identity(_private(directory, directory=True)), "files": {}}
    _exclusive(directory / MARKER, token.encode("ascii"))
    for name in DATABASES:
        file = directory / name
        _exclusive(file)
        version = secrets.randbelow(0x7ffffffe) + 1
        with closing(_raw_connect(file)) as db:
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            db.execute(f"PRAGMA user_version={version}")
        data["files"][name] = {"identity": _identity(_private(file)), "version": version}
    _exclusive(directory / MANIFEST, json.dumps(data, sort_keys=True).encode("utf-8"))
    isolate_process(directory)
    from database.development_atomic_schema import development_atomic_schema
    engine = _engine(directory / "snapshot.sqlite")
    try:
        development_atomic_schema()[0].create_all(engine)
    finally:
        engine.dispose()
    return verify_directory(directory)


def clean_environment(directory) -> dict[str, str]:
    """允許清單，不繼承金鑰、代理、DB URL、PYTHONPATH 或使用者 HOME。"""
    directory = _path(directory)
    return {
        "PATH": "/usr/bin:/bin", "HOME": str(directory), "LANG": "en_US.UTF-8",
        "MPLBACKEND": "Agg",  # Streamlit 使用固定的非互動繪圖後端。
        # Streamlit 啟動準備會補入此鍵；先固定空值，避免 UI 重入誤判環境異動。
        # 不繼承使用者 token，仍以整份環境相等檢查拒絕後續新增／修改。
        "MAPBOX_API_KEY": "",
        "PYTHON_DOTENV_DISABLED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "FOUR_QUOTE_DIRECTORY": str(directory),
        # URI 百分比編碼保留空白、#、?；mode=rw 禁止 fallback 隱式建庫。
        "DB_CONN_STR": "sqlite:///" + (directory / "master.sqlite").as_uri() + "?mode=rw&uri=true",
        "OPENAI_API_KEY": "", "OPENAI_MODEL": "disabled",
        "OPENAI_BASE_URL": "", "AZURE_OPENAI_API_KEY": "",
        "AZURE_OPENAI_ENDPOINT": "", "AZURE_OPENAI_AD_TOKEN": "",
        "AZURE_OPENAI_API_VERSION": "2024-10-21",
        "WORKGROUP": "103", "PRODUCT_PREFIX": "CMT1", "ORDKIND_PRODUCT": "1",
        "REQUIRED_OPTNOS": "A001", "MARKUP_RATE": "0.3", "TAX_RATE": "0.05",
        "MAX_DISCOUNT_RATE": "0.1", "LOG_FILE": os.devnull,
        "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
        "STREAMLIT_SERVER_ADDRESS": "127.0.0.1", "STREAMLIT_SERVER_HEADLESS": "true",
        "STREAMLIT_SERVER_FILE_WATCHER_TYPE": "none",
    }


def _database_path(value) -> Path:
    text = os.fsdecode(value)
    if text.startswith("file:"):
        uri = urlsplit(text)
        query = parse_qs(uri.query, keep_blank_values=True)
        if uri.netloc or set(query) - {"mode"} or query.get("mode", ["rw"])[0] not in {"ro", "rw"}:
            raise BootstrapError("拒絕 SQLite URI 選項")
        text = unquote(uri.path)
    candidate = _path(text)
    directory, _ = _manifest(_directory)
    if candidate.parent != directory or candidate.name not in DATABASES:
        raise BootstrapError("拒絕驗收目錄以外的資料庫")
    return candidate


def _loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    # 不允許 DNS 或可重新解析至外部的主機名稱。
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        file = Path(os.fsdecode(args[0]))
        if any(part.startswith(".env") for part in file.parts):
            raise BootstrapError("驗收模式禁止讀取私人設定")
        if file.suffix in {".db", ".sqlite", ".sqlite3"}:
            # DBAPI 的原生 open 另由 sqlite3.connect audit 守門。
            if _path(file).parent != _directory:
                raise BootstrapError("禁止存取外部資料庫檔案")
    if event == "sqlite3.connect":
        _database_path(args[0])
    if event in {"socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg"}:
        address = args[-1]
        if not isinstance(address, tuple) or not _loopback(address[0]):
            raise BootstrapError("只允許 loopback 網路")
    if event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
        if not _loopback(args[0]):
            raise BootstrapError("禁止外部 DNS／網路")
    if event == "import" and args[0].split(".")[0] in {
        "pyodbc", "pymysql", "MySQLdb", "mysql", "psycopg", "psycopg2", "pymssql",
    }:
        raise BootstrapError("驗收模式禁止外部資料庫 driver")


def _authorizer(action, arg1, arg2, database, source):
    if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and str(arg1).lower() in {"application_id", "user_version"} and arg2 is not None:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _connect(*args, **kwargs):
    db = _raw_connect(*args, **kwargs)
    db.set_authorizer(_authorizer)
    db.execute("PRAGMA foreign_keys=ON")
    return db


def isolate_process(directory) -> Path:
    """在 Streamlit／核心匯入前呼叫；同目錄可重入，禁止切庫。"""
    global _directory
    with _lock:
        directory, _ = _manifest(directory)
        if _directory is not None:
            if directory != _directory:
                raise BootstrapError("同程序不得切換驗收目錄")
            import config
            expected = clean_environment(directory)
            if (config.DB_CONN_STR != expected["DB_CONN_STR"] or config.USE_LLM
                    or config.USE_AZURE or config.LOG_FILE != os.devnull
                    or config.OPENAI_API_KEY or config.AZURE_OPENAI_ENDPOINT
                    or not config.IS_SQLITE or config.IS_MYSQL or config.IS_MSSQL
                    or dict(os.environ) != expected):
                raise BootstrapError("隔離設定已被修改")
            return directory
        _fresh_process()
        os.umask(0o077)
        os.environ.clear()
        os.environ.update(clean_environment(directory))
        sys.dont_write_bytecode = True
        _directory = directory
        sys.addaudithook(_audit)
        logging.disable(sys.maxsize)
        logger = logging.getLogger("ai_quote")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        import dotenv
        import dotenv.main
        dotenv.load_dotenv = dotenv.main.load_dotenv = lambda *a, **kw: False
        dotenv.dotenv_values = dotenv.main.dotenv_values = lambda *a, **kw: {}
        dotenv.find_dotenv = dotenv.main.find_dotenv = lambda *a, **kw: ""
        import _sqlite3
        sqlite3.connect = sqlite3.dbapi2.connect = _sqlite3.connect = _connect
        # 全域 dialect hook 在任何 driver 建立連線前檢查，而非連線後才拒絕。
        from sqlalchemy import event
        from sqlalchemy.engine import Dialect

        @event.listens_for(Dialect, "do_connect")
        def only_managed_sqlite(dialect, record, args, kwargs):
            if dialect.name != "sqlite":
                raise BootstrapError("只允許驗收 SQLite")
            _database_path(args[0])

        import config
        if config.USE_LLM or config.USE_AZURE or not config.IS_SQLITE:
            raise BootstrapError("設定隔離失敗")
        return directory


def _engine(file: Path):
    from sqlalchemy import URL, create_engine
    from sqlalchemy.pool import NullPool
    return create_engine(URL.create("sqlite", database=str(file)), poolclass=NullPool,
                         connect_args={"timeout": 0, "check_same_thread": False}, echo=False)


def snapshot_engine(path):
    """回傳有外鍵、獨立 checkout 的檔案 SQLite engine；不隱式建表。"""
    directory = isolate_process(path)
    verify_directory(directory)
    return _engine(directory / "snapshot.sqlite")
