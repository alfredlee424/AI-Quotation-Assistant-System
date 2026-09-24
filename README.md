# AI 報價助理系統（AI Quotation Assistant System）

> 讓業務人員用自然語言描述客戶需求，由 AI 理解需求、查詢產品規格並轉換成正式代碼，最後交由**報價引擎**計算成本與售價，產生可追溯的正式報價。

本專案採用 **全 Python + Streamlit 的本機模組化架構**，核心設計原則為：

> **AI 負責理解需求，Database 負責提供資料，Engine 負責計算價格，Snapshot 負責保存當下報價狀態。**

---

> **目前流程已更新為固定版本報價。** LLM 先提出需求變更，程式依完整路徑與合法分支驗證，成本除以裁切量；成功預覽後由 UI 人工確認，保存同一版本，不在確認時重新查價。部署與相容性請先閱讀 [固定版本報價流程](docs/固定版本報價流程.md)。

> **新增生產工單核對工作區。** 可貼上多張工單、核對明細候選及人工修正分類／拆單邊界；目前不接正式報價、不換算台尺，也不覆蓋既有單品草稿。操作及限制請閱讀[生產工單核對操作說明](docs/生產工單核對操作說明.md)。

> **新增多明細配置草稿工作區。** 可轉接單張工單或複製單品，分別管理每筆產品與合法選配，支援封存、恢復及排序；目前不做整單計價或正式保存。詳見[多明細配置草稿操作說明](docs/多明細配置草稿操作說明.md)。

> **多明細配置支援 AI 提案核對。** 先指定可修改明細，AI 提案經整批驗證後由人工套用；來源阻擋仍保留，不產生正式報價。詳見[多明細 AI 配置提案操作說明](docs/多明細AI配置提案操作說明.md)。

> **新增來源需求帳本與人工問題核對。** 可拆分原文、對照選配及記錄結案依據；相關配置變更後重新核對，不代表正式工程核准。詳見[來源需求帳本操作說明](docs/來源需求帳本與問題核對操作說明.md)。

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
    - [AI Agent Tools（目前版本）](#ai-agent-tools目前版本)
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

1. LLM 接收本回合需求、目前完整路徑草稿與資料庫合法選單，提出新增／修改／移除提案。
2. 程式原子驗證產品、完整路徑、規格代碼、產品數量與每件配件數量。
3. 共用解析器啟用合法分支、補齊必要結構，歧義或缺項先詢問，不生成預覽。
4. 材料单價使用自身規格代碼；用量依用量表指定的驅動部位及尺寸代碼查詢。
5. 成本＝產品數量 × 每件項目數量 × 用料量 ÷ 裁切量 × 採購單價。
6. 程式固定選配、成本、加成、折扣、稅與公式版本，產生版本化預覽。
7. UI 兩次人工確認同一預覽 ID，保存相同版本的明細及完整文件；不重新套用主檔。

**LLM 不得提供價格或材料用量，也不能直接建立報價。未知產品、缺失用量、無效分母與未釐清問題都會阻止預覽。**

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
               • 產品類別與結構樹載入 (invdoc / ordstr)
               • 缺項檢查 (Missing Node Check, 依 ordstr.must_chose)
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
       invdoc ─ ordstr ─ ordspd ─ ordspe ─ ordqty
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
| `invdoc` | 產品類別主檔 | 定義「有哪些可報價的產品類別（ordkind=1）、報價率（quo_rate）」 |
| `ordstr` | 產品結構／必選項目樹 | 以父子鄰接邊（pathf→pathc）定義結構樹與必選項目（must_chose） |
| `ordspd` | 選項定義檔 | 定義「有哪些選項類別」（如 A001 尺寸、S002 材質、B001 腳架） |
| `ordspe` | 可選項目檔 | 定義「某選項底下有哪些項目、採購成本（compri）」 |
| `ordqty` | 產品部位用量檔 | 定義「某項目／部位需要多少標準用量（stdqty）」；`codsc` 欄位在真實 DB 為 NULL |
| `ordqdt_ai` | 報價規格明細檔（建議新增） | 記錄「本次報價實際選了什麼」（歷史快照） |

資料流：

```text
invdoc (產品類別、prodkind、quo_rate)
  └─→ ordstr (結構樹、must_chose、seq)
        └─→ ordspd (選項定義)
              └─→ ordspe (可選項目，含 compri 採購成本)
        └─→ ordqty (部件用量 stdqty，依 path 對應)
              └─→ calculate_from_draft()
                    └─→ ordqdt_ai (報價選擇快照)
```

### Path 路徑階層設計

產品路徑通常為 `{prodkind}\{optno}`，例如：

```text
CMT1\A001    ← 尺寸選項類別（在 ordspd 定義）
CMT1\S002    ← 桌面材質選項類別
CMT1\B001    ← 腳架選項類別
CMT1\S005    ← 工費選項類別
```

`ordspe` 的每個項目（如 `60*120 桌面`、`美耐板`）都掛在對應的 `path` 下。`ordqty` 依相同 `path` 記錄標準用量（`stdqty`）。

> **注意**：正式流程的前綴由 `invdoc.prodkind` 取得；若 `invdoc` 不可用，才使用 [`config.py`](config.py) 的 `PRODUCT_PREFIX` fallback。

### 關鍵設計：主檔 vs. 快照

- `ordspe.compri` = **目前主檔採購成本**
- `ordqdt_ai.compri` = **建立該報價當時的成本快照**

正式報價確認後，成本／用量／規格／單價／金額都完整保存於 `ordqdt_ai`，避免主檔異動影響歷史報價。

> 詳細欄位定義、Primary Key、索引與 SQL 範例請參考 [`docs/報價訂單資料庫結構與關聯設計.md`](docs/報價訂單資料庫結構與關聯設計.md)。

---

## 報價流程與 Agent 狀態機

理解需求／提案 → 完整路徑驗證 → 啟用分支 → 查用量與單價 → 成本試算 → 固定預覽 → 人工確認 → 同版快照。

- 修改、提案格式錯誤或查詢失敗會使舊預覽失效。
- 有缺項／問題時等待補充，不能僅靠 LLM 文字回覆設為預覽完成。
- 確认聊天訊息僅提示使用 UI 按鈕；LLM 不寫入報價。
- 已建立報價不可就地修改，須開啟新草稿。

### AI Agent Tools（目前版本）

LLM 僅使用 [`PROPOSAL_TOOL`](agent/core.py:30) 提出變更。查目錄、套用變更、配置解析與預覽皆由程式協調。
原先公開的搜尋／試算工具仍保留程式 API，但不再作為 LLM 可任意執行的流程。

---

## 計價引擎

**明細成本＝產品數量 × 每件項目數量 × 用料量 ÷ 裁切量 × 採購單價。**

由產品主檔取得報價係數，加成後套用折扣與稅。不同路徑同名選項分開保存，不再只計葉節點，零價結構節點亦進入快照。

- 60×180 尺寸是 C005；桌面立水 4.5 ÷ 24 × 700＝131.25。
- 三張桌每張兩個圓孔：3 × 2 × 350＝2,100。
- 缺尺寸用量規則不能回退為 1／1；只有明列按件政策且整個路徑沒有用量表時允許按件計價。
- 歷史訂單樣本 QU26821001-01001 按快照算為 4,009.08，與現行主檔新報價不同，不可混用。

舊種子資料只是展示，不是完整正式 BOM，也不再以原 8,513 驗算式作為正確性依據。

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
2. 建立本機報價資料表（包含 `invdoc` / `ordstr` / `ordspd` / `ordspe` / `ordqty` / `ordqdt_ai`；實際以目前 migration／seed 定義為準）
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
| 尺寸（A001） | 「60*120」「75*180」 | 查詢 `CMT1\A001` 根節點下的 `ordspe` |
| 板材（S004） | 「MDF」「夾板」 | 查詢 `CMT1\A001\S004` 路徑下的 `ordspe` |
| 色紙（S001） | 「胡桃」「白橡」 | 查詢 `CMT1\A001\S005\S001` 路徑下的 `ordspe` |
| 腳架（B001） | 「45寬」「木腳」「環式腳」 | 查詢 `CMT1\B001` 根節點下的 `ordspe` |
| 確認建立 | 「確認」「是」「建立報價」 | 關鍵字比對 → `create_quote` |

> 選項類別（optno）完全由資料庫 `ordspd` 驅動，不寫死於程式碼。

---

## 設計原則

1. **AI 與計價邏輯分離**：AI 不自行判斷成本／售價，Prompt 中不硬編碼價格。
2. **optno 驅動，不寫死選項**：選擇以完整路徑為唯一鍵，動態渲染，不用 `size/material/color/leg` 等固定欄位。
3. **報價必須具備可追溯性**：正式報價建立後寫入 `ordqdt_ai` 快照，歷史報價不受主檔異動影響。
4. **UI / Agent / Engine / Database 解耦**：方便未來替換前端或 API 化。
5. **AI 不直接執行 SQL**：透過受控 Tool / API 存取，確保權限與稽核。
6. **報價與訂單分開**：報價確認後才轉為訂單，不將報價資料直接當作訂單資料。
7. **`PRODUCT_PREFIX` 對齊真實資料庫**：系統所有路徑查詢皆依 `config.PRODUCT_PREFIX` 組合，切換產品線或環境只需修改此設定。

---

## 開發路線圖

### MVP 第一版

自然語言輸入 → 查 `invdoc` 選產品類別 → 以 `ordstr` 展開結構樹 → 依 `must_chose` 詢問缺項 → 以 `ordspe`／`ordqty` 補成本與用量 → 計算報價（數量 × 用料量 ÷ 裁切量 × 採購單價）→ 顯示預覽 → 使用者確認 → 建立報價。

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

- **必選項目來源**：權威來源為 `ordstr.must_chose='Y'`（透過 [`repository.get_required_nodes()`](database/repository.py) 取得）；當 `ordstr` 尚無資料時，回退至 `config.REQUIRED_OPTNOS`（fallback，目前預設 `["A001"]`）。實際部署時請根據業務規則調整。
- **加成率（報價係數）來源**：權威來源為 `invdoc.quo_rate`（報價係數，直接相乘，例如 1.3；`markup_rate = quo_rate - 1.0`）；當 `invdoc` 無資料時回退至 `config.MARKUP_RATE`（fallback，預設 `1.30`）。稅率 `tax_rate=1.05` 仍暫存於 `config.py`，生產環境建議從 ERP 主檔取得。
- **`ordqty.codsc` 欄位**：真實資料庫中此欄位為 NULL，已在 `models.py` 移除 PK 並設為 `nullable=True`。若未來資料庫補齊此欄位，可視需要重新加入查詢條件。
- **`ref_no` 報價單號編號規則**：目前依 `WORKGROUP` + 日期 + 流水號產生；如 ERP 有其他規則請調整 [`engine/snapshot.py`](engine/snapshot.py)。
- **`ordspd` / `ordspe` / `ordqty` 之間是否存在正式 Foreign Key**：目前依賴 `path` 字串關聯，無 DB 層 FK 約束。
- **報價主檔、訂單主檔的既有結構**（如 `ord940`、`ord920`）：訂單轉換功能留待第三階段確認。
- **客戶資料表與使用者權限規則**：MVP 階段未實作，留待第二階段。

---

## 相關文件

- [`docs/AI報價助理系統_開發設計規格.md`](docs/AI報價助理系統_開發設計規格.md) — 系統目標、核心原則、Tool 設計、報價流程
- [`docs/AI 報價助理系統－全 Python（Streamlit）本機架構設計書.md`](docs/AI%20報價助理系統－全%20Python（Streamlit）本機架構設計書.md) — 全 Python 架構、目錄、模組職責、啟動方式
- [`docs/報價訂單資料庫結構與關聯設計.md`](docs/報價訂單資料庫結構與關聯設計.md) — 資料表結構、關聯、SQL 與快照設計（含 `invdoc`、`ordstr`）
- [`docs/改善後報價流程.md`](docs/改善後報價流程.md) — 以 `ordstr` 結構樹展開、`invdoc.quo_rate` 報價係數的改善後報價流程
- [`plans/fix-real-data-model-alignment.md`](plans/fix-real-data-model-alignment.md) — 真實資料模型對齊修正紀錄

---

## 授權

本專案採用 [`LICENSE`](LICENSE) 所載授權條款。
