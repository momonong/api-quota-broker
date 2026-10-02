# 七家免費能力接通：剩餘診斷方案（2026-10-02）

**本文件與新腳本已準備；沒有新 live 派送。** 七家各至少一個免費能力真實成功、經統一 Gateway 路由並記錄 SQLite 是完成條件。既有成功不能用來宣稱其餘供應商也可用。

## 先更正 NVIDIA 證據

唯讀核對 `.state/gateway-live-once.sqlite`：2026-09-30 的 `nvidia/riva-translate-4b-instruct-v2` 已透過統一 Gateway 取得 HTTP 200、`completed`、1628 ms、供應商 input 22／output 3 tokens，reservation 已結算。這不是只有獨立 direct probe；先前回報漏列了此 Gateway 證據。相同 DB 的 Nemotron 為 `unknown`，不改寫。

因此歷史 Gateway 成功包括 NVIDIA Riva，加上 2026-10-02 Gemini／Cloudflare／OpenRouter／OCR.space，共五家有實際能力回應證據；Cloudflare 的 Neurons 仍未知。9 月 30 日成功不證明 NVIDIA 今天的帳號資格、服務狀態或模型用量。Gemma 兩次逾時仍未知，不能把 NVIDIA 全家定論為不通，也不能把 Riva 成功當成 Gemma 成功。

## 官方資料與現有請求的對照

### NVIDIA

[Gemma 官方參考](https://docs.api.nvidia.com/nim/reference/google-gemma-4-31b-it-infer)支援目前 endpoint、`enable_thinking=false`、`stream=false`，亦支援 SSE 和 202 pending。沒有證據指出目前請求格式錯。Streaming 可觀察分段資料，但不保證更早回 headers；202 需要另一次 poll，並不包含在本方案。

[NVIDIA 官方模型列表](https://build.nvidia.com/nvidia)仍列 Riva v2 為 Free Endpoint；[模型卡](https://docs.api.nvidia.com/nim/reference/nvidia-riva-translate-4b-instruct-v2)說明語言對 system prompt，[API](https://docs.api.nvidia.com/nim/re/reference/nvidia-riva-translate-4b-instruct-v2-infer)支援既有 chat endpoint。下一輪優先測既有 catalog 的 Riva 翻譯，短句 `Good morning.`、`en-zh-cn`、32 輸出 tokens；這是新的能力驗證，不重播 Gemma 或任何舊 request key。

診斷用本機既有 `/usr/bin/curl`（8.5.0），[官方 curl 說明](https://curl.se/docs/manpage.html)的 timing 欄位提供 DNS 完成、TCP 完成、TLS 完成、首位元組與總耗時。它們是從請求開始算的累積時間；TCP 的 peer 可能是 proxy，不能當成直連 NVIDIA。只根據正值縮小階段：TLS 後未收到 headers、TLS／proxy tunnel、TCP，或 DNS／連線尚不能區分。逾時時不採用部分 body。此替代 transport 只用於新 Riva 診斷；正式 Gateway 的 urllib transport 與 Gemma 請求不變。

### Mistral

[官方 quickstart](https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key)使用相同 `mistral-small-latest` 與 chat endpoint，Free mode 不需信用卡。兩次 429 不足以歸因請求格式。[官方錯誤格式](https://docs.mistral.ai/resources/error-glossary)列出四種固定 type；安全診斷現在保留這四種 type、body 結構、code 是否存在、param 是否為 model、固定 Content-Type 分類與解析後 Retry-After 秒數。其他 type/code 值一律不保存；仍可能不足以判斷精確原因，不承諾每種未知錯誤都能分類。

[官方支援說明](https://help.mistral.ai/en/articles/698531-why-am-i-hitting-api-rate-limits-and-how-do-i-increase-them)列 RPS、每分鐘／每月 tokens，限額適用組織且可能依模型不同；亦提醒 Free mode API key 與 Vibe plan key 的來源不同。[Usage/limits](https://docs.mistral.ai/admin/billing-usage/usage-limits)另列 Workspace spending cap。應由有權限者核對目前模型限額、月用量、Workspace cap 與 key 類型；不能僅由 429 判斷是哪一項，亦不啟用 pay-as-you-go。

新 GET 採[官方模型列表](https://docs.mistral.ai/api/endpoint/models) `/v1/models` 一頁，檢查固定 alias 是否在 `id` 或 `aliases` 且支援 `completion_chat`。不用尚未確認支援 alias 的 retrieve 路徑。GET 超限、失敗、未列出或不支援 chat 都保守跳過 Mistral POST，**不推定 chat 模型不存在或帳號無權限**；沒有第二個 GET、翻頁或自動換模型。

### Groq

[官方 Groq 相容文件](https://console.groq.com/docs/openai)支援官方 SDK／OpenAI 相容 client；[官方 Python SDK](https://github.com/groq/groq-python)用 httpx、預設兩次重試、預設一分鐘 timeout。[SDK 原始碼](https://github.com/groq/groq-python/blob/main/src/groq/_base_client.py)亦有自己的 SDK 身分／平台 headers，預設 client 可跟 redirect。現有 urllib 只送 JSON／認證，無 SDK 平台 headers，拒絕 redirect、無重試。這些差異不能證明改用 SDK 就能解除 1010。

[GPT-OSS 官方 API](https://console.groq.com/docs/api-reference)支援目前 low reasoning／不輸出 reasoning 參數；[model permissions](https://console.groq.com/docs/model-permissions)的 org/project block 與 [Cloudflare 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/) 是不同分支。舊 POST body 未留存，不能追認其原因；新的無認證 GET 曾確認本機有 1010。

**本次準備方案對 Groq 為零呼叫。** 站方確認合法 API client 可從目前主機存取後，才提出使用真實官方 SDK 的有界驗證：`max_retries=0`、30 秒 timeout、client `follow_redirects=False`、沒有自訂 UA／proxy／IP、關閉 SDK debug logging；最多一筆 models GET，成功且固定 `openai/gpt-oss-20b` 可見才送一筆 32-token POST。若又 1010，立即停止並交站方；若是官方 org/project block，交帳號管理者檢查權限。未安裝 SDK，不複製其 headers 偽裝成 SDK，也不替 user 聯絡站方或變更設定。

## 可檢閱的下一輪最小範圍

以下是**待 main 取得一次明確追加批准**的方案；原次數上限已用完。

| 順序 | 上限與方法 | 固定內容／限制 | 失敗仍留下的證據 |
| --- | --- | --- | --- |
| 1 | Mistral GET `/v1/models` × 1 | 15 秒，response ≤64 KiB；只核對固定 alias/chat 能力，不保存列表／account ID | 狀態、HTTP、固定 body/type 分類與 Retry-After 秒數 |
| 2 | Mistral POST `/v1/chat/completions` × 1 | 僅 GET gate 通過才送；`mistral-small-latest`、32 tokens、30 秒 | 固定 error type、code 存在旗標、結構、HTTP；未知仍 hold |
| 3 | NVIDIA POST `/v1/chat/completions` × 1 | `nvidia/riva-translate-4b-instruct-v2` 翻譯、32 tokens、connect 10 秒、總 60 秒 | curl 累積耗時、固定失敗階段、HTTP；202 不 poll、未知仍 hold |

總上限一筆 provider GET、兩筆獨立推論 POST；Groq／Google／Cloudflare／OpenRouter／OCR.space 零呼叫。兩筆推論都經 `Gateway.run` 路由／admission／SQLite ledger。免費資格沿用使用者先前對有界測試的帳號陳述；public Free Endpoint 不代表帳號餘額，若新資訊與免費／billing disabled 矛盾，送出前停止。

`scripts/v1_remaining_once.py` 預設只列 offline 計畫，已執行過的兩份 smoke 與一份追加 DB 全部用 `mode=ro` 核對，要求 NVIDIA／Mistral 各已派送兩筆。新 DB `.state/v1-remaining-2026-10-02.sqlite` 用 `O_EXCL`／`0600` 建立；同檔拒絕重跑。新 `gateway_diagnostics` 以 request key 連到 Gateway 紀錄；只存固定分類、數值耗時／用量及旗標，沒有任意 message/type/code/header、輸入或回答。既有 DB 不更新，舊未知 hold 不結算、不重播。

Doppler `api-quota-broker/dev` 五分鐘整個 config 唯讀 Service Token 一次，程序內快取最多讀取 NVIDIA／Mistral key 各一次，無 renewal。curl 憑證／payload 只用 stdin pipe，argv 不含秘密；stdout 僅在父程序記憶體，原始 stderr 丟棄。禁用 `.curlrc`，使用 TLS 驗證與 curl 本身身分；無 shell、retry、redirect、trace、response 檔或 secrets file。限制總輸出，逾時或超限 kill 並 wait child；未拿到完整成功回應都保留 unknown。Token 近到期即停止，不補呼叫。

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

Ubuntu 本地完整 suite 為 `123 passed`；新診斷的 13 項 fixture 另驗證 stdin config 真正經既有 curl 解析、`file:///dev/null` 的 write-out delimiter、HTTP/2／1xx、逾時不採用部分回答、輸出上限／deadline kill child、Mistral gate、已知錯誤 type 與秘密不落庫／不顯示、舊 DB 不變及同檔拒絕重跑。只有 fixture、loopback 與空本地檔案，沒有 provider live 呼叫。`ruff check .`、`ruff format --check .`、`mypy src/quota_broker` 及 `git diff --check` 通過；新 live DB 尚未建立。
