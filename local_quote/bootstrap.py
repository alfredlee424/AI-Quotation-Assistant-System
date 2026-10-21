"""本機 UI 新程序入口；只讀兩項設定，不執行一般應用初始化。"""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def clean_environment():
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT")
            if k in os.environ}
    env.update(MAPBOX_API_KEY="", PYTHON_DOTENV_DISABLED="1")
    return env


def startup_error(exc):
    """僅格式化此後端主動產生的安全錯誤，不輸出 DBAPI 例外。"""
    from local_quote.backend import ConnectionSettingsError, SchemaError, SettingsError
    if isinstance(exc, SchemaError):
        return "既有資料表不相容：" + str(exc) + "；請交由資料庫管理員核對既有七表，不會自動建表。"
    if isinstance(exc, ConnectionSettingsError):
        return "資料庫連線失敗：" + str(exc)
    if isinstance(exc, SettingsError):
        return "設定或驅動檢查未通過：" + str(exc)
    return "請檢查已安裝的專案套件、本機連線、工作組及既有七表結構；不會自動建表。"


def launch(*, worker=False):
    if not worker:
        # 不退回 PATH 或全域 Python，避免使用未安裝專案依賴的環境。
        python = ROOT / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if not python.is_file():
            print(f"本機模式拒絕啟動：找不到專案 Python：{python}\n"
                  "請在部署電腦的專案根目錄執行 python -m venv venv，"
                  "再使用該環境的 Python 執行 -m pip install -r requirements.txt；"
                  "不要沿用其他電腦或作業系統的 venv。", file=sys.stderr)
            return 2
        command = [str(python), "-I", "-B", "-c",
                   "import sys; sys.path.insert(0, sys.argv.pop(1)); "
                   "from four_quote_acceptance import main; raise SystemExit(main())",
                   str(ROOT), "--local-db", "--worker"]
        try:
            return subprocess.run(command, env=clean_environment(), cwd=ROOT,
                                  stdin=subprocess.DEVNULL, close_fds=True, shell=False).returncode
        except OSError:
            print(f"本機模式拒絕啟動：無法執行專案 Python：{python}\n"
                  "請檢查執行權限，或在這台電腦重新建立 venv 並安裝 requirements.txt。",
                  file=sys.stderr)
            return 2
    # 不允許從一般應用或已被 acceptance 改造的程序切換。
    if any(n in sys.modules for n in ("config", "app", "database.connection", "acceptance.bootstrap")):
        print("本機模式必須使用全新程序。", file=sys.stderr)
        return 2
    env = clean_environment()
    os.environ.clear()
    os.environ.update(env)
    try:
        from local_quote.backend import LocalDatabase, load_settings
        database = LocalDatabase(load_settings(ROOT / ".env"))
        try:
            database.inspect_schema()
        finally:
            database.engine.dispose()
    except Exception as exc:
        # 不輸出 driver exception（可能包含連線、SQL 或參數）。
        try:
            message = startup_error(exc)
        except ImportError:
            message = "無法載入資料庫套件；請用專案 Python 安裝 requirements.txt。"
        print("本機模式拒絕啟動：" + message, file=sys.stderr)
        return 2
    with TemporaryDirectory(prefix="local-quote-ui-") as directory:
        # 不讀專案或使用者 Streamlit 設定，也不把客戶內容落到暫存目錄。
        os.environ["HOME"] = directory
        # Windows 的 expanduser 使用 USERPROFILE，而非 HOME。
        os.environ["USERPROFILE"] = directory
        previous_directory = Path.cwd()
        try:
            os.chdir(directory)
            sys.argv = ["streamlit", "run", str(ROOT / "local_quote/dynamic_ui.py"),
                        "--server.address", "127.0.0.1", "--server.headless", "true",
                        "--browser.gatherUsageStats", "false", "--server.fileWatcherType", "none"]
            runpy.run_module("streamlit", run_name="__main__")
        finally:
            # Windows 無法移除目前工作目錄；正常及異常退出都先離開。
            os.chdir(previous_directory)
    return 0
