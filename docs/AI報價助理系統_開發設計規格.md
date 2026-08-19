# AI 報價助理系統－開發設計規格

> 依據目前已確認的 `ordspd`、`ordspe`、`ordqty` 資料結構，以及「依產品部件與選擇項目產生報價」的需求整理。
>
> **核心原則：AI 負責理解需求、查詢資料、提出選項與確認；正式的產品組合、用量、價格與報價金額由程式與報價引擎決定，不由 LLM 自行猜測。**

---

## 1. 系統目標

讓業務人員用自然語言描述客戶需求，例如：

> 我要 20 張 1200×600 的桌子，桌面用美耐板白色，木腳。

AI 應能：

1. 理解客戶需求
2. 找到對應產品
3. 找出產品所需部件
4. 找出各部件可選規格
5. 將自然語言轉成正式代碼
6. 找出缺少的必要條件並詢問
7. 依 `ordqty` 等資料計算部件需求量
8. 呼叫報價引擎計算成本與售價
9. 顯示報價預覽
10. 使用者確認後產生正式報價單
11. 未來可由報價單轉成正式訂單

---

# 2. 核心設計原則

## 2.1 AI 不直接決定價格

錯誤做法：

```text
LLM → 「我覺得應該報 8,000 元」
```

正確做法：

```text
使用者需求
    ↓
AI 理解
    ↓
查詢產品 / 部件 / 選項
    ↓
建立報價條件
    ↓
Quote Engine
    ↓
正式計算成本與售價
    ↓
AI 解釋結果
```

## 2.2 AI 不直接修改資料庫

AI 不應直接執行 SQL，而應透過受控的 Business API / Tool：

```text
search_product()
get_product_parts()
search_option()
get_part_quantity()
calculate_quote()
preview_quote()
create_quote()
convert_to_order()
```

這樣可以控制權限、商業規則、價格計算、稽核與資料完整性。

---

# 3. 建議系統架構

```text
                         使用者
                           │
                           │ 自然語言
                           ▼
                 ┌──────────────────┐
                 │   AI 報價助理     │
                 │                  │
                 │ 需求理解          │
                 │ 查詢資料          │
                 │ 選擇規格          │
                 │ 詢問缺少條件      │
                 │ 解釋報價          │
                 └────────┬─────────┘
                          │
                     Tool / API
                          │
          ┌───────────────┼────────────────┐
          ▼               ▼                ▼
   Product Service   Option Service   Quote Service
          │               │                │
          └───────────────┼────────────────┘
                          ▼
                    Quote Engine
                          │
                          ▼
                       Database
                          │
          ┌───────────────┼────────────────┐
          ▼               ▼                ▼
       ordspd           ordspe           ordqty
          │               │                │
          └───────────────┼────────────────┘
                          ▼
                        ordqdt
                          │
                          ▼
                     報價主檔/明細
                          │
                          ▼
                         訂單
```

---

# 4. 現有資料表在 AI 報價中的角色

## 4.1 `ordspd`

目前截圖顯示為資料字典／選項定義檔：

| 欄位 | 中文 | 型態 | 長度 |
|---|---|---|---:|
| workgroup | 事業別 | NChar | 3 |
| kind | 類別 | NChar | 1 |
| optno | 代碼 | NChar | 10 |
| optdesc | 說明 | NChar | 30 |
| code | 編碼 | NChar | 10 |
| adddate | 建立日期 | Char | 10 |
| addusrno | 建立人員 | Char | 8 |
| abndat | 異動日期 | Char | 10 |
| abntim | 異動時間 | Char | 8 |
| usrno | 異動人員 | Char | 8 |
| prgno | 異動程式 | Char | 10 |

主要用途：

```text
自然語言
   ↓
搜尋選項
   ↓
找到正式 optno / code
```

例如：

```text
美耐板
  ↓
CLMT
```

AI 不應自行猜測 `CLMT`，而應由資料庫查出。

---

## 4.2 `ordspe`

目前截圖顯示：

| 欄位 | 中文 | 型態 | 長度 |
|---|---|---|---:|
| workgroup | 事業別 | NChar | 3 |
| path | 路徑 | NChar | 100 |
| code | 項目編號 | NChar | 10 |
| codsc | 項目名稱 | NChar | 40 |
| compri | 採購單價 | Float | 8 |
| adddate | 建立日期 | Char | 10 |
| addusrno | 建立人員 | Char | 8 |
| abndat | 異動日期 | Char | 10 |
| abntim | 異動時間 | Char | 8 |
| usrno | 異動人員 | Char | 8 |
| prgno | 異動程式 | Char | 10 |

主要用途：

```text
正式選項
    ↓
規格 / 項目
    ↓
採購價格
```

例如：

```text
code = CLMT
codsc = 美耐板
compri = 500
```

這是報價引擎取得成本的重要來源。

---

## 4.3 `ordqty`

目前截圖顯示：

| 欄位 | 中文 | 型態 | 長度 |
|---|---|---|---:|
| workgroup | 事業別 | NChar | 3 |
| ref_no | 報價單號 | NChar | 21 |
| path | 批購路徑 | NChar | 100 |
| qty | 數量 | NChar | 1 |
| spc_code | 規格代碼 | NChar | 10 |
| spdsc | 規格 | NChar | 40 |
| stdqty | 用料量 | Float | 8 |
| stdpar | 裁切量 | Float | 8 |
| compri | 採購單價 | Float | 8 |
| abndat | 異動日期 | Char | 10 |
| abntim | 異動時間 | Char | 8 |
| usrno | 異動人員 | Char | 8 |
| prgno | 異動程式 | Char | 10 |

`ordqty` 是 AI 報價系統的重要 BOM／用量規則來源：

```text
產品 / 部件
    ↓
規格
    ↓
標準用料量
    ↓
裁切量
    ↓
採購單價
```

---

# 5. `ordqdt` 的角色

建議 `ordqdt` 作為「本次報價選擇結果／報價規格快照」。

概念上保存：

```text
報價單號
產品代碼
部件代碼
規格代碼
選項代碼
規格說明
數量
用料量
採購成本
報價單價
報價金額
建立/異動資訊
```

## 為什麼需要快照？

例如今天：

```text
美耐板採購價 = 500
```

產生報價：

```text
Q20260818001
```

一個月後：

```text
美耐板採購價 = 600
```

歷史報價不能跟著改變，因此正式報價必須保存當時的：

- 規格
- 數量
- 成本
- 單價
- 報價
- 計算結果

不能每次重新從主檔計算。

---

# 6. 建議報價主檔與明細

如果目前系統尚未有明確的 AI 報價資料結構，建議概念上增加：

```text
quote_header
quote_detail
```

## 6.1 quote_header

```text
quote_id
quote_no
workgroup
customer_id
customer_name
quote_date
currency
subtotal
discount
tax
total
status
created_by
created_at
updated_by
updated_at
```

## 6.2 quote_detail

```text
quote_id
line_no
product_code
product_name
part_code
part_name
option_code
option_name
spec_code
spec_name
quantity
unit_cost
unit_price
amount
```

---

# 7. 完整資料關係

```text
                         Product
                            │
                            ▼
                         ordqty
                     部件 / 用量規則
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
           ordspd                       ordspe
        選項定義                       選項項目
              │                           │
              └─────────────┬─────────────┘
                            ▼
                          ordqdt
                       報價選擇快照
                            │
                            ▼
                       Quote Engine
                            │
                            ▼
                     quote_header
                            │
                            ▼
                      quote_detail
                            │
                            ▼
                          Order
```

---

# 8. AI Agent Tool 設計

第一版建議建立以下 Tool。

## 8.1 `search_product`

用途：根據產品名稱或關鍵字搜尋產品。

```json
{
  "keyword": "桌子"
}
```

---

## 8.2 `get_product_parts`

取得產品包含哪些部件。

```json
{
  "product_code": "A001"
}
```

---

## 8.3 `search_option`

將自然語言轉成正式規格。

```json
{
  "keyword": "美耐板"
}
```

回傳例如：

```json
{
  "code": "CLMT",
  "name": "美耐板"
}
```

---

## 8.4 `get_part_quantity`

取得部件標準用量。

```json
{
  "part_code": "LEG"
}
```

---

## 8.5 `calculate_quote`

所有正式價格計算都透過此 Tool。

```json
{
  "items": [
    {
      "product_code": "A001",
      "option_code": "CLMT",
      "quantity": 20
    }
  ]
}
```

---

## 8.6 `preview_quote`

產生尚未正式建立的報價預覽。

---

## 8.7 `create_quote`

只有使用者確認後才能執行。

```json
{
  "customer_id": "C001",
  "items": [],
  "confirmed": true
}
```

---

## 8.8 `convert_to_order`

未來將報價轉成正式訂單。

```json
{
  "quote_no": "Q20260818001"
}
```

---

# 9. AI 報價流程

```text
使用者
  │
  │ 我要 20 張 1200x600 桌子
  │ 美耐板白色、木腳
  ▼
AI Agent
  │
  ├── search_product()
  ├── get_product_parts()
  ├── search_option()
  └── get_part_quantity()
  │
  ▼
建立報價條件
  │
  ├── 產品
  ├── 尺寸
  ├── 材質
  ├── 顏色
  ├── 腳架
  └── 數量
  │
  ▼
檢查必要欄位
  │
  ├── 缺資料 → 問使用者
  │
  └── 資料完整
        │
        ▼
calculate_quote()
        │
        ▼
   報價預覽
        │
        ▼
「是否建立報價？」
        │
        ├── 否 → 修改
        │
        └── 是
             │
             ▼
        create_quote()
             │
             ▼
        Q20260818001
```

---

# 10. 缺少條件的處理

例如：

> 我要 100 張桌子，美耐板。

AI 應先檢查：

```text
✓ 數量
✓ 材質
✗ 尺寸
✗ 外型
✗ 腳架
```

然後詢問：

> 已確認數量 100 張、材質為美耐板。
>
> 還缺少：
>
> 1. 桌面尺寸
> 2. 桌面外型
> 3. 腳架規格
>
> 如果都使用標準規格，請回答「使用標準規格」。

---

# 11. 自然語言與正式代碼轉換

AI 可能收到：

```text
白色
美耐板
木腳
1200x600
```

不能直接把文字寫入正式報價，而應查詢：

```text
白色
 ↓
search_option()
 ↓
WHITE

美耐板
 ↓
search_option()
 ↓
CLMT

木腳
 ↓
search_option()
 ↓
LEG

1200x600
 ↓
search_option()
 ↓
SIZE1200600
```

最後形成正式條件：

```json
{
  "color_code": "WHITE",
  "material_code": "CLMT",
  "leg_code": "LEG",
  "size_code": "SIZE1200600"
}
```

---

# 12. 報價計算引擎

報價引擎必須由程式負責。

```text
客戶需求
   ↓
產品
   ↓
部件
   ↓
標準用量
   ↓
採購價格
   ↓
成本
   ↓
人工
   ↓
管理費
   ↓
利潤
   ↓
折扣
   ↓
稅
   ↓
最終報價
```

例如：

```text
桌面：
數量 = 20
標準用量 = 1
採購成本 = 500

成本 = 20 × 1 × 500
     = 10,000
```

木腳：

```text
數量 = 20
標準用量 = 4
採購成本 = 120

成本 = 20 × 4 × 120
     = 9,600
```

總成本：

```text
19,600
```

再依公司正式規則計算：

```text
材料成本
+ 人工
+ 管理費
+ 利潤
- 折扣
+ 稅
= 報價
```

---

# 13. 報價確認機制

正式建立報價前必須有明確確認。

```text
AI：

已完成報價試算：

產品：桌子
數量：20
尺寸：1200 × 600
材質：美耐板
顏色：白色
腳架：木腳

成本：$XX,XXX
報價：$XX,XXX

是否建立正式報價單？
```

只有使用者明確確認後才呼叫：

```text
create_quote()
```

---

# 14. 歷史報價能力

未來可以支援：

> 跟上次王先生那張一樣，改成 20 張，桌面改白色。

流程：

```text
搜尋客戶
   ↓
搜尋歷史報價
   ↓
取得報價明細
   ↓
複製原始規格
   ↓
修改數量
   ↓
修改顏色
   ↓
重新計算
   ↓
建立新報價
```

**歷史報價只能作為參考，新的報價仍然必須重新計算。**

---

# 15. 權限設計

AI 報價系統必須繼承登入使用者權限。

| 功能 | 業務 | 主管 | 管理員 |
|---|---:|---:|---:|
| 查產品 | ✓ | ✓ | ✓ |
| 查成本 | 可限制 | ✓ | ✓ |
| 試算報價 | ✓ | ✓ | ✓ |
| 建立報價 | ✓ | ✓ | ✓ |
| 修改價格 | 限制 | ✓ | ✓ |
| 超過折扣 | ✗ | ✓ | ✓ |
| 修改成本 | ✗ | ✗ | ✓ |
| 刪除報價 | 限制 | ✓ | ✓ |

---

# 16. AI 稽核紀錄

建議建立：

```text
ai_quote_log
```

欄位概念：

```text
log_id
user_id
session_id
quote_no
user_input
ai_action
tool_name
tool_parameter
tool_result
created_at
```

例如：

```text
使用者：
我要 20 張桌子

AI：
search_product()

AI：
找到 A001

AI：
search_option("美耐板")

AI：
找到 CLMT

AI：
calculate_quote()

AI：
報價 13,500

使用者：
建立

AI：
create_quote()
```

這些紀錄對查錯、稽核與改善 AI 很重要。

---

# 17. MVP 第一版

第一版只完成：

```text
① 自然語言輸入
② 搜尋產品
③ 搜尋部件
④ 搜尋規格
⑤ 選擇產品組合
⑥ 詢問缺少資訊
⑦ 計算報價
⑧ 顯示預覽
⑨ 使用者確認
⑩ 建立報價
```

暫時不做：

```text
Email 自動讀取
PDF OCR
Excel 自動匯入
自動議價
自動訂單
客戶信用分析
自動排程
```

---

# 18. 第二階段

增加歷史報價：

```text
歷史報價搜尋
      ↓
複製歷史報價
      ↓
自然語言修改
      ↓
重新報價
```

例如：

> 複製 Q20260715001，數量改 50，顏色改黑色。

---

# 19. 第三階段

增加文件輸入：

```text
Email
Excel
PDF
Word
客戶訊息
```

全部轉換成：

```text
Quotation Requirement
```

再交給相同的 AI Quote Agent。

```text
Excel ─┐
PDF ───┤
Email ─┤
聊天 ──┤
       ▼
Requirement Parser
       ▼
AI Quote Agent
       ▼
Quote Engine
```

---

# 20. 與既有 ERP 整合

如果目前 ERP 是 RAD Studio / Delphi 系統，不建議為了 AI 把 ERP 全部重寫。

建議：

```text
                 舊 ERP
                   │
          ┌────────┴────────┐
          │                 │
       原有程式           AI API
          │                 │
          │                 ▼
          │           AI Quote Agent
          │                 │
          └────────┬────────┘
                   ▼
               Database
```

AI 系統可以是獨立服務，透過 API 使用既有 ERP 資料。

---

# 21. 建議 API

```text
/api/products
/api/products/{code}/parts

/api/options
/api/options/search

/api/quantities
/api/quantities/calculate

/api/quotes/preview
/api/quotes/calculate
/api/quotes/create

/api/quotes/{quote_no}
/api/quotes/{quote_no}/details

/api/orders/create
```

AI 不直接碰資料庫，而是呼叫這些 API。

---

# 22. AI Tool 分階段設計

第一版：

```text
search_product
get_product_parts
search_option
get_part_quantity
calculate_quote
preview_quote
create_quote
```

第二版：

```text
search_customer
search_history_quote
copy_quote
modify_quote
```

第三版：

```text
parse_excel
parse_pdf
parse_email
convert_to_order
```

---

# 23. 前端介面建議

建議「聊天 + 結構化報價」雙區：

```text
┌─────────────────────────────────────────────┐
│ AI 報價助理                                 │
├──────────────────────┬──────────────────────┤
│ 對話區               │ 報價內容             │
│                      │                      │
│ 業務：               │ 產品：桌子           │
│ 我要 20 張桌子       │ 數量：20             │
│                      │ 尺寸：1200×600       │
│ AI：                 │ 材質：美耐板         │
│ 已找到標準規格。     │ 顏色：白色           │
│                      │ 腳架：木腳           │
│                      │                      │
│ [輸入訊息...]        │ 成本：XXX            │
│                      │ 報價：XXX            │
│                      │                      │
│                      │ [修改] [建立報價]    │
└──────────────────────┴──────────────────────┘
```

---

# 24. AI 狀態機

建議 Agent 使用受控狀態：

```text
NEW
 ↓
UNDERSTANDING
 ↓
COLLECTING_REQUIREMENTS
 ↓
VALIDATING
 ↓
READY_TO_CALCULATE
 ↓
CALCULATING
 ↓
PREVIEW
 ↓
WAITING_CONFIRMATION
 ↓
CONFIRMED
 ↓
CREATING_QUOTE
 ↓
COMPLETED
```

錯誤：

```text
ERROR
```

這比讓 Agent 無限制自由操作更容易控制與除錯。

---

# 25. 建議開發順序

不要從 AI Chat UI 開始。

```text
Step 1
整理資料庫關係
        ↓
Step 2
定義產品 / 部件 / 選項關係
        ↓
Step 3
建立 Quote Engine
        ↓
Step 4
建立 Business API
        ↓
Step 5
建立報價預覽
        ↓
Step 6
建立正式報價
        ↓
Step 7
接 AI Agent
        ↓
Step 8
增加歷史報價
        ↓
Step 9
增加 Excel / PDF / Email
        ↓
Step 10
報價 → 訂單
```

---

# 26. 最終使用情境

最終希望業務只需要說：

> 王先生要 30 張跟上次一樣的桌子，數量改 30，桌面改白色，今天報價。

AI 自動：

```text
搜尋王先生
    ↓
找到歷史報價
    ↓
讀取原報價
    ↓
複製規格
    ↓
數量 = 30
    ↓
顏色 = 白色
    ↓
檢查規格
    ↓
重新取得價格
    ↓
Quote Engine 計算
    ↓
顯示報價
    ↓
等待確認
    ↓
建立正式報價
```

最後：

> 已完成報價 Q20260818001，總金額 XXX 元。

---

# 27. 實際開發前需要補齊的資料

目前已經可以開始設計 AI 報價架構，但要進入實際程式開發，還需要確認：

1. `ord940` 的完整資料結構
2. `ord920` 的完整資料結構
3. 目前正式報價單主檔
4. 目前正式報價單明細
5. 目前訂單主檔
6. 目前訂單明細
7. `ordqty.path` 的實際資料範例
8. `ordspe.path` 的實際資料範例
9. 產品與部件的實際關聯方式
10. 成本轉報價的計算規則
11. 折扣規則
12. 稅率規則
13. 客戶資料表
14. 使用者權限規則

其中最重要的是 **`path` 的實際內容與各表實際資料範例**，因為目前從資料字典只能確認欄位，尚不能百分之百確認產品、部件、規格之間的實際關聯規則。

---

# 28. 結論

這個 AI 報價助理不應設計成：

```text
ChatGPT
   ↓
自己猜產品
   ↓
自己算價格
   ↓
自己寫報價
```

而應設計成：

```text
                  AI
                   │
          理解業務自然語言
                   │
                   ▼
             查詢產品資料
                   │
                   ▼
             查詢部件規則
                   │
                   ▼
             查詢規格選項
                   │
                   ▼
             建立報價條件
                   │
                   ▼
             Business API
                   │
                   ▼
              Quote Engine
                   │
             正式計算價格
                   │
                   ▼
              報價預覽
                   │
             使用者確認
                   │
                   ▼
              正式報價
                   │
                   ▼
                訂單
```

**真正的核心是「資料庫 + 產品規則 + Quote Engine」，AI 則負責把業務人員的自然語言轉換成系統可以執行的條件。**

這樣可以保留目前 ERP，並逐步加入 AI，不需要一次重寫整個系統。
