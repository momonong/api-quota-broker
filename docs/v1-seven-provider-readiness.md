# 七家 API v1 本地驗證邊界（2026-10-02）

## 目前結果

本分支實作七家固定路由與 SQLite 逐次嘗試紀錄。`scripts/v1_smoke_once.py` 的預設模式只列計畫。本地 fixture 是工程驗證，不能視為帳號或真實服務已通。2026-10-02 在使用者批准後，以五分鐘 config 唯讀 Service Token 執行兩輪真實 smoke。第一輪 NVIDIA 單次 POST 逾時，收據為 `unknown`；Gemini 在送出前失敗，沒有派送。第二輪根據第一輪收據排除 NVIDIA，Gemini 與其餘五家各派送一次。合計七家各至多一筆已派送 HTTP，均不重送。收據保留在 `.state/v1-smoke-2026-10-02.sqlite` 與 `.state/v1-smoke-2026-10-02-remaining.sqlite`，不存憑證、秘密或回應內容。

| 供應商 | 實測收據 | HTTP | 回報用量 | 本地估算／限制 |
| --- | --- | --- | --- | --- |
| NVIDIA | `unknown`，60 秒逾時 | 無回應 | 未知 | 輸入上界 376 tokens；實際是否執行未知 |
| Gemini | `unknown` | 404 | 未知 | 輸入上界 376 tokens；首輪送出前失敗，次輪才派送 |
| Cloudflare | `completed_usage_unknown` | 200 | 未回報 tokens／Neurons | 輸入上界 376 tokens、Neurons 本地上界 30；實際 Neurons 未知，保留 hold |
| Groq | `unknown` | 403 | 未知 | 輸入上界 376 tokens |
| Mistral | `unknown` | 429 | 未知 | 輸入上界 376 tokens；未證明明確配額拒絕 |
| OpenRouter | `completed` | 200 | 17 輸入、47 輸出 tokens，供應商回報 | 固定零價 `:free` 模型 |
| OCR.space | `completed`，合成 `OK` 圖辨識成功 | 200 | conversion 用量未回報 | 送出 129-byte PNG；本地只記 1 筆 HTTP |

HTTP 200 證明單次 API 呼叫回傳，但不證明免費帳號的餘額或刷新週期。404／403／429 的原因沒有可靠的細分證據；不推定模型不可用、權限或配額耗盡。`unknown` 與 `completed_usage_unknown` 均不自動重送。上述輸入上界是本地保留量，不是供應商用量。

## 2026-10-02 根因追查與修正

- **Cloudflare 解析缺陷已確認並修正。** [官方 REST API](https://developers.cloudflare.com/workers-ai/get-started/rest-api/)以 `result` 包裝模型輸出，[模型 schema](https://developers.cloudflare.com/workers-ai/models/llama-3.2-1b-instruct/)及[Run API schema](https://developers.cloudflare.com/api/resources/ai/methods/run/)列出模型 `usage`。舊解析器只讀最外層 `usage`，會漏掉 `result.usage`。新解析器接受兩種已知位置，若兩處衝突則不採用用量。官方 schema 的 token 用量欄位不保證包含 Neurons；缺 Neurons 時仍保留配額 hold。第一次真實回應內容未保存，故不能追認它實際帶有任何 token 數值。
- **Gemini 404 的可驗證線索是模型存取限制。** [Google 退場／存取頁](https://ai.google.dev/gemini-api/docs/deprecations)明確限制新專案使用 2.5 Flash-Lite，推薦 3.5 Flash-Lite；[官方定價](https://ai.google.dev/gemini-api/docs/pricing)列 3.5 Flash-Lite 的免費輸入與輸出。v1 的停用範例路由已改為 `gemini-3.5-flash-lite`，舊 2.5 模型保留於 catalog 供歷史收據辨識。Google 標準錯誤也可能是數字 `error.code=404` 與 `error.status=NOT_FOUND`；新診斷器只分類為 `google_not_found`，不從自由文字猜測模型原因。原始 404 body 未保存，仍不能確認本帳號是否因這項限制而失敗；新模型尚未真實派送。
- **Groq 的新唯讀探針確認本機還有邊緣封鎖。** 協調 task 對 `GET /openai/v1/models` 做的無認證唯讀檢查回 HTTP 403，頂層 `error_code=1010`、`error_name=browser_signature_banned`。這符合 [Cloudflare Error 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/) 的客戶端指紋封鎖，應由網站擁有者處理；不改 User-Agent／IP 或偽裝瀏覽器繞過。此 GET 的證據不能倒推先前 POST 403 必然同因：舊回應 body 未保存。[Groq 官方錯誤碼](https://console.groq.com/docs/errors)及[模型權限文件](https://console.groq.com/docs/model-permissions)也列 403 權限受限與組織／專案模型封鎖。新增固定白名單分類，以後若看到 1010 或官方模型封鎖碼才記錄相應非秘密代碼；不保存任意訊息。
- **Mistral 429 未能細分。** [官方用量與限制](https://docs.mistral.ai/admin/billing-usage/usage-limits)說明 Free mode 的組織、Workspace 和模型限額；[錯誤格式](https://docs.mistral.ai/resources/error-glossary)使用頂層 `object/type/code`，`rate_limit_error` 是固定類別。新診斷器只在未來回應明確符合 `object=error`、`type=rate_limit_error` 時記錄白名單碼，仍不會自動重送或釋放 hold。目前舊收據僅有 429，沒有可信的錯誤類別或用量，不能確認是哪一種限制，也不能升級付費。
- **NVIDIA 推論逾時階段未知。** 協調 task 的無認證 `GET /v1/models` 回 200、包含模型列表且約 155 ms，證明當時從主機可達模型列表端點；不能推論先前推論 POST 成功或失敗。舊收據只有 60 秒 timeout 與無 HTTP 回應，不能區分連線、首位元組等待、模型執行或回應讀取。該筆維持 `unknown`，沒有重送。

未來回應會以固定、非秘密診斷碼區分憑證取得與請求建構的送出前失敗，並只對 Google 模型不存在、Groq 組織／專案模型封鎖的明確官方碼做白名單分類。這些修正不會改寫舊收據或推定舊 body。精確確認剩餘帳號層級原因需要額外供應商查詢或新的推論呼叫，超出本輪「每家最多一次」的批准。

## 固定路由與唯讀名稱

| 供應商 | 固定模型／能力 | 官方端點 | Doppler 名稱 |
| --- | --- | --- | --- |
| NVIDIA | `google/gemma-4-31b-it`，文字 | `https://integrate.api.nvidia.com/v1/chat/completions` | `NVIDIA_API_KEY` |
| Gemini | `gemini-3.5-flash-lite`，文字；原實測為 `gemini-2.5-flash-lite` | `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent` | `GEMINI_API_KEY` |
| Cloudflare | `@cf/meta/llama-3.2-1b-instruct`，文字 | `https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/meta/llama-3.2-1b-instruct` | `CLOUDFLARE_API_TOKEN`，`CLOUDFLARE_ACCOUNT_ID` |
| Groq | `openai/gpt-oss-20b`，文字 | `https://api.groq.com/openai/v1/chat/completions` | `GROQ_API_KEY` |
| Mistral | `mistral-small-latest`，文字 | `https://api.mistral.ai/v1/chat/completions` | `MISTRAL_API_KEY` |
| OpenRouter | `liquid/lfm-2.5-2.6b:free`，文字 | `https://openrouter.ai/api/v1/chat/completions` | `OPENROUTER_API_KEY` |
| OCR.space | Engine 2，單張 PNG/JPEG | `https://api.ocr.space/parse/image` | `OCRSPACE_API_KEY` |

2026-10-02 Doppler `api-quota-broker/dev` 唯讀名稱核對顯示上表名稱均存在；`GOOGLE_API_KEY` 不存在，故 Gemini 用 `GEMINI_API_KEY`。這只證明名稱存在，沒有驗證值、權限、帳號方案或餘額。舊 `api-provider-nvidia/dev` 名稱查詢失敗；不能以舊名稱的批准建立新專案 token。

## 官方依據與資料語義

- [Gemini 錯誤碼](https://ai.google.dev/gemini-api/docs/api-errors)區分 429 `RESOURCE_EXHAUSTED`／配額碼與 503；[速率限制](https://ai.google.dev/gemini-api/docs/rate-limits)依專案和模型，實際帳號值仍未知。
- [Cloudflare Workers AI 錯誤碼](https://developers.cloudflare.com/workers-ai/platform/errors/)把每日免費額度耗盡列為 429／`3036`，容量不足是不同的 `3040`；[定價](https://developers.cloudflare.com/workers-ai/platform/pricing/)提供每日 10,000 Neurons 免費配額與所選模型換算率。smoke 的 `neuron_bound=30` 是根據每百萬輸入 2,457／輸出 18,252 Neurons、短輸入與 64 輸出 token 所設的保守**本地估算**，不是供應商用量回報。
- [Groq 限制與 429 回應](https://console.groq.com/docs/rate-limits)記載 `openai/gpt-oss-20b` Free 基準 30 RPM、1,000 RPD、8,000 TPM、200,000 TPD；實際組織額度可能不同。僅帶 `retry-after` 的結構化 429 才可轉往不同帳號 scope。
- [Mistral Free mode](https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key)不要求信用卡；[用量限制](https://docs.mistral.ai/admin/billing-usage/usage-limits)包含組織層級及月度用量，帳號數值未知。
- [OCR.space Free API](https://ocr.space/ocrapi)提供每月 25,000 次 Engine 1/2 conversion、每日每 IP 500 次、免費檔案上限 1 MB。本 v1 限縮為單張不超過 36 KB 的 PNG/JPEG；OCR 回傳文字不進 SQLite。此前 OCR 真實 key 讀取與 POST 曾被自動審查拒絕；本輪使用者已明確批准七家各一次，續測仍須經正式執行審查。
- [OpenRouter 公開模型目錄](https://openrouter.ai/api/v1/models)在 2026-10-02 顯示固定 `:free` 模型 prompt/completion 價格為零。live 前腳本會重新核對所有回傳價格欄位均為零及純文字輸入輸出；[額度說明](https://openrouter.ai/docs/api_reference/limits)的帳號餘額仍須與實際 key 分開確認。請勿加入 OpenRouter `models` 自動 fallback。
- NVIDIA 現有模型的[官方 API 參考](https://docs.api.nvidia.com/nim/reference/google-gemma-4-31b-it-infer)證明路徑與請求形式；使用者提到的 40 RPM 是速率上限，**不等於**總免費額度或刷新週期。帳號可用性仍待真實回應。

曾檢查 `nemotron-mini-4b-instruct` 作更小的 NVIDIA smoke 候選；[NVIDIA 模型詳情頁](https://build.nvidia.com/nvidia/nemotron-mini-4b-instruct)目前未提供可核對的 Free Endpoint 狀態，因此沒有僅憑搜尋列表將固定候選改成它。

停用的 `gateway.example.json` 也包含 Cloudflare 官方每日 10,000 Neurons、UTC 00:00 重置的 `short_renewable`／86,400 秒證據，標明 placeholder account scope、來源及七天有效期；target 仍 disabled、free_eligible=false，不能因此推定帳號可用。bounded smoke 會以同一官方規則建立五分鐘有效的 runtime profile，作**免費額度刷新排序**；實際帳號餘額仍未知。其他供應商 capacity 保持 unknown：OCR 月 conversion、Groq 日請求上限與 Mistral 月 included usage 尚不能以此短刷新欄位可靠表達。RPM/TPM 只作限流，不代表免費額度按分鐘刷新。`gateway_attempts` 記錄每次派送的 HTTP、延遲、供應商回報量和本地估算；`gateway_tasks` 是任務摘要。`usage.requests` 計數已派送 HTTP 次數，包括已明確拒絕的請求；`ledger_charges` 為配額核算，其中已確認未執行的配額拒絕歸零。未知結果保留 hold，不重送。

## 故障切換與有界 smoke

只有官方文件明確表示未執行的配額拒絕會釋放本地 hold、設定 account/project scope 冷卻，並在同一任務最多三次、每目標最多一次的邊界內改選其他帳號 scope。現有辨識：Gemini 結構化配額碼、Cloudflare `3036`、Groq 帶 `retry-after` 的結構化 429，以及 OpenRouter 平台 `error.metadata.error_type=rate_limit_exceeded`、沒有 upstream `provider_code`、同時帶齊三項 `X-RateLimit-*` 標頭的 429。帶 usage／partial content 的 429 不切換。NVIDIA、Mistral、OCR.space 尚無足夠可靠的結構化配額拒絕辨識，故不會由其 429 自動 fallback。其餘 429、逾時、斷線、5xx、回應格式錯誤維持 unknown，不能自動重送或改路由。認證／模型錯誤只診斷，不認定配額耗盡。

`scripts/v1_smoke_once.py` 的 live 方案是 `api-quota-broker/dev` 整個 config 唯讀、五分鐘到期的 Doppler Service Token，只留在程序記憶體；每家至多一筆，七筆總量，輸出上限 64 token，循序執行，每筆 provider HTTP 最多 60 秒，臨近 token 到期即停。使用新 `v1-smoke-*` SQLite 檔先獨占建立非敏感收據，不能對同一 DB 意外重跑。腳本使用本地產生的 `OK` PNG，驗證 OCR 回應是否含預期字樣，只輸出布林結果。個別供應商送出前失敗後可繼續獨立測其他家；token 時效則停止。續測指定 `--provider` 與 `--prior-db`，會以唯讀方式核對前次收據並拒絕再次派送已送出供應商。

兩份收據檔 mode 均為 `0600`，父目錄 `0700`。32-byte HMAC key 在程序記憶體隨機產生，不另存 key 檔。第二輪實際執行的續測命令如下，**不可重執行**：

```bash
.venv/bin/python scripts/v1_smoke_once.py --live --db /home/ubuntu/projects/api-quota-broker/.state/v1-smoke-2026-10-02-remaining.sqlite --prior-db /home/ubuntu/projects/api-quota-broker/.state/v1-smoke-2026-10-02.sqlite --provider google --provider cloudflare --provider groq --provider mistral --provider openrouter --provider ocrspace
```

## 本地驗證

`pytest -q`（103 passed）、`ruff check .`、`ruff format --check .`、`mypy src/quota_broker` 在 Ubuntu 執行；fixture 覆蓋七家固定路由、OCR 表單、秘密不落庫、明確 429 fallback、Cloudflare `3040`／5xx／逾時不 fallback、同帳號 scope 冷卻、逐次監控、smoke plan、前次派送防重跑與個別送出前失敗後續測。真實 provider 全面可用性、free 帳號資格、配額剩餘和 OCR 對真實文件的品質尚未驗證。
