"""每次安全斷言都在全新子程序，絕不依賴 conftest 的設定替換。"""
import json
import os
from pathlib import Path
import subprocess
import textwrap
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "venv/bin/python"
PRELUDE = r'''
import sys, os
from pathlib import Path
root, target = map(Path, sys.argv[1:3])
sys.path.insert(0, str(root))
def protect(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        p = Path(os.fsdecode(args[0])).absolute()
        if any(x.startswith(".env") for x in p.parts) or p.is_relative_to(root / "logs"):
            raise AssertionError("private setting/log access attempted")
sys.addaudithook(protect)
from acceptance import bootstrap as b
'''


@pytest.fixture(autouse=True)
def isolated_db():
    """覆蓋既有 autouse；本檔的父程序不需要任何業務連線。"""
    yield


def worker(target, source, *, env=None, check=True):
    result = subprocess.run([str(PYTHON), "-I", "-B", "-c", PRELUDE + textwrap.dedent(source),
                             str(ROOT), str(target)], cwd=ROOT, env=env or {},
                            capture_output=True, text=True, timeout=30)
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def initialized(tmp_path):
    target = tmp_path.resolve() / "acceptance"
    worker(target, "b.initialize_directory(target)")
    return target


def test_import_is_stdlib_only(tmp_path):
    worker(tmp_path, '''
assert not any(n in sys.modules for n in ("config", "sqlalchemy", "dotenv", "streamlit", "database.connection"))
assert "FOUR_QUOTE_DIRECTORY" not in os.environ
''')


def test_initialize_reopen_and_engine(tmp_path):
    target = initialized(tmp_path)
    worker(target, '''
assert b.verify_directory(target) == target
b.isolate_process(target)
assert b.isolate_process(target) == target
from sqlalchemy import inspect
engine = b.snapshot_engine(target)
assert engine.url.database == str(target / "snapshot.sqlite")
assert set(inspect(engine).get_table_names()) == b.SNAPSHOT_TABLES
with engine.connect() as connection:
    assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    assert connection.exec_driver_sql("PRAGMA application_id").scalar() == b.APPLICATION_ID
engine.dispose()
import sqlite3
from contextlib import closing
for name in ("master.sqlite", "workflow.sqlite"):
    with closing(sqlite3.connect(target / name)) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        db.execute("CREATE TABLE synthetic_test (id INTEGER PRIMARY KEY)")
assert b.verify_directory(target) == target
''')


def test_environment_dotenv_config_and_logs(tmp_path):
    target = tmp_path.resolve() / "fresh"
    worker(target, '''
b.initialize_directory(target)
import config, dotenv, logging
assert not config.USE_LLM and not config.USE_AZURE
assert config.IS_SQLITE and not config.IS_MYSQL and not config.IS_MSSQL
assert config.DB_CONN_STR == "sqlite:///" + (target / "master.sqlite").as_uri() + "?mode=rw&uri=true"
assert config.WORKGROUP == "103" and config.TAX_RATE == 0.05
assert config.LOG_FILE == os.devnull
assert os.environ["MAPBOX_API_KEY"] == ""
assert not any("sensitive-canary" in value for value in os.environ.values())
assert "UNKNOWN_SECRET" not in os.environ and "HTTPS_PROXY" not in os.environ
assert dotenv.load_dotenv(root / ".env") is False
assert dotenv.dotenv_values(root / ".env") == {}
from utils.logger import log_action
log_action("canary", params={"source": "sensitive-canary"})
assert isinstance(logging.getLogger("ai_quote").handlers[0], logging.NullHandler)
assert not list(target.glob("*.log"))
''', env={"OPENAI_API_KEY": "sensitive-canary", "DB_CONN_STR": "mysql://sensitive-canary",
          "MAPBOX_API_KEY": "sensitive-canary",
          "UNKNOWN_SECRET": "sensitive-canary", "HTTPS_PROXY": "sensitive-canary",
          "WORKGROUP": "999", "TAX_RATE": "99", "HOME": str(ROOT),
          "PYTHONPATH": "sensitive-canary"})


@pytest.mark.parametrize("module", ["config", "database.connection"])
def test_preimported_unsafe_process_rejected(tmp_path, module):
    worker(tmp_path.resolve() / "fresh", f'''
import types
sys.modules[{module!r}] = types.ModuleType({module!r})
try:
    b.initialize_directory(target)
except b.BootstrapError:
    pass
else:
    raise AssertionError("unsafe process accepted")
assert not target.exists()
''')


@pytest.mark.parametrize("kind", ["existing", "symlink", "ancestor", "nonprivate", "foreign"])
def test_bad_directory_rejected(tmp_path, kind):
    target = tmp_path.resolve() / "target"
    if kind == "existing":
        target.mkdir(mode=0o700)
    elif kind == "symlink":
        target.symlink_to(tmp_path.resolve(), target_is_directory=True)
    elif kind == "ancestor":
        target.symlink_to(tmp_path.resolve(), target_is_directory=True)
        target = target / "new"
    elif kind == "nonprivate":
        target.mkdir(mode=0o755)
        target.chmod(0o755)
    elif kind == "foreign":
        target.mkdir(mode=0o700)
        (target / "snapshot.sqlite").write_bytes(b"unmanaged")
    worker(target, '''
for operation in (b.initialize_directory, b.verify_directory):
    try:
        operation(target)
    except (b.BootstrapError, OSError):
        pass
    else:
        raise AssertionError("bad directory accepted")
''')


@pytest.mark.parametrize("mutation", ["marker", "manifest", "database", "permissions", "directory_permissions", "symlink", "hardlink", "missing_table", "sidecar"])
def test_tamper_rejected_without_repair(tmp_path, mutation):
    target = initialized(tmp_path)
    file = target / "snapshot.sqlite"
    if mutation == "marker":
        (target / ".four-quote-acceptance").write_text("wrong")
    elif mutation == "manifest":
        data = json.loads((target / "manifest.json").read_text())
        data["files"]["snapshot.sqlite"]["version"] += 1
        (target / "manifest.json").write_text(json.dumps(data))
    elif mutation == "database":
        replacement = target / "replacement"
        replacement.write_bytes(file.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(file)
    elif mutation == "permissions":
        file.chmod(0o644)
    elif mutation == "directory_permissions":
        target.chmod(0o755)
    elif mutation == "symlink":
        file.unlink()
        file.symlink_to(target / "master.sqlite")
    elif mutation == "hardlink":
        os.link(file, target / "hardlink")
    elif mutation == "missing_table":
        worker(target, '''
b.isolate_process(target)
import sqlite3
with sqlite3.connect(target / "snapshot.sqlite") as db:
    db.execute("DROP TABLE development_atomic_cost")
''')
    elif mutation == "sidecar":
        (target / "snapshot.sqlite-wal").symlink_to(target / "master.sqlite")
    before = (target / "manifest.json").read_bytes()
    worker(target, '''
try:
    b.verify_directory(target)
except (b.BootstrapError, OSError):
    pass
else:
    raise AssertionError("tamper accepted")
''')
    assert (target / "manifest.json").read_bytes() == before


def test_no_overwrite(tmp_path):
    target = initialized(tmp_path)
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    worker(target, '''
try:
    b.initialize_directory(target)
except FileExistsError:
    pass
else:
    raise AssertionError("overwrite accepted")
''')
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before


def test_network_and_database_guards(tmp_path):
    target = initialized(tmp_path)
    worker(target, '''
b.isolate_process(target)
import socket, sqlite3
from sqlalchemy import create_engine
for action in (
    lambda: socket.getaddrinfo("example.invalid", 443),
    lambda: socket.socket().connect(("192.0.2.1", 443)),
    lambda: socket.socket().bind(("0.0.0.0", 0)),
    lambda: socket.socket(type=socket.SOCK_DGRAM).sendto(b"test", ("192.0.2.1", 443)),
    lambda: sqlite3.connect(":memory:"),
    lambda: sqlite3.connect(target.parent / "outside.sqlite"),
    lambda: create_engine("sqlite:///:memory:").connect(),
    lambda: create_engine("mysql+pymysql://test:test@127.0.0.1/test").connect(),
):
    try:
        action()
    except (b.BootstrapError, ImportError):
        pass
    else:
        raise AssertionError("external access accepted")
assert not (target.parent / "outside.sqlite").exists()
with socket.socket() as server:
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    with socket.create_connection(server.getsockname()) as client:
        connection, _ = server.accept()
        connection.close()
with sqlite3.connect(target / "workflow.sqlite") as db:
    for sql in ("ATTACH DATABASE ':memory:' AS external", "PRAGMA user_version=5"):
        try:
            db.execute(sql)
        except sqlite3.DatabaseError:
            pass
        else:
            raise AssertionError("attach/identity mutation accepted")
''')


@pytest.mark.parametrize("mutation", ["model", "mapbox", "missing_mapbox", "unknown"])
def test_reentry_detects_environment_mutation(tmp_path, mutation):
    target = initialized(tmp_path)
    worker(target, f'''
b.isolate_process(target)
if {mutation!r} == "model":
    os.environ["OPENAI_API_KEY"] = "changed"
elif {mutation!r} == "mapbox":
    os.environ["MAPBOX_API_KEY"] = "changed"
elif {mutation!r} == "missing_mapbox":
    os.environ.pop("MAPBOX_API_KEY", None)
else:
    os.environ["UNEXPECTED_RUNTIME_VARIABLE"] = "changed"
try:
    b.isolate_process(target)
except b.BootstrapError:
    pass
else:
    raise AssertionError("changed environment accepted")
''')


def test_server_preparation_then_real_ui_rerun_preserves_environment(tmp_path):
    """AppTest 本身不執行伺服器啟動準備；兩個階段都必須驗收。"""
    target = tmp_path.resolve() / "private"
    worker(target, '''
b.initialize_directory(target)
os.chdir(target)
before = dict(os.environ)
from streamlit.web.bootstrap import prepare_streamlit_environment
prepare_streamlit_environment(str(root / "acceptance/ui.py"))
assert dict(os.environ) == before, "server preparation mutated isolated environment"
assert os.environ["MAPBOX_API_KEY"] == ""
assert b.isolate_process(target) == target
from streamlit.testing.v1 import AppTest
app = AppTest.from_file(str(root / "acceptance/ui.py"))
for _ in range(2):
    app.run(timeout=15)
    assert not app.exception, str(app.exception)
    assert not app.error, str(app.error)
    assert app.button(key="create").label == "產生四張成本明細"
    assert not app.checkbox
    assert dict(os.environ) == before
    assert b.isolate_process(target) == target
''')


def test_actual_streamlit_reruns_and_core_connection(tmp_path):
    target = tmp_path.resolve() / "private # 驗收 ? directory"
    worker(target, '''
b.initialize_directory(target)
from database import connection
with connection.engine.connect() as db:
    assert db.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    assert db.exec_driver_sql("PRAGMA database_list").fetchone()[2] == str(target / "master.sqlite")
from streamlit.testing.v1 import AppTest
source = "import os\\nfrom acceptance.bootstrap import isolate_process\\n" \\
         "isolate_process(os.environ['FOUR_QUOTE_DIRECTORY'])\\n" \\
         "import streamlit as st\\nst.write('isolated')\\n"
app = AppTest.from_string(source)
app.run()
assert not app.exception, str(app.exception)
app.run()
assert not app.exception, str(app.exception)
assert b.verify_directory(target) == target
''')


def test_launcher_worker_isolates_before_streamlit(tmp_path):
    target = tmp_path.resolve() / "fresh"
    worker(target, '''
import four_quote_acceptance as launcher
def streamlit(name, run_name):
    assert name == "streamlit" and run_name == "__main__"
    assert os.getcwd() == str(target)
    assert os.environ["FOUR_QUOTE_DIRECTORY"] == str(target)
    assert os.environ["HOME"] == str(target)
    assert "config" in sys.modules and not sys.modules["config"].USE_LLM
    assert sys.argv[sys.argv.index("--server.address") + 1] == "127.0.0.1"
    assert sys.argv[2] == str(root / "acceptance/ui.py")
    assert sys.argv[-3:] == ["--", "--directory", str(target)]
    b.isolate_process(target)
launcher.runpy.run_module = streamlit
assert launcher.main(["--worker", "--initialize", "--directory", str(target)]) == 0
''')


def test_launcher_clean_subprocess_contract(tmp_path):
    worker(tmp_path.resolve() / "fresh", '''
import four_quote_acceptance as launcher
os.environ["UNKNOWN_SECRET"] = "sensitive-canary"
def run(command, **kwargs):
    assert command[0] == str(root / "venv/bin/python")
    assert command[1:3] == ["-I", "-B"]
    assert "--worker" in command and "--initialize" in command
    assert "UNKNOWN_SECRET" not in kwargs["env"]
    assert kwargs["env"]["OPENAI_API_KEY"] == ""
    assert kwargs["shell"] is False and kwargs["close_fds"] is True
    assert "config" not in sys.modules
    from types import SimpleNamespace
    return SimpleNamespace(returncode=0)
launcher.subprocess.run = run
assert launcher.main(["--initialize", "--directory", str(target)]) == 0
''')


def test_real_launcher_invalid_directory_stays_clean(tmp_path):
    target = tmp_path.resolve() / "existing"
    target.mkdir(mode=0o700)
    result = subprocess.run([str(PYTHON), "four_quote_acceptance.py", "--directory", str(target)],
                            cwd=ROOT, env={"OPENAI_API_KEY": "sensitive-canary"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "sensitive-canary" not in result.stdout + result.stderr
    assert not list(target.iterdir())


@pytest.mark.parametrize("initialize", [True, False], ids=["new-directory", "reopen-directory"])
def test_real_streamlit_server_startup(tmp_path, initialize):
    """真正伺服器及 WebSocket 觸發實際 UI，不能只測 health。"""
    import socket
    from urllib.request import urlopen
    from urllib.error import URLError
    from websockets.sync.client import connect
    from streamlit.proto.BackMsg_pb2 import BackMsg
    from streamlit.proto.ForwardMsg_pb2 import ForwardMsg

    target = tmp_path.resolve() / "managed"
    before = None
    if not initialize:
        worker(target, "b.initialize_directory(target)")
        before = {p.name: (p.stat().st_ino, p.read_bytes()) for p in target.iterdir()}
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    source = PRELUDE + textwrap.dedent(f'''
import four_quote_acceptance as launcher
original_run = launcher.runpy.run_module
def run(name, run_name):
    position = sys.argv.index("--")
    sys.argv[position:position] = ["--server.port", {str(port)!r}]
    return original_run(name, run_name=run_name)
launcher.runpy.run_module = run
args = ["--worker", "--directory", str(target)]
if {initialize!r}:
    args.append("--initialize")
raise SystemExit(launcher.main(args))
''')
    process = subprocess.Popen([str(PYTHON), "-I", "-B", "-c", source, str(ROOT), str(target)],
                               cwd=ROOT, env={}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    healthy = False
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=0.5) as response:
                    healthy = response.status == 200
                    break
            except (URLError, TimeoutError, ConnectionError):
                time.sleep(0.05)
        assert healthy, "Streamlit health did not become ready"
        with connect(f"ws://127.0.0.1:{port}/_stcore/stream", subprotocols=["streamlit"],
                     origin=f"http://127.0.0.1:{port}", proxy=None,
                     open_timeout=10, close_timeout=3) as websocket:
            for _ in range(2):
                request = BackMsg()
                request.rerun_script.SetInParent()
                websocket.send(request.SerializeToString())
                elements = []
                deadline = time.monotonic() + 20
                while True:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0, "UI did not finish rendering"
                    message = ForwardMsg()
                    message.ParseFromString(websocket.recv(timeout=remaining))
                    if message.HasField("delta"):
                        elements.append(str(message.delta))
                    if message.HasField("script_finished"):
                        break
                rendered = "\n".join(elements)
                assert "拒絕啟動" not in rendered, rendered
                assert "產生四張成本明細" in rendered, rendered
                assert "明確載入合成主檔／初始化或續作" not in rendered, rendered
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
    assert healthy, stdout + stderr
    assert "private setting/log access attempted" not in stdout + stderr
    worker(target, "assert b.verify_directory(target) == target")
    if before is not None:
        for name, original in before.items():
            assert ((target / name).stat().st_ino, (target / name).read_bytes()) == original
