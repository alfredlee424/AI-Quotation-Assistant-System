"""
config.py - 系統參數與環境設定

讀取 .env 環境變數，提供全域設定物件。
判斷是否啟用 LLM（偵測到 OPENAI_API_KEY 時自動啟用）。
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 載入 .env（若存在）
load_dotenv(override=False)


# ============================================================
# LLM 設定
# ============================================================

OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ----------------------------------------------------------
# Azure OpenAI（API Key 認證，適合一鍵啟動，無需 az login）
# 設定 AZURE_OPENAI_ENDPOINT 時自動切換至 Azure OpenAI
# ----------------------------------------------------------
AZURE_OPENAI_ENDPOINT: str = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
AZURE_OPENAI_API_VERSION: str = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")

# USE_AZURE：有 endpoint 且有 API Key 時啟用
USE_AZURE: bool = bool(AZURE_OPENAI_ENDPOINT and OPENAI_API_KEY.strip())

# 有 API Key 時自動啟用 LLM（Azure 或標準 OpenAI 皆支援）
USE_LLM: bool = bool(OPENAI_API_KEY.strip())


# ============================================================
# 資料庫設定
# ============================================================

_db_conn_str: str = os.getenv("DB_CONN_STR", "").strip()

# 未設定 DB_CONN_STR 時回退 SQLite（開發用）
if _db_conn_str:
    DB_CONN_STR: str = _db_conn_str
else:
    _sqlite_path = Path(__file__).parent / "dev.db"
    DB_CONN_STR = f"sqlite:///{_sqlite_path}"

IS_SQLITE: bool = DB_CONN_STR.startswith("sqlite")
IS_MYSQL:  bool = DB_CONN_STR.startswith("mysql")
IS_MSSQL:  bool = DB_CONN_STR.startswith("mssql")

# ============================================================
# 事業別
# ============================================================

WORKGROUP: str = os.getenv("WORKGROUP", "001")


# ============================================================
# 產品路徑前綴（真實資料庫的路徑格式為 {PRODUCT_PREFIX}\{optno}）
# 例：CMT1\A001、CMT1\B001
#
# 【fallback】漸進式改善：未來產品類別由 invdoc.prodkind 選定，
# 此值僅作為 invdoc 查無資料時的預設前綴。
# ============================================================

PRODUCT_PREFIX: str = os.getenv("PRODUCT_PREFIX", "CMT1")


# ============================================================
# 產品類別篩選值（invdoc.ordkind）
# ordkind=1 代表可報價的產品類別
# ============================================================

ORDKIND_PRODUCT: str = os.getenv("ORDKIND_PRODUCT", "1")


# ============================================================
# 必填 optno 清單（至少選完這些類別才能進入報價試算）
# 以逗號分隔，例如 "A001,B001"
#
# 【fallback】必選項目權威來源已改為 ordstr.must_chose；
# 此清單僅作為 ordstr 查無資料時的 fallback。
# ============================================================

_required_optnos_env: str = os.getenv("REQUIRED_OPTNOS", "A001")
REQUIRED_OPTNOS: list[str] = [
    s.strip() for s in _required_optnos_env.split(",") if s.strip()
]


# ============================================================
# 報價計算規則
#
# 【fallback】加成率來源已改為 invdoc.quo_rate（報價係數，直接相乘）；
# MARKUP_RATE 僅作為 invdoc.quo_rate 為 NULL 時的 fallback。
# ============================================================

MARKUP_RATE: float = float(os.getenv("MARKUP_RATE", "0.3"))
TAX_RATE: float = float(os.getenv("TAX_RATE", "0.05"))
MAX_DISCOUNT_RATE: float = float(os.getenv("MAX_DISCOUNT_RATE", "0.1"))


# ============================================================
# 稽核日誌
# ============================================================

LOG_FILE: str = os.getenv("LOG_FILE", "logs/ai_quote.log")

# 確保 logs 目錄存在
Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)


# ============================================================
# 診斷輸出（僅開發用）
# ============================================================

def print_config() -> None:
    """列印目前設定（不含機敏資料）"""
    print("=" * 50)
    print("AI 報價助理系統 - 目前設定")
    print("=" * 50)
    print(f"  LLM 啟用   : {USE_LLM} (模型: {OPENAI_MODEL if USE_LLM else 'N/A'})")
    print(f"  資料庫     : {'SQLite (開發模式)' if IS_SQLITE else 'MSSQL (正式)'}")
    print(f"  DB URL     : {DB_CONN_STR if IS_SQLITE else '(已設定，略過顯示)'}")
    print(f"  事業別     : {WORKGROUP}")
    print(f"  加成率     : {MARKUP_RATE:.0%}")
    print(f"  稅率       : {TAX_RATE:.0%}")
    print(f"  最大折扣   : {MAX_DISCOUNT_RATE:.0%}")
    print(f"  日誌路徑   : {LOG_FILE}")
    print("=" * 50)


if __name__ == "__main__":
    print_config()
