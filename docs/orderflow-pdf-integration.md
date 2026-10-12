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

PDF 缺 input_tokens 估值時預設仍拒絕。服務明確啟用 `--google-pdf-count-tokens`，且請求指定 Google / gemini-3.5-flash-lite / text_generation / PDF，才採以下持久化預檢；不影響其他模型或 legacy 字串路徑。呼叫端不可傳 resource_bounds 自行降低保留值。

## CountTokens admission

[官方 endpoint](https://ai.google.dev/api/tokens) 接受完整 generateContentRequest，包含模型、PDF、prompt、schema 與生成設定。官方示例 count 與實際 prompt usage 可差 1，這裡明確標記 `provenance=estimated`，不宣稱數學上界或 billed usage。

- 估值為 `ceil(totalTokens × 1.2) + 256`：20% 比例餘量與 256 固定協定餘量是本地保守政策，不是官方精度保證。只接受正整數且不超過模型 input 上限的計數；估值仍受既有 input cap 131,328/min 與 context/output cap 阻擋，不截低估值硬放行。
- 估值有效 120 秒，HMAC 綁定 target snapshot、endpoint、Content-Type 與實際 generation body bytes。派送前重驗效期與相同 wire；不跨 target/model 重用。
- 先建立 gateway job，再把 count intent、target/scope/model、wire HMAC 與 request charge 寫入 SQLite，才外部 POST。只保存 metadata，不保存明文 PDF、prompt、schema、credential 或原始錯誤。
- count POST 最多 30 秒、response 64 KiB。成功才新增可信 internal resource bound；缺失、拒絕、逾時、壞格式全部停止 generation。count 完成前後 crash 均不能讓 queue 換 execution key 自動重做 count；原工作保留收據，沒有隱藏重試。
- 計數與 generation 分開記錄；count 消耗一個保守本地 requests charge，generation 沿既有 ledger 結算。預檢先核對本地及已知觀測 request 餘額可容納兩次 POST。count charge 留存，不因逾時退款。新證據後的觀測扣减與本地 cap 分開處理；不宣稱官方 count 費用或 token 配額消耗已知。
- count unknown 不永久封鎖獨立工作；僅仍處於最多 30 秒傳輸窗口的 intent 暫時佔用 scope，原 job 永不自動重播。generation unknown 繼續沿既有 reservation hold 規則。
- explain/catalog/status/diagnostics 不解析 provider secret、不觸發 POST。待計數時 explain 回 selected_target_id=null、preflight_target_id、preflight_required=true、estimated_input_tokens=null；這表示可進入預檢，不是生成已 admission。

結果的 `token_preflight` 包含 state、counted_tokens、estimated_tokens、provenance、created/completed/expires 時間。生成實際用量仍看 reported_input_tokens、reported_output_tokens 與 ledger_basis；估值與 provider reported 分開。

## 8 MiB 相容與資源驗證

保留原 8 MiB PDF 上傳契約，Broker 候選為 media=8 MiB、request=16 MiB、result=4 MiB。服務新增 `--serialize-work`，POST 在 body read 之前取得共用 gate，持續到回應完成；worker tick 及 queue result 解密/回應共用同一 gate。GET 小 metadata 保持可用；status/recent 與到期 maintenance 只投影 metadata，result 只取存在旗標，不載入完整加密 payload/result。body 有 30 秒總讀取期限，慢速滴流不能無限延長。

忙碌時固定 HTTP 503、JSON `{"error":"work_busy"}`、Content-Type: application/json、精確 Content-Length、Connection: close。大 body 上傳可能先遇 BrokenPipe；只可在同 socket 讀得完整且唯一此錯誤的 framed response 時重送，沒有回應／不完整／不同錯誤仍視未知。對 POST 此回覆在讀 body／submit 前發出，表示零接受、零持久化、零派送，可沿同業務 job/key 有界等待再送。只有此精確錯誤適用，不能泛化其他 503 或 timeout。建議退避 1/2/4/8 秒、8 秒封頂，總 240 秒或原 job deadline 較短者；耗盡保持未提交/busy 可續同 job，不換 key。result GET 遇此錯誤只重取原 key。

Google count/generation transport 各最多 30 秒，Gateway execution budget 與 queue execution lease 為 180 秒；gate 本身沒有整個 worker 的強制 wall watchdog，不能把 180 秒稱為含所有 DB/解析操作的絕對上限。PDF parser 保留獨立 256 MiB / 3 CPU 秒 / 5 秒 wall / 最多 128 頁限制。

[資源量測工具](../scripts/measure_pdf_memory.py) 使用乾淨 spawn Broker，client 在另一程序；完整 HTTP queue→count→generation→result，沿用真 transport 的 base64/config/子程序管道，仅把 HTTP child 替換成不連網 fixture。恰好 8,388,608 bytes 合法 PDF，HTTP body 11,185,446 bytes。兩次 5ms 採樣的 Broker 子樹 RSS 峰值 262,176 / 262,336 KiB，PSS 242,979 / 243,073 KiB，count/generation 各一次，正常 output cap 8192、smoke 2048。這不是 ASUS cgroup peak 或 TLS/live 驗收。

ASUS 原生隔離測量已通過同一 whitespace PDF（SHA256 `d40c321fbe27105c07781b4a77812a3791fab48dade1141c04154b8de74d7b13`）：Broker 子樹獨立 CPU50% / MemoryHigh384M / MemoryMax512M / Swap0 / Tasks32 / NOFILE128；client/sampler 在另一無 CPU 限制 user unit，每秒 GET metadata。完整 queue→count fixture→generation fixture→result 42.821 秒成功，count/gen 各一，零真 provider POST；5 次 parser wall 3.096–3.646 秒、CPU 1.452–1.512 秒，原限制未變。cgroup MemoryPeak 237,977,600 bytes、SwapPeak0；5ms 子樹 RSS/PSS 峰值 268,028 / 234,336 KiB。Python 啟動前的少量頁面仍歸原 client cgroup，故同列子樹 RSS，不能將 cgroup peak 當作完整部署峰值。OrderFlow PID 未變，約35 MiB；不代表同時高負載壓測或 TLS/live 驗收。

先前 client 與 Broker 同 CPU50% cgroup 的失敗不作有效資源上界；分離後也曾因每50ms metadata polling 加上 SELECT * 載大 ciphertext 造成第二次 parser 5.082 秒 timeout。修正 metadata 投影並按實際輪詢節奏重驗後通過，沒有調寬 parser CPU/wall/memory 保護。兩個8MiB payload/result metadata測試確認外部metadata相同、Python配置峰值低於1MiB。

部署模板候選 MemoryHigh=384 MiB、MemoryMax=512 MiB、MemorySwapMax=0，CPU50%/Tasks32/NOFILE128 不變。原生證據已過，正式服務尚待固定artifact部署。

## Credential 與部署候選邊界

現有 `/run/api-quota-broker-client/client_token` 為 0600 morris:morris、父目錄 0711 root:root。OrderFlow 的 User/Group=orderflow 不應讀取或改 owner／mode 取得它。

候選為在經審查的 OrderFlow unit 增加 `LoadCredentialEncrypted=client_token:/etc/api-quota-broker/credentials/client_token.cred`，後端只讀自己的 systemd credential 路徑（`%d/client_token`／CREDENTIALS_DIRECTORY 下同名檔）。必須保留 encrypted credential 的名稱綁定 client_token，不憑猜測重命名；需由有權限者核對本機 systemd credential 能否按該名稱供給，保持 root-only 加密來源與 per-service runtime 權限。候選未執行 decrypt、copy、chmod、chown 或 restart。

一般客戶端拿到的是共享 client 權限，仍沒有專案 ACL；不可傳到瀏覽器或記錄值，不給 provider/admin/queue key。現有 Doppler provider credential 留 Broker。將此值給 OrderFlow 也代表它進入可信合作式客戶端範圍，需保留該界線。

部署前須共同核對：Broker capability release、正式 bound/cap 政策、8MiB 資源 gate 與 unit 候選、OrderFlow adapter 與 client credential、服務停止/恢復及原 ledger/queue/session/PDF 保留。不得用歷史維運封包直接重跑，或把 source revision 當已部署。

## 離線證據及有限 live 計畫

- `uv run --locked pytest -q tests/test_orderflow_pdf_contract.py tests/test_registry.py tests/test_family_gateway.py tests/test_family_adapters.py tests/test_families.py`：540 passed。
- 新增 12 個契約 cases；兩份 schema、PDF wire 轉換、HTTP queue、結果重取、legacy 字串、缺失／過期估算、未知不重播、截斷與用量分開判讀、非法 schema／媒體／caller bound 拒絕。
- 本階段全套初跑 2676 passed / 39 skipped / 1 failed；唯一失敗為 acceptance 對新增 diagnostics.execution_policy 的 exact-key gate，已修正並以受影響測試驗證。
- count/queue/acceptance/prerequisite 修正後定向108 passed；metadata投影新增後 queue/count/work-gate 73 passed；prerequisite lock drift/recovery 14 passed。
- `uv run --locked mypy src`：27 個來源檔通過；修改檔 Ruff check/format 通過。不把局部修正後結果宣稱為另一次全套。

有限 live 候選由 OrderFlow 保存一份無私人資料、可人工核對的合成 PDF、其 hash、固定 prompt/schema 與預期品項，建立一個持久化業務 job。只用此既有 Google provider/key/model，max_attempts=1、max_output_tokens≤2048；以同 payload explain 核對 preflight target 與非 token gates，再由持久化工作進行 count 與 generation。2026-10-12 新的人類批准涵蓋必要 count 預檢及唯一合成 PDF 生成；只有 OrderFlow 原 task 發起該工作，Broker 不另作 generation probe。

任一未知結果保留原 job/execution/收據並停止新派送；只查原 job/result。結果需通過完成 metadata、JSON/schema、預期品項與人工確認；用量結算另看 provider 回報。合成成功不代表私人文件、全部大小／頁數、一般大量品項或使用者驗收完成。

## 固定維運準備與恢復邊界

`prepare_pdf_maintenance.py` 是此次固定 prerequisite：只更新已安裝的 verifier allowlist（一個來源檔）、broker-v1.service 模板及 package manifest 對應 hash；不改 `/etc/systemd/system/api-quota-broker.service`、active-release、current、帳號、socket、sudoers、DB 或服務狀態。先核對全 package/固定 baseline、取得既有 operation.lock、root-only 備份及 once intent；來源有漂移則停止。`prepare_pdf_transport.py` 沿用既有 Doppler/PAM 私有管道，只在精確 morris prompt 後取值，兩端 swap/core guard、固定 root source hash、unknown 不重送。

| 狀態 | 服務及維運行為 | 恢復 |
| --- | --- | --- |
| prep 前 | 舊 current / installed unit / active pins | 無變更 |
| prep 成功、尚未 deploy | 舊 unit 繼續運行；重啟/開機仍用舊 unit。active pins 仍指舊 unit；package 新 hash 自洽 | 可沿既有維運 preflight/deploy；不把新模板當正在運行的 unit |
| prep 可判定寫入失敗 | 僅對仍與本次新 bytes 相同的已改檔復原，保留備份/intent/result | 不動服務/DB；不自動重試 |
| prep crash／foreign drift | package hash 可能不一致，維運 fail closed；舊 installed unit/current 未改 | 讀原 receipt 與三檔 hash，人工判定後固定修復；不換 ID 重送 |
| deploy 成功 | 既有 maintenance transaction 備份 ledger/config/unit/policy/active，安裝候選、更新 installed unit/current/active pins並重啟 | 既有保列後檢與 rollback；不還原 DB |
| deploy 可判定失敗 | 既有 transaction 恢復原 installed unit/current/active；prep 模板仍為新候選，package hash仍自洽 | 查原 deployment receipt；新操作需可判定原狀態，不盲重送 |

prep 不改 gateway.json，故 Google 正式 output 8192、input/request caps、免費資格及歷史未知 hold 保留。新來源採加法 schema initialization，原 ledger/queue 全部保留。最終 source SHA、artifact/unit/config hash、runtime current、備份與原生驗證回條在部署完成後另記，不能以本頁候選當現場證据。
