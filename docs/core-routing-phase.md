# 統一 Gateway 與路由階段

狀態：2026-09-30 本地實作及 fixture 驗證完成；真實 LLM 結果未知，待階段驗收。起點 `259b928892a12bbdc4607c3b961a57f231780488`，工作分支 `feat/unified-gateway-routing`。使用者已授權本階段的本地實作、測試與分支提交；合併、推送、部署及公開入口不在此授權內。本階段在離線驗證後另授權最多兩筆新識別的正式 gateway 真實請求；單次 Doppler 唯讀 helper 的「不呼叫 NVIDIA」只限定該 helper 操作。

現行 Doppler project 於 2026-10-01 原地改名為 `api-quota-broker`，config 仍為 `dev`。下文的 `api-provider-nvidia/dev` 是 2026-09-30 實測時的歷史範圍；未重跑該請求或改寫收據。

## 已確認範圍

- 單一受 bearer token 保護、只綁 loopback 的 HTTP gateway；CLI 走同一 API，提供 catalog、路由解釋／dry run、提交任務、查狀態及依 provider／model／時間查用量。CLI 支援 JSON 及簡短文字。
- 能力限 `text_generation` 及帶明確來源／目標語言的 `translation`。NVIDIA Riva 翻譯與一個經官方頁核對的文字生成候選入 catalog；保留 Google、Cloudflare 的既有模型。provider 是實際計費／呼叫服務，與模型作者分開。同一 provider 可配置多個模型。
- 路由先排除不支援能力、使用者限制、帳號免費資格或有效期不足、計費已啟用、秘密參照缺失、官方有效剩餘為零及本地冷卻／並行／額度不足者；候選依短期可更新容量的最短 refresh、未知免費容量、一次性贈額排序，再依 priority 與 target id。未知官方額度保留 null，絕不視為零或無限；本地上限與官方資料分別呈現。免費資格未知則拒絕。
- gateway 同進程執行 provider adapter，核心 broker 只見估算界線與用量，不接觸 prompt、回答、金鑰。Doppler 秘密由 executor 在送出前經固定 project/config/name 的直接 HTTPS API 取得。provider 目的地為 catalog 固定 HTTPS URL，拒絕 redirect。三個 provider 都走 reserve→dispatch→report；既有 DirectClient 與 NVIDIA 單獨 executor 介面維持相容。
- 請求鍵由持久 HMAC 摘要綁定完整任務內容；同鍵不同內容衝突。SQLite 保留非內容中繼資料與用量。派送後結果不明或程序中止時不自動重送／改路；相同鍵只查狀態。成功但缺少供應商用量時保留獨立 `completed_usage_unknown`，本地預留帳本維持 unknown，不將缺值寫為零。
- 狀態與用量紀錄包含實際 provider／model、路由理由、時間、延遲、HTTP 狀態、安全錯誤碼、供應商回報的輸入／輸出數值與未知數。官方剩餘、本地預留估算、實際回報分開。舊 `.state` smoke JSON 不匯入或改寫。

## 驗證與界線

離線 fixture 覆蓋三 provider 的 HTTP／CLI、同 provider 多模型、能力拒絕、額度與共享 scope、429 冷卻、同鍵衝突及併發、重啟不重送、資料庫／API 不洩漏秘密或內容、用量未知聚合。正式 gateway 與 SQLite 最多各執行一筆 Riva 翻譯及目前官方頁標為 Free Endpoint 的 Nemotron 3.5 Lightning 文字生成；第二筆只在第一筆結果明確後執行。不部署。Google／Cloudflare 若無實際帳號證據，保持 fixture 驗證。現有真實 Riva 單次 receipt 不是正式 gateway 的 SQLite 用量證據，也不表示本階段已人工驗收。

## 2026-09-30 本機實測結果

- 先通過 72 個 fixture／單元測試、Ruff 與 mypy。Doppler `api-provider-nvidia/dev` 的單一 5 分鐘、整個 config 唯讀 Service Token 在記憶體使用；`NVIDIA_API_KEY`、Service Token、輸入及回答均未輸出或寫入檔案。正式 HTTP API 綁定 `127.0.0.1`，未部署。
- 帳號來源是使用者回報的 NVIDIA UI：`API Quota Broker`、ACTIVE、到期日 2027-09-29（未確認時區及精確時間）、「up to 40 rpm」。官方 Build 頁分別顯示兩模型 Free Endpoint，但官方帳號剩餘及共享 scope 未知。忽略式本機 profile 只允許此兩模型，共用 1 RPM、2 RPD、1024 input TPM 與並行 1 的本機安全限額；40 rpm 沒有當作本機上限或剩餘額度。
- Riva 翻譯 `gateway-live-translation-178b68172afc49428b752cf3bd0a76b1`：HTTP 200，`completed`，provider 回報 input 22／output 3 tokens，ledger `settled_provider_usage`；延遲 1628 ms。回答只存在首次呼叫進程記憶體。
- Nemotron 3.5 Lightning 文字生成 `gateway-live-text_generation-86de20ca937647548cd2b686285c757d`：已派送，但 60334 ms 後 `TimeoutError`，無 HTTP 狀態或 provider 用量，任務與 ledger 均為 `unknown`；本機保留估算 input 312 tokens。供應商是否執行未知，**不得重送或換模型補呼叫**。這筆不是文字生成成功驗收。
- 兩筆任務在重新建立 gateway 物件後，狀態與重複 request key 均只回中繼資料，CLI 狀態與用量與 HTTP 一致；SQLite 有 2 tasks、2 reservations、2 reports，模式 `0600`，經內容掃描未包含測試 prompt 或回答。兩份既有 smoke receipt SHA-256 在本次前後一致。HMAC key 僅在本次進程記憶體，這次實測是 gateway 物件／HTTP server 重建；真正跨進程用同鍵查詢須依 README 使用受保護的持久 digest key file。
- Google、Cloudflare 僅 fixture 驗證，沒有用真實帳號呼叫。工程驗證與人工驗收分開；本次沒有合併、推送或部署。

## 固定介面草案

`POST /v1/tasks` 接受 `request_key`、`capability`、`input`、`max_output_tokens`，可選 `provider`／`model` 與翻譯的 `source_language`／`target_language`。首次成功回傳回答及安全 metadata；後續同鍵回傳 metadata，無回答。`POST /v1/routes/explain` 接受同一任務格式但不派送；`GET /v1/catalog`、`GET /v1/tasks/{request_key}`、`GET /v1/usage` 為唯讀。輸入、回答與秘密不進 DB 或一般日誌。實作若需調整欄位，以最終測試與 README 的具體 schema 為準。
