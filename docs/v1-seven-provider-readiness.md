# 七家 API v1 本地驗證邊界（2026-10-02）

## 目前結果

本分支實作七家固定路由與 SQLite 逐次嘗試紀錄。`scripts/v1_smoke_once.py` 的預設模式只列計畫；截至本文件日期，沒有在此分支建立 Service Token、讀取秘密值或呼叫七家供應商。本地 fixture 是工程驗證，不能視為帳號或真實服務已通。

## 固定路由與唯讀名稱

| 供應商 | 固定模型／能力 | 官方端點 | Doppler 名稱 |
| --- | --- | --- | --- |
| NVIDIA | `google/gemma-4-31b-it`，文字 | `https://integrate.api.nvidia.com/v1/chat/completions` | `NVIDIA_API_KEY` |
| Gemini | `gemini-2.5-flash-lite`，文字 | `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent` | `GEMINI_API_KEY` |
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
- [OCR.space Free API](https://ocr.space/ocrapi)提供每月 25,000 次 Engine 1/2 conversion、每日每 IP 500 次、免費檔案上限 1 MB。本 v1 限縮為單張不超過 36 KB 的 PNG/JPEG；OCR 回傳文字不進 SQLite。此前 OCR 真實 key 讀取與 POST 曾被自動審查拒絕，尚未重新獲得正式批准。
- [OpenRouter 公開模型目錄](https://openrouter.ai/api/v1/models)在 2026-10-02 顯示固定 `:free` 模型 prompt/completion 價格為零。live 前腳本會重新核對所有回傳價格欄位均為零及純文字輸入輸出；[額度說明](https://openrouter.ai/docs/api_reference/limits)的帳號餘額仍須與實際 key 分開確認。請勿加入 OpenRouter `models` 自動 fallback。
- NVIDIA 現有模型的[官方 API 參考](https://docs.api.nvidia.com/nim/reference/google-gemma-4-31b-it-infer)證明路徑與請求形式；使用者提到的 40 RPM 是速率上限，**不等於**總免費額度或刷新週期。帳號可用性仍待真實回應。

曾檢查 `nemotron-mini-4b-instruct` 作更小的 NVIDIA smoke 候選；[NVIDIA 模型詳情頁](https://build.nvidia.com/nvidia/nemotron-mini-4b-instruct)目前未提供可核對的 Free Endpoint 狀態，因此沒有僅憑搜尋列表將固定候選改成它。

所有 provider `capacity.kind=unknown`，直到有可靠的免費額度刷新事實。RPM/TPM 只作限流，不代表免費額度按分鐘刷新。`gateway_attempts` 記錄每次派送的 HTTP、延遲、供應商回報量和本地估算；`gateway_tasks` 是任務摘要。`usage.requests` 計數已派送 HTTP 次數，包括已明確拒絕的請求；`ledger_charges` 為配額核算，其中已確認未執行的配額拒絕歸零。未知結果保留 hold，不重送。

## 故障切換與有界 smoke

只有官方文件明確表示未執行的配額拒絕會釋放本地 hold、設定 account/project scope 冷卻，並在同一任務最多三次、每目標最多一次的邊界內改選其他帳號 scope。現有辨識：Gemini 結構化配額碼、Cloudflare `3036`、Groq 帶 `retry-after` 的結構化 429。其餘 429、逾時、斷線、5xx、回應格式錯誤維持 unknown，不能自動重送或改路由。認證／模型錯誤只診斷，不認定配額耗盡。

`scripts/v1_smoke_once.py` 的 live 方案是 `api-quota-broker/dev` 整個 config 唯讀、五分鐘到期的 Doppler Service Token，只留在程序記憶體；每家至多一筆，七筆總量，輸出上限 64 token，循序執行，每筆 provider HTTP 最多 60 秒，臨近 token 到期即停。使用新 `v1-smoke-*` SQLite 檔先獨占建立非敏感收據，不能對同一 DB 意外重跑。腳本使用本地產生的 `OK` PNG，驗證 OCR 回應是否含預期字樣，只輸出布林結果。個別供應商失敗後可繼續獨立測其他家；共享憑證不可用與 token 時效則停止。未經本輪精確批准，不執行 `--live`。

預定收據檔為 `/home/ubuntu/projects/api-quota-broker/.state/v1-smoke-2026-10-02.sqlite`；2026-10-02 核對時不存在，父目錄 owner 為 `ubuntu`、mode `0700`。live 程式會以 `0600` 獨占建立此檔；32-byte HMAC key 在程序記憶體隨機產生，不另存 key 檔。若程序中斷，保留該 DB 的已派送／unknown 收據；同一檔不能再執行。正式批准後的唯一預定命令是：

```bash
.venv/bin/python scripts/v1_smoke_once.py --live --db /home/ubuntu/projects/api-quota-broker/.state/v1-smoke-2026-10-02.sqlite
```

## 本地驗證

`pytest -q`、`ruff check .`、`ruff format --check .`、`mypy src/quota_broker` 在 Ubuntu 執行；新增 fixture 覆蓋七家固定路由、OCR 表單、秘密不落庫、明確 429 fallback、Cloudflare `3040`／5xx／逾時不 fallback、同帳號 scope 冷卻、逐次監控、smoke plan 與防重跑。真實 provider 成功率、free 帳號資格、配額剩餘和 OCR 辨識品質仍未驗證。
