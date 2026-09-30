# 統一 Gateway 與路由階段

狀態：2026-09-30 開發中。起點 `259b928892a12bbdc4607c3b961a57f231780488`，工作分支 `feat/unified-gateway-routing`。使用者已授權本階段的本地實作、測試與分支提交；合併、推送、部署及公開入口不在此授權內。本階段在離線驗證後另授權最多兩筆新識別的正式 gateway 真實請求；單次 Doppler 唯讀 helper 的「不呼叫 NVIDIA」只限定該 helper 操作。

## 已確認範圍

- 單一受 bearer token 保護、只綁 loopback 的 HTTP gateway；CLI 走同一 API，提供 catalog、路由解釋／dry run、提交任務、查狀態及依 provider／model／時間查用量。CLI 支援 JSON 及簡短文字。
- 能力限 `text_generation` 及帶明確來源／目標語言的 `translation`。NVIDIA Riva 翻譯與一個經官方頁核對的文字生成候選入 catalog；保留 Google、Cloudflare 的既有模型。provider 是實際計費／呼叫服務，與模型作者分開。同一 provider 可配置多個模型。
- 路由先排除不支援能力、使用者限制、帳號免費資格或有效期不足、計費已啟用、秘密參照缺失、官方有效剩餘為零及本地冷卻／並行／額度不足者；候選依短期可更新容量的最短 refresh、未知免費容量、一次性贈額排序，再依 priority 與 target id。未知官方額度保留 null，絕不視為零或無限；本地上限與官方資料分別呈現。免費資格未知則拒絕。
- gateway 同進程執行 provider adapter，核心 broker 只見估算界線與用量，不接觸 prompt、回答、金鑰。Doppler 秘密由 executor 在送出前經固定 project/config/name 的直接 HTTPS API 取得。provider 目的地為 catalog 固定 HTTPS URL，拒絕 redirect。三個 provider 都走 reserve→dispatch→report；既有 DirectClient 與 NVIDIA 單獨 executor 介面維持相容。
- 請求鍵由持久 HMAC 摘要綁定完整任務內容；同鍵不同內容衝突。SQLite 保留非內容中繼資料與用量。派送後結果不明或程序中止時不自動重送／改路；相同鍵只查狀態。成功但缺少供應商用量時保留獨立 `completed_usage_unknown`，本地預留帳本維持 unknown，不將缺值寫為零。
- 狀態與用量紀錄包含實際 provider／model、路由理由、時間、延遲、HTTP 狀態、安全錯誤碼、供應商回報的輸入／輸出數值與未知數。官方剩餘、本地預留估算、實際回報分開。舊 `.state` smoke JSON 不匯入或改寫。

## 驗證與界線

離線 fixture 覆蓋三 provider 的 HTTP／CLI、同 provider 多模型、能力拒絕、額度與共享 scope、429 冷卻、同鍵衝突及併發、重啟不重送、資料庫／API 不洩漏秘密或內容、用量未知聚合。完成後以正式 gateway 與 SQLite 執行最多兩筆新識別的小型 Riva 翻譯真實請求；第二筆只在第一筆結果已知或修正後必要時執行。不部署。Google／Cloudflare 若無實際帳號證據，保持 fixture 驗證。現有真實 Riva 單次 receipt 不是正式 gateway 的 SQLite 用量證據，也不表示本階段已人工驗收。

## 固定介面草案

`POST /v1/tasks` 接受 `request_key`、`capability`、`input`、`max_output_tokens`，可選 `provider`／`model` 與翻譯的 `source_language`／`target_language`。首次成功回傳回答及安全 metadata；後續同鍵回傳 metadata，無回答。`POST /v1/routes/explain` 接受同一任務格式但不派送；`GET /v1/catalog`、`GET /v1/tasks/{request_key}`、`GET /v1/usage` 為唯讀。輸入、回答與秘密不進 DB 或一般日誌。實作若需調整欄位，以最終測試與 README 的具體 schema 為準。
