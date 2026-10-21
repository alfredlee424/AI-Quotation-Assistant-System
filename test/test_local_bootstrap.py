"""本機入口跨平台路徑與失敗提示；不連真實資料庫。"""
import subprocess
import sys
from unittest.mock import Mock

import pytest

from local_quote import bootstrap


@pytest.mark.parametrize("platform, relative", [
    ("win32", "venv/Scripts/python.exe"),
    ("darwin", "venv/bin/python"),
    ("linux", "venv/bin/python"),
])
def test_project_python_and_clean_subprocess(monkeypatch, tmp_path, platform, relative):
    root = tmp_path / "中文 project with spaces"
    python = root / relative
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("OPENAI_API_KEY", "secret-canary")
    monkeypatch.setenv("DB_CONN_STR", "secret-canary")
    run = Mock(return_value=Mock(returncode=7))
    monkeypatch.setattr(subprocess, "run", run)

    assert bootstrap.launch() == 7
    (command,), kwargs = run.call_args
    assert command[0] == str(python)
    assert command[1:4] == ["-I", "-B", "-c"]
    assert command[5:] == [str(root), "--local-db", "--worker"]
    assert kwargs["cwd"] == root
    assert kwargs["shell"] is False and kwargs["close_fds"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["env"]["MAPBOX_API_KEY"] == ""
    assert not any("secret-canary" in value for value in kwargs["env"].values())


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_missing_environment_does_not_fall_back(monkeypatch, tmp_path, capsys, platform):
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "platform", platform)
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    assert bootstrap.launch() == 2
    run.assert_not_called()
    error = capsys.readouterr().err
    assert "找不到專案 Python" in error and "python -m venv venv" in error
    assert "requirements.txt" in error


@pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError(), OSError()])
def test_unusable_environment_has_actionable_error(monkeypatch, tmp_path, capsys, error):
    python = tmp_path / "venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=error))
    assert bootstrap.launch() == 2
    assert "無法執行專案 Python" in capsys.readouterr().err


def test_windows_runtime_environment_preserves_only_allowed_values(monkeypatch):
    values = {"SYSTEMROOT": "C:\\Windows", "TEMP": "C:\\Temp", "TMP": "C:\\Temp"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for key in ("USERPROFILE", "HOME", "PYTHONPATH", "STREAMLIT_SERVER_ADDRESS", "HTTPS_PROXY"):
        monkeypatch.setenv(key, "private-canary")
    env = bootstrap.clean_environment()
    assert all(env[key] == value for key, value in values.items())
    assert "private-canary" not in env.values()


@pytest.mark.parametrize("raises", [False, True])
def test_worker_home_isolation_and_cleanup_in_fresh_process(raises):
    code = '''
import os, sys, socket
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from local_quote import bootstrap as b
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
database = SimpleNamespace(inspect_schema=lambda: None, engine=SimpleNamespace(dispose=lambda: None))
sys.modules["local_quote.backend"] = SimpleNamespace(
    LocalDatabase=lambda settings: database, load_settings=lambda path: None)
original = Path.cwd()
seen = []
raises = sys.argv[2] == "True"
def streamlit(name, run_name):
    directory = Path.cwd()
    seen.append(directory)
    assert name == "streamlit" and run_name == "__main__"
    assert directory == Path(os.environ["HOME"]).resolve() == Path(os.environ["USERPROFILE"]).resolve()
    assert directory == Path.home().resolve()
    assert sys.argv[sys.argv.index("--server.address") + 1] == "127.0.0.1"
    if raises:
        raise SystemExit(9)
b.runpy.run_module = streamlit
try:
    assert b.launch(worker=True) == 0
except SystemExit as exc:
    assert raises and exc.code == 9
else:
    assert not raises
assert Path.cwd() == original
assert len(seen) == 1 and not seen[0].exists()
'''
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(bootstrap.ROOT), str(raises)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("kind,expected", [
    ("settings", "設定或驅動檢查未通過"),
    ("schema", "既有資料表不相容"),
    ("driver", "ODBC"),
    ("login", "登入失敗"),
    ("unknown", "請檢查已安裝的專案套件"),
])
def test_worker_startup_diagnostics_disposes_engine_and_never_echoes_driver(kind, expected):
    code = '''
import sys, socket
sys.path.insert(0, sys.argv[1])
from types import SimpleNamespace
from local_quote import bootstrap as b, backend
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
backend.dotenv_values = lambda *a, **k: (_ for _ in ()).throw(AssertionError("private settings"))
backend.load_settings = lambda path: None
kind = sys.argv[2]
errors = {
    "settings": backend.SettingsError("WORKGROUP 必須為 1 至 3 字元"),
    "schema": backend.SchemaError("ordqdt_ai: missing table"),
    "driver": backend._connection_error(RuntimeError("IM002", "secret-canary")),
    "login": backend._connection_error(RuntimeError("28000", "secret-canary")),
    "unknown": RuntimeError("secret-canary-server-password-query"),
}
disposed = []
def inspect():
    raise errors[kind]
backend.LocalDatabase = lambda settings: SimpleNamespace(
    inspect_schema=inspect, engine=SimpleNamespace(dispose=lambda: disposed.append(True)))
b.runpy.run_module = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not start UI"))
assert b.launch(worker=True) == 2
assert disposed == [True]
assert not any(n in sys.modules for n in ("config", "database.connection", "acceptance.bootstrap"))
'''
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(bootstrap.ROOT), kind],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stderr
    assert "secret-canary" not in result.stdout + result.stderr
