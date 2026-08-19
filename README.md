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

> 我要 20 張 1200×600 的桌子，桌面用美耐板白色，木腳。

系統會自動完成以下步驟：

1. 理解客戶需求
2. 找到對應產品
3. 找出產品所需部件
4. 找出各部件可選規格
5. 將自然語言轉成正式代碼（例如「美耐板」→ `CLMT`）
6. 找出缺少的必要條件並主動詢問
7. 依 `ordqty` 等資料計算部件需求量
8. 呼叫報價引擎計算成本與售價
9. 顯示報價預覽
10. 使用者確認後產生正式報價（寫入 `ordqdt` 快照）
11. 未來可由報價單轉成正式訂單

**核心原則：AI 負責理解需求、查詢資料、提出選項與確認；正式的產品組合、用量、價格與報價金額由程式與報價引擎決定，不由 LLM 自行猜測。**

---

## 系統特色

- **AI 與計價邏輯完全分離**：LLM 不得自行計算任何金額，所有價格由 [`engine/calculator.py`](engine/calculator.py) 依資料庫正式資料計算。
- **AI 不直接操作資料庫**：透過受控的 Business API / Tool 存取資料，可控制權限、商業規則與稽核。
- **自然語言轉正式代碼**：例如「白色 / 美耐板 / 木腳 / 1200x600」會經由 `search_option()` 查詢對映為正式代碼，避免直接把文字寫入報價。
- **報價快照可追溯**：正式報價建立時將當下成本、用量、規格寫入 `ordqdt`，即使日後主檔異動，歷史報價金額與規格仍維持原始內容。
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
              • 缺項檢查 (Missing Field Check)
              • Function Calling / Tool 派發
                            │
             ┌──────────────┴───────────────┐
             ▼                              ▼
      資料服務層 (database/)          報價計算引擎 (engine/)
      • search_product()             • calculate_cost()
      • search_option()              • calculate_price()
      • get_part_qty()               • build_qdt_snapshot()
             └──────────────┬───────────────┘
                            ▼
                  本機資料庫 (Local Database)
      ordspd (選項定義) ─ ordspe (可選項目) ─ ordqty (部件用量)
                            │
                            ▼
                  ordqdt (報價規格快照)
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
- **資料庫**：MSSQL（可對接既有 ERP 資料表）
- **語言**：Python

---

## 專案結構

```text
ai_quote_assistant/
│
├── README.md
├── requirements.txt              # 套件依賴清單
├── .env                          # 環境變數（API Key、DB 連線字串）
├── app.py                        # Streamlit 主程式入口
├── config.py                     # 系統參數與環境設定
│
├── agent/                        # AI Agent 核心模組
│   ├── __init__.py
│   ├── core.py                   # LangChain / OpenAI Agent 流程與 Prompt
│   ├── tools.py                  # Agent Tools 定義（Function Calling）
│   └── state.py                  # Agent 狀態機定義
│
├── engine/                       # 商業邏輯與計價引擎（非 AI 計算）
│   ├── __init__.py
│   ├── calculator.py             # 成本／售價計算公式
│   └── snapshot.py               # 生成 ordqdt 快照邏輯
│
├── database/                     # 資料庫存取層（ORM / SQL）
│   ├── __init__.py
│   ├── connection.py             # SQLAlchemy 資料庫連線管理
│   ├── models.py                 # ordspd、ordspe、ordqty、ordqdt 模型
│   └── repository.py             # 查詢產品、部件、代碼對映之 SQL 實作
│
└── utils/                        # 輔助工具
    ├── logger.py                 # 本地 ai_quote_log 稽核日誌
    └── helpers.py                # 格式化輸出、數值轉換
```

---

## 資料表設計

| 資料表 | 用途 | 核心角色 |
|---|---|---|
| `ordspd` | 選項定義檔 | 定義「有哪些選項類別」 |
| `ordspe` | 可選項目檔 | 定義「某選項底下有哪些項目、採購成本」 |
| `ordqty` | 產品部位用量檔 | 定義「某項目／部位需要多少用量」 |
| `ordqdt` | 報價規格明細檔（建議新增） | 記錄「本次報價實際選了什麼」（歷史快照） |

資料流：

```text
ordspd (選項定義) ─┐
                   ├─→ ordqty (部件用量規則) ─→ ordqdt (報價選擇快照) ─→ 報價/訂單
ordspe (可選項目) ─┘
```

### 關鍵設計：主檔 vs. 快照

- `ordspe.compri` = **目前主檔採購成本**
- `ordqdt.compri` = **建立該報價當時的成本快照**

正式報價確認後，成本／用量／規格／單價／金額都完整保存於 `ordqdt`，避免主檔異動影響歷史報價。

> 詳細欄位定義、Primary Key、索引與 SQL 範例請參考 [`docs/報價訂單資料庫結構與關聯設計.md`](docs/報價訂單資料庫結構與關聯設計.md)。

---

## 報價流程與 Agent 狀態機

```text
NEW
 │ 使用者輸入需求
 ▼
ANALYZING（Agent 解析需求）
 ▼
CHECKING（檢查必要欄位）
 ├── 缺少必要欄位 ─► WAITING_FOR_INPUT ─► （使用者補充）─┐
 └── 資料完整 ─────────────────────────────────────────┘
 ▼
PREVIEW（報價草稿試算）
 │ 使用者確認
 ▼
CONFIRMED
 │ 建立快照
 ▼
SNAPSHOT_CREATED（寫入 ordqdt）
```

| 狀態 | 說明 |
|---|---|
| `NEW` | 尚未建立報價 |
| `ANALYZING` | Agent 正在分析需求 |
| `CHECKING` | 檢查必要欄位與資料 |
| `WAITING_FOR_INPUT` | 等待使用者補充資訊 |
| `PREVIEW` | 報價草稿已建立，可試算 |
| `CONFIRMED` | 使用者已確認正式報價 |
| `SNAPSHOT_CREATED` | 已成功建立 `ordqdt` 快照 |

### AI Agent Tools（第一版）

| Tool | 用途 |
|---|---|
| `search_product` | 依名稱或關鍵字搜尋產品 |
| `get_product_parts` | 取得產品包含哪些部件 |
| `search_option` | 將自然語言轉成正式規格代碼 |
| `get_part_quantity` | 取得部件標準用量 |
| `calculate_quote` | 透過報價引擎計算價格 |
| `preview_quote` | 產生尚未正式建立的報價預覽 |
| `create_quote` | 使用者確認後建立正式報價 |

---

## 計價引擎

所有金額計算皆由計價引擎負責，AI 不得介入。

部件成本公式：

```text
部件成本 = 數量 × 標準用量(stdqty) × 採購成本(compri)
```

報價計算流程：

```text
材料成本 + 人工 + 管理費 + 利潤 - 折扣 + 稅 = 最終報價
```

範例：

```text
桌面：20 張 × 標準用量 1 × 採購成本 500 = 10,000
木腳：20 張 × 標準用量 4 × 採購成本 120 =  9,600
總成本 = 19,600
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
2. 建立四張資料表（`ordspd` / `ordspe` / `ordqty` / `ordqdt`）
3. 灌入辦公桌範例假資料

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

**步驟 2：設定 `.env` 的 `DB_CONN_STR`**

常見連線字串格式：

| 情境 | 連線字串 |
|---|---|
| 本機 SQL Express（Windows 驗證） | `mssql+pyodbc://@localhost\SQLEXPRESS/QuoteDB?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes` |
| 本機（SQL Server 驗證） | `mssql+pyodbc://sa:password@localhost/QuoteDB?driver=ODBC+Driver+17+for+SQL+Server` |
| 遠端伺服器 | `mssql+pyodbc://user:password@192.168.1.100/QuoteDB?driver=ODBC+Driver+17+for+SQL+Server` |

**步驟 3：首次啟動自動建立資料表**

切換到 MSSQL 後，執行 `streamlit run app.py`，系統會自動於資料庫建立所有資料表（`CREATE TABLE IF NOT EXISTS`）。

> **注意**：MSSQL 模式不會自動灌入範例假資料，請手動匯入或連接既有 ERP 資料。

---

## AI 模式切換

| 情境 | 設定方式 |
|---|---|
| 規則式（預設，無需 API） | `.env` 不設定 `OPENAI_API_KEY` |
| LLM 模式（OpenAI） | `.env` 設定 `OPENAI_API_KEY=sk-...` |
| 其他相容 LLM（Ollama 等） | 設定相容 OpenAI API 的端點 |

**規則式模式**支援的解析能力（無需網路）：

| 需求類型 | 範例輸入 | 辨識結果 |
|---|---|---|
| 數量 | 「20張」「100個」 | qty = 20 |
| 桌面尺寸 | 「1200x600」「1500×600」 | SIZE code |
| 材質 | 「美耐板」「實木」「玻璃」 | MATS code |
| 顏色 | 「白色」「黑色」「胡桃」 | COLOR code |
| 腳架 | 「木腳」「金屬腳」「鋼管腳」 | LEG code |
| 確認建立 | 「確認」「是」「建立報價」 | → create_quote |

---

## 設計原則

1. **AI 與計價邏輯分離**：AI 不自行判斷成本／售價，Prompt 中不硬編碼價格。
2. **報價必須具備可追溯性**：正式報價建立後寫入 `ordqdt` 快照，歷史報價不受主檔異動影響。
3. **UI / Agent / Engine / Database 解耦**：方便未來替換前端或 API 化。
4. **AI 不直接執行 SQL**：透過受控 Tool / API 存取，確保權限與稽核。
5. **報價與訂單分開**：報價確認後才轉為訂單，不將報價資料直接當作訂單資料。

---

## 開發路線圖

### MVP 第一版

自然語言輸入 → 搜尋產品／部件／規格 → 選擇產品組合 → 詢問缺少資訊 → 計算報價 → 顯示預覽 → 使用者確認 → 建立報價。

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
→ calculator.py 計價 → 報價 Preview 與 ordqdt Snapshot
→ 稽核日誌 / 報價輸出 / 權限與版本管理
```

---

## 待確認事項

進入實際程式開發前，仍需確認以下項目（詳見設計文件）：

- `ordspd` / `ordspe` / `ordqty` 之間是否存在正式 Foreign Key
- `path` 的實際階層解析規則、`part_path` 如何指向產品部件
- `stdqty` 與 `stdpar` 的實際計算公式、`compri` 的單位與幣別
- 報價加成率 / 折扣率 / 稅率來源
- 報價主檔、報價明細、訂單主檔、訂單明細的既有結構（如 `ord940`、`ord920`）
- `ref_no` 報價單號的編號產生規則、客戶資料表與使用者權限規則

---

## 相關文件

- [`docs/AI報價助理系統_開發設計規格.md`](docs/AI報價助理系統_開發設計規格.md) — 系統目標、核心原則、Tool 設計、報價流程
- [`docs/AI 報價助理系統－全 Python（Streamlit）本機架構設計書.md`](docs/AI%20報價助理系統－全%20Python（Streamlit）本機架構設計書.md) — 全 Python 架構、目錄、模組職責、啟動方式
- [`docs/報價訂單資料庫結構與關聯設計.md`](docs/報價訂單資料庫結構與關聯設計.md) — 資料表結構、關聯、SQL 與快照設計

---

## 授權

本專案採用 [`LICENSE`](LICENSE) 所載授權條款。
