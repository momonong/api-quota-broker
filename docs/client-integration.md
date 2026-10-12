# 跨專案 Gateway 接入指南

其他專案的後端可以透過 Gateway HTTP API 共用路由、配額帳本與供應商執行。呼叫端使用 Broker client token，供應商金鑰由 Broker 管理。這是獨立的 JSON 契約；既有 Gemini／OpenAI SDK 不能只換 base URL 或 API key 就完成接入。

本文於 2026-10-12 隨套件 `1.1.0` 更新；PDF 實作與既有部署證據基準為 `81725056364b9c88296a38c93a745e90000fa57e`。Git／套件版本更新不表示 ASUS 已部署 `1.1.0`，也不證明帳號或模型目前可用。歷史部署與有限 live 驗收見[維運紀錄](asus-broker-ops.md)及 [PDF 接入證據](orderflow-pdf-integration.md)；typed 能力與限制見[能力契約](provider-capability-expansion.md)。

## 1. 連線與認證

ASUS 已驗收的入口為該主機的 `http://127.0.0.1:18084`。只有相同主機、相同 network namespace 的程式能直接使用這個 loopback。另一台主機、VM 或容器中的 `127.0.0.1` 指向它自己；接入前須另確認既有可用連線方式，不因本文件自行開放外網、建立 tunnel、改服務或重啟。

所有下列 HTTP 路由都需要 `Authorization: Bearer <client token>`。POST 使用 `Content-Type: application/json`，傳送完整 JSON body 與 Content-Length。由後端從已授權的受保護檔案／credential 路徑讀取 token；不要放入前端、Git、命令列參數、請求日誌或錯誤訊息。ASUS 的 `aqb` 是既有一般使用者 CLI 入口；新服務帳號仍須安排自己的合法 credential 存取，不能假設它可直接取得該 token。

| 憑證／金鑰 | 用途與持有人 |
| --- | --- |
| Broker client token | 專案後端呼叫 Gateway；不是 Google AI Studio key |
| Provider key／Doppler runtime credential | Broker 執行供應商請求；一般接入端不傳入 task body |
| 獨立 admin token | 管理觀測值、探索與修復等管理操作；不提供給一般業務 adapter |
| Queue encryption key | Broker 加解密持久化輸入及結果；不是 HTTP 認證 token |

目前 client token 屬於**可信、合作式客戶端共用權限**，沒有專案／使用者 ACL。同 token 的呼叫端可能查到其他工作的 metadata 或 queue 結果；request key 前綴只是辨識用途，不提供租戶隔離。若專案需要彼此隔離，必須先另行設計並驗收，不能把此入口直接當多租戶公開服務。

## 2. 接入端會用到的路由

以下為業務端常用子集；路徑皆相對於 Gateway base URL。

| 方法與路徑 | 回傳／用途 |
| --- | --- |
| `GET /v1/catalog` | JSON array；配置、能力、features、配額證據及 request limits |
| `GET /v1/diagnostics` | JSON object；路由與 queue worker 狀態，無 provider／secret I/O |
| `POST /v1/routes/explain` | 以完整 Task JSON 檢查路由，不呼叫 provider、不保留額度 |
| `POST /v1/tasks` | 同步執行；正常回傳 HTTP 200，須再判讀 body.state |
| `GET /v1/tasks/{request_key}` | 同步 task 的持久化 metadata，沒有 answer／result |
| `GET /v1/tasks?limit=20&state=unknown` | 最近 task；另支援 before、provider、model |
| `GET /v1/usage` | JSON array；可帶 provider、model、from、to |
| `POST /v1/queue` | 非同步提交完整 Task JSON；HTTP 200 回傳 job metadata |
| `GET /v1/queue/{request_key}` | job 狀態、execution_key、result_available 等 metadata |
| `GET /v1/queue/{request_key}/result` | 解密後的完整 Gateway outcome，可在保留期限內重取 |
| `POST /v1/queue/{request_key}/cancel` | body 為 `{}`；只取消可安全取消的未送出工作 |

`GET /v1/queue` 回傳 `{"tasks": [...]}`，預設最多 100 筆；queue 路由不接受 query parameters。`POST /v1/queue/tick` 會推進工作並可能呼叫真實 provider，屬於 worker 操作，業務端正常接入不應自行驅動它。確認服務已啟用 queue 與 worker；HTTP 可連線不表示 worker 正常。沒有可供此指南使用的免認證 `/health` 路由。

Catalog 的 available／diagnostics 的 ready 是快照，不保證實際輸入可通過 admission，也不保證供應商會成功。用 explain 核對這一次完整 payload，送出時仍會重驗。usage 是本地帳本與 provider 回報的聚合，不是供應商即時餘額或帳單；unknown／null 不能解讀為零。from／to 須帶時區，範圍為 from 含、to 不含。

## 3. 同步文字範例

以下 JSON 可交給 `POST /v1/routes/explain` 或 `POST /v1/tasks`。**explain 不送 provider；tasks 會送。** 所有範例都是合成內容，模型名稱只展示欄位格式，不保證已部署、仍免費或帳號可用；接入時以實際 catalog、配置及驗收為準。每個新工作需自行產生唯一 request key，不要在正式環境反覆使用範例 key。

```json
{
  "request_key": "orderflow-demo-sync-001",
  "capability": "text_generation",
  "provider": "google",
  "model": "gemini-2.5-flash-lite",
  "input": "Reply with OK.",
  "max_output_tokens": 128,
  "max_attempts": 1
}
```

成功 outcome 的**欄位節錄**如下，不是完整 response schema；實際還包含 attempts、usage 與 ledger 等 metadata。

```json
{
  "request_key": "orderflow-demo-sync-001",
  "state": "completed",
  "provider": "google",
  "model": "gemini-2.5-flash-lite",
  "answer": "OK",
  "ledger_basis": "settled_provider_usage"
}
```

字串 input 的首次成功呼叫回傳 `answer`。之後 `GET /v1/tasks/{key}` 或同 key、同 payload 再 POST，只有 metadata，**無法補取答案**；同 key 改 payload 則 HTTP 409。同步接入端必須立即保存結果；若 HTTP 回應遺失，即使 status 顯示完成，也不能以新 key 重跑取答案。需要斷線後重取結果的工作，優先使用 queue。

不要只看 HTTP 200 或 `state=completed`：它們不保證內容完整或符合業務規則。判讀 `finish_reason`、`response_truncated`；`length`／true 是截斷，missing／unclassified／null 不能證明完整。不同 adapter 支援的完成 metadata 不同。

## 4. 非同步提交、查詢與重取結果

向 `POST /v1/queue` 提交：

```json
{
  "request_key": "orderflow-demo-queue-001",
  "capability": "text_generation",
  "provider": "google",
  "model": "gemini-2.5-flash-lite",
  "input": "Reply with OK.",
  "max_output_tokens": 128,
  "priority": 0,
  "wait_policy": "wait",
  "max_attempts": 1
}
```

提交後通常為 `state=queued`、`result_available=false`。已知 job 一律沿原 key 查詢；按 `next_retry_at` 與應用程式的有限退避等待，避免高頻輪詢。完成後 `GET /v1/queue/orderflow-demo-queue-001` 的欄位節錄：

```json
{
  "request_key": "orderflow-demo-queue-001",
  "state": "completed",
  "gateway_state": "completed",
  "execution_key": "q-0123456789abcdef0123456789abcdef",
  "result_available": true
}
```

隨後 `GET /v1/queue/orderflow-demo-queue-001/result` 回傳**完整 Gateway outcome 本身**，不是 `{"result": <outcome>}` 外包裝。字串 input 的文字仍在 `answer`。此 outcome 的 `request_key` 是內部 `q-...` execution key，與外層業務 job key 不同；呼叫端需保存外層 key 與 metadata.execution_key 的對應，不能以內層 key 覆寫業務識別。

Queue 的 `state=completed` 仍可能搭配 `gateway_state=completed_usage_unknown`，表示拿到內容但部分用量未知。`result_available` 只表示有保留的 outcome，仍須檢查 outcome.state、answer／result、截斷與業務驗證。

結果可重取，不會因一次 GET 消耗掉；預設 TTL 為 **86,400 秒，自提交時間起算**，不是完成後再保留一天。等待及執行也會占用此期限，應以 job.expires_at 為準。到期會清除加密內容，但 metadata／防重播紀錄保留；不是刪除工作後可重跑。接入端需在期限內保存所需答案，並自行負責保存期間與資料權限。

結果未就緒時目前回 HTTP 503、`error=queue_result_pending`；已無內容時可能為 HTTP 503、`error=queue_content_expired`，包括到期或終態沒有可取內容。不要把兩者都當成「重新提交」。queue 未啟用則 `queue_disabled`。取消也不代表能中止已送出的 provider 執行。

## 5. Typed／結構化文字輸出

需要 JSON schema 等選項時，改用 typed input；字串 input 不接受 options。以下範例不指定 provider/model，由具備要求能力且通過 admission 的路由選擇；若要固定 Google，另加已核實的 provider/model。

```json
{
  "request_key": "orderflow-demo-structured-001",
  "capability": "text_generation",
  "input": {
    "messages": [
      {"role": "user", "content": "Return an object whose ok field is true."}
    ]
  },
  "options": {
    "json_schema": {
      "type": "object",
      "properties": {"ok": {"type": "boolean"}},
      "required": ["ok"],
      "additionalProperties": false
    }
  },
  "requirements": {"features": ["structured_output"]},
  "max_output_tokens": 128,
  "max_attempts": 1
}
```

Typed 首次成功 outcome 使用 `result`，例如以下節錄；`result.text` 仍是字串，接入端需 JSON parse 並再次驗證自己的 schema／業務規則。

```json
{
  "state": "completed",
  "result": {"text": "{\"ok\":true}"}
}
```

`options.json_schema` 直接放 schema object，不使用供應商 SDK 的外包裝。`options.response_format` 則是 `"text"` 或 `"json_object"` 字串，勿與 json_schema 同時使用；json_object 要求 json_output feature，json_schema 要求 structured_output feature。feature gate、adapter 支援、模型與帳號存取都必須成立，不能假定不同供應商有相同 schema、工具呼叫、品質或媒體能力。

Typed 另有 vision、embedding、rerank、audio、image、document 等已實作的 buffered 契約；詳見[能力指南](provider-capability-expansion.md)。這些不代表全部模型已 live 驗收。串流與 realtime 不在此接入契約內；HTTP 呼叫端不能傳自己機器上的檔案路徑讓 Broker 讀取，也不能假定任意遠端媒體 URL 會被代抓。OrderFlow 的 PDF／圖片流程需逐項對照真實輸入型態與模型，不能直接把舊 Gemini payload 原封轉送。

## 6. Request key、錯誤與重試

第一次送出前，呼叫端應持久化業務 job ID、採用同步或 queue、完整 task payload／版本及 request key。key 為 1–120 個 `[A-Za-z0-9._~-]` 字元，建議使用隨機 opaque ID 與非敏感專案前綴；不要包含客戶名稱、檔名、訂單內容或金鑰。

同 endpoint 內，同 key／同正規化 payload 可識別既有工作；改 payload 為 conflict。同步與 queue 的 key 空間不同，**不可把同一業務工作切換 endpoint 或換 key 當作重試策略**。網路 timeout／斷線只代表呼叫端沒有收到結果，不代表未執行；先查原 task/job，保持既有工作關聯。查詢與 queue result GET 可有限重試，但不要開啟通用 HTTP／SDK 自動重送 POST。

| 可見結果 | 接入端處理 |
| --- | --- |
| HTTP 200 + completed | 保存內容，再驗證完整性、格式與業務規則 |
| completed_usage_unknown | 可能有可用內容；用量保持未知，不為補齊用量重新執行 |
| unknown 或 transport timeout | 沿原 key 查詢／交由維運處理；不另起 provider 請求 |
| queued／waiting／running | 查原 queue job，有限退避；依 deadline／expires_at 停止等待 |
| failed／rejected／quota_exhausted／expired／cancelled | 檢查 attempts、error_code 與執行證據，業務另決定後續；不要只憑終態名稱推定 provider 一定沒執行 |
| HTTP 400 invalid_request | 修正契約；不可用盲重試掩蓋輸入錯誤 |
| HTTP 401 unauthorized | 修正合法 client credential 配置，勿把 token 寫入日誌 |
| HTTP 404 not_found | 檢查 URL、key、同步／queue 模式及所連 instance；本身不證明另一條路徑未執行 |
| HTTP 409 conflict | 檢查 key／payload 或取消狀態，不自動產生新 key 逃過衝突 |
| HTTP 503 | 先解析 error；可能是 admission 不可用、pending、內容到期或服務錯誤，不能一律重新 POST |

錯誤 body 通常有 `error`、`message`、`wait_until`，部分有 `details`；認證、解析與內部錯誤可能只有 error。正常 outcome 也有 state／error_code，不能只依 HTTP status 分支。

Broker 僅在送出前失敗，或有明確「未執行」配額拒絕證據時，依 attempt budget 選擇後續路由。一般 429、5xx、timeout 或未知執行結果不授權 fallback；這也不是第三方 exactly-once 保證。呼叫端不可在 Broker 結果未知時自行改用原 AI Studio key、其他供應商或另一個 Broker instance 重送。

## 7. 輸入與成本邊界

| 欄位／邊界 | 目前契約 |
| --- | --- |
| 文字輸出上限 | 一般預設 1024，可依匹配的配置／模型較小上限調整；顯式值 1–65,536，仍受 target/model admission 限制 |
| 字串文字 input | 最多 32,768 UTF-8 bytes；translation 另有來源／目標語言及 adapter 限制 |
| 字串 OCR | base64 PNG/JPEG，最多 36,000 decoded bytes；max_output_tokens 為 1 |
| Typed 媒體 | 預設 decoded input 8 MiB、HTTP request 16 MiB、result 32 MiB、最多 32 parts；不同 family 有額外限制 |
| Typed 非 token family | max_output_tokens 正規化為 0；不要沿用文字的 128／1024 |
| priority | 有效 Gateway 入口範圍 -100..100，預設 0 |
| deadline | ISO 8601 且帶時區；與 queue.expires_at 是不同限制 |
| wait_policy | wait／reject；queue 預設 wait，同步路徑不會像 queue 一樣持久等待 |
| max_attempts | 1–32，預設 32；送出前失敗也可能消耗 attempt，本指南範例明確設 1 |

字串文字的輸入 hold 採 `4 × UTF-8 bytes + 256`，是保守本地估算，不是 tokenizer 或官方用量。限制最終以 catalog.request_limits、explain 與實際 dispatch 檢查為準。指定 provider/model 會限制選擇；省略可讓 Broker 在合格路由間選擇，但不保證結果語意或品質相同。

本地 caps、觀測餘額、已驗證免費資格是不同證據；不明、過期或不合格時應停止 admission，不能自行開啟付費 fallback。

## 8. OrderFlow PDF 接入與驗收界線

2026-10-12 已完成 Broker `8172505` 部署、ASUS 原生 8 MiB 離線 fixture，以及唯一合成 PDF 的 OrderFlow backend live 驗收。Broker 原 key 核對為一次 countTokens、一次 generation，結果完成且用量已結算；**HTTP upload、form login、browser 與人工驗收未完成**。精確 payload、schema、部署識別與回條見 [PDF 接入與部署紀錄](orderflow-pdf-integration.md)。本次 `1.1.0` Git 收尾不重新部署或呼叫 provider。

PDF 使用 typed `text_generation`，要求 `document_input`／`structured_output`；接入端須解析 `result.text` 並驗證 schema、截斷與業務內容。已知或未知工作都保持原 queue key，不切回直連、不換 provider、不以新 key 補取結果；結果仍需經業務人工確認。

服務明確啟用 `--google-pdf-count-tokens` 後，限定 Google／`gemini-3.5-flash-lite` 的 PDF 路徑才可使用持久化預檢。countTokens 綁定完整 generation payload，輸入 hold 為 `ceil(count × 1.2) + 256`，有效期 120 秒；admission 須保留 count 與 generation 共兩次 request 的空間。receipt 缺失、未知或過期不自動重做計數／生成；provider 用量及帳號餘額仍須分開判讀。

部署證據的 media/request/result 上限為 **8/16/4 MiB**，與套件通用預設不同。從 diagnostics.execution_policy 核對實例的限制及 `--serialize-work`、計數功能是否啟用。序列 gate 涵蓋 POST body 讀取、worker 與 result 解密／回應，小型 metadata 查詢保持可用。

只有完整、精確的 HTTP 503 `{"error":"work_busy"}`，含 JSON Content-Type、正確 Content-Length 與 Connection: close，才表示該 POST 在讀 body／提交前被拒絕，可沿原 job/key 有限退避再送。BrokenPipe 本身不是此證據；須從同一 socket 讀得完整回應。建議 1/2/4/8 秒退避、8 秒封頂，總計 240 秒或原 deadline 較短者；不完整回應、其他 503 或 timeout 仍按未知工作處理。result GET 僅重取原 key。

這份文件不授權新的 OrderFlow 改碼、部署、服務／網路變更或真實 provider 呼叫。

## 9. 可核對的實作與驗證範圍

| 契約 | 實作入口 |
| --- | --- |
| 路由、HTTP code、認證、worker | [gateway_server.py](../src/quota_broker/gateway_server.py) 的 make_gateway_server |
| Task 驗證、metadata、首次答案、usage | [gateway.py](../src/quota_broker/gateway.py) 的 validate_task、Gateway.run/status/catalog/usage |
| Queue key、TTL、結果、終態保留 | [queue.py](../src/quota_broker/queue.py) 的 DurableQueue.submit/status/result、_maintenance、_finish |
| Typed messages／options／結果 | [families.py](../src/quota_broker/families.py) 與 [family_adapters.py](../src/quota_broker/family_adapters.py) |
| CLI 與操作範例 | [v1-local-guide.md](v1-local-guide.md) |

2026-10-11 文件驗證解析全部 6 個 JSON 區塊，並以本機 loopback Gateway、暫存資料庫及注入的 provider fixture 執行 3 個請求範例。核對同步／queue／typed response 節錄、同 key 不重播與衝突、pending／TTL 錯誤、結果重取、priority 邊界及 schema 轉換；本地文件連結也已核對。驗證使用既有 [HTTP fixture helpers](../tests/test_framework.py) 與 [typed registry helpers](../tests/test_family_gateway.py)，socket guard 僅允許 loopback 連線。該次文件驗證未重跑完整測試套件、不消耗 provider 配額，也不能單獨證明 live 模型行為或 OrderFlow 接入。

2026-10-09 的七家代表性驗證與限制保留於[原始維運紀錄](asus-broker-ops.md)。2026-10-12 新增的 PDF backend 證據只涵蓋上述單一合成工作，不擴張成全部模型、帳號、瀏覽器流程或人工驗收。本次版本收尾未連 ASUS，未刷新主機健康、部署 revision 或官方餘額；引用時須保留證據日期與範圍。
