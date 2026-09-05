@echo off
:: ============================================================
:: launcher.bat - AI 報價助理系統啟動橋接批次檔
:: 由 launcher.vbs 呼叫，使用者請勿直接雙擊此檔案
:: （若直接執行會顯示 CMD 視窗）
:: ============================================================

:: 切換至批次檔所在目錄，確保路徑正確
cd /d "%~dp0"

:: ── 虛擬環境偵測（優先使用 .venv，其次 venv）──────────────
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
    goto :run
)
if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
    goto :run
)

:: 未找到虛擬環境，使用系統 Python
:run
python launcher.py

:: 若發生錯誤（如 python 指令不存在），顯示提示
if %ERRORLEVEL% neq 0 (
    echo.
    echo [錯誤] 啟動失敗，請確認 Python 環境是否正確安裝。
    echo 按任意鍵關閉...
    pause >nul
)
