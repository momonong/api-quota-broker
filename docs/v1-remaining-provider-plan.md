# 七家免費能力接通：剩餘診斷方案（2026-10-02）

**本文件與新腳本已準備；沒有新 live 派送。** 完成條件是六家文字 API（包含 NVIDIA 一般 LLM）與 OCR.space 各取得免費能力的真實成功，經統一 Gateway 路由並記錄 SQLite。目前四家成功；NVIDIA、Mistral、Groq 尚未完成。沒有證據顯示使用者缺信用卡、必須升級或必須更換 key。

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

[GPT-OSS 官方 API](https://console.groq.com/docs/api-reference)支援目前 low reasoning／不輸出 reasoning 參數；[model permissions](https://console.groq.com/docs/model-permissions)的 org/project block 與 [Cloudflare 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/) 是不同分支。舊 POST body 未留存，不能追認其原因；新的無認證 GET 曾確認本機有 1010。

**本次準備方案對 Groq 為零呼叫。** 站方確認合法 API client 可從目前主機存取後，才提出使用真實官方 SDK 的有界驗證：`max_retries=0`、30 秒 timeout、client `follow_redirects=False`、沒有自訂 UA／proxy／IP、關閉 SDK debug logging；最多一筆 models GET，成功且固定 `openai/gpt-oss-20b` 可見才送一筆 32-token POST。若又 1010，立即停止並交站方；若是官方 org/project block，交帳號管理者檢查權限。未安裝 SDK，不複製其 headers 偽裝成 SDK，也不替 user 聯絡站方或變更設定。

可供使用者經 [Groq 官方 Contact](https://groq.com/contact) 提交的非秘密摘要：Ubuntu 合法 API client、`GET /openai/v1/models` 無認證請求回 403／1010／browser_signature_banned；請站方確認 API 入口的 client fingerprint／WAF 規則。附既有 UTC 收據時間及已知 cf-ray（若有），不附 API key、HAR、raw body。帳號管理者只有在明確看到 `model_permission_blocked_org`／`model_permission_blocked_project` 時，才依官方文件查看 org/project 模型允許清單；目前沒有這兩碼的證據。1010 是站方規則線索，無法斷言使用者漏了哪個設定，也不能斷言 SDK 可解決。

## 可檢閱的下一輪最小範圍

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

Ubuntu 本地完整 suite 為 `153 passed`；剩餘診斷的 43 項 fixture 驗證 stdin config 真正經既有 curl 解析、`file:///dev/null` 的 write-out delimiter、HTTP/2／1xx、逾時不採用部分回答、輸出上限／deadline kill child、Mistral gate、固定訊息分類與秘密過濾、三個 NVIDIA 模型請求回歸、空答案／length／缺 finish_reason 維持 unknown、malformed HTTP 200 保留狀態與 timing、舊 DB 不變及同檔拒絕重跑。只有 fixture、loopback 與空本地檔案，沒有 provider live 呼叫。`ruff check .`、`ruff format --check .`、`mypy src/quota_broker` 及 `git diff --check` 通過；新 live DB 尚未建立。
