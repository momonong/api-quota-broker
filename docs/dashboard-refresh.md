# 配額資料重新核對與刷新

`scripts/dashboard_refresh.py` 提供可重複的本機 metadata 收集、現有 schema 驗證及私人證據匯入。預設 dry run；只有明確 `--apply` 才寫入或送出本機 Broker GET。它不執行生成、不讀 provider key、不登入供應商、不改計費、不啟用 target、不改 live profile，也不建立排程或管理服務。

這次實作與離線測試不代表已同步任何供應商的官方帳戶餘額。工具輸出分別標示 `checked_at`（本機檢查）、`collected_at`（本機 GET 完成）、原始 `as_of`／`valid_until`（來源證據）；重新整理不能將後兩者改成現在。

## 1. 檢查既有證據

從 repo 根目錄執行；沿用已存在的 uv 環境與鎖定版本：

```bash
uv run --offline --locked python scripts/dashboard_refresh.py inspect \
  --accounts-file .state/dashboard-accounts/accounts.json \
  --reviewed-config .state/dashboard-reviewed/reviewed.json
```

可只提供其中一個輸入。`validate` 是同一命令的別名。stdout 僅包含供應商／模型、來源、原始時間、狀態與數值是否可用，不列出餘額、帳戶識別碼或整份私人內容。過時資料保留 `stale`；餘額未知保留 `unknown`。檢查 reviewed bundle 時僅在臨時私有目錄建立驗證用 SQLite；不讀寫正式帳本。

輸入須為目前使用者擁有的 `0600` 單一連結一般檔，上層目錄為 `0700`；所有祖先路徑不得為 symlink。拒絕 hardlink、FIFO、重複 JSON key、超限檔案、額外欄位、credential 標記、未允許的官方來源及未來觀測時間。請使用專用私人目錄，勿把正式 credential 或網站認證檔交給工具。

## 2. 自動收集本機 Broker metadata

這是已有憑證及路由的 Broker 的唯讀資料收集，與官方帳戶餘額查詢分開：

```bash
uv run --offline --locked python scripts/dashboard_refresh.py collect \
  --upstream http://127.0.0.1:18084 \
  --token-file /private/path/gateway-client.token \
  --output /private/path/broker-metadata.json
```

dry run 驗證 loopback URL、列出固定 GET 路徑；不開啟 token 檔、不連網、不建立輸出。確認目的路徑後加 `--apply` 才收集。輸出目錄須預先存在且為 `0700`，client token 為 `0600`；client token 只保留在程序內，不進 stdout 或證據檔。只接受明確 `http://127.0.0.1:<port>`，不使用 DNS、代理、redirect 或路徑前綴。

允許路徑為 `GET /v1/catalog`、`GET /v1/diagnostics`、`GET /v1/usage`、`GET /v1/tasks?limit=6`、`GET /v1/model-inventory`。沿用 Dashboard 的有限時間／大小讀取與欄位投影，不呼叫 routes/explain、execution、queue 或 admin。舊版 Broker 沒有 model-inventory 時，模型來源保持 unavailable；活動來源失敗保持未知，不填零。`/v1/usage` 是既有未帶篩選的本機活動摘要；完整歷史查詢由另行的 usage report 介面負責。

輸出 `kind: broker_readonly_metadata`，清楚標示 `local_broker_metadata_not_official_account_balance` 與 `provider_balance_state: manual_required`。catalog 中的模型與 cap 不證明目前可免費執行；本機已記錄用量不等於供應商帳單。證據檔只保留投影後資料，不保留提示詞、回覆、request key、原始 diagnostics 或 provider request ID。重複收集可原子替換同一份輸出，舊 bytes 先保留為 `*.previous.<UTC>-<隨機尾碼>`，均為 `0600`。

## 3. 匯入重新核對的官方帳戶觀測

人工在官方帳戶頁面登入並觀測後，將允許的 metadata 填入獨立 `0600` JSON；不匯出 cookie、完整帳戶頁、原始網路回應或 credential。工具沿用 [dashboard 帳戶 schema](dashboard.md#獨立點數帳戶與活動來源)，目前只支援 OpenAI USD 帳戶。匯入前先 dry run：

```bash
uv run --offline --locked python scripts/dashboard_refresh.py import-accounts \
  --input /private/path/accounts-new-observation.json \
  --destination /private/path/accounts.json
```

明確加 `--apply` 才寫入。既有目標會先保留完整舊證據，再以同目錄臨時檔、fsync、原子替換寫入；內容相同則不重寫。觀測有變更時，該帳戶的新 `as_of` 必須晚於原值，不能只延長舊 `valid_until`。過時輸入可以保存為歷史證據，但不會變成 current。

同一 account 的 `evidence` 涵蓋該列所有欄位，因此只重新核對餘額時，不能把未重核的舊 billing、模型 limits、benefit 或 usage 一起沿用為新事實。用 `qualification: unverified`、`billing: unknown`、`model_limits: []`、`usage_page: null`、`benefit.recurrence: unknown`，其餘 benefit 值填 null。grant 只有日期時保留 `precision: date`，不製造時間／時區。balance、grant remaining、rate limit、Batch queue 容量及下一次補充各有不同意義，不互相換算。`sources` 必須至少一筆實際核對的允許官方頁面；schema 允許的最長有效期為 24 小時，應依實際資料流動選擇較短期限。

工具不能證明人工確實重新登入或看見數值。`--apply` 表示操作者確認這份來源與觀測時間；不要用批次字串替換「現在時間」製造新觀測。

## 4. 保留七家供應商的人工核對 bundle

reviewed 模式沿用三件套 `reviewed.json`、`gateway.json`、`registry.json`。必須對應同一次已完成的人工核對，且所有 targets 仍為 `enabled: false`。工具會使用既有 `create_reviewed` 驗證跨檔時間、quota、資格、Registry 與停用狀態：

```bash
uv run --offline --locked python scripts/dashboard_refresh.py import-reviewed \
  --input /private/path/new-audit/reviewed.json \
  --destination /private/path/evidence/audit-YYYYMMDD-HHMMSS
```

加 `--apply` 後，三件套完整寫入新私有目錄再原子移入目的地。目的目錄必須不存在；不覆寫前次 bundle、不修改 active profile，不自動重啟工作台或更換設定路徑。這是完整證據的 staging；將來 ASUS 部署時仍須另行核對服務讀取路徑、檔案擁有者、重載及網站驗收。

reviewed schema 的人工 session 有固定 14 日期限；Cloudflare 從 used/limit 推算的 neurons remaining 只有獨立短期限，不能因重載續期。其餘 provider limits 不是 remaining；未確認的擴充供應商繼續保持 unknown／停用。不要把只重新核對一家供應商當成整份 bundle 的重新稽核。

## 供應商自動化能力（2026-11-05 核對）

模型目錄、帳戶的模型權限、時間窗用量、速率上限和貨幣餘額各自有不同的來源。僅在取得對應帳戶與時間窗的資料時，才記錄可用額度。新的核對包保留自己的觀測時間，舊包可繼續供稽核。

| 來源 | 可由程式讀取的官方資料 | 帳戶餘額與限制 |
|---|---|---|
| OpenRouter | 一般金鑰的 `GET /api/v1/key` 提供 UTC 日的免費請求計數，`GET /api/v1/models` 提供目錄。Management key 的 `GET /api/v1/credits` 提供帳戶購買與消耗總額。 | 金鑰預算、免費日計數與帳戶 USD 餘額分開。日計數還有 BYOK／豁免條件。 [免費限制](https://openrouter.ai/docs/api_reference/limits) · [帳戶點數](https://openrouter.ai/docs/api/api-reference/credits/get-credits) |
| Google Gemini | Google 的 `gemini-api models list --all` 或 `GET /v1beta/models` 查目錄。Cloud Monitoring `projects.timeSeries.list` 具備 Gemini 專案每模型的 quota usage／limit 指標，需 OAuth 與監控讀權限。 | RPD 為專案配額，太平洋時間午夜重置。AI Studio 的峰值有資料延遲。Prepay 可用金額從 AI Studio Billing 核對。 [模型 API](https://ai.google.dev/api/models) · [官方實驗 CLI](https://github.com/google-gemini/gemini-api-cli) · [監控指標](https://docs.cloud.google.com/monitoring/api/metrics_gcp_d_h#generativelanguage) · [計費](https://ai.google.dev/gemini-api/docs/billing) |
| Groq | `GET /openai/v1/models` 查有效模型。已授權請求的 rate-limit headers 回傳 RPD 和 TPM 窗口的剩餘與重置。 | 組織／專案限制在 Console。讀取模型目錄不會取得同等的額度資料，且要另核對模型權限。 [模型 API](https://console.groq.com/docs/api-reference) · [速率限制](https://console.groq.com/docs/rate-limits) · [模型權限](https://console.groq.com/docs/model-permissions) |
| Mistral | `GET /v1/models` 查 workspace 金鑰可見模型。Enterprise Admin API 可讀用量、速率與支出上限。 | 預付 credits 在 Billing。若帳戶頁面與公開 Free 方案說明衝突，保留存取資格未知。 [模型與 workspace](https://docs.mistral.ai/getting-started/quickstarts/admin/manage-workspaces) · [用量與權限](https://docs.mistral.ai/admin/billing-usage/usage-limits) · [Admin API](https://docs.mistral.ai/admin/admin-api/usage-metrics) |
| Cloudflare Workers AI | `GET /accounts/{account_id}/ai/models/search` 查模型目錄。用量自動化可先以 Account Analytics Read 核對該帳戶可用的 GraphQL schema。 | 直接 Workers AI 使用 Neurons，當日已用可從官方頁面讀取。AI Gateway Unified Billing 的 `credit-balance` 只涵蓋使用該設定的 Gateway。 [Workers AI 價格](https://developers.cloudflare.com/workers-ai/platform/pricing/) · [Analytics 權限](https://developers.cloudflare.com/analytics/graphql-api/getting-started/authentication/api-token-auth/) · [Gateway 餘額](https://developers.cloudflare.com/api/resources/ai_gateway/subresources/billing/methods/credit_balance/) |
| NVIDIA Build | 匿名 `GET https://integrate.api.nvidia.com/v1/models` 查 hosted 模型 ID。 | 本次未找到可讀 hosted 試用餘額的官方 API 或 CLI。模型目錄與 Public API Endpoints 存取權限分開。 [Hosted 模型](https://docs.nvidia.com/nemo-helix/documentation/models-and-inference/) |
| OCR.space | MyAPI 的 `POST /conversions` 查截至前一天的月轉換累計，供應商新版公告支援 Free key。 | 舊 MyAPI 說明仍寫 PRO 專用。無效 key 也會回零，且月轉換數與每日每 IP 請求數不同。 [新版公告](https://forum.ocr.space/t/myapi-endpoint-updated-engine3-support-added/29732) · [MyAPI](https://ocr.space/ocrapi/myapi) · [OCR Engine 2](https://ocr.space/ocrapi) |
| OpenAI | 官方 CLI 與 Admin API 可查組織用量、費用與專案模型權限。普通專案金鑰依權限列模型。 | 本次未找到可讀 API credit grant 餘額的官方 CLI 或 REST，使用 API 組織 Billing／Credit grants。 [CLI](https://developers.openai.com/api/reference/cli) · [API credits](https://developers.openai.com/api/docs/guides/chatgpt-api-credits) |
| SambaNova | SambaCloud 的 production 型錄列有 `Meta-Llama-3.3-70B-Instruct`。 | 該帳戶的 hosted 存取與點數維持未知。 [SambaCloud 模型](https://docs.sambanova.ai/docs/en/models/sambacloud-models) |
| Hugging Face | 官方 Cerebras 路由文件列 `openai/gpt-oss-120b:cerebras`。Enterprise 組織可以用具 billing read 權限的 token 查推理用量。 | Free 方案目前沒有每月包含的推理點數。個人用量看官方 Inference Providers 設定頁。 [模型](https://huggingface.co/docs/inference-providers/en/providers/cerebras) · [用量與方案](https://huggingface.co/docs/inference-providers/main/pricing) |
| Cerebras | 官方 MCP 文件列出 `gpt-oss-120b`。 | 直接 API 的帳戶可見性和免費額度尚待核對。 [模型清單](https://inference-docs.cerebras.ai/integrations/code-mcp) |

### 單獨讀取 OpenRouter 免費次數

既有金鑰已由獲授權的秘密來源提供到非互動 stdin 時，可使用：

```bash
uv run --offline --locked python scripts/provider_metadata_fetch.py --openrouter-key-stdin
```

僅向固定的 `https://openrouter.ai/api/v1/key` 發一筆 GET，沿用既有連線與資料大小限制。輸出經過既有數字欄位驗證，`free_model_daily_requests` 缺失時為 null。輸出的 `credits.limit_remaining` 屬於金鑰的預算資訊。實際帳戶總餘額需要 Management key 與獨立授權。這個模式不會建立 Doppler token，也不會寫回 Dashboard 或正式路由。

完整模型批次仍由 `scripts/provider_metadata_fetch.py` 準備，預設列出計畫。它的 `--execute` 使用獨立授權的五分鐘 Doppler service token。受控的模型目錄讀取與餘額更新應各按自己的資料來源與憑證範圍執行。本文列出的其他供應商讀取能力尚未接成定時任務。

### 匯入模型清單與免費日計數

將正規化的批次結果、獨立模型清單或 OpenRouter 查詢結果匯入私有檔案。可重複指定 `--input`。`--input -` 接收前一個 CLI 產出的 JSON，適合接入已有憑證注入的流程。

```bash
uv run --offline --locked python scripts/dashboard_refresh.py import-provider-metadata \
  --input /private/path/normalized-provider-output.json \
  --reviewed-config /private/path/reviewed/reviewed.json \
  --destination /private/path/provider-observations.json --apply
```

省略 `--apply` 可先查看會採用的資料。批次提前停止時，僅採用已成功正規化的個別結果。以完整供應商及模型 ID 比對配置，保留未更新來源的原始時間。完整清單未列出的模型會標記未列出，未讀完的清單保留未知。供應商回報的輸出 token 上限大於 context 上限時，保留模型識別與能力，兩個 token 上限都維持未知。OCR.space 只有文件證據。匯入會驗證格式和來源欄位，實際來源仍由收集流程確認。同一路徑由一個寫入程序管理，先保留舊檔再原子替換。

以 `dashboard-serve --provider-metadata-file /private/path/provider-observations.json` 指定檔案後，每次載入儀表板資料會讀取最新檔案。型錄觀測有效 24 小時。OpenRouter 免費日計數有效至觀測後 15 分鐘與下一個 UTC 午夜中較早的時間，跨午夜取得的回應保留未知。這些資料獨立於 Broker 帳本與執行判定，單次匯入不會改變路由、資格或計費。

## 離線驗證

```bash
uv run --offline --locked pytest -q tests/test_dashboard_refresh.py
uv run --offline --locked ruff check scripts/dashboard_refresh.py tests/test_dashboard_refresh.py
uv run --offline --locked ruff format --check scripts/dashboard_refresh.py tests/test_dashboard_refresh.py
```

測試使用合成帳戶與 localhost HTTP fixture，檢查 default dry run、metadata GET allowlist、私人活動欄位排除、過時與來源時間、同 `as_of` 續期拒絕、原子更新／歷史保留、0700／0600 與 symlink／hardlink 保護。它不接觸正式 Broker、官方供應商、真實帳戶或真實 token。
