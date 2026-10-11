# OrderFlow PDF 接入候選（2026-10-12）

狀態：本機能力與契約 fixture 已驗證，**尚未部署或進行 PDF live 驗收，不能宣稱 OrderFlow 已接通**。本階段沿既有 Google provider/key，以 `gemini-3.5-flash-lite` 為候選；不沿用 OrderFlow 舊直連的 `gemini-3.1-flash-lite` 作為已驗證 Broker 模型。

接入契約基準為 `2145d9ba7b47c153af2022102439d67a1c789365` 的 [README](../README.md) 與 [client-integration.md](client-integration.md)。此頁及能力修改是其後的工作分支候選，尚未部署或發布。固定合作指南版本為 `c7fbf48250a44baf2638c22470410012109efc5d`。OrderFlow 的程式由其既有專案團隊修改，本候選不改該 repo。

## 已核對的現場與缺口

ASUS 唯讀檢查日期為台灣時間 2026-10-12；用既有 `aqb` 做 catalog／diagnostics／合成 PDF explain，未讀出 credential 值、未呼叫 provider。

| 項目 | 現場結果 |
| --- | --- |
| Broker / OrderFlow | 兩個 systemd service active；不等於接入驗收 |
| Broker current | `release-510b514833ce06ee491128a8c12d5ab2acb2666d0f9839ee79cdc82e0ec4dda8` |
| Google target | `asus-google-pool` / `google` / `gemini-3.5-flash-lite` |
| 現行 features | `text_generation`, `text`；未宣告 PDF／structured |
| 帳號證據 | free_eligible=true、billing_enabled=false、期限 2026-10-22T03:53:20.337024+00:00；不是官方即時餘額 |
| 本地 caps | requests 3/min、100/day；input_tokens 131,328/min；shared concurrency 1 |
| 文字輸出 target cap | 8,192；本次一筆合成 smoke 限 2,048，不更動正式 cap |
| Queue worker | enabled=true、running=true、error_code=null |
| 最低 readiness | credential_inventory_unknown；不可推定 key 不存在或可用 |
| 現場媒體／HTTP／結果上限 | 1 MiB / 2 MiB / 4 MiB，與套件預設 8 / 16 / 32 MiB 不同 |
| Runtime | Python 3.14.4、pypdf 6.19.0 已存在 |
| 合成 PDF explain | 無 selected target、permanent_rejection=true；不派送 provider |

Google 的[模型頁](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)列明 PDF 輸入與 structured outputs；官方模型 input 上限 1,048,576、output 上限 65,536。這支持能力宣告，不取代同一帳號的模型存取／免費資格證據，也不證明 PDF 實際擷取品質。既有 2026-10-09 Google live 成功是文字呼叫，不能當成 PDF+schema live。

## 精確請求與回應契約

PDF 擷取使用 **`capability=text_generation` 的 typed messages**；不是 legacy OCR，也沒有一個可直接傳入的 `document` capability。

```json
{
  "request_key": "of-synthetic-opaque-job-id",
  "provider": "google",
  "model": "gemini-3.5-flash-lite",
  "capability": "text_generation",
  "input": {
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "Extract line items. Reply in JSON only."},
        {"type": "document", "mime_type": "application/pdf", "data": "<valid PDF base64>"}
      ]
    }]
  },
  "options": {
    "json_schema": {
      "type": "object",
      "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"description": {"type": "string"}, "quantity": {"type": "integer"}},
        "required": ["description", "quantity"]
      }}},
      "required": ["items"]
    }
  },
  "requirements": {"features": ["document_input", "structured_output"]},
  "max_output_tokens": 2048,
  "max_attempts": 1,
  "wait_policy": "reject"
}
```

這是欄位範本，base64 佔位符不可直接送出。正式 prompt 與欄位規則仍由 OrderFlow 維護；不要用此短 prompt 替換原業務語意。MANAGEMENT_SCHEMA 的 orderNo、invoiceNo、client、product、code、qty、unitPrice、amount、currency、date、incoterms、unit 仍保留字串型別及原 required 語意。

舊 schema 使用 Google `responseSchema` 的 `OBJECT/ARRAY/STRING/INTEGER`；Broker 的 `options.json_schema` 使用 JSON Schema，須在 schema 的 type 欄位轉為 `object/array/string/integer`。不要小寫化其他字串、欄位名或 enum。兩份原 schema 已與 OrderFlow 原始碼核對，僅做 type 正規化；[測試](../tests/test_orderflow_pdf_contract.py)不依賴安裝 OrderFlow。

既有 `gemini_inference` adapter 會送 Google `contents[].parts[].inlineData`（mimeType、data），並使用 `generationConfig.responseMimeType=application/json`、`responseJsonSchema` 與 maxOutputTokens。不能混送 options.response_format，也不直接傳 Gemini SDK 的 body。[官方 JSON Schema 契約](https://ai.google.dev/gemini-api/docs/generate-content/structured-output)是子集；結果仍須經 OrderFlow 原有 schema／業務驗證與人工確認。

使用 `POST /v1/queue` 提交，回 HTTP 200 與 metadata。`GET /v1/queue/{外層key}` 可取得：request_key、state、priority、deadline、wait_policy、max_attempts、attempt_count、created_at、expires_at、completed_at、next_retry_at、error_code、execution_key、result_available、attempt_budget_used、attempts；有 outcome 後另包含 gateway_state 及用量／完成 metadata。

`GET /v1/queue/{外層key}/result` 回完整 outcome；`outcome.request_key` 是內部 q- execution key，文字在 `outcome.result.text`，不是 answer，也不是把整個 outcome 再包一層 result。Google typed 完成標記目前為 `finish_reason=stop`、`response_truncated=false`；截斷為 `max_tokens`、true。missing／未知完成 metadata 不可當成完整。

Queue completed 不代表用量已結算；gateway_state 可能為 completed_usage_unknown。保留答案與未知用量，不重新執行補帳。unknown／timeout 沿原 key 查詢，不換 key、model、instance 或直連路徑重送。預設結果自提交起保留 86,400 秒，應依 expires_at 及時保存；防重播 metadata 在內容到期後仍保留。

HTTP 400 invalid_request、401 unauthorized、404 not_found、409 conflict、503 各 error 分開處理。pending 是 503 queue_result_pending；無可取內容可為 503 queue_content_expired；勿把 503 一律當作新 POST。完整語意沿用[接入指南](client-integration.md)。

## 本機能力候選

只在 `Registry.builtin()` 為既有 Google 3.5 Flash-Lite 增加 document_input、structured_output，保留 builtin:google 與既有 typed adapter alias。這避免使用 replace_builtin 改成純 family adapter 後，原字串 Google 請求失效。其他模型／provider 不擴張；未新增 Gemini 3.1、adapter、依賴或對外路由。

這項程式變更只解除 feature gate。PDF media 不會自動生成 input_tokens bound；ASUS Google 有 input_tokens cap，沒有可信且有效的 target resource_estimates 時仍以 estimation_unconfigured 拒絕。呼叫端不可傳 resource_bounds 自行降低保留值。fixture 的 4096 只驗證機制，**不是正式 PDF 估算配置**。

## 正式 admission 的待決策方案

PDF 的壓縮大小、page count、base64 長度都不是可靠的 billed input token 上界。[Google 文件處理說明](https://ai.google.dev/gemini-api/docs/document-processing)的通用 page 說明與 Gemini 3 原生文字／影像處理不同，不把 258/page 直接套給 3.5。

1. **模型上限保留方案（配置候選）**：以官方 input 上限 1,048,576 作每次 PDF 的最壞保留值，max_input_bytes 覆蓋完整序列化 input+options，max_output_tokens 保留既有正式 8192。此值不是實際用量。現有 131,328/min cap 必定阻擋，若選此方案須明確批准更大的本地 cap，保留原 shared bucket、concurrency=1、requests caps、免費資格、expiry 與未知 hold。官方模型上限與帳號可用 TPM 仍是不同事；供應商可能拒絕。不得自行提高 cap 或把未知官方餘額設為足夠。
2. **CountTokens 方案（需新增實作與操作契約）**：[官方 endpoint](https://ai.google.dev/api/tokens)接受 generateContentRequest，可把完整相同模型、PDF、prompt、schema 一起送去計數；但官方示例的 countTokens 與實際 prompt usage 可差 1，不能稱為嚴格上界。需要明確保守 margin／本地 cap、模型及完整 wire payload 綁定、短期效期、失敗不派送 generation，以及獨立的 preflight intent/result 持久化。countTokens 是另一個認證外部 POST，費用／配額、timeout 與可否重試需獨立核對並授權；max_attempts=1 本身不授權它。此階段未實作或呼叫該 endpoint。

選定可信上界與資源政策前，保持 PDF admission fail closed。不以任意小值、略過 input_tokens cap 或客戶端宣告替代。

## 8 MiB 相容與資源驗證

OrderFlow 原上傳上限 8 MiB，不能默默縮為 1 MiB 後宣稱完整替代。8 MiB base64 約 10.67 MiB，加上 JSON／prompt／schema 後須使用至少足夠的 HTTP envelope；候選為套件預設 media=8 MiB、request=16 MiB，結果仍可維持原 4 MiB。這些值尚未套到 ASUS。

桌機 Python 3.12 的有界單工作 fixture 使用恰好 8,388,608 bytes 的合法單頁 PDF，完整 HTTP body 11,185,795 bytes，經 queue→加密保存→worker→fixture→結果成功，只有一次注入 transport。10ms 採樣包含 HTTP client、Gateway/worker 與 PDF 子程序的總 RSS 峰值 378,316 KiB（約 369.4 MiB）、PSS 峰值 358,378 KiB（約 350 MiB）；耗時約 1.53 秒。client 與服務在同一測試程序，因此不是純 Broker 數字，也不是 ASUS 負載驗收。單程序／child 的 ru_maxrss 因 fork 繼承峰值不可直接相加當 cgroup 用量。

ASUS 當時 MemAvailable 約 1.43 GiB；Broker MemoryHigh=256 MiB、MemoryMax=384 MiB、當前約 25.3 MiB；OrderFlow MemoryMax=768 MiB、當前約 34.2 MiB。空閒記憶體不是保留容量。單 fixture 已接近 Broker 硬上限，且 ThreadingHTTPServer 的多個 POST body 可同時解析；provider concurrency=1 不限制這些 buffer，也不限制另一個 submit 與 queue worker 的重疊。

因此**不能只改兩個 media flags 就宣稱 8 MiB 安全**。部署候選需明確限制大 body 入站／worker 重疊、評估必要的服務記憶體政策，再在 ASUS 核對完整 cgroup peak、CPU/PDF parser timeout 與共存 OrderFlow 工作。現有 PDF parser 有獨立 256 MiB／3 CPU 秒／5 秒 wall bound、最多 128 頁；不為放行而移除。若暫時只能支援 1 MiB，必須送出前明示缺口並由 main 決策，不是完整驗收。

## Credential 與部署候選邊界

現有 `/run/api-quota-broker-client/client_token` 為 0600 morris:morris、父目錄 0711 root:root。OrderFlow 的 User/Group=orderflow 不應讀取或改 owner／mode 取得它。

候選為在經審查的 OrderFlow unit 增加 `LoadCredentialEncrypted=client_token:/etc/api-quota-broker/credentials/client_token.cred`，後端只讀自己的 systemd credential 路徑（`%d/client_token`／CREDENTIALS_DIRECTORY 下同名檔）。必須保留 encrypted credential 的名稱綁定 client_token，不憑猜測重命名；需由有權限者核對本機 systemd credential 能否按該名稱供給，保持 root-only 加密來源與 per-service runtime 權限。候選未執行 decrypt、copy、chmod、chown 或 restart。

一般客戶端拿到的是共享 client 權限，仍沒有專案 ACL；不可傳到瀏覽器或記錄值，不給 provider/admin/queue key。現有 Doppler provider credential 留 Broker。將此值給 OrderFlow 也代表它進入可信合作式客戶端範圍，需保留該界線。

部署前須共同核對：Broker capability release、正式 bound/cap 政策、8MiB 資源 gate 與 unit 候選、OrderFlow adapter 與 client credential、服務停止/恢復及原 ledger/queue/session/PDF 保留。不得用歷史維運封包直接重跑，或把 source revision 當已部署。

## 離線證據及有限 live 計畫

- `uv run --locked pytest -q tests/test_orderflow_pdf_contract.py tests/test_registry.py tests/test_family_gateway.py tests/test_family_adapters.py tests/test_families.py`：540 passed。
- 新增 12 個契約 cases；兩份 schema、PDF wire 轉換、HTTP queue、結果重取、legacy 字串、缺失／過期估算、未知不重播、截斷與用量分開判讀、非法 schema／媒體／caller bound 拒絕。
- `uv run --locked mypy src/quota_broker`：26 個來源檔通過；修改檔 Ruff check/format 通過。不是全套或實機驗收。

有限 live 候選由 OrderFlow 保存一份無私人資料、可人工核對的合成 PDF、其 hash、固定 prompt/schema 與預期品項，建立一個持久化業務 job。只用此既有 Google provider/key/model，max_attempts=1、max_output_tokens≤2048；先以同 payload explain 核對選定 target、bound 與 cap，正式取得新 live 額度／部署授權後才 submit 一次。若選 countTokens，必須把該額外 POST 另列入批准上限，不能藏在一次 job 內。

任一未知結果保留原 job/execution/收據並停止新派送；只查原 job/result。結果需通過完成 metadata、JSON/schema、預期品項與人工確認；用量結算另看 provider 回報。合成成功不代表私人文件、全部大小／頁數、一般大量品項或使用者驗收完成。
