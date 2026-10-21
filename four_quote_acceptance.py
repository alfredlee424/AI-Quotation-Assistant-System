"""明確分流本機 MySQL／MSSQL 與隔離驗收；不匯入一般應用。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="工單報價：本機逐張主檔或四張私有隔離驗收", allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--directory", help="私有隔離 SQLite 驗收目錄")
    mode.add_argument("--local-db", action="store_true", help="依 .env 連線本機 MySQL／MSSQL；僅 UI 確認後保存")
    parser.add_argument("--initialize", action="store_true", help="只接受全新私有目錄")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.local_db:
        if args.initialize:
            parser.error("本機模式禁止初始化或建表")
        # 在匯入 acceptance 前分流；本機引擎絕不注入隔離驗收。
        from local_quote.bootstrap import launch
        return launch(worker=args.worker)
    from acceptance.bootstrap import (BootstrapError, clean_environment,
                                      initialize_directory, isolate_process, verify_directory)
    try:
        if not args.worker:
            # -I 忽略 PYTHON* 與 user site；固定使用本專案虛擬環境。
            command = [str(ROOT / "venv/bin/python"), "-I", "-B", "-c",
                       "import sys; sys.path.insert(0, sys.argv.pop(1)); "
                       "from four_quote_acceptance import main; raise SystemExit(main())",
                       str(ROOT), "--worker", "--directory", os.path.abspath(args.directory)]
            if args.initialize:
                command.append("--initialize")
            return subprocess.run(command, env=clean_environment(args.directory),
                                  cwd=ROOT, stdin=subprocess.DEVNULL, close_fds=True,
                                  shell=False).returncode
        directory = (initialize_directory(args.directory) if args.initialize
                     else verify_directory(args.directory))
        isolate_process(directory)
        # HOME 及 cwd 都指向專用目錄，不讀使用者／專案 Streamlit 設定。
        os.chdir(directory)
        sys.argv = ["streamlit", "run", str(ROOT / "acceptance/ui.py"),
                    "--server.address", "127.0.0.1", "--server.headless", "true",
                    "--browser.gatherUsageStats", "false", "--server.fileWatcherType", "none",
                    "--", "--directory", str(directory)]
        # 等價 python -m streamlit，但必須先完成本程序隔離。
        runpy.run_module("streamlit", run_name="__main__")
        return 0
    except (BootstrapError, OSError, ValueError):
        print("拒絕啟動：需全新私有目錄，或完整且識別相符的驗收目錄。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
