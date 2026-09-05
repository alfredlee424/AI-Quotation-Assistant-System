"""
launcher.py - AI 報價助理系統啟動器（Windows）

執行邏輯：
  1. 計算 app.py 的 MD5 hash（版次識別）
  2. 偵測 Port 8501 是否有 Streamlit 在執行
     - 沒開：啟動 Streamlit 背景執行 → 等待 → 開啟瀏覽器
     - 已開且版次相同：直接開瀏覽器
     - 已開但版次不同：終止舊程序 → 重新啟動 → 開啟瀏覽器
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser

# ── 常數設定 ──────────────────────────────────────────────────────────────────
APP_FILE   = "app.py"        # 被監控版次的主程式
STATE_FILE = ".launcher_state"  # 儲存執行狀態（PID + hash）
PORT       = 8501
URL        = f"http://localhost:{PORT}"
STARTUP_WAIT_SEC   = 8   # 等待 Streamlit 啟動的秒數（可視主機速度調整）
STARTUP_RETRY      = 20  # 輪詢次數上限（每 0.5 秒一次）
KILL_WAIT_SEC      = 2   # kill 後等待釋放 port 的秒數


# ── 工具函式 ──────────────────────────────────────────────────────────────────

def hash_file(path: str) -> str:
    """計算檔案的 MD5 hash，作為版次識別碼。"""
    try:
        return hashlib.md5(open(path, "rb").read()).hexdigest()
    except FileNotFoundError:
        return ""


def is_port_open(port: int = PORT, timeout: float = 1.0) -> bool:
    """嘗試連線 localhost:port，判斷服務是否已在執行。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("localhost", port)) == 0


def load_state() -> dict:
    """讀取上次儲存的啟動狀態；若檔案不存在則回傳預設值。"""
    if os.path.exists(STATE_FILE):
        try:
            return json.loads(open(STATE_FILE, "r", encoding="utf-8").read())
        except (json.JSONDecodeError, OSError):
            pass
    return {"pid": None, "hash": ""}


def save_state(pid: int, file_hash: str) -> None:
    """將目前的 PID 與 hash 寫入狀態檔。"""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"pid": pid, "hash": file_hash}, f)


def kill_process(pid: int) -> None:
    """強制終止指定 PID 的程序（Windows taskkill）。"""
    if pid is None:
        return
    # /F 強制終止；/T 連同子程序；靜默輸出
    os.system(f"taskkill /F /T /PID {pid} >nul 2>&1")


def start_streamlit() -> int:
    """
    在背景啟動 Streamlit，不彈出 CMD 黑視窗。
    回傳子程序的 PID。
    """
    CREATE_NO_WINDOW = 0x08000000  # Windows flag，隱藏主控台視窗
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "streamlit", "run", APP_FILE,
            "--server.headless", "true",
            "--server.port", str(PORT),
        ],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        creationflags=CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.pid


def wait_for_streamlit() -> bool:
    """
    輪詢等待 Streamlit 啟動完成（最多 STARTUP_RETRY * 0.5 秒）。
    回傳 True 表示啟動成功；False 表示逾時。
    """
    for _ in range(STARTUP_RETRY):
        if is_port_open():
            return True
        time.sleep(0.5)
    return False


# ── 主邏輯 ────────────────────────────────────────────────────────────────────

def main() -> None:
    # 切換工作目錄至本腳本所在位置，確保相對路徑正確
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    current_hash = hash_file(APP_FILE)

    if not is_port_open():
        # ── 情況 1：Streamlit 尚未啟動 ──
        print("[啟動器] Streamlit 未執行，正在啟動...")
        pid = start_streamlit()
        save_state(pid, current_hash)

        if wait_for_streamlit():
            print(f"[啟動器] Streamlit 啟動成功（PID={pid}），開啟瀏覽器...")
        else:
            print(f"[啟動器] 警告：等待逾時，嘗試直接開啟瀏覽器...")

        webbrowser.open(URL)

    else:
        # ── 情況 2：Streamlit 已在執行 ──
        state = load_state()
        saved_hash = state.get("hash", "")
        saved_pid  = state.get("pid")

        if saved_hash == current_hash:
            # ── 情況 2a：版次相同，直接開瀏覽器 ──
            print("[啟動器] Streamlit 已執行且版次最新，直接開啟瀏覽器...")
            webbrowser.open(URL)

        else:
            # ── 情況 2b：版次不同，重新啟動 ──
            print(f"[啟動器] 偵測到版次更新，重新啟動 Streamlit（舊 PID={saved_pid}）...")
            kill_process(saved_pid)
            time.sleep(KILL_WAIT_SEC)  # 等待 port 釋放

            pid = start_streamlit()
            save_state(pid, current_hash)

            if wait_for_streamlit():
                print(f"[啟動器] 重新啟動成功（PID={pid}），開啟瀏覽器...")
            else:
                print("[啟動器] 警告：等待逾時，嘗試直接開啟瀏覽器...")

            webbrowser.open(URL)


if __name__ == "__main__":
    main()
