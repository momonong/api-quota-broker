# 按需更新額度與期限

`/quota` 的首屏先列餘額與期限，再連到供應商型錄。總用量與任務建議維持獨立頁籤。數字分為目前觀測、過時的最後觀測、供應商上限及未知；不同單位不相加。讀值過期不會抹掉歷史數字。點數實際到期、每日／每月補充、本機再核對期限各自獨立。

## 操作者的單一更新流程

`scripts/quota_refresh.py plan` 純離線列出每家的來源與限制。`run` 用既有 Doppler CLI `api-quota-broker/dev/OPENROUTER_API_KEY`，只取該 secret 到記憶體，對官方 `GET https://openrouter.ai/api/v1/key` 做一次有界查詢。TLS 必須驗證，不跟隨 redirect、不重試、不沿用代理、不建立新金鑰或 service token；不呼叫模型、不購買、不更改 billing。輸出只有允許的數字、固定狀態碼與時間。

```text
uv run --locked --offline python scripts/quota_refresh.py run \
  --metadata <目前版本的私有metadata目錄> \
  --output <新的私有輸出目錄> \
  --base-package <目前已發布且核對的release.json> \
  --publish
```

由維護代理呼叫，不要求使用者搬運指令或輸入 sudo。省略 `--publish` 只準備離線封包；省略 `--base-package` 只收集。新輸出目錄不得已存在。若官方帳戶頁剛核對完成，可附 `--accounts-input <私有accounts.json>`；沿用原始觀測時刻、來源與日期精度，必須比原紀錄新，最多24小時有效。未知讀值不能靠重讀續期。

發布沿用既有 HP 專用入口與 Doppler 部署 scope。只重用 baseline package 的已發布 app bytes，不能夾帶工作樹草稿。僅 `accounts.json` 和 `provider-observations.json` 可變；資格、路由、registry 與模型觀測不可變。以 active release+manifest 作 CAS，證據不得倒退，同一時刻不得換值。舊 app 不支援 schema2 時先停止，須先完成正常應用升級。發布仍採原 durable intent／receipt／reconcile，不換 UUID 繞過未知結果。

這是按需維護 pipeline，不是伺服器上的每日排程。網站「重讀本機資料」只重讀已發布結果，不假稱會即時查詢各家。root launcher 在發布時載入該代資料，因此完成 publish 才代表網站收到更新。

## 每家能自動到哪裡（官方文件核對 2026-11-06）

| 供應商 | 本版處理 | 額度、期限的限制 |
|---|---|---|
| OpenRouter | CLI取key、官方GET日計數 | `free_model_daily_requests` 直接回報 used/limit/remaining；`limit_remaining` 是金額上限，不是免費次數。讀值15分鐘或當日UTC午夜失效，以較早者為準。缺欄／失敗保留歷史，但不標為當前可用。 |
| OpenAI | 已登入帳戶頁核對，驗證匯入 | 一般project key不提供已核對的credit grants查詢契約。保留餘額、逐筆到期與原始日期精度；Usage/Costs不等於剩餘credits。 |
| Google Gemini | 權限缺口明列 | Cloud Monitoring可查Developer API用量／上限，需OAuth+`monitoring.timeSeries.list`及正確專案映射，推論key不能替代。監控延遲、不完整視窗不能倒算即時remaining。本機gcloud的snap啟動目前失敗，未發送監控查詢。 |
| Cloudflare Workers AI | 帳戶頁與公開上限 | 每日10,000 Neurons、00:00 UTC重置；GraphQL analytics不是帳單讀值，AI Gateway prepaid balance不是Workers AI免費Neurons。 |
| Groq | 官方Console；可重用正常推論既有觀測 | 剩餘通常是推論回應標頭。禁止為查額度額外推論；缺全帳戶餘額就未知。 |
| Mistral | Console；專用權限缺口明列 | Admin用量API限Enterprise；一般key不適用。預付credits、每月內含額度、spend limit分開，不互相冒充。 |
| NVIDIA | Console待核對 | 未找到可接線的官方免費剩餘查詢契約。 |
| OCR.space | 免費方案公開限制 | conversions API僅PRO/PRO PDF且延遲到昨日，無效key也可能回零；不接到免費額度。 |
| Hugging Face | Billing頁待核對 | 未找到可接線的官方credits CLI/API契約；whoami不代表可查餘額。 |
| Cerebras | Console待核對 | 官方metrics API為Dedicated產品，不拿來代表免費試用。 |
| SambaNova | Console待核對 | 尚無已核對的免費餘額契約，維持既定停用。 |

`completed_with_gaps` 表示可自動來源已嘗試且其他來源有明確缺口，不代表11家餘額都已同步。現有停用的三家不因新增型錄或同步狀態而啟用。手動帳戶核對不直接開放路由。

官方依據：[OpenRouter limits](https://openrouter.ai/docs/api_reference/limits)、[OpenAI credits](https://developers.openai.com/api/docs/guides/chatgpt-api-credits)、[Gemini limits](https://ai.google.dev/gemini-api/docs/rate-limits)、[Google Monitoring metrics](https://docs.cloud.google.com/monitoring/api/metrics_gcp_d_h#generativelanguage)、[Cloudflare pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)、[Cloudflare GraphQL](https://developers.cloudflare.com/analytics/graphql-api/)、[Groq](https://console.groq.com/docs/rate-limits)、[Mistral Admin](https://docs.mistral.ai/admin/admin-api/usage-metrics)、[OCR.space](https://ocr.space/ocrapi/myapi)、[HF](https://huggingface.co/docs/inference-providers/en/pricing)、[Cerebras metrics](https://inference-docs.cerebras.ai/capabilities/metrics)、[NVIDIA](https://docs.api.nvidia.com/nim/docs/api-quickstart)。
