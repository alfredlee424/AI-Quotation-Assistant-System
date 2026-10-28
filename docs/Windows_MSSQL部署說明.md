# Windows 本機 MSSQL 部署說明

適用於 [four_quote_acceptance.py](../four_quote_acceptance.py) 的本機逐張報價模式。**不需要 MySQL 伺服器**；原 MySQL 模式與隔離合成模式仍保留。此入口不是一般應用初始化工具，不建表、不修表、不灌資料、不讀模型金鑰。

## 1. 安裝與啟動

1. 在部署電腦安裝 Python，於專案根目錄建立本機虛擬環境：[python -m venv venv](../venv/)。不要複製 macOS 或別台電腦的虛擬環境。
2. 安裝依賴：[.\venv\Scripts\python.exe -m pip install -r .\requirements.txt](../requirements.txt)。包含 Python ODBC 套件與 Windows 台灣時區資料。
3. 另行安裝 [Microsoft ODBC Driver for SQL Server](https://aka.ms/downloadmsodbcsql)，版本 17 或 18，位元架構必須與 Python 相符。只有安裝 SQL Server 或 Python 套件不代表已有 Microsoft ODBC 驅動。
4. 可唯讀列出驅動：[.\venv\Scripts\python.exe -c "import pyodbc; print(pyodbc.drivers())"](../requirements.txt:6)。應包含設定所指定的完整名稱，例如 ODBC Driver 17 for SQL Server。
5. 保留部署電腦既有 [`.env`](../.env)，核對下一節兩項設定；**不要拿範例覆蓋現場檔案**。
6. 停止舊服務後啟動：[.\venv\Scripts\python.exe .\four_quote_acceptance.py --local-db](../four_quote_acceptance.py:15)。不加初始化選項、不改用一般應用補表。預設只開放 http://127.0.0.1:8501 。

## 2. 沿用現場設定

格式與可複製的去識別範例見 [`.env.example`](../.env.example)。只需資料庫連線與工作組，不需要模型金鑰。

- [DB_CONN_STR](../local_quote/backend.py:29)：格式為 [mssql+pyodbc://quote_user:CHANGE_ME@localhost/QuoteDB?driver=ODBC+Driver+17+for+SQL+Server](../.env.example)。範例帳號、密碼與資料庫均須替換，不能原樣使用。
- 主機可保持**該 Windows 電腦的實際電腦名稱**，不必改成 localhost，也不寫死在程式中。接受 localhost、IPv4／IPv6 loopback 及本機具名執行個體；拒絕遠端、DSN、原始 ODBC 字串及繞道參數。具名執行個體名稱限英文字母起首、英數或底線、最多 16 字元；不與連接埠同時指定。
- 目前本機 MSSQL 路徑使用 SQL Server 帳密驗證；SQL Server 必須允許 SQL 登入，帳號須可存取目標資料庫。Windows 整合驗證不在此入口本次支援範圍。
- [WORKGROUP](../local_quote/backend.py)：必須為 1～3 字元，與既有主檔事業別一致；不是產品類別。例如四字元產品類別不能當工作組填入。
- 設定直接讀取專案根目錄的 [`.env`](../.env)，不受啟動位置影響；不使用繼承的環境變數覆蓋，不插值。密碼含 URL 保留字需百分比編碼；本入口拒絕帳密／主機／資料庫中的分號、大括號、控制字元及原始井字號，避免 ODBC 參數注入。

### TLS 憑證

[連線政策](../local_quote/backend.py)強制加密且預設驗證伺服器憑證。正式建議配置可信憑證。ODBC 17 既有連線若過去未要求加密，更新後可能先遇到本機自簽憑證驗證失敗。

**只有確認是這台本機 SQL Server，且接受不驗證憑證身份的風險時**，才在原連線字串尾端加上 [&TrustServerCertificate=yes](../.env.example)。傳輸仍加密，但不驗證憑證身份；程式不會自行退回未加密或自動信任憑證。ODBC 18 自動啟用長文字參數支援。

## 3. 既有資料庫與權限前置條件

需 SQL Server **2016 或以上**，使用一般磁碟資料表。啟動與每次保存前均以 [MSSQL 結構檢查](../local_quote/mssql_schema.py)核對：

- 有效預設結構必須為 dbo，具資料庫層級 [VIEW DEFINITION](../local_quote/mssql_schema.py:21) 權限，避免中繼資料隱藏被誤判為安全。
- 既有五張主檔：[invdoc、ordstr、ordspd、ordspe、ordqty](../local_quote/backend.py)；兩張保存表：[ordqdt_ai、quote_snapshot_document](../local_quote/backend.py)。欄位長度、可空性、主鍵及快照的工作組＋單號唯一鍵均須符合 [七表合約](../local_quote/backend.py)。
- 需七表的讀取權限、兩張保存表的新增權限，及執行 [sp_getapplock／sp_releaseapplock](../local_quote/storage.py:273) 的權限。本入口不要求修改、刪除、建表或主檔寫入權限；建議使用專用最小權限帳號，不使用系統管理員帳號。
- 檢查會拒絕觸發器、雙向外鍵、列級安全政策、失效／過濾／特殊索引、生成欄位，以及寫表的未預期唯一鍵、檢查約束、預設值和額外必要欄位等未支援風險。現場若有這些結構，應由 DBA 評估映射，**不要為了通過而直接刪除約束或資料**。
- 主檔支援定長／變長一般字串與 Unicode；只在載入主檔時去除尾端 ASCII 補白，讓定長產品碼與路徑可正確展開，不更動工單原文及歷史快照。
- 寫表支援變長 Unicode、UTF-8、CP950 及 CP1252；大型快照可用 Unicode／相應編碼的大型文字欄位。寫入前依欄位實際編碼核對 bytes 容量與嚴格編碼往返，再以 Unicode 參數寫入，提交前精確讀回。CP950 不能保存的字、窄欄位超量及轉碼差異均拒絕，不替換成問號、不截斷。
- 六個成本數值欄需為一致的浮點精度；53 位採 binary64，24 位或 real 採 binary32。混合精度、Decimal 或其他型別不自動轉換，須 DBA 核對。新版文件保留自身精度契約，讀回不使用模糊誤差容忍；舊四張文件沒有精度標記，不能精確重現則拒絕保存。

缺表或結構不符時，啟動會停止並指出合約表／欄位與原因。**本次沒有附帶自動建表或遷移，也不會借用一般應用初始化整個 ERP。** 過去只有 ERP 五張主檔、沒有兩張報價保存表的資料庫，仍須 DBA 預先審核準備。

## 4. 交易與資料保護

- [唯讀主檔連線](../local_quote/backend.py)採可序列化交易，結束時回滾；有程式層查詢守門，不是 SQL Server 的伺服器唯讀帳號，亦不是敵意程式的沙箱。可能持有讀鎖；應避免在尖峰時段長時間分析大量主檔。
- [MSSQL 配號鎖](../local_quote/storage.py:273)為資料庫範圍、同一連線持有，新版與舊版共用，最多等候 5 秒。取得鎖後核對结构、分配號碼及保存，提交／回滾後才釋放；鎖狀態不明時丟棄連線，不放回連線池。
- 預览不配號、不寫庫；人工確認才保存本張。失敗以同一確認身份重試，提交狀態不明時先查歷史，不另建確認。未使用本協定的外部程式仍須依既有唯一鍵防護，不能宣稱所有外部寫入都已被本鎖管制。
- 快照維持現有版本及容量政策：完整邏輯內容最多 8 MiB，實際儲存封套最多 65,535 UTF-8 bytes；不因 MSSQL 大型欄位可容納更多而放寬。

## 5. 錯誤排除與現場驗收

### 一般主程式可連線，但本機模式顯示連線失敗

兩個入口的連線政策不同，不代表資料庫服務一定停止：

| 比較 | 一般 [app.py](../app.py) | [本機模式](../local_quote/backend.py) |
|---|---|---|
| 設定來源 | 環境變數優先於設定檔；缺連線設定時可回退 SQLite | 只讀專案根目錄 [`.env`](../.env)，不插值、不回退 |
| MSSQL 加密 | 直接交由原連線字串與 ODBC 預設決定 | 強制加密，預設驗證憑證 |
| 初始化 | 啟動會走初始化流程 | 只檢查既有結構，不建表、不灌資料 |

因此 ODBC 17 原本未要求加密、而 SQL Server 使用自簽憑證時，可能只有本機模式失敗。一般主程式顯示「資料已存在、略過灌入」僅證明它查到資料，**不能證明兩個入口連到同一資料庫**。

依序處理：

1. 在現場確認一般主程式實際使用的資料庫種類、主機、執行個體、資料庫與帳號，是否與專案 [`.env`](../.env) 一致；檢查 PowerShell／系統是否另設連線環境變數。不要將完整連線或密碼貼到回報中。
2. 若為同一台本機 SQL Server，優先配置可信憑證。**若確認使用本機自簽憑證且接受不驗證身份的風險**，可在既有 [DB_CONN_STR](../local_quote/backend.py:132) 的查詢參數尾端加入 [&TrustServerCertificate=yes](../local_quote/backend.py:84)。既有參數若為 no，改成 yes，不要重複加入；保留原帳密、主機、資料庫及驅動，不必換成 localhost。不要設定 [Encrypt=no](../local_quote/backend.py:84)，本機模式不允許關閉加密。
3. 停止舊服務後，以 [專案 Python 啟動本機模式](../four_quote_acceptance.py:15)：[.\venv\Scripts\python.exe .\four_quote_acceptance.py --local-db](../four_quote_acceptance.py:15)。這是部署端設定變更，本次程式更新不會自動修改現場設定或信任憑證。
4. 若仍是 SQLSTATE 08001，檢查執行個體、實際連接埠、SQL Server TCP/IP 與服務狀態；具名執行個體另核對 SQL Server Browser。**08001 不是憑證專用錯誤碼**，信任憑證不能修復主機／連接埠錯誤。
5. 若訊息轉成「既有資料表不相容」，代表已越過連線階段，依第 3 節由 DBA 處理既有七表及權限；不要加初始化選項。

[安全錯誤分類](../local_quote/backend.py:27)會將 Windows ODBC 已知的憑證鏈不受信任／名稱不符原生碼轉成固定中文訊息；未辨識的 08001 保留一般排除提示。不輸出原始 ODBC 錯誤、SQL、帳密或主機資訊，也不自動降低 TLS 政策。此辨識不涵蓋所有 ODBC／TLS 錯誤。

### 其他錯誤與驗收

| 訊息類型 | 核對方向 |
|---|---|
| 找不到專案 Python | 在本台電腦重建虛擬環境，不沿用 macOS 環境 |
| 設定或驅動檢查未通過 | 本機主機名稱、工作組、驅動名稱、套件及連線參數白名單 |
| 找不到／無法載入 ODBC | 安裝 Microsoft 驅動，確認 32／64 位元與 Python 相符 |
| 登入失敗 | SQL 帳密、SQL Server 混合驗證及目標資料庫存取權限 |
| 無法建立連線／逾時 | 本機服務、執行個體、連接埠、TCP/IP 與 TLS 憑證；憑證問題不一定有獨立 SQLSTATE |
| 既有資料表不相容 | dbo、metadata 權限、缺表／欄位型別／長度／索引及不支援風險；不自動修表 |
| 中文編碼／容量不符 | 字元是否可由欄位編碼無損保存、實際 bytes 長度；不可刪來源或改成問號繞過 |

1. 在現場使用原命令啟動，通過唯讀七表檢查，確認產品選單讀到正確工作組。不按保存時，兩張保存表應無新增資料。
2. **另在 DBA 授權的測試資料庫**執行中文主檔分析、單張保存、同確認重試、重啟後歷史查回，核對金額、全部明細與中文。
3. 在測試庫驗證兩個程序同時保存不撞號，以及寫入中斷／回滾／鎖逾時；不在正式庫故意注入故障。先完成備份、還原、權限與現場驗收，才進行正式保存。
4. 回報僅提供安全錯誤分類、合約欄位與驅動版本；不要貼帳密、完整連線字串或客戶快照。

## 6. 自動驗證界線

測試入口：[MSSQL 後端](../test/test_local_mssql_backend.py)、[MSSQL 保存協定與隔離 SQLite 交易](../test/test_local_mssql_storage.py)、[Windows 啟動](../test/test_local_bootstrap.py)。覆蓋方言查詢、中文容量、主檔補白、浮點精度、回滾、鎖清理、重試、歷史及秘密遮蔽。

2026-10-21 最終回歸：[PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ./venv/bin/python -m pytest test/test_local_*.py test/test_acceptance_bootstrap.py -q -p no:cacheprovider](../test/test_local_mssql_storage.py:1)，**503 項及 274 個子測試通過（52.15 秒）**；包括全部本機系列與隔離啟動測試，差異空白檢查亦通過。

本批在 macOS 執行離線測試，不讀私人設定、不連正式庫。**尚未在真實 Windows／SQL Server／ODBC 17 或 18 完成連線、併發或保存驗收，也未重跑全專案所有測試。** 上述數字不作為現場主檔、權限或正式保存已通過的證明。

2026-10-28 連線診斷回歸：沿用上述離線測試命令，**515 項及 274 個子測試通過（52.24 秒）**。新增涵蓋原始／SQLAlchemy 包裝的憑證錯誤、不明 08001 不誤判、秘密遮蔽與啟動失敗清理；未在部署端 Windows／SQL Server 驗證，不表示現場連線已恢復。
