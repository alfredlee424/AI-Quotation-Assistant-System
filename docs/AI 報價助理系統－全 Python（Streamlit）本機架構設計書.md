# AI 報價助理系統－全 Python（Streamlit）本機架構設計書

> **文件版本**：v1.0  
> **系統類型**：單機部署（Local Deployment）／全 Python 架構  
> **核心技術**：Streamlit + FastAPI + LangChain / OpenAI SDK + SQLAlchemy

---

## 1. 系統整體架構（System Architecture）

採用 **全 Python 模組化單機架構**，前端介面與 Agent 流程統一由 Streamlit 驅動，資料庫與計價邏輯則透過內部 Python 模組獨立管理，確保介面、AI 邏輯與商業計算解耦。

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                使用者瀏覽器 (http://localhost:8501)                    │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    Streamlit 應用程式 (app.py)                         │
│                                                                         │
│  ┌──────────────────────────────┐   ┌──────────────────────────────┐   │
│  │ 左側：對話互動區 (Chat UI)   │   │ 右側：報價單試算卡片 (Preview)│   │
│  └──────────────┬───────────────┘   └──────────────┬───────────────┘   │
└─────────────────┼──────────────────────────────────┼───────────────────┘
                  │                                  │
                  ▼                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      AI Agent 邏輯層 (agent.py)                         │
│                                                                         │
│  • 意圖解析 (Intent Parsing)                                           │
│  • 缺項檢查 (Missing Field Check)                                      │
│  • Function Calling / Tool 派發                                        │
└─────────────────┬───────────────────────────────────────────────────────┘
                  │
        ┌─────────┴───────────────────────┐
        ▼                                 ▼
┌──────────────────────────────┐  ┌──────────────────────────────┐
│ 資料服務層 (db_service.py)   │  │ 報價計算引擎 (quote_engine)   │
│                              │  │                              │
│ • search_product()           │  │ • calculate_cost()           │
│ • search_option()            │  │ • calculate_price()          │
│ • get_part_qty()             │  │ • build_qdt_snapshot()       │
└───────────────┬──────────────┘  └──────────────┬───────────────┘
                │                                │
                └───────────────┬────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        本機資料庫 (Local Database)                     │
│                                                                         │
│  ordspd (選項定義) ─── ordspe (可選項目) ─── ordqty (部件用量)         │
│                                      │                                  │
│                                      ▼                                  │
│                            ordqdt_ai (報價規格快照)                        │
└─────────────────────────────────────────────────────────────────────────┘
```

### 1.1 架構分層

| 層級 | 主要模組 | 主要職責 |
|---|---|---|
| UI 層 | `app.py` | Streamlit 頁面、Chat UI、報價預覽、Session State |
| AI Agent 層 | `agent/` | 意圖解析、欄位檢查、Tool Calling |
| 資料服務層 | `database/` | 資料庫查詢、ORM、Repository |
| 計價引擎層 | `engine/` | 成本、售價與報價快照計算 |
| 資料庫層 | Local DB | 儲存產品、選項、部件用量與報價快照 |

---

## 2. 模組結構與目錄設計（Project Directory）

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
│   └── snapshot.py               # 生成 ordqdt_ai 快照邏輯
│
├── database/                     # 資料庫存取層（ORM / SQL）
│   ├── __init__.py
│   ├── connection.py             # SQLAlchemy 資料庫連線管理
│   ├── models.py                 # ordspd、ordspe、ordqty、ordqdt_ai 模型
│   └── repository.py             # 查詢產品、部件、代碼對映之 SQL 實作
│
└── utils/                        # 輔助工具
    ├── logger.py                 # 本地 ai_quote_log 稽核日誌
    └── helpers.py                # 格式化輸出、數值轉換
```

---

## 3. 核心模組職責說明

### 3.1 前端介面層（`app.py`）

#### 雙區 Layout

使用：

```python
st.columns([1, 1])
```

將畫面分成兩個主要區域：

- **左側：對話互動區**
  - 使用 `st.chat_input()` 接收使用者需求
  - 使用 `st.chat_message()` 顯示對話歷史
- **右側：報價試算區**
  - 顯示目前報價草稿
  - 顯示選配明細
  - 顯示成本與建議售價
  - 提供正式建立報價單功能

#### Session State 管理

利用 `st.session_state` 保存：

- `messages`：對話紀錄
- `current_quote`：目前報價草稿
- `status`：目前報價流程狀態

---

### 3.2 AI Agent 層（`agent/`）

AI Agent 主要負責「理解需求」與「調用工具」，**不負責金額計算**。

#### 自然語言對映

透過 `tools.py` 進行產品與選項搜尋，將使用者輸入轉換為正確的：

- `optno`
- `code`

#### 缺項提醒

檢查必要的產品與選配條件是否完整。

若資料不足，Agent 應提示使用者補充，例如：

> 「目前尚缺少桌面材質，請選擇美耐板、實木或其他材質。」

#### 零算價介入

AI **不得直接計算任何金額**。

所有金額必須由：

```text
engine/calculator.py
```

依據資料庫中的正式資料進行計算，再將計算結果回傳至 UI。

---

### 3.3 計價引擎層（`engine/`）

計價引擎負責所有商業計算，並與 AI Agent 解耦。

#### 成本計算

部件成本公式：

$$
\text{部件成本}
=
\text{數量}
\times
\text{標準用量（stdqty）}
\times
\text{採購成本（compri）}
$$

其中：

| 欄位 | 說明 |
|---|---|
| `數量` | 使用者需求數量 |
| `stdqty` | 部件標準用量 |
| `compri` | 採購成本 |

#### 報價計算

計價引擎可進一步負責：

- 部件成本計算
- 總成本計算
- 毛利／加成計算
- 建議售價計算
- 報價明細組裝

#### 快照複製

確認正式報價後，將當時的主檔數據完整寫入 `ordqdt_ai`。

主要包含：

- `compri`
- `stdqty`
- `spdsc`
- 其他與當次報價相關的主檔資訊

如此可避免未來主檔資料異動後，**歷史報價金額與規格跟著改變**。

---

### 3.4 資料庫存取層（`database/`）

資料庫存取層負責所有資料庫操作，避免 UI 或 AI Agent 直接執行 SQL。

可使用：

- SQLAlchemy
- PyODBC

對應既有資料表：

| 資料表 | 用途 |
|---|---|
| `ordspd` | 選項定義 |
| `ordspe` | 可選項目 |
| `ordqty` | 部件用量 |
| `ordqdt_ai` | 報價規格快照 |

主要 Repository 功能包括：

- 查詢產品
- 查詢選項
- 查詢部件
- 查詢標準用量
- 查詢代碼對映
- 建立報價快照

---

## 4. Agent 狀態流程

建議將報價流程設計成明確的狀態機：

```text
NEW
 │
 │ 使用者輸入需求
 ▼
ANALYZING
 │
 │ Agent 解析需求
 ▼
CHECKING
 │
 ├── 缺少必要欄位 ──► WAITING_FOR_INPUT
 │                         │
 │                         │ 使用者補充
 │                         └──────────────┐
 │                                        │
 └── 資料完整 ───────────────────────────┘
                                          ▼
                                      PREVIEW
                                          │
                                          │ 使用者確認
                                          ▼
                                      CONFIRMED
                                          │
                                          │ 建立快照
                                          ▼
                                   ordqdt_ai SNAPSHOT
```

### 建議狀態定義

| 狀態 | 說明 |
|---|---|
| `NEW` | 尚未建立報價 |
| `ANALYZING` | Agent 正在分析需求 |
| `CHECKING` | 檢查必要欄位與資料 |
| `WAITING_FOR_INPUT` | 等待使用者補充資訊 |
| `PREVIEW` | 報價草稿已建立，可試算 |
| `CONFIRMED` | 使用者已確認正式報價 |
| `SNAPSHOT_CREATED` | 已成功建立 `ordqdt_ai` 快照 |

---

## 5. 主程式 UI 與流程雛形（Code Skeleton）

### 5.1 `app.py`

```python
import streamlit as st

from agent.core import run_quote_agent
from engine.calculator import calculate_quote
from database.repository import save_quote_snapshot


# 1. 頁面配置
st.set_page_config(
    page_title="AI 報價助理（本機版）",
    layout="wide"
)

st.title("🤖 AI 產品報價助理")


# 2. 初始化 Session State
if "messages" not in st.session_state:
    st.session_state.messages = []

if "current_quote" not in st.session_state:
    st.session_state.current_quote = None


# 3. 畫面雙區佈局
left_col, right_col = st.columns([1, 1])


# ============================================================
# 左區：對話互動
# ============================================================

with left_col:

    st.subheader("💬 需求對話")

    # 渲染歷史訊息
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    # 使用者輸入
    if prompt := st.chat_input(
        "請輸入需求，如：我要20張 1200x600 的桌子，美耐板白色..."
    ):

        st.session_state.messages.append({
            "role": "user",
            "content": prompt
        })

        with st.chat_message("user"):
            st.write(prompt)

        # 呼叫 Agent 處理
        with st.chat_message("assistant"):

            with st.spinner("AI 分析需求與查詢規格中..."):

                response, updated_quote = run_quote_agent(
                    prompt,
                    st.session_state.current_quote
                )

                st.write(response)

                # 更新對話歷史
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": response
                })

                # 更新報價狀態
                if updated_quote:
                    st.session_state.current_quote = updated_quote
                    st.rerun()


# ============================================================
# 右區：結構化報價卡片與快照
# ============================================================

with right_col:

    st.subheader("📋 報價單即時試算")

    quote_data = st.session_state.current_quote

    if quote_data:

        st.info(
            f"報價狀態：{quote_data.get('status', '草稿中')}"
        )

        # 顯示選配明細
        st.table(
            quote_data.get("items", [])
        )

        # 顯示引擎計算結果
        calc_result = calculate_quote(quote_data)

        st.metric(
            "估算總成本",
            f"${calc_result['total_cost']:,.0f}"
        )

        st.metric(
            "建議報價金額",
            f"${calc_result['total_price']:,.0f}"
        )

        # 正式建立報價
        if st.button(
            "確認建立正式報價單（寫入 ordqdt_ai）",
            type="primary"
        ):

            ref_no = save_quote_snapshot(
                quote_data,
                calc_result
            )

            st.success(
                f"已成功寫入報價快照！單號：{ref_no}"
            )

            st.session_state.current_quote["status"] = "已確認"

    else:

        st.info(
            "尚無試算資料，請於左側輸入需求。"
        )
```

---

## 6. 主要資料流

完整報價資料流程如下：

```text
使用者自然語言需求
        │
        ▼
┌───────────────────┐
│ Streamlit Chat UI │
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│    AI Agent       │
│                   │
│ Intent Parsing    │
│ Missing Check     │
│ Tool Calling      │
└─────────┬─────────┘
          │
          ├──────────────────────┐
          ▼                      ▼
┌───────────────────┐   ┌───────────────────┐
│ Database          │   │ Quote Engine      │
│                   │   │                   │
│ Product Search    │   │ Cost Calculation  │
│ Option Search     │   │ Price Calculation │
│ Part Quantity     │   │ Snapshot          │
└─────────┬─────────┘   └─────────┬─────────┘
          │                       │
          └───────────┬───────────┘
                      ▼
              ┌───────────────┐
              │ Quote Preview │
              └───────┬───────┘
                      │
                使用者確認
                      │
                      ▼
              ┌───────────────┐
              │ ordqdt_ai Snapshot│
              └───────────────┘
```

---

## 7. 環境需求與啟動方式

### 7.1 依賴套件（`requirements.txt`）

```text
streamlit>=1.30.0
langchain>=0.1.0
openai>=1.0.0
sqlalchemy>=2.0.0
pyodbc>=5.0.0
python-dotenv>=1.0.0
pandas>=2.0.0
```

---

### 7.2 建立虛擬環境

#### Linux / macOS

```bash
python -m venv venv
source venv/bin/activate
```

#### Windows

```powershell
python -m venv venv
venv\Scripts\activate
```

---

### 7.3 安裝套件

```bash
pip install -r requirements.txt
```

---

### 7.4 設定環境變數

建立 `.env`：

```env
OPENAI_API_KEY=your_key_here

DB_CONN_STR=mssql+pyodbc://user:pass@localhost/dbname
```

> 實際部署時請勿將 `.env` 提交至 Git Repository。

建議同時建立 `.gitignore`：

```gitignore
.env
venv/
__pycache__/
*.pyc
```

---

### 7.5 啟動 Streamlit

```bash
streamlit run app.py
```

啟動後可透過瀏覽器開啟：

```text
http://localhost:8501
```

---

## 8. 系統設計原則

### 8.1 AI 與計價邏輯分離

AI Agent 僅負責：

```text
自然語言
   ↓
需求解析
   ↓
產品／選項對映
   ↓
資料查詢
   ↓
建立結構化報價需求
```

計價引擎則負責：

```text
結構化報價需求
   ↓
取得正式資料
   ↓
成本計算
   ↓
售價計算
   ↓
報價結果
```

因此應避免：

```text
❌ AI 自行判斷成本
❌ AI 自行計算售價
❌ Prompt 中硬編碼價格
❌ 將價格資訊直接交由 LLM 推算
```

---

### 8.2 報價必須具備可追溯性

正式報價建立後，應將當下所使用的資料完整保存至 `ordqdt_ai`。

如此可以確保：

> **今天建立的報價，即使明天產品主檔、採購成本或標準用量發生變更，歷史報價仍維持原始內容。**

---

### 8.3 UI、Agent、Engine、Database 解耦

建議維持以下依賴方向：

```text
app.py
   │
   ▼
agent/
   │
   ├──────────────► database/
   │
   └──────────────► engine/
                          │
                          ▼
                     database/
```

不建議：

```text
app.py ──► SQL
app.py ──► 計價公式
agent ──► 直接修改資料庫
LLM ──► 直接產生最終價格
```

這樣可降低後續維護成本，並方便未來將 Streamlit UI 替換成其他前端介面。

---

## 9. 後續擴充方向

第一階段以「本機單機版」為目標，後續可逐步擴充：

1. **FastAPI API Layer**
   - 將 Agent、計價引擎與資料庫服務 API 化。
   - Streamlit 僅負責前端 UI。

2. **使用者與權限管理**
   - 業務人員
   - 報價審核人員
   - 管理員

3. **報價版本管理**
   - 同一需求建立不同版本報價。
   - 支援重新報價與歷史版本比較。

4. **報價單輸出**
   - PDF
   - Excel
   - 列印格式

5. **稽核與操作紀錄**
   - 使用者輸入
   - Agent 判斷
   - Tool Calling
   - 計價結果
   - 報價確認時間
   - 報價人員

6. **快取與效能最佳化**
   - Streamlit Cache
   - 資料庫查詢快取
   - 常用產品／選項快取

7. **模型抽換**
   - OpenAI API
   - 本地 LLM
   - 其他相容 OpenAI API 的模型服務

---

## 10. MVP 實作優先順序

建議依以下順序開發：

```text
Phase 1
│
├── 建立 Streamlit UI
├── 建立 Session State
└── 完成基本 Chat UI
        │
        ▼
Phase 2
│
├── 建立 database/
├── 建立 SQLAlchemy Models
└── 完成產品／選項查詢
        │
        ▼
Phase 3
│
├── 建立 Agent
├── Intent Parsing
├── Missing Field Check
└── Tool Calling
        │
        ▼
Phase 4
│
├── 建立 calculator.py
├── 完成成本計算
└── 完成售價計算
        │
        ▼
Phase 5
│
├── 建立報價 Preview
├── 使用者確認
└── 建立 ordqdt_ai Snapshot
        │
        ▼
Phase 6
│
├── 稽核日誌
├── 報價單輸出
└── 權限／版本管理
```

---

## 11. 結論

本系統採用 **全 Python + Streamlit 的本機模組化架構**，核心設計原則為：

> **AI 負責理解需求，Database 負責提供資料，Engine 負責計算價格，Snapshot 負責保存當下報價狀態。**

整體架構可概括為：

```text
使用者
  │
  ▼
Streamlit
  │
  ▼
AI Agent
  │
  ├──► Database
  │
  └──► Quote Engine
          │
          ▼
      Quote Preview
          │
          ▼
      User Confirm
          │
          ▼
      ordqdt_ai Snapshot
```

透過上述分層設計，可以在維持本機部署簡單性的同時，保留未來擴充至 FastAPI、Web API、權限管理、報價版本管理及企業級部署的彈性。