"""
app.py - AI 報價助理（對話式報價）

左側對話：先決定產品類別，再一次貼上整份工單，或一問一答逐步訂製。
右側報價單：項目名稱與數量、逐項明細（缺漏紅字）、未稅金額／稅率／總額。

規則與 four_quote_acceptance.py --local-db 相同（local_quote 本機規則，非 LLM）；
主檔唯讀取自 .env 的本機資料庫；報價完整後可按「保存到資料庫（ordqdt_ai）」，每個項目各自一個單號保存。

啟動：
  streamlit run app.py

舊版單品報價與核對工作區：streamlit run legacy_app.py
"""
from local_quote.chat_ui import main

main()
