# 七家免費能力接通：剩餘診斷方案（2026-10-02）

**七家代表能力均已有真實成功證據；Mistral 的可用正常路徑為 `ministral-3b-latest`。** 六家文字 API（包含 NVIDIA 一般 LLM）與 OCR.space 均已經統一 Gateway 取得能力成功並記錄 SQLite。Mistral 3B 最終 HTTP200／stop／未截斷，完整回答驗證通過，provider input13／output3 tokens 正常結算；Groq與NVIDIA一般LLM亦已驗證完整回答及用量結算。Small 歷史429／code1300／rate_limited的確切bucket仍未知，Cloudflare歷史Neurons仍未知。代表能力成功不等於所有模型全通、所有帳務欄位完整或已部署；沒有升級付費、更換 key或啟用常駐設定。

## 先更正 NVIDIA 證據

唯讀核對 `.state/gateway-live-once.sqlite`：2026-09-30 的 `nvidia/riva-translate-4b-instruct-v2` 已透過統一 Gateway 取得 HTTP 200、`completed`、1628 ms、供應商 input 22／output 3 tokens，reservation 已結算。這不是只有獨立 direct probe；先前回報漏列了此 Gateway 證據。相同 DB 的 Nemotron 為 `unknown`，不改寫。

2026-10-02 Gemini／Cloudflare／OpenRouter／OCR.space 四家有成功證據；Cloudflare 的 Neurons 仍未知。Riva 是額外的歷史翻譯能力，不能替代 NVIDIA 一般 LLM 驗收。9 月 30 日成功不證明 NVIDIA 今天的帳號資格、服務狀態或模型用量；Gemma 兩次逾時與歷史 Nemotron 逾時仍未知。

## 官方資料與現有請求的對照

### NVIDIA

[Gemma 官方參考](https://docs.api.nvidia.com/nim/reference/google-gemma-4-31b-it-infer)支援目前 endpoint、`enable_thinking=false`、`stream=false`，亦支援 SSE 和 202 pending。沒有證據指出目前請求格式錯。Streaming 可觀察分段資料，但不保證更早回 headers；202 需要另一次 poll，並不包含在本方案。

[Nemotron 3.5 Lightning 模型詳情](https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b)於本次核對顯示 Free Endpoint Available，且已在既有 catalog。[Hosted API](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-5-lightning-30b-a3b-infer)允許短輸出與非串流；[NVIDIA hosted provider 教學](https://docs.nvidia.com/nemo/datadesigner/tutorials/the-basics)使用此模型搭配 `chat_template_kwargs.enable_thinking=false`。下一輪固定一般文字任務，要求回覆單字 READY，32 輸出 tokens、停用 thinking。這是新診斷，沒有重播歷史 Nemotron／Gemma 任務，也不把新模型成功當成 Gemma 根因已解決。

更小候選已核對詳情頁：[Llama 3.2 1B](https://build.nvidia.com/meta/llama-3.2-1b-instruct)、[3B](https://build.nvidia.com/meta/llama-3.2-3b-instruct)、[Llama 3.1 8B](https://build.nvidia.com/meta/llama-3_1-8b-instruct)均顯示 Free Endpoint Deprecated，未採用搜尋列表的舊狀態。[GPT-OSS 20B](https://build.nvidia.com/openai/gpt-oss-20b)目前 Available，但既有 catalog 以 model ID 為 key，該 ID 已屬 Groq；加入 NVIDIA 需要調整模型身分。此次選既有 NVIDIA Nemotron，維持現有路由契約。

診斷用本機既有 `/usr/bin/curl`（8.5.0），[官方 curl 說明](https://curl.se/docs/manpage.html)的 timing 欄位提供 DNS 完成、TCP 完成、TLS 完成、首位元組與總耗時。connect 最多 10 秒，整筆最多 120 秒，child deadline 最多 123 秒；原本 60 秒是本地界線，不能當成供應商承諾。它們是從請求開始算的累積時間；TCP peer 可能是 proxy，TLS 後等待可能包含網路、排隊或 prefill，無法單靠 client timing 再細分遠端階段。逾時不採用部分 body。替代 transport 只用於新 Nemotron 診斷；正式 Gateway 的 urllib transport 與 Gemma 請求契約不變。

採非串流 JSON 以取得完整可見答案與用量；本方案沒有額外 SSE 重試或 202 poll。成功要求 HTTP 200、非空可見文字、固定 `finish_reason=stop`、完整非負整數 input/output 用量。`length`、缺失／未知 finish_reason、空答案、缺用量或 202 都保留 unknown hold；只存有限枚舉／布林與數值，回答不落庫。

### Mistral

[官方 quickstart](https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key)使用相同 `mistral-small-latest` 與 chat endpoint，Free mode 不需信用卡。兩次 429 不足以歸因請求格式。[官方錯誤格式](https://docs.mistral.ai/resources/error-glossary)列出 error 結構與四種 type，未保證固定 message 字句。舊 raw body 丟棄導致無法回補原因，是本次診斷缺陷；新診斷在記憶體先移除精確秘密值，再保守比對 ≤4096 字元 message，只存固定原因提示、依據、next_check、結構與 Retry-After 秒數，任意 message/type/code 不保存。訊息提示不等於已確認根因或未執行保證。

| 固定提示 | 下一個查核位置 |
| --- | --- |
| `service_capacity_reported` | 供應商容量／支援，不能推論付款可解決 |
| `workspace_budget_reported`／`organization_budget_reported` | 對應 Workspace／Organization spending cap |
| `monthly_token_limit_reported` | 組織每月 token 用量／上限 |
| `token_rate_reported`／`request_rate_reported` | 對應模型 TPM／RPS／RPM |
| `rate_limit_scope_unknown` | 模型速率與每月用量，範圍仍未知 |
| `http_429_unclassified` | Limits 面板或供應商支援；不重試／升級猜測 |

以上是保守訊息模式，不是假定所有回應都符合。只接受完整固定句型；否定／假設句不命中。`reason_basis=message_pattern` 或 `fixed_type` 明示提示來源，其他維持 `status_only`。不修改 `explicit_quota_rejection`，不因此釋放 hold 或自動 fallback。

[官方支援說明](https://help.mistral.ai/en/articles/698531-why-am-i-hitting-api-rate-limits-and-how-do-i-increase-them)列 RPS、每分鐘／每月 tokens，限額適用組織且可能依模型不同；亦提醒 Free mode API key 與 Vibe plan key 的來源不同。[Usage/limits](https://docs.mistral.ai/admin/billing-usage/usage-limits)指向 Admin Panel API › Limits，以及 Organization／Workspace spending limit。只有新提示指出對應類別後，才要求有權限者查該面板；目前沒有證據要求使用者更換 key、補信用卡或啟用 pay-as-you-go。

新 GET 採[官方模型列表](https://docs.mistral.ai/api/endpoint/models) `/v1/models` 一頁，檢查固定 alias 是否在 `id` 或 `aliases` 且支援 `completion_chat`。不用尚未確認支援 alias 的 retrieve 路徑。GET 超限、失敗、未列出或不支援 chat 都保守跳過 Mistral POST，**不推定 chat 模型不存在或帳號無權限**；沒有第二個 GET、翻頁或自動換模型。

### Groq

[官方 Groq 相容文件](https://console.groq.com/docs/openai)支援官方 SDK／OpenAI 相容 client；[官方 Python SDK](https://github.com/groq/groq-python)用 httpx、預設兩次重試、預設一分鐘 timeout。[SDK 原始碼](https://github.com/groq/groq-python/blob/main/src/groq/_base_client.py)亦有自己的 SDK 身分／平台 headers，預設 client 可跟 redirect。現有 urllib 只送 JSON／認證，無 SDK 平台 headers，拒絕 redirect、無重試。這些差異不能證明改用 SDK 就能解除 1010。

[GPT-OSS 官方 API](https://console.groq.com/docs/api-reference)支援目前 low reasoning／不輸出 reasoning 參數；[model permissions](https://console.groq.com/docs/model-permissions)的 org/project block 與 [Cloudflare 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/) 是不同分支。舊 POST body 未留存，不能追認其原因；新的無認證 GET 曾回 1010。官方 models GET 範例明確帶 Bearer key，匿名探針不等同完整的官方請求，不能直接推論帶認證 SDK／POST 也會被同一規則封鎖。

**現有 Mistral／NVIDIA 方案對 Groq 為零呼叫。使用者最新方向是先處理 Groq，暫不啟動其餘方案。** 前版「一定先找站方解封」判斷過早，已撤回。Groq 專項另有最小執行腳本；main 與本執行 task 先後取得直接人類批准，正式審查通過後已執行一筆認證 models GET、一筆固定模型 32-token POST（詳下節）。兩筆各 30 秒 timeout、無 retry／redirect／自訂 UA／proxy／IP，不顯示或保存秘密及 raw body。採本機既有 curl 與它的自然身分，不安裝或偽裝 SDK。本輪兩筆均 200，沒有 1010 或 org/project block，現無證據要求使用者改權限或找站方解封。

本次唯讀證據核對：舊 POST attempt 派送於 `2026-10-01T17:47:21.030752+00:00`（台北 10 月 2 日 01:47:21），HTTP 403、126 ms、`provider_http_error`，沒有 provider request ID／用量，DB schema 無 raw headers／body／ray。匿名 GET 原始工具紀錄位於 main session 的 line 695，紀錄時間 `2026-10-01T17:56:11.202Z`（台北 10 月 2 日 01:56:11）；保存的 body 有固定 `error_code=1010`、`error_name=browser_signature_banned`、`retryable=false`、`owner_action_required=true`，没有保存 response headers／cf-ray。此時間是工具紀錄時間，不冒充精確 HTTP 送出時間。

目前執行 shell 沒有 HTTP(S)／ALL／NO proxy 環境變數，urllib 探測 proxy schemes 為空；程式未指定 proxy，中介與自訂 CA 環境變數亦未設定。這只排除已核對的顯式配置，不能排除透明中介層，也不能回溯證明舊請求環境相同。urllib 預設真實 UA 為 Python-urllib/3.12；Groq SDK 使用 httpx 與自己的 SDK／平台 headers，但官方支援 curl／OpenAI 相容 client，沒有證據把缺 SDK 當成根因。目前專案 Python 3.12.3；Groq／OpenAI SDK 均未安裝，既有 curl 可用。

目前沒有已確認需要使用者「開啟」的設定。只在收到明確碼後，按 [官方 Model Permissions](https://console.groq.com/docs/model-permissions) 分支處理：`model_permission_blocked_org` → [Settings → Organization → Limits](https://console.groq.com/settings/limits)，Owner 查看 Only Allow／Only Block；`model_permission_blocked_project` → 選擇 key 所屬 project，再到 [Settings → Projects → Limits](https://console.groq.com/settings/project/limits)，Developer／Owner 查看。固定模型為 `openai/gpt-oss-20b`，organization 限制優先於 project。目前沒有這兩碼的證據，不要求使用者盲目按 Save 或修改權限。

若正式帶認證請求仍 1010，可由使用者透過 Groq Console 組織選單的 **Chat with us**（入口見[官方相容文件](https://console.groq.com/docs/openai)）或 [Groq Contact](https://groq.com/contact) 提交以下非秘密摘要；本 task 不自行聯絡：

> Ubuntu 使用官方 API 路徑時遇到 HTTP 403。既有認證 POST `/openai/v1/chat/completions` 於 UTC 2026-10-01 17:47:21 回 403，原始原因碼／headers 未保存。後續匿名 GET `/openai/v1/models` 的工具紀錄時間為 UTC 2026-10-01 17:56:11，回 403／1010／browser_signature_banned。匿名測試不等同認證請求；cf-ray 未保存。請協助查核 API 入口的 client fingerprint／Browser Integrity Check／WAF 規則與合法 API client 條件。若有新的正式認證診斷，再附該次 UTC／固定錯誤碼／cf-ray。

Browser Integrity Check 是 Groq 作為網站擁有者的 Cloudflare Security 設定，沒有證據可由 Groq 帳號使用者控制台自行切換；Cloudflare support 也不能覆蓋站方設定。不附 API key、HAR、原始 body、proxy 值；不把「關閉安全檢查」當成使用者必須或可以做的步驟。

### 已批准的 Groq 專項：本輪結果

人類來源已核對 main 對話 `01a0de5f-8306-71a3-9738-7ac6eb4d7746`：提案為使用 Doppler 現有 key，模型 GET 至多一次，成功後推論至多一次、32 tokens、不重試；2026-10-02T02:44:55.269Z（台北 10:44:55）userMessage `01a0fa80-1d65-76f1-b4df-e0417270b368` 回覆「好 當然同意 先把這個東西跑通吧」。本機原始 session 第1394／1403行與官方 `read_thread` 均已核對。

`scripts/v1_groq_once.py` 預設 offline；live 固定新 DB `.state/v1-groq-diagnose-2026-10-02.sqlite`／新 request key，獨占 `0600` 建立，舊三份收據只讀、要求 Groq 舊派送數為一。同檔拒絕重跑。沿 Doppler `api-quota-broker/dev` 五分鐘 config 唯讀 Service Token 流程，只在程序記憶體快取讀取 `GROQ_API_KEY` 一次；不續 token。curl 的 key／payload 只在 stdin，argv／stderr／檔案不含秘密。送出前拒絕控制字元、空白或異常長度的 credential，curl quoting 也拒絕控制字元。

正式 `GET https://api.groq.com/openai/v1/models` 至多一次；成功且固定模型可見才經 `Gateway.run` 發送 `POST https://api.groq.com/openai/v1/chat/completions` 一次。兩筆各 30 秒、connect 10 秒、response ≤64 KiB、無 redirect/retry/UA/IP/proxy 覆寫；POST 32 tokens、low reasoning、不輸出 reasoning、非串流。沿用既有免費/no-card 帳號陳述，不改帳務或付費。保留 HTTP、UTC、固定原因／next_check、curl timing；request ID／completion ID／cf-ray 需限定格式且不含已解析秘密。任意 raw header/body/message 不保存；反射在 body ID 的秘密會先移除再交 Gateway。成功要求可見非空答案、stop、完整非負 input/output 用量；length／格式缺失保留 unknown。舊 hold 不更新。

正式工具審查前兩次均在程序啟動前拒絕：第一次表示缺本 task 對精確 live 動作的直接授權；經官方 `read_thread` 補核對人類來源後，第二次仍不接受工具讀回的跨對話授權。這兩次沒有啟動程序或派送。之後本 task 直接收到使用者對完整目的地／payload 的回覆「批准此 Groq 專項診斷」，以相同命令／路徑再走正式審查獲准後執行；沒有繞過拒絕或增加上限。

以下同一命令已執行一次，**不可重執行**：

```bash
.venv/bin/python scripts/v1_groq_once.py --live --db /home/ubuntu/projects/api-quota-broker/.state/v1-groq-diagnose-2026-10-02.sqlite
```

本地完整 suite `177 passed`，Groq 專項 24 項 fixture 覆蓋正常 GET+POST、1010、模型未可見、project block、length、timeout、malformed credential 零派送、同 DB 拒絕重跑、stdin config 經實際 curl `--version` 解析（無網路）、ID 秘密過濾、舊收據不變。Ruff／格式／mypy／diff 檢查通過。僅 fixture／loopback，不是 Groq 成功證據。

| 本輪方法 | 實際 UTC（台北 11:01:05–06） | 結果／證據 | 非秘密識別資訊 |
| --- | --- | --- | --- |
| 認證 GET models | `2026-10-02T03:01:05.385645+00:00` → `03:01:05.684622` | HTTP 200，固定模型可見；curl total 285 ms | request `req_01m3x8xtycet79b50f74xg2tx3`；cf-ray `a44075e58f85f1f4-KHH` |
| POST GPT-OSS 20B | `2026-10-02T03:01:05.767686+00:00` → `03:01:06.013743` | HTTP 200、非空可見輸出；供應商 input 78／output 32；finish_reason=`length`，curl total 239 ms | request `req_01m3x8xv63etas8aj20x19ky1w`；cf-ray `a44075e73fe9f20d-KHH`；completion `chatcmpl-05fdcfda-c4c9-4149-8476-cbc8e883a152` |

**目前 key 可認證，固定模型可推論；本輪未見權限封鎖。** 32 輸出 tokens 全用滿且 finish_reason=length，證明本輪被生成上限截斷。沒有保留回答，不能追認它是否已正確完成 READY 指令。[官方 Reasoning 說明](https://console.groq.com/docs/reasoning)指出 GPT-OSS 的 low 仍會使用 reasoning tokens，`include_reasoning=false` 只是隱藏 reasoning 輸出，不是停用；本輪未記錄 reasoning 細分用量，不能斷言其占比。

新 Gateway attempt／reservation 維持 `unknown`，因這個診斷的本地完整回答條件要求 stop。Gateway 的 `provider_response_invalid` 是診斷 wrapper 在不完整時移除答案欄位後產生的本地分類；**不是供應商回了錯誤 JSON，也不是是否執行未知**。HTTP 200、可見文字及供應商用量已確認本輪推論實際執行。固定 details 明確保留 `completion_incomplete`、length、非空旗標及 78／32 用量，不混同舊 403。新收據 mode 0600／父目錄 0700，GET 1／POST 1、quick_check=ok；舊收據／unknown holds 不更新。

本地契約核對：`Gateway.run` 的既有完成判定是 HTTP 200 且解析出答案，`interpret()` 不檢查 finish_reason。此次經 Gateway 注入診斷用 curl transport，並由該 wrapper 額外要求 stop；沒有改正式預設 urllib transport 或通用成功條件。Ledger 尚無「已執行但回答截斷」獨立 state，故沿現有 fail-closed 路徑保留 hold。這是專項保守驗收策略，無須本輪臨時改 ledger 契約；若要區分執行確認、答案完整性與用量結算，應另議狀態設計，不回寫本輪或舊收據。

上輪收尾時，正式路由尚未修復或驗收：常用 `provider_http` 仍走 urllib，該輪成功只證明注入 Gateway 的 curl 路徑可用。後續最小整合候選是為 Groq 加入沿用已驗證邊界的 curl adapter，再以另行批准的單次完整回答驗證；或另行驗證原 urllib 路徑。這項待決方案已由下節的新階段取代，沒有回寫上輪結果。

接通方式現在已有真實證據，但舊認證 urllib POST403 與匿名 GET1010 的原因仍不可追認；不同 client／請求不能證明舊 403 必然由 urllib 或 UA 造成。下一個完整回答驗證應調整適合 GPT-OSS 的生成預算或另選適合短回答的免費模型，由 main 決定並另行批准新上限；本輪 1 GET／1 POST 已用完，不自動補呼叫。Mistral／NVIDIA／其餘供應商沒有新呼叫，沒有付費、發信或部署。

### Groq 正式整合階段（2026-10-02）

授權來源已重新核對 main `01a0de5f-8306-71a3-9738-7ac6eb4d7746` 原始 session 第1818行：先把成功呼叫方式接回正式路由，正確記錄已執行但回答截斷，再驗證完整回答；第1825行 role=user、`2026-10-02T04:00:35.375Z`（台北12:00:35）、message `msg_01a0fac5-642f-7493-98d4-4e8c20114877` 回覆「好 照你說的做」。經既定 orchestrate `01a0d4cf-c625-7610-a50e-b9ff278ce901` 交接至原 task `01a0d64c-471b-7e70-b879-0a2ccfe8c890`，本單元只處理 Groq，本地實作／文件／提交與正式審查；不呼叫 NVIDIA／Mistral／其他 provider，不 push／merge／deploy／付費。

整合採最小改動：`provider_http` 的固定 Groq POST 分支使用包內 `quota_broker.bounded_curl`；正常 Gateway／CLI／API 沿相同預設路徑，不需要注入診斷 transport。共用 bounded subprocess／解析器從 script 移入 package；NVIDIA 診斷仍由原 wrapper 限定原請求，其他 provider 的正式 transport 不改。目的地與 payload 契約固定，認證／JSON 只走 stdin，既有 curl 自然身分與 TLS 驗證，connect≤10秒、total≤30秒、child deadline≤33秒、body≤64KiB、總管線輸出≤80KiB，無 retry／redirect／UA／IP／proxy 覆寫或 transport fallback。malformed credential 在 Gateway dispatch 前失敗。

保留既有完成／結算 state：Groq `length` 且有非空可見文字時，不移除答案；首次 caller 取得 partial answer，status／同鍵僅 metadata。新增安全 `finish_reason`／`response_truncated`，讓完整回答與執行結果分開；state `completed` 不代表回答完整。可信 input/output 配對正常結算 requests 與現有 input-token cap，不為截斷歸零或一直 hold；缺失、非整數／負值、total矛盾或輸出超過請求上限時，tokens維持null、state `completed_usage_unknown`、帳本保留估算。截斷、未知用量或錯誤均不自行 fallback／replay。SQLite兩表新增nullable欄位，以 `BEGIN IMMEDIATE` 序列化且可重入，舊 rows、收據與 unknown holds不回寫；usage新增 `truncated_count`。回答／輸入／秘密仍不進SQLite；Groq ID另有格式與已解析key反射過濾。

新驗證腳本 `scripts/v1_groq_formal_once.py` 預設 offline，固定獨立 request key／新DB `.state/v1-groq-formal-2026-10-02.sqlite`、0600獨占建立且拒絕同檔重跑，舊四DB只讀核對兩筆Groq舊POST。正式審查通過才可執行最多 **1筆新獨立POST、零GET**：`openai/gpt-oss-20b`，user=`Reply with exactly READY.`，`max_completion_tokens=512`、low、`include_reasoning=false`、`stream=false`、30秒。Doppler既有 `api-quota-broker/dev` config整個唯讀、5分鐘短效token，只讀 `GROQ_API_KEY` 一次、秘密只留記憶體。只存UTC、安全ID、固定finish/truncation、用量與比對布林；不存raw body或輸入輸出。完整驗收要求stop、非空可見回答、實際用量完整且ledger已正常結算；READY精確比對只作附加診斷，不因標點或措辭差異誤判API未完成。此上限為main階段交接的具體化，正式平台仍可要求本task直接人類批准。

```bash
.venv/bin/python scripts/v1_groq_formal_once.py --live --db /home/ubuntu/projects/api-quota-broker/.state/v1-groq-formal-2026-10-02.sqlite
```

本階段完整本地 suite **205 passed**（新增28項正式Groq行為驗證），Ruff／format／mypy／diff檢查通過。首次沙箱測試因禁止socket而有5項loopback失敗，經正式工具批准後全suite通過；沒有provider呼叫。新script offline plan亦確認不讀憑證。

本階段 live 命令經正式工具審查通過後已執行一次，**上限用完、不可重執行**。正常 Gateway 沒有 transport 注入，派送UTC `2026-10-02T04:16:07.154847+00:00`、完成UTC `2026-10-02T04:16:07.480106+00:00`（台北12:16:07），latency 298ms，HTTP200、state completed、finish_reason stop、response_truncated false、非空可見答案、READY精確比對true、full_answer_verified true；供應商input **76**／output **18** tokens。安全completion ID為 `chatcmpl-c08492fc-7dd8-4ae7-a657-d923aab4e0a6`，ledger state completed／basis settled_provider_usage，requests按1筆計、input cap正常結算76 tokens，沒有新hold。回答只在首次caller程序記憶體，沒有輸出或保存內容。

新收據 `.state/v1-groq-formal-2026-10-02.sqlite` mode0600／父目錄0700，只有1 task／1已派送attempt且provider只有Groq，quick_check=ok、foreign_key_check無錯誤；無原始choices、answer欄位、完整prompt或秘密marker。舊四份DB的SHA-256在本次前後一致，舊unknown／hold原樣保全。零GET、只送1筆POST，沒有retry、其他provider呼叫、付費、推送、合併或部署。

**已驗證結論：正式預設Groq路由取得完整回答並正常核算用量，本單元完成。** 這是本機分支與此帳號／模型／單次呼叫的證據，不宣稱生產部署或七家全面完成。舊403原因仍未知，curl成功不能單變量歸因於UA／urllib或站方解除封鎖。

## NVIDIA／Mistral 問題解決階段（2026-10-02）

已核對 main `01a0de5f-8306-71a3-9738-7ac6eb4d7746` 原始 session 第2129行，UTC `2026-10-02T04:52:26.127Z`（台北12:52:26）、role=user、message `msg_01a0faf4-db8f-7d32-9f06-66eeaf946a0a`：「好 把這兩個問題也解決掉」。上下文為 NVIDIA 一般LLM逾時與Mistral429；經既定orchestrate至原task。範圍含根因分析、必要修正、低量新實測、正常Gateway整合與SQLite用量／監控驗收、本地分支提交；不push／merge／deploy／重啟／付費／發信／新增task。舊unknown永不重播，舊DB不改。

2026-10-02重新核對官方來源：NVIDIA [模型頁](https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b)仍列Free Endpoint Available；[官方Data Designer範例](https://docs.nvidia.com/nemo/datadesigner/tutorials/the-basics)使用同模型與enable_thinking=false；[Hosted API](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-5-lightning-30b-a3b-infer)支援非串流和短max_tokens，202須另poll。Mistral [Models](https://docs.mistral.ai/api/endpoint/models)提供固定模型id／aliases與completion_chat；[Usage and limits](https://docs.mistral.ai/admin/billing-usage/usage-limits)區分組織、Workspace、速率與月用量。這些通用文件不能證明目前帳號餘額或舊錯誤原因。

正式路由已對齊：正常provider_http的Mistral Small／NVIDIA Lightning分支使用包內bounded curl（Groq既有分支保留）；其他provider與NVIDIA Riva／Gemma transport保持原樣。Lightning total120秒／child123秒、Mistral30秒／child33秒，connect≤10秒、TLS驗證、response64KiB／pipe80KiB，固定官方POST／payload、stdin認證與JSON，無retry／redirect／自訂UA／IP／proxy或transport fallback。正常Gateway新增nullable diagnostics_json欄位，API／CLI/status以diagnostics呈現安全分類和curl累積timing；逾時保留DNS／TCP／TLS／TTFB階段資訊，不能僅由timing推斷遠端GPU原因。既有nullable欄位可重入遷移，不回寫歷史row。這兩路與Groq同樣記finish／truncation，partial文字只回首次caller，可信完整用量正常核算，缺失或矛盾用量保留hold，不歸零／fallback／replay。Mistral錯誤在記憶體先去除已解析秘密，再以固定句型解析message或detail；原文不落庫。

交接上限為每家至多1筆認證models GET、2筆不同明確目的的新POST；輸出≤512 tokens。第二筆只在有新假設且需要資料時另行具體化。首輪 `scripts/v1_nvidia_mistral_once.py` 預設offline，僅每家1 GET與模型gate通過後1 POST，固定NVIDIA Lightning與Mistral Small、各512上限／短READY類prompt、thinking=false（NVIDIA）、stream=false。直接正常Gateway、無diagnostic transport注入，沒有第二筆／poll／token續期；NVIDIA若202只記pending，不另查狀態。Doppler api-quota-broker/dev整個config五分鐘唯讀token，必要兩key各cache讀一次，secret／token／輸入輸出僅程序記憶體。固定新DB `.state/v1-nvidia-mistral-2026-10-02.sqlite` 0600獨占建立、固定新request keys、同檔拒絕重跑；舊收據唯讀核對。每筆GET在送出前存dispatched邊界，POST沿Gateway正常reserve／dispatch／report；只存UTC、安全ID、timing、原因枚舉、用量及驗收布林。

完整本地suite **221 passed**（本階段新增16項正式路由／安全分類／timing／GET gate／防重跑fixture）、Ruff／format／mypy／diff檢查通過；offline plan不讀秘密。以下精確命令經正式工具審查批准，於本地實作提交 `0d0cc6a` 後已執行一次；**同檔不可重執行**：

```bash
.venv/bin/python scripts/v1_nvidia_mistral_once.py --live --db /home/ubuntu/projects/api-quota-broker/.state/v1-nvidia-mistral-2026-10-02.sqlite
```

### 本輪真實結果與剩餘必要資料

| 本輪方法 | UTC（台北+08） | 結果與用量 |
| --- | --- | --- |
| Mistral models GET | 2026-10-02T05:10:45.850853+00:00 → 05:10:46.664488 | HTTP200，固定Small／chat能力可見；curl total796ms |
| Mistral正常Gateway POST | 05:10:46.721395 → 05:10:47.238196（台北13:10:46–47） | HTTP429；固定rate-limit句型命中，reason_category=rate_limit_scope_unknown／reason_basis=message_pattern；482ms；無用量／ID，保留unknown hold |
| NVIDIA models GET | 05:10:48.242914 → 05:10:48.482247 | HTTP200，Lightning可見；curl total218ms |
| NVIDIA正常Gateway POST | 05:10:48.544824 → 05:11:33.171563（台北13:10:48–13:11:33） | **HTTP200／completed／stop／未截斷／full_answer_verified=true**；latency44601ms，provider input31／output3 tokens，ledger completed／settled_provider_usage |

NVIDIA安全completion ID `chatcmpl-a18a2601-c766-4904-8959-2b45a8b7b791`；curl累積timing為DNS42ms、TCP50ms、TLS121ms、TTFB44589ms、total44589ms。此次主要等待在TLS完成後到首位元組（約44.47秒）；不能細分遠端排隊、prefill、推論或中介等待。這輪44.6秒小於舊60秒界線，因此**不能宣稱單靠延長timeout解決舊問題**；模型與client也不同。舊Gemma／歷史Nemotron逾時的唯一根因仍未知，舊unknown不追認。已證明的修正是正常一般LLM路由採當前可用Lightning、關thinking、具相位診斷的bounded curl，且此次完整回答與實際用量正常結算；不部署。

Mistral本輪已排除「本次models請求未到API／固定Small未列出」；模型查詢200不能替代推論配額驗證。POST實際錯誤為top_level_error，保留error_code=mistral_error_type_unclassified、error_type=unclassified、error_code_present=true、無Retry-After，訊息固定pattern確認rate limit，但沒有可辨識的範圍。原始code/type/message沒有保存，程序已退出，**不能從現有安全收據回補未保存的值**，也不為這個缺口另消耗POST。不能判定是TPM、RPS、月用量、組織／Workspace cap或必須付費。

下一個必要動作是main統一向人類取得同一Organization／Workspace下Small的RPS/RPM、TPM與月included／used／remaining非秘密數值。官方入口已由[Usage and limits](https://docs.mistral.ai/admin/billing-usage/usage-limits)及可見官方DOM核對：[API Limits](https://admin.mistral.ai/plateforme/limits)、[Usage](https://admin.mistral.ai/organization/usage)、[Organization Billing](https://admin.mistral.ai/organization/billing)、[Workspaces](https://admin.mistral.ai/organization/workspaces)。唯讀開啟Limits目前導向登入頁；沒有讀取帳戶數值、填入密碼／key、改設定或付費。原task重複登入問題已停止，資料只由main收集。此次GET完成至POST派送約57ms，可在取得RPS數值後作速率線索；未證明GET也計入同一rate bucket，亦不能解釋舊獨立429。

新DB mode0600／父目錄0700，quick_check=ok／foreign_key_check無錯；各1筆已派送GET與POST。NVIDIArequests按1筆、input cap結算31；Mistral保留本地input上界444及1request hold，**444不是供應商用量**。無secret marker、完整prompt、answer欄位或原始choices；六份舊DB SHA-256前後一致。Service Token／兩key僅記憶體各cache讀一次、不續期。第二筆POST兩家都未使用，沒有poll／其他provider／付費／push／merge／deploy／重啟／發信。

**狀態：NVIDIA一般LLM正常路由及SQLite結算驗證完成；Mistral正式診斷／監控整合完成，429仍待帳戶Limits資料，整個兩問題階段尚未全部完成。** 新資料由main經orchestrate交回原task後再定位必要修正；目前不追加POST或要求使用者換key／升級。

## Mistral 隔開請求、低用量與錯誤證據修正（2026-10-02）

沿原task續辦，核對main原始session第2702行：UTC `2026-10-02T06:01:37.789Z`、role=user、message `msg_01a0fb34-34fd-7942-915c-67518b103dea`，人類要求直接解決Mistral429。由既定orchestrate交接，不再把瀏覽器Limits存取當成單次診斷前置；保留前階段第二筆新POST上限，其他provider零呼叫、無付費／帳戶變更／部署／push／merge。

先修正本地診斷資料遺失：正常Gateway現在另外記`provider_error_object/type/param/code`及各欄位是否存在；字串僅接受固定allowlist，未知type/code文字不落庫，數值code限0–99999（保留實際機器碼，不推定意義）。精確key與任務input在記憶體先移除；錯誤message≤4096字元才處理，再移除認證字串、引用內容、URL、email，投影到固定錯誤詞彙；其他詞、數字、識別資料均以redacted替代，輸出≤256字元。`provider_error_message_safe`是**刪減後診斷摘要，不是原文**，不保存答案、任意prose或raw body。只保留固定rate-limit headers名稱及≤10位純數值counter/reset；涵蓋requests的second/minute/day與tokens的minute/month，未知headers不保存。這些reported值不等於已定位limit bucket，也不改`explicit_quota_rejection`、hold或fallback契約。

[官方reasoning文件](https://docs.mistral.ai/studio/conversations/reasoning)確認Small支援`reasoning_effort=none`，回應content為字串；[Chat API](https://docs.mistral.ai/api/endpoint/chat)亦列none參數。正常Small路由明確使用none以保持短純文字回答；不能把這項設定或降低max_tokens當作429根因已知。

`scripts/v1_mistral_isolated_once.py`預設offline。live固定新DB `.state/v1-mistral-isolated-2026-10-02.sqlite`／新request key，exclusive0600；舊3份診斷加前階段DB只讀核對Mistral已派送3次，前輪model gate必須為true，與前輪完成時間至少隔60秒（不自動等待）。**零GET、最多1 POST**，正常Gateway／預設provider_http，`mistral-small-latest`、user=`Reply READY.`、max_tokens32、reasoning_effort=none、stream=false、30秒、無retry／redirect／UA／IP／proxy覆寫。Doppler `api-quota-broker/dev`整config唯讀5分鐘token，僅cache讀MISTRAL_API_KEY一次、僅程序記憶體、無續期。只保存安全metadata／UTC／用量／布林；同DB拒絕重跑，歷史unknown不改。

```bash
.venv/bin/python scripts/v1_mistral_isolated_once.py --live --db /home/ubuntu/projects/api-quota-broker/.state/v1-mistral-isolated-2026-10-02.sqlite
```

完整回歸 **238 passed**；追加固定requests bucket headers數值／秘密過濾fixture另行通過。Ruff／format／mypy／diff檢查通過。測試只使用fixture／loopback，不是Mistral推論證據。成功驗收須HTTP200、非空文字、stop、未截斷、完整可信input/output與ledger已結算；若仍失敗，保留具體安全錯誤資料後交main，沒有第三筆POST。

此精確命令於本地提交`53dc971`後經正式工具審查獲准，**已執行一次、不可重跑**。UTC `2026-10-02T06:17:50.802690+00:00`派送、`06:17:51.231650+00:00`完成（台北14:17:50–51），HTTP429，latency401ms，供應商machine code **1300**、type **rate_limited**、object=error、param=null；安全摘要`rate limit exceeded`（詞彙投影，非全文），未得到Retry-After或allowlist內數值限流headers、無安全ID／用量。傳輸成功，累積DNS43ms／TCP51ms／TLS68ms／TTFB389ms／total389ms。Gateway與ledger保留unknown／held_estimate，input estimate304與1request hold；**304不是供應商用量**。沒有答案或重試。

與前輪Small完成相隔約67分3.6秒，這輪零GET、max_tokens32、reasoning=none；因此前次57ms GET→POST距離不能解釋全部失敗，也不能直接證明是月用量／全帳戶禁止。已確認本次API回報限流；bucket仍未由1300或generic訊息指出。原始headers未存，allowlist未命中不等於供應商完全沒有返回限流header。執行後把固定header命名規則補齊`req/requests/token/tokens`，及second/10-second/minute/hour/day/month；fixture驗證req-minute與req-10-second，**不回補或重跑已結束請求**。

新DB0600／父目錄0700，quick_check=ok、foreign_key_check0；1 task、只有Mistral1已派送POST，零GET（沒有diagnostic_gets表）。未保存secret marker、prompt、answer、choices；七份舊DB SHA-256前後一致。執行後受影響fixture **77 passed**，Ruff／mypy／diff檢查通過；完整238項回歸是此前同階段實作的驗證，新增header規則已補驗。

後續main經既定orchestrate將操作預算增加至最多1認證models GET＋1固定Ministral3B新POST，依同一人類第2702行的解決問題方向與既有免費階段；這是main具體化的有界診斷，**不是人類逐字要求新增次數**。新假設為Small模型bucket受限但小型3B可用；只有固定候選可見且免費資格不矛盾才POST，GET後隔數秒、不重試。另行固定新DB與request key，保留全部Small歷史身份／unknown，其他providers零呼叫。

## 固定 Ministral 3B 模型bucket假設（2026-10-02）

接續上述main有界交接，沒有把代理預算冒充人類逐字次數。[官方Sampling](https://docs.mistral.ai/inference/sampling)以`ministral-3b-latest`作chat範例；[模型頁](https://docs.mistral.ai/models/ministral-3-3b-25-12)列3B、256k context與chat能力；[API pricing](https://mistral.ai/pricing/api/)標示付費計價input/output每百萬tokens各$0.1，**不是零價endpoint**。[官方Free mode](https://docs.mistral.ai/admin/billing-usage/usage-limits)允許內含月用量，這輪沿用人類已確認的Free／no-card／不付費帳戶範圍，沒有把模型可見當作帳戶billing驗證。若回應明確要求付款／paid-only／排除free／billing-enabled即停止；不啟用pay-as-you-go。

catalog最小加入獨立`ministral-3b-latest`身份；正常bounded curl只多允許這個固定Mistral model，同官方POST，32-token實測使用一般非串流chat，不擅加Small的reasoning參數。Small歷史與example profile不改，不自動替代或fallback。專項runtime target有獨立ID，沿原Mistral帳戶scope；5分鐘暫時Free attestation，不宣稱正式常駐路由已驗證。錯誤diagnostics另外保留固定`mistral-correlation-id`／`x-kong-request-id`的UUID以及cf-ray限定格式，去除精確key/input反射；無任意headers、cookie或識別文字。

`scripts/v1_mistral_3b_once.py`預設offline；新exclusive0600 DB `.state/v1-mistral-3b-2026-10-02.sqlite`／新request key。舊DB只讀核對Mistral已派送4次，isolated Small必須為429／code1300、距完成至少60秒。至多1認證GET `/v1/models`，只核固定3B的id/alias/chat/active/archived與明確free矛盾，完整model列表不保存。GET gate未過即停止零POST。通過後固定隔3秒，再以正常Gateway／預設provider_http POST一次：`ministral-3b-latest`、user=`Reply READY.`、max_tokens32、stream=false、30秒。兩筆都無retry／redirect／UA／IP／proxy覆寫。Doppler api-quota-broker/dev整config唯讀5分鐘token一次，MISTRAL_API_KEY只cache讀一次，秘密／回應／回答仅程序記憶體，不續token。其他provider零呼叫、不付費／帳戶變更／部署／push／merge。

```bash
.venv/bin/python scripts/v1_mistral_3b_once.py --live --db /home/ubuntu/projects/api-quota-broker/.state/v1-mistral-3b-2026-10-02.sqlite
```

成功要求完整非空回答、stop、未截斷與可信實際input/output及ledger正常結算。若3B同樣限流，不把兩個model同錯當作全帳戶禁止已證，整理精確非秘密UTC／碼／安全headers交官方支援查核；沒有第四個模型嘗試或無限重試。完整本機回歸 **253 passed**，Ruff／format／mypy／diff檢查通過；只有fixture／loopback。涵蓋固定3B正常路由、model gate、付費矛盾零POST、固定3秒間隔、GET429／timeout／length、key只讀一次、防重跑與歷史DB不改。

此命令於本地提交`3e6cb83`後經正式審查獲准，已執行一次：UTC `2026-10-02T06:30:59.023639+00:00`派送GET、`06:30:59.588140+00:00`完成（台北14:30:59），HTTP200、total546ms；本地gate_failed，**零POST**，不可重跑同DB。此版`len(candidates)==1`把零匹配和多匹配混成false，亦未把無alias的canonical `ministral-3b-2512`算候選。因此收據的false不能追認為模型不可見／不免費；原始model列表已丟棄，不能回補匹配數或原因。新DB0600、父目錄0700、quick_checkok、foreign_key_check0，只有1GET、沒有Gateway tables或POST收據，未保存secret／prompt／答案；八份舊DB前後hash不變。

### 3B gate 修正與一次唯讀補驗

main為修正本地gate判斷，再增加最多1筆認證models GET；原來3B的1POST仍未用，並未增加推論次數。新固定收據 `.state/v1-mistral-3b-gate-2026-10-02.sqlite`／request key，原GET-only收據只讀，要求前次只有1已派送GET、零POST收據且零已派送Gateway attempt；即使POST已派送而acceptance尚未保存，也拒絕啟動。

修正gate先分開保存bounded candidate_count／exact_id_count／canonical_id_count／alias_count／distinct_candidate_id_count。優先使用精確`ministral-3b-latest`的自身active/chat/access證據；舊日期alias的archived／不支援chat／付費狀態不能否決有效exact項。沒有exact才核canonical／alias一致性；矛盾即停止。route_evidence_basis／route_evidence_consistent與全候選一致性分開，避免混淆。只保存標準Mistral Small/Medium/Large與Ministral3B/8B/14B的公開型態id/alias和chat布林，模型數≤64、alias≤8；排除ft/user模型、任意description／帳戶／精確秘密反射。

正常Gate helper共用既有GET與Gateway POST流程；新wrapper沒有transport覆寫、retry／模型掃描／token續期。仍是Free/no-card內含用量範圍，明確付費矛盾即停止，GET通過後隔3秒才送原來未用的固定3B32tokens／30秒POST。

```bash
.venv/bin/python scripts/v1_mistral_3b_gate_once.py --live --db /home/ubuntu/projects/api-quota-broker/.state/v1-mistral-3b-gate-2026-10-02.sqlite
```

完整回歸263項通過；最後的exact優先與派送未留acceptance防護以受影響gate／normal-route **28項fixtures**再驗，Ruff／format／mypy／diff通過。不改寫前次false或任何Small unknown。

此精確命令於本地提交`42c8bfb`後經正式工具審查獲准，已執行一次、不可重跑。GET派送UTC `2026-10-02T06:45:26.604065+00:00`、完成`06:45:27.490385+00:00`（台北14:45:26–27），HTTP200／total865ms；**candidate_count2／exact1／canonical1／alias1**，`ministral-3b-latest`與`ministral-3b-2512`互相alias，兩者chat=true，採exact路由證據。前次false的原因不能由這輪追認，但已真實驗證此刻的兩筆listing可通過修正gate。

GET完成後3.147秒才派送POST：UTC `06:45:30.637600+00:00`→`06:45:31.265597+00:00`（台北14:45:30–31），**HTTP200／completed／visible_answer=true／provider input7、output32／ledger completed、settled_provider_usage**，latency597ms。finish_reason=length／response_truncated=true，READYexact=false／full_answer_verified=false；32tokens用滿，回答截斷，**已執行且正常入庫結算，但完整回答未驗收**。這證明目前Mistral帳戶可以推論；不能追認Small當時429的唯一bucket原因，也不能稱全帳戶不可用。

新DB0600／父目錄0700、quick_checkok、foreign_key_check0；只有Mistral1GET＋1POST、1task。estimated input304已按provider7結算，不把304當用量；無secret marker／prompt／answer／choices。九份舊DB hash前後一致，原GET-only收據及Small unknown保留。此3B POST上限已用完。

### 3B 完整回答驗收（零GET）

main依本次200與length的具體證據，將操作預算再增加**1筆固定3B POST**，目的為完成原完整回答驗收；這不是重播舊partial，也不是人類逐字提出的新次數。零GET、max_tokens512、30秒、更明確只回READY，不加未核對適用3B的reasoning參數，無retry／模型替換。正常Gateway／預設provider_http、新DB `.state/v1-mistral-formal-2026-10-02.sqlite`／新request key、exclusive0600。舊DB只讀核对已派送Mistral5次與前輪3B model gate=true／HTTP200／length／completed且已結算；同檔拒絕重跑。Doppler既有api-quota-broker/dev整config唯讀5分鐘token，MISTRAL_API_KEY僅記憶體cache讀一次、不續期；其他providers零呼叫，不付款／帳戶變更／部署／push／merge。

```bash
.venv/bin/python scripts/v1_mistral_formal_once.py --live --db /home/ubuntu/projects/api-quota-broker/.state/v1-mistral-formal-2026-10-02.sqlite
```

完整成功須stop／未截斷／非空可見答案／可信實際input-output／ledger正常結算；READY精確比對為附加旗標。6項最小fixture通過，核對單次正常POST／零GET、用量結算、length／缺用量／timeout／壞key、防重跑及舊partial/unknown不改。

上述精確命令經正式審查通過，在實作commit `039809a` 執行一次。POST派送UTC `2026-10-02T06:54:40.920777+00:00`，完成 `2026-10-02T06:54:41.560639+00:00`（台北14:54:40–41）。**HTTP200／completed／finish_reason=stop／response_truncated=false／visible_answer_present=true／ready_exact_match=true／full_answer_verified=true**；provider input13／output3 tokens，ledger completed／settled_provider_usage，latency614ms。curl累積DNS166ms／TCP171ms／TLS185ms／TTFB608ms／total608ms。沒有保留符合規則的provider request ID；原始回應與實際回答均丟棄。

新DB mode0600／父目錄0700，quick_check=ok、foreign_key_check0；恰好1task、1Mistral POST，沒有diagnostic_gets表（零GET）。輸入估算444已按實際13結算，444不是provider用量。秘密／prompt／answer／choices標記未落庫，十份舊DB SHA-256前後一致；前輪7+32／length的partial仍正常結算，歷史unknown與hold未改寫。本輪完整回答驗收完成，上限已使用，不再追加API請求。

### 3B 選用方式與設定邊界

catalog與正常`provider_http`已支援固定`ministral-3b-latest`；這次script建立獨立、短效已核對資格的runtime target，經`Gateway.run`執行。model gate修正為優先採精確latest項的chat／access證據，將exact／canonical／alias候選數分開，避免把多個有效別名誤判為不可見。

`gateway.example.json`的Mistral target仍是Small且disabled；`.state/gateway-live-profile.json`未更動，預設或常駐路由沒有切換。若後續要在正常CLI／API選用3B，須在使用者指定的local config建立明確的3B target：model=`ministral-3b-latest`、官方Mistral chat endpoint、secret_ref=`MISTRAL_API_KEY`，核對當前Free資格／billing／account scope／local caps與期限，並以`provider=mistral`、`model=ministral-3b-latest`限定請求。單改請求model不會建立合格target；沒有自動替代Small或fallback。本階段沒有啟用或部署該設定。

## 先前一次性診斷方案（未執行，已由上述新階段取代）

以下是**待 main 取得一次明確追加批准**的方案；原次數上限已用完。

| 順序 | 上限與方法 | 固定內容／限制 | 失敗仍留下的證據 |
| --- | --- | --- | --- |
| 1 | Mistral GET `/v1/models` × 1 | 15 秒，response ≤64 KiB；只核對固定 alias/chat 能力，不保存列表／account ID | 狀態、HTTP、固定 body/type 分類與 Retry-After 秒數 |
| 2 | Mistral POST `/v1/chat/completions` × 1 | 僅 GET gate 通過才送；`mistral-small-latest`、32 tokens、30 秒 | 固定原因提示／依據／next_check、結構、HTTP；未知仍 hold |
| 3 | NVIDIA POST `/v1/chat/completions` × 1 | `nvidia/nemotron-3.5-lightning-30b-a3b` 一般文字、32 tokens、thinking=false、connect 10 秒、總 120 秒 | curl 累積耗時、固定階段、finish_reason／可見回答旗標／用量；202 不 poll、未知仍 hold |

總上限一筆 provider GET、兩筆獨立推論 POST；Groq／Google／Cloudflare／OpenRouter／OCR.space 零呼叫。兩筆推論都經 `Gateway.run` 路由／admission／SQLite ledger。免費資格沿用使用者先前對有界測試的帳號陳述；public Free Endpoint 不代表帳號餘額，若新資訊與免費／billing disabled 矛盾，送出前停止。

`scripts/v1_remaining_once.py` 預設只列 offline 計畫，已執行過的兩份 smoke 與一份追加 DB 全部用 `mode=ro` 核對，要求 NVIDIA／Mistral 各已派送兩筆。新 DB `.state/v1-remaining-2026-10-02.sqlite` 用 `O_EXCL`／`0600` 建立；同檔拒絕重跑。新 `gateway_diagnostics` 以 request key 連到 Gateway 紀錄；只存固定分類、數值耗時／用量及旗標，沒有任意 message/type/code/header、輸入或回答。既有 DB 不更新，舊未知 hold 不結算、不重播。

Doppler `api-quota-broker/dev` 五分鐘整個 config 唯讀 Service Token 一次，程序內快取最多讀取 NVIDIA／Mistral key 各一次，無 renewal。NVIDIA 送出前必須仍有 123 秒 deadline 加 30 秒餘裕，否則停止。curl 憑證／payload 只用 stdin pipe，argv 不含秘密；stdout 僅在父程序記憶體，原始 stderr 丟棄。禁用 `.curlrc`，使用 TLS 驗證與 curl 本身身分；無 shell、retry、redirect、trace、response 檔或 secrets file。限制總輸出，逾時或超限 kill 並 wait child；未拿到完整成功回應都保留 unknown。Token 近到期即停止，不補呼叫。

離線計畫（不讀憑證、不新增 provider 呼叫）：

```bash
.venv/bin/python scripts/v1_remaining_once.py
```

**尚未批准或執行的 live 命令**，須由 main 對上述範圍取得人類批准後再走正式執行審查：

```bash
.venv/bin/python scripts/v1_remaining_once.py --live --db /home/ubuntu/projects/api-quota-broker/.state/v1-remaining-2026-10-02.sqlite
```

不推送、合併、付費、部署、變更服務或對外入口；本方案準備完成不等於七家完成驗收。

## 本地驗證

Ubuntu完整suite依修正階段為221、238、253，model gate首版修正後 **263 passed**。其後精確路由優先與防意外派送變更的受影響fixture **28 passed**；最終零GET／512-token腳本 **6 passed**。263是其後兩項變更前的完整回歸，不能稱最終HEAD跑過完整273項。安全錯誤／header修正另有受影響77項通過。測試涵蓋正常路由、用量結算、別名gate、秘密過濾、curl邊界、逾時／截斷、舊收據不改及同檔防重跑，只用fixture、loopback與空本地檔案，不呼叫provider。

最後程式變更後`ruff check .`、`ruff format --check .`（59 files）、`mypy src/quota_broker`（15 source files）及diff檢查通過。Groq／NVIDIA／Mistral的live完整回答收據已建立；本次最終文件修改另通過`git diff --check`。先前未執行的一次性方案保持未執行，歷史結果依當時證據保留。
