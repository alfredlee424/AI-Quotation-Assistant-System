# AI 報價助理系統（AI Quotation Assistant System）

> 讓業務人員用自然語言描述客戶需求，由 AI 理解需求、查詢產品規格並轉換成正式代碼，最後交由**報價引擎**計算成本與售價，產生可追溯的正式報價。

本專案採用 **全 Python + Streamlit 的本機模組化架構**，核心設計原則為：

> **AI 負責理解需求，Database 負責提供資料，Engine 負責計算價格，Snapshot 負責保存當下報價狀態。**

---

## 目錄

- [AI 報價助理系統（AI Quotation Assistant System）](#ai-報價助理系統ai-quotation-assistant-system)
  - [目錄](#目錄)
  - [核心概念](#核心概念)
  - [系統特色](#系統特色)
  - [系統架構](#系統架構)
    - [架構分層](#架構分層)
  - [技術棧](#技術棧)
  - [專案結構](#專案結構)
  - [資料表設計](#資料表設計)
    - [Path 路徑階層設計](#path-路徑階層設計)
    - [關鍵設計：主檔 vs. 快照](#關鍵設計主檔-vs-快照)
  - [報價流程與 Agent 狀態機](#報價流程與-agent-狀態機)
    - [AI Agent Tools（第一版）](#ai-agent-tools第一版)
  - [計價引擎](#計價引擎)
  - [安裝與啟動（Windows）](#安裝與啟動windows)
    - [前置需求](#前置需求)
    - [1. 建立虛擬環境](#1-建立虛擬環境)
    - [2. 安裝套件](#2-安裝套件)
    - [3. 設定環境變數](#3-設定環境變數)
    - [4. 啟動 Streamlit](#4-啟動-streamlit)
  - [資料庫切換（SQLite → MSSQL）](#資料庫切換sqlite--mssql)
    - [開發模式（預設：SQLite，免設定）](#開發模式預設sqlite免設定)
    - [切換至 MSSQL（正式 ERP）](#切換至-mssql正式-erp)
  - [AI 模式切換](#ai-模式切換)
  - [設計原則](#設計原則)
  - [開發路線圖](#開發路線圖)
    - [MVP 第一版](#mvp-第一版)
    - [第二階段](#第二階段)
    - [第三階段](#第三階段)
  - [待確認事項](#待確認事項)
  - [相關文件](#相關文件)
  - [授權](#授權)

---

## 核心概念

業務人員只需輸入自然語言，例如：

> 我要 20 張 60*120 的桌子，桌面用美耐板，木腳。

系統會自動完成以下步驟：

1. 理解客戶需求（數量、尺寸、材質、腳架等）
2. 找到對應產品（依 `PRODUCT_PREFIX` 設定定位，例如 `CMT1`）
3. 以 `optno` 為驅動，找出各選項類別（A001 尺寸、S002 桌面材質、B001 腳架…）
4. 將自然語言轉成正式代碼（例如「美耐板」→ `CLMT`）
5. 找出缺少的必要條件並主動詢問（依 `REQUIRED_OPTNOS` 設定）
6. 依 `ordqty` 資料查詢部件標準用量（`stdqty`）
7. 呼叫報價引擎計算成本與售價：`compri × stdqty × qty`
8. 顯示報價預覽
9. 使用者確認後產生正式報價（寫入 `ordqdt_ai` 快照）
10. 未來可由報價單轉成正式訂單

**核心原則：AI 負責理解需求、查詢資料、提出選項與確認；正式的產品組合、用量、價格與報價金額由程式與報價引擎決定，不由 LLM 自行猜測。**

---

## 系統特色

- **AI 與計價邏輯完全分離**：LLM 不得自行計算任何金額，所有價格由 [`engine/calculator.py`](engine/calculator.py) 依資料庫正式資料計算。
- **AI 不直接操作資料庫**：透過受控的 Business API / Tool 存取資料，可控制權限、商業規則與稽核。
- **optno 驅動動態選項**：選項類別（A001/S002/B001/S005 等）完全來自資料庫 `ordspd`，不寫死於程式碼；新增選項類別無需修改 Agent 邏輯。
- **自然語言轉正式代碼**：例如「60*120 / 美耐板 / 木腳」會查詢對映為正式代碼，避免直接把文字寫入報價。
- **報價快照可追溯**：正式報價建立時將當下成本、用量、規格寫入 `ordqdt_ai`，即使日後主檔異動，歷史報價金額與規格仍維持原始內容。
- **雙區介面**：左側對話互動區（Chat UI）、右側結構化報價試算卡片（Preview）。
- **可擴充**：本機單機版起步，後續可 API 化（FastAPI）、加入權限管理、版本管理與文件輸入。

---

## 系統架構

```text
                          使用者瀏覽器 (http://localhost:8501)
                                       │
                                       ▼
                       Streamlit 應用程式 (app.py)
              ┌──────────────────────────┬──────────────────────────┐
              │ 左側：對話互動區 (Chat)  │ 右側：報價試算卡片 (Preview) │
              └─────────────┬────────────┴─────────────┬────────────┘
                            ▼                          ▼
                       AI Agent 邏輯層 (agent/)
              • 意圖解析 (Intent Parsing)
              • 缺項檢查 (Missing Field Check, 依 REQUIRED_OPTNOS)
              • Function Calling / Tool 派發
                            │
             ┌──────────────┴───────────────┐
             ▼                              ▼
      資料服務層 (database/)          報價計算引擎 (engine/)
      • search_product()             • calculate_from_draft()
      • search_option()              • build_snapshot_items()
      • get_part_quantity()
             └──────────────┬───────────────┘
                            ▼
                  本機資料庫 (Local Database)
      ordspd (選項定義) ─ ordspe (可選項目) ─ ordqty (部件用量)
                            │
                            ▼
                  ordqdt_ai (報價規格快照)
```

### 架構分層

| 層級 | 主要模組 | 主要職責 |
|---|---|---|
| UI 層 | [`app.py`](app.py) | Streamlit 頁面、Chat UI、報價預覽、Session State |
| AI Agent 層 | [`agent/`](agent/) | 意圖解析、欄位檢查、Tool Calling |
| 資料服務層 | [`database/`](database/) | 資料庫查詢、ORM、Repository |
| 計價引擎層 | [`engine/`](engine/) | 成本、售價與報價快照計算 |
| 資料庫層 | Local DB | 儲存產品、選項、部件用量與報價快照 |

依賴方向（單向解耦）：

```text
app.py → agent/ → database/
              └─→ engine/ → database/
```

---

## 技術棧

- **前端 / 應用框架**：Streamlit
- **AI Agent**：LangChain / OpenAI SDK（Function Calling）
- **資料存取**：SQLAlchemy、PyODBC
- **資料庫**：MSSQL（可對接既有 ERP 資料表）/ SQLite（開發模式）
- **語言**：Python 3.10+

---

## 專案結構

```text
ai_quote_assistant/
│
├── README.md
├── requirements.txt              # 套件依賴清單
├── .env                          # 環境變數（API Key、DB 連線字串、產品前綴）
├── .env.example                  # 環境變數範本
├── app.py                        # Streamlit 主程式入口
├── config.py                     # 系統參數（PRODUCT_PREFIX、REQUIRED_OPTNOS 等）
│
├── agent/                        # AI Agent 核心模組
│   ├── __init__.py
│   ├── core.py                   # LangChain / OpenAI Agent 流程與 Prompt
│   ├── tools.py                  # Agent Tools 定義（Function Calling）
│   ├── rule_parser.py            # 規則式解析（optno 驅動，無需 LLM）
│   └── state.py                  # Agent 狀態機定義
│
├── engine/                       # 商業邏輯與計價引擎（非 AI 計算）
│   ├── __init__.py
│   ├── calculator.py             # 成本／售價計算公式（optno 驅動）
│   └── snapshot.py               # 生成 ordqdt_ai 快照邏輯
│
├── database/                     # 資料庫存取層（ORM / SQL）
│   ├── __init__.py
│   ├── connection.py             # SQLAlchemy 資料庫連線管理
│   ├── models.py                 # ordspd、ordspe、ordqty、ordqdt_ai 模型
│   ├── repository.py             # 查詢產品、部件、代碼對映之 SQL 實作
│   └── seed_data.py              # 開發用範例假資料（CMT1 optno 結構）
│
├── docs/                         # 設計文件
│   ├── AI報價助理系統_開發設計規格.md
│   ├── AI 報價助理系統－全 Python（Streamlit）本機架構設計書.md
│   └── 報價訂單資料庫結構與關聯設計.md
│
├── plans/                        # 開發計畫與修正紀錄
│   └── fix-real-data-model-alignment.md
│
├── test/                         # 真實 DB 資料截圖與測試資料
│   ├── ordqty.txt
│   ├── ordspd.txt
│   └── ordspe.txt
│
└── utils/                        # 輔助工具
    ├── logger.py                 # 本地 ai_quote_log 稽核日誌
    └── helpers.py                # 格式化輸出、動態摘要渲染
```

---

## 資料表設計

| 資料表 | 用途 | 核心角色 |
|---|---|---|
| `ordspd` | 選項定義檔 | 定義「有哪些選項類別」（如 A001 尺寸、S002 材質、B001 腳架） |
| `ordspe` | 可選項目檔 | 定義「某選項底下有哪些項目、採購成本（compri）」 |
| `ordqty` | 產品部位用量檔 | 定義「某項目／部位需要多少標準用量（stdqty）」；`codsc` 欄位在真實 DB 為 NULL |
| `ordqdt_ai` | 報價規格明細檔（建議新增） | 記錄「本次報價實際選了什麼」（歷史快照） |

資料流：

```text
ordspd (選項定義)
  └─→ ordspe (可選項目，含 compri 採購成本)
        └─→ ordqty (部件用量 stdqty，依 path 對應)
              └─→ calculate_from_draft()
                    └─→ ordqdt_ai (報價選擇快照)
```

### Path 路徑階層設計

路徑格式為 `{PRODUCT_PREFIX}\{optno}`，例如：

```text
CMT1\A001    ← 尺寸選項類別（在 ordspd 定義）
CMT1\S002    ← 桌面材質選項類別
CMT1\B001    ← 腳架選項類別
CMT1\S005    ← 工費選項類別
```

`ordspe` 的每個項目（如 `60*120 桌面`、`美耐板`）都掛在對應的 `path` 下。`ordqty` 依相同 `path` 記錄標準用量（`stdqty`）。

> **注意**：`PRODUCT_PREFIX` 需與真實資料庫的 `ordspd.path` 前綴一致（見 [`config.py`](config.py)）。

### 關鍵設計：主檔 vs. 快照

- `ordspe.compri` = **目前主檔採購成本**
- `ordqdt_ai.compri` = **建立該報價當時的成本快照**

正式報價確認後，成本／用量／規格／單價／金額都完整保存於 `ordqdt_ai`，避免主檔異動影響歷史報價。

> 詳細欄位定義、Primary Key、索引與 SQL 範例請參考 [`docs/報價訂單資料庫結構與關聯設計.md`](docs/報價訂單資料庫結構與關聯設計.md)。

---

## 報價流程與 Agent 狀態機

```text
NEW
 │ 使用者輸入需求
 ▼
ANALYZING（Agent 解析需求，依 optno 分類選項）
 ▼
CHECKING（依 REQUIRED_OPTNOS 檢查必要欄位）
 ├── 缺少必要欄位 ─► WAITING_FOR_INPUT ─► （使用者補充）─┐
 └── 資料完整 ─────────────────────────────────────────┘
 ▼
PREVIEW（報價草稿試算：compri × stdqty × qty）
 │ 使用者確認
 ▼
CONFIRMED
 │ 建立快照
 ▼
SNAPSHOT_CREATED（寫入 ordqdt_ai）
```

| 狀態 | 說明 |
|---|---|
| `NEW` | 尚未建立報價 |
| `ANALYZING` | Agent 正在分析需求 |
| `CHECKING` | 依 `REQUIRED_OPTNOS` 檢查必要欄位 |
| `WAITING_FOR_INPUT` | 等待使用者補充資訊 |
| `PREVIEW` | 報價草稿已建立，可試算 |
| `CONFIRMED` | 使用者已確認正式報價 |
| `SNAPSHOT_CREATED` | 已成功建立 `ordqdt_ai` 快照 |

### AI Agent Tools（第一版）

| Tool | 用途 |
|---|---|
| `search_product` | 依名稱或關鍵字搜尋產品 |
| `get_product_parts` | 取得產品包含哪些部件（依 `PRODUCT_PREFIX` 路徑） |
| `search_option` | 將自然語言轉成正式規格代碼 |
| `get_part_quantity` | 取得部件標準用量（`stdqty`，依 path 查 ordqty） |
| `calculate_quote` | 透過報價引擎計算價格 |
| `preview_quote` | 產生尚未正式建立的報價預覽 |
| `create_quote` | 使用者確認後建立正式報價 |

---

## 計價引擎

所有金額計算皆由計價引擎負責，AI 不得介入。

部件成本公式：

```text
部件成本 = 數量(qty) × 標準用量(stdqty) × 採購成本(compri)
```

報價計算流程：

```text
Σ 各 optno 部件成本（材料 + 工費）
  × 加成率（markup_rate，預設 1.30）
  × 稅率（tax_rate，預設 1.05）
  = 最終報價金額
```

標準範例（`qty=20`，`60*120` 桌子）：

```text
S002/C001  美耐板桌面：20 × 1.0 × $500 = $10,000
B001/C001  木腳：      20 × 4.0 × $120 =  $9,600
S005/C001  工費：      20 × 1.0 × $150 =  $3,000
           材料小計                      $22,600
           × 加成 30% × 稅 5%           $26,754（最終報價）
```

---

## 安裝與啟動（Windows）

### 前置需求

- Python 3.10 以上（建議 3.11）：https://www.python.org/downloads/
- pip（隨 Python 安裝）

### 1. 建立虛擬環境

```powershell
python -m venv venv
venv\Scripts\activate
```

macOS / Linux：

```bash
python3 -m venv venv
source venv/bin/activate
```

### 2. 安裝套件

```powershell
pip install -r requirements.txt
```

`requirements.txt` 主要依賴：

```text
streamlit>=1.30.0
langchain>=0.1.0
langchain-openai>=0.0.5
openai>=1.0.0
sqlalchemy>=2.0.0
pyodbc>=5.0.0
python-dotenv>=1.0.0
pandas>=2.0.0
```

### 3. 設定環境變數

複製範本並填入設定：

```powershell
copy .env.example .env
```

開啟 `.env` 並根據需求填寫：

```env
# 【選填】設定後自動啟用 LLM 模式，未設定則使用規則式解析
OPENAI_API_KEY=your_key_here

# 【選填】資料庫連線（未設定則使用 SQLite 開發模式）
# DB_CONN_STR=mssql+pyodbc://使用者:密碼@伺服器/資料庫?driver=ODBC+Driver+17+for+SQL+Server

# 【必填，切換 MSSQL 時】產品路徑前綴（需與 ordspd.path 的 prefix 一致）
PRODUCT_PREFIX=CMT1

# 【必填】報價必須填寫的選項類別（逗號分隔的 optno 清單）
REQUIRED_OPTNOS=A001

# 【選填】ERP workgroup（預設 CMT1）
WORKGROUP=CMT1
```

> 請勿將 `.env` 提交至 Git Repository（已加入 [`.gitignore`](.gitignore)）。

### 4. 啟動 Streamlit

```powershell
streamlit run app.py
```

啟動後瀏覽器會自動開啟，或手動前往：`http://localhost:8501`

---

## 資料庫切換（SQLite → MSSQL）

### 開發模式（預設：SQLite，免設定）

啟動時系統自動：
1. 建立 `dev.db`（SQLite 本機資料庫）
2. 建立四張資料表（`ordspd` / `ordspe` / `ordqty` / `ordqdt_ai`）
3. 灌入 CMT1 辦公桌範例假資料（依真實 optno 結構：A001/S002/B001/S005）

若需手動重置假資料：

```bash
rm dev.db
python3 -m database.seed_data
```

### 切換至 MSSQL（正式 ERP）

**步驟 1：安裝 ODBC Driver 17 for SQL Server（Windows）**

前往下載並安裝：

```
https://aka.ms/downloadmsodbcsql
```

安裝後確認驅動名稱（ODBC 資料來源管理員可查詢）：

```
ODBC Driver 17 for SQL Server
```

**步驟 2：設定 `.env`**

```env
DB_CONN_STR=mssql+pyodbc://使用者:密碼@伺服器/資料庫?driver=ODBC+Driver+17+for+SQL+Server

# 必須與真實 ordspd.path 的前綴一致（例如 ordspd.path = 'CMT1\A001' → PRODUCT_PREFIX=CMT1）
PRODUCT_PREFIX=CMT1
WORKGROUP=CMT1
```

常見連線字串格式：

| 情境 | 連線字串 |
|---|---|
| 本機 SQL Express（Windows 驗證） | `mssql+pyodbc://@localhost\SQLEXPRESS/QuoteDB?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes` |
| 本機（SQL Server 驗證） | `mssql+pyodbc://sa:password@localhost/QuoteDB?driver=ODBC+Driver+17+for+SQL+Server` |
| 遠端伺服器 | `mssql+pyodbc://user:password@192.168.1.100/QuoteDB?driver=ODBC+Driver+17+for+SQL+Server` |

**步驟 3：首次啟動自動建立資料表**

切換到 MSSQL 後，執行 `streamlit run app.py`，系統會自動於資料庫建立所有資料表（`CREATE TABLE IF NOT EXISTS`）。

> **注意**：MSSQL 模式不會自動灌入範例假資料，請連接既有 ERP 資料（ordspd / ordspe / ordqty）。
> `PRODUCT_PREFIX` 必須與 ERP 中 `ordspd.path` 的前綴完全一致，否則查詢結果為空。

---

## AI 模式切換

| 情境 | 設定方式 |
|---|---|
| 規則式（預設，無需 API） | `.env` 不設定 `OPENAI_API_KEY` |
| LLM 模式（OpenAI） | `.env` 設定 `OPENAI_API_KEY=sk-...` |
| 其他相容 LLM（Ollama 等） | 設定相容 OpenAI API 的端點 |

**規則式模式**支援的解析能力（無需網路）：

| 需求類型 | 範例輸入 | 辨識方式 |
|---|---|---|
| 數量 | 「20張」「100個」 | 正規表達式萃取，`qty` |
| 尺寸（A001） | 「60*120」「120×60」 | 查詢 `CMT1\A001` 路徑下的 `ordspe` |
| 桌面材質（S002） | 「美耐板」「實木」 | 查詢 `CMT1\S002` 路徑下的 `ordspe` |
| 腳架（B001） | 「木腳」「金屬腳」 | 查詢 `CMT1\B001` 路徑下的 `ordspe` |
| 工費（S005） | （自動帶入預設值） | `_apply_defaults()` 自動填入 |
| 確認建立 | 「確認」「是」「建立報價」 | 關鍵字比對 → `create_quote` |

> 選項類別（optno）完全由資料庫 `ordspd` 驅動，不寫死於程式碼。

---

## 設計原則

1. **AI 與計價邏輯分離**：AI 不自行判斷成本／售價，Prompt 中不硬編碼價格。
2. **optno 驅動，不寫死選項**：`selections` 以 `optno` 為 key，動態渲染，不用 `size/material/color/leg` 等固定欄位。
3. **報價必須具備可追溯性**：正式報價建立後寫入 `ordqdt_ai` 快照，歷史報價不受主檔異動影響。
4. **UI / Agent / Engine / Database 解耦**：方便未來替換前端或 API 化。
5. **AI 不直接執行 SQL**：透過受控 Tool / API 存取，確保權限與稽核。
6. **報價與訂單分開**：報價確認後才轉為訂單，不將報價資料直接當作訂單資料。
7. **`PRODUCT_PREFIX` 對齊真實資料庫**：系統所有路徑查詢皆依 `config.PRODUCT_PREFIX` 組合，切換產品線或環境只需修改此設定。

---

## 開發路線圖

### MVP 第一版

自然語言輸入 → 依 optno 搜尋產品／部件／規格 → 選擇產品組合 → 詢問缺少資訊 → 計算報價（compri × stdqty × qty）→ 顯示預覽 → 使用者確認 → 建立報價。

### 第二階段

- 歷史報價搜尋、複製、以自然語言修改後重新報價
- 使用者與權限管理（業務／審核／管理員）
- 報價版本管理

### 第三階段

- 文件輸入解析（Excel / PDF / Email / Word）
- 報價轉正式訂單
- FastAPI API Layer、報價單輸出（PDF / Excel）、稽核與效能最佳化

建議開發順序：

```text
Streamlit UI → database/ 查詢 → Agent 與 Tool Calling
→ calculator.py 計價 → 報價 Preview 與 ordqdt_ai Snapshot
→ 稽核日誌 / 報價輸出 / 權限與版本管理
```

---

## 待確認事項

以下為尚未完全確認、需視實際 ERP 環境調整的項目：

- **`REQUIRED_OPTNOS` 設定**：目前預設 `["A001"]`（尺寸為必填），其餘 S002/B001 為選填（有預設值）。實際部署時請根據業務規則調整。
- **加成率與稅率來源**：目前 `markup_rate=1.30`、`tax_rate=1.05` 硬編碼於 `config.py`；生產環境建議從 ERP 主檔取得。
- **`ordqty.codsc` 欄位**：真實資料庫中此欄位為 NULL，已在 `models.py` 移除 PK 並設為 `nullable=True`。若未來資料庫補齊此欄位，可視需要重新加入查詢條件。
- **`ref_no` 報價單號編號規則**：目前依 `WORKGROUP` + 日期 + 流水號產生；如 ERP 有其他規則請調整 [`engine/snapshot.py`](engine/snapshot.py)。
- **`ordspd` / `ordspe` / `ordqty` 之間是否存在正式 Foreign Key**：目前依賴 `path` 字串關聯，無 DB 層 FK 約束。
- **報價主檔、訂單主檔的既有結構**（如 `ord940`、`ord920`）：訂單轉換功能留待第三階段確認。
- **客戶資料表與使用者權限規則**：MVP 階段未實作，留待第二階段。

---

## 相關文件

- [`docs/AI報價助理系統_開發設計規格.md`](docs/AI報價助理系統_開發設計規格.md) — 系統目標、核心原則、Tool 設計、報價流程
- [`docs/AI 報價助理系統－全 Python（Streamlit）本機架構設計書.md`](docs/AI%20報價助理系統－全%20Python（Streamlit）本機架構設計書.md) — 全 Python 架構、目錄、模組職責、啟動方式
- [`docs/報價訂單資料庫結構與關聯設計.md`](docs/報價訂單資料庫結構與關聯設計.md) — 資料表結構、關聯、SQL 與快照設計
- [`plans/fix-real-data-model-alignment.md`](plans/fix-real-data-model-alignment.md) — 真實資料模型對齊修正紀錄

---

## 授權

本專案採用 [`LICENSE`](LICENSE) 所載授權條款。
