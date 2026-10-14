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
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
           if k in os.environ}
    env.update(MAPBOX_API_KEY="", PYTHON_DOTENV_DISABLED="1")
    return env


def launch(*, worker=False):
    if not worker:
        command = [str(ROOT / "venv/bin/python"), "-I", "-B", "-c",
                   "import sys; sys.path.insert(0, sys.argv.pop(1)); "
                   "from four_quote_acceptance import main; raise SystemExit(main())",
                   str(ROOT), "--local-db", "--worker"]
        return subprocess.run(command, env=clean_environment(), cwd=ROOT,
                              stdin=subprocess.DEVNULL, close_fds=True, shell=False).returncode
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
    except Exception:
        # 不輸出 driver exception（可能包含連線、SQL 或參數）。
        print("本機模式拒絕啟動：請檢查本機連線、工作組及既有七表結構；不會自動建表。", file=sys.stderr)
        return 2
    with TemporaryDirectory(prefix="local-quote-ui-") as directory:
        # 不讀專案或使用者 Streamlit 設定，也不把客戶內容落到暫存目錄。
        os.environ["HOME"] = directory
        os.chdir(directory)
        sys.argv = ["streamlit", "run", str(ROOT / "local_quote/dynamic_ui.py"),
                    "--server.address", "127.0.0.1", "--server.headless", "true",
                    "--browser.gatherUsageStats", "false", "--server.fileWatcherType", "none"]
        runpy.run_module("streamlit", run_name="__main__")
    return 0
