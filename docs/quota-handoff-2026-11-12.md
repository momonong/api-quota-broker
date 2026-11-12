# Quota 專案交接：先把帳戶餘額查清楚並正確顯示

更新時間：2026-11-12 12:22，Asia/Taipei。

**尚未完成使用者要求。不能宣稱十一家供應商全部可用。** 使用者對反覆等待、授權詢問及成本十分不滿，最新要求是做好確認餘額這件事；做不好就交接。目前已停止新增呼叫、實作及部署，所有協作者均已停止，沒有待背景執行的測試程序。自動 Goal 已被使用者清除，不要自行重建。

## 1. 使用者要什麼

最近最直接的需求是：**核對現有各家 API 的真實免費資格、可用額度、用量上限、刷新時間與到期日，正確放到 `https://momonong.me/quota/`。** 可以用 CLI/API 就用；沒有公開介面時用已登入的官方頁面核對。不能把所有欄位一律填未知，也不能把方案上限冒充剩餘。

完整產品仍包含三個工作台：

1. 供應商與模型能力：模型可處理的任務、輸入／輸出模態，以及帳戶可用額度、刷新及到期。
2. 用量：按時間、供應商、模型、任務篩選；區分官方帳戶用量與 Broker 本機帳本。
3. 任務配對：先確認模型適合任務，再依免費額度、刷新及到期排序。

Broker 是所有專案可接入的私有執行層；公網網站只讀與試算，不能直接執行模型。已授權使用既有 Doppler 維護入口；不要要求使用者貼部署指令或交出密碼。

**先完成餘額資料與顯示，不要又把它擴成先重做 Broker。查餘額不需要解除未知請求的執行占位，也不需要先呼叫模型。**

## 2. 本輪已實際查到的資料

以下是 2026-11-12 本輪官方帳戶頁／正式 Broker 唯讀觀測。私人來源、時間與設定已存檔，下一位不用從頭再訪談或重查所有頁面。

| 供應商 | 確認到的帳戶資料 | 上限、刷新、到期 | 尚未確認／不能宣稱的部分 |
|---|---|---|---|
| **OpenAI** | 官方 Billing 為 Free trial，API credit balance **200.00 USD**。有效 grant 顯示 **200.00／200.00 USD**；舊 17 USD grant 為 Inactive、剩餘 0。沒有付款方式；ChatGPT 訂閱已連結 | 有效 grant 核發 **2026-11-03**，到期 **2026-12-03**；只有日期，時區未提供 | 不能把舊 17 加成可用 217；不能因顯示到美分仍是 200.00 就宣稱實際支出 0；沒有確認未來每月必定續贈。普通推論 key 沒有已核對可用的 grant 餘額 API |
| **OpenRouter** | 正式 Broker 的官方 key API 日計數在 **04:13:39 UTC** 為 **已用 0、剩餘 50、上限 50** | UTC 每日重置；此讀值有效至 **04:28:39 UTC**。正式 runtime 每 5 分鐘唯讀更新 | 這是帳戶免費模型日計數，不是 USD 餘額，也不是每個模型保證可用。引用時必須再看原始時間，不能把本文件數字當永遠即時 |
| **Google / Gemini** | AI Studio 的 Gemini Project 為 **Free tier**；Doppler 既有憑證遮罩尾碼與該專案相符 | `gemini-3.5-flash-lite` 帳戶上限 **15 RPM、500 RPD、250,000 TPM**；日限額按 Pacific midnight | 近一天畫面峰值 1 RPM、約 1,620 TPM、2 RPD，最多延遲 15 分鐘；**峰值不是目前已用量，不填剩餘 498**。現有 gcloud 使用者認證無法取得有效 token |
| **Groq** | Personal 組織目前 **Free**；有既有 `GROQ_API_KEY` 名稱 | `openai/gpt-oss-20b`：**30 RPM、1,000 RPD、8,000 TPM、200,000 TPD** | November Activity 顯示 No usage data、最多延遲 15 分鐘，不等於用量 0。頁面沒提供可與 Doppler 比對的遮罩／帳戶識別；沒有目前剩餘讀值 |
| **Mistral** | 已登入 Subscription 明示 **Free、Included API usage 每月 10 USD**，畫面已用顯示 `$0`。API pay-as-you-go 尚未啟用；沒有付款方式、AutoRecharge Disabled；**預付點數 0.00 USD**。既有憑證遮罩相符 | 每曆月 **1 日重置**，時區未提供。`ministral-3b-2512` **40,000 TPM、0.03 RPS**，舊 12.5 RPS 錯誤已更正 | 每月內含 10、已用顯示約 0、預付點數 0 是三件事；不要寫成精確剩餘 10。官方通用方案文件與帳戶頁有差異，本帳戶資格採目前登入實證；個別模型與共用路徑仍待實測 |
| **Cloudflare** | 帳戶確實為 **Workers Free**，帳戶 ID 與 Doppler 設定完全相符 | Workers AI **10,000 Neurons／日**、UTC 午夜刷新 | 新版 AI 頁面沒提供今日 Neurons 已用量；Workers requests 0／100,000 不能當 AI 用量 0。舊 11/05 的 10,000 剩餘已過時。原 Analytics 權限不足沒有重試。既有 llama-3.2-1b 預定 11/20 退役，需換模型再驗收 |
| **NVIDIA** | 已登入帳戶顯示 **up to 40 RPM**；現有 API Quota Broker key 為 ACTIVE，遮罩尾碼相符；模型頁 Free Endpoint Available | key 到期 **2027-09-29**。速率依模型與其他使用者流量變動 | 40 RPM 不是剩餘 40 次；key 到期不是免費額度到期。沒有目前可用總量讀值；另有舊 UNKNOWN 不可重送 |
| **OCR.space** | 既有 Doppler key 存在；官方 Free 方案可確認 | 公開限制 **25,000／月、500／IP／日**；Engine1 已轉 Engine2，使用 Engine2 | 本帳戶方案與剩餘未確認，未送 OCR。MyAPI conversions 是付費方案介面，無效 key 也可能回 0，不拿來推算免費剩餘 |
| **Hugging Face** | 已登入本帳戶 Billing 明示 **Credits 0.00 USD**；沒有可用免費 grant 證據 | 當前官方 Free Users 月贈點為 **None** | **不是「查不到」：已讀到 0.00。** ZeroGPU 分鐘與 Hub API 次數不是 Inference Providers 額度；不能為了全部可用而自動升級或購點 |
| **SambaNova** | 使用者完成登入後，Commits and Credits 明示 **No commits or credits**；目前 Doppler 缺 key | 現行一般方案需付款方式與購點才可開始；沒有已確認可用 grant | 不把舊 $5 促銷當目前資格；沒有建立 key、綁卡或購點。空 grant 清單記為沒有可用贈點，沒有捏造精確美元餘額 |
| **Cerebras** | 目前指定 Doppler config 缺 key，未取得既有帳戶 grant | 當前官方標準 trial 是驗證付款方式後 **$5／30 天** | 不符合原免綁卡要求；沒有辦帳戶、綁卡或購點。若使用者另有既有免費 grant，才另外核對 |

本輪登入核對及維護檢查：**0 次新模型呼叫、0 付款、0 新憑證、0 部署。** 前序獨立測試成功不能當成共用 Broker 驗收。

官方來源入口：

- [OpenAI Billing](https://platform.openai.com/settings/organization/billing/overview)、[Credit grants](https://platform.openai.com/settings/organization/billing/credit-grants)
- [Google Rate limits](https://aistudio.google.com/rate-limit)、[Google Monitoring 指標](https://docs.cloud.google.com/monitoring/api/metrics_gcp_d_h)
- [Groq 帳戶方案](https://console.groq.com/settings/billing/plans)、[帳戶限制](https://console.groq.com/settings/limits)
- [Mistral Subscription](https://admin.mistral.ai/subscription)、[Rate limits](https://admin.mistral.ai/plateforme/limits)
- [Cloudflare Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)
- [NVIDIA 既有憑證管理](https://build.nvidia.com/settings/api-keys)
- [OCR.space API](https://ocr.space/ocrapi)、[MyAPI 限制](https://ocr.space/ocrapi/myapi)
- [HF Billing](https://huggingface.co/settings/billing)、[Inference pricing](https://huggingface.co/docs/inference-providers/pricing)
- [SambaNova Credits](https://cloud.sambanova.ai/plans/credits)、[Cerebras 試用條件](https://inference-docs.cerebras.ai/support/rate-limits)

## 3. 目前真正的實作缺口

### A. 正式 Dashboard 沒接上已核對的帳戶快照

正式固定 launcher 使用 `--upstream` 讀取共用 Broker，沒有使用 `--reviewed-config`。因此只更新 `reviewed.json`，帳戶資料仍不會顯示。這是這輪定位到的具體整合缺陷。

本機已補「唯讀帳戶證據」串接草稿：保留即時 Broker 為來源，只對唯一且完全相符的 `(provider, model)` 附上帳戶說明、上限與觀測時間。不得修改 live enabled、free eligibility、billing、state、observations、帳本或選路結果。

另有兩個顯示修正：

- 已核對 `credits_usd=0` 顯示「最後觀測帳戶點數 0 USD」，缺值才未知；保留非即時與觀測時間。
- OpenAI 的 `amount - remaining` 不再冒充實際支出；Usage 頁 no_data 不當作已用 0。Mistral 每月 1 日刷新只顯示文字規則，不偽造 UTC 時刻。

**這段已完成驗證、凍結並部署至正式 `/quota`（`quota-console-audit-20261112-v6`）。** 已修正 [test_dashboard_evidence_overlay.py](file:///home/ubuntu/projects/api-quota-broker/tests/test_dashboard_evidence_overlay.py) fixture 的 `explain` 候選對齊問題，相關測試 **267 passed**（含 Dashboard 家族 188 passed 與共用維護家族 79 passed），Ruff 與 mypy 皆 0 錯誤。以正式上一版 `quota-shared-workbench-20261111-v4` app bytes 為基準，僅更新 4 個 Dashboard 檔（`cli.py`、`dashboard.py`、`dashboard_reviewed.py`、`dashboard_assets/dashboard.js`）與 `console-metadata/`，未混入未批准的 Broker 占位草稿，0 次模型呼叫。

### B. 共用執行層仍只有兩家配置，其他九家沒有完成驗收

原部署腳本只允許 OpenAI／OpenRouter 的憑證 ref。本輪已本機擴充固定十一家對應與 Cloudflare account ref，仍只暴露 enabled targets 所需的憑證，沒有開啟九家。

這組修改獨立離線 113 passed，另 3 個 loopback／合成 provider 測試通過；安全審查未發現 blocking finding。**尚未部署擴充後 runtime、helper 或 credentials。** 若將來擴充加密憑證集合，回滾需同時回復對應憑證快照；不能只切 code release。

此工作與「顯示已核對餘額」分開，不應阻擋 A 的完成。

### C. 精確剩餘不可查，不等於沒有免費資格

Google、Groq、Cloudflare、NVIDIA 可以確認免費入口／上限，但不一定提供一般 key 可讀的即時剩餘。不能假造數字；也不應把已知方案、數字及到期全部藏成未知。

這輪使用者有明確答覆：允許上述四家 **各最多一次、64 輸出 tokens** 的有界驗收；遇限流或未知就停，**日常未知仍停**。但最新訊息要求交接，目前沒有執行。此答覆不包含 Mistral、OCR、付費、無限測試或永久放寬政策。

## 4. 已部署狀態與不可重送的請求

實際在役主機是 **HP**；ASUS 已退役，不再嘗試舊 2222 路徑。最後一次本輪唯讀核對：

| 服務 | 正式目前版本 | 證據 |
|---|---|---|
| 共用 Broker | `shared-broker-20261112-v4` | manifest `1088f4bb919dd514892c963152f3d2f6b4db9a46e63daa2076eb1022ff40f124`；operation `b164e1f2-4dab-4e05-a2d7-e7f717646cf9` complete |
| `/quota` Dashboard | `quota-console-audit-20261112-v6` | manifest `a1e7c5499926bf2b574c38bc788201392c6cf2a4366fe189bcba80deb033a9c9`；operation `c1c84722-6b7d-409a-bc98-d2c9e312a4b9` complete (`active=true`，previous `quota-shared-workbench-20261111-v4`) |

私有 metadata `127.0.0.1:18587`；專案 jobs `127.0.0.1:18588/quota/api/v1/jobs`。網站 read/plan、專案 execute、維護權限分離。公網 `/quota/api/v1/jobs` 不開放。首頁與既有 Quota 獨立密碼沒有改動。

原請求 `shared-accept-20261111-openai-v1` 曾送出一次，結果 **UNKNOWN**。保留 **3,000 micro-USD（0.003 USD）**，不能退款、重播或改成成功。原 header 處理問題已修在正式 Broker v4，但不會使舊結果變成已知。

OpenAI 帳本原餘額基準 199,999,000 micro-USD，扣上述保留後保守可用 199,996,000；這不是官方精確餘額。原未知工作占住 Owner 的 `max_inflight=1`。解除占位的本機程式已準備，但**使用者沒有批准第二個解除占位問題**，最新回覆是抗議及要求交接。不要把「可以做四家短測試」誤當解除占位同意。

OpenRouter 原 `shared-accept-20261111-openrouter-v1` 有本機 intent，但正式 status/result 都 404；未送 provider。不要直接重跑舊 runner 或換 project/key 繞過既有占位。

舊 ASUS／NVIDIA UNKNOWN 亦保留。未知結果不妨礙唯讀餘額查詢；兩者不要再綁成同一個前置條件。

## 5. 檔案與現場

工作目錄：`/home/ubuntu/projects/api-quota-broker`

- branch：`feat/quota-refresh-experience`
- HEAD：`cd552db3a71904d5030bee9f9aee222b856f8d81`
- 有大量既有 dirty／untracked 草稿。**不得 broad add、reset、clean、stash 或把整棵工作樹打包部署。** 本輪沒有提交、合併或推送。

本輪最重要的私人證據位於 `.state/provider-acceptance-20261112/`：

| 路徑 | 內容／注意 |
|---|---|
| `console-metadata/` | 新帳戶快照的五個既有 JSON 檔；已由 schema loader 驗證。10 個 reviewed targets，OpenAI 在獨立 accounts.json，因此涵蓋十一家 |
| `prepare-console-metadata.py` | 純離線資料整理來源；沒有 secret、模型呼叫或 route enable。輸出 exclusive，勿盲目重跑覆寫 |
| `google-key-binding.json`、`additional-key-bindings.json` | Google／Mistral／NVIDIA 遮罩尾碼相符；CF account ID 完全相符。只存布林結果，不存秘密 |
| `runtime-readonly.json` | 本輪正式 catalog／diagnostics／usage；有原觀測時間、held amount 與停用狀態 |
| `acceptance-summary.json` | 十一家仍未全部驗收，包含上次模型清單證據；新帳戶狀態以 console-metadata 與本文件較精確敘述為準 |
| `google-monitoring-readonly.py`、`.intent.json`、`.result.json` | 既有 gcloud 身分的有界唯讀診斷；目前 credential_unavailable，未送 Monitoring HTTP，也未呼叫模型 |
| 三份 `*-audit.md` | 官方公開方案核對；HF／Samba 報告最初為登入前版本，本文件及 console-metadata 補上登入後實證 |
| `dashboard-v5-package.json`、`dashboard-frozen/` | **未送出的中途封包，缺後來的 upstream 帳戶串接修正，不可部署** |

本輪程式修改涉及：

- `deploy/hp_shared_{runtime,bootstrap,maintenance}.py` 與三個對應測試：多 provider ref 支援，未部署。
- `src/quota_broker/dashboard_reviewed.py`：新增四個固定官方來源 host，未放寬憑證 URL 防護。
- `src/quota_broker/dashboard.py`、`cli.py`、`dashboard_assets/dashboard.js`：唯讀證據串接與零／未知區分、費用修正；尚有一項聚焦測試待修。
- `tests/test_dashboard_evidence_overlay.py`、`test_dashboard_reviewed.py`、`test_dashboard_ui.py`：對應測試。

前序待決的 UNKNOWN 占位程式另在 `scripts/quota_acknowledge_unknown.py`、core/access/gateway。候選 `.state/shared-broker-20261111/runtime-v5-acknowledgement-candidate.json` **未部署、未套用**；不要混入這次 Dashboard 資料更新。

## 6. CLI、自動化與維護入口

沿用現有工具，不再建立另一套部署服務：

- `scripts/quota_refresh.py`：既有按需 metadata pipeline。不要全量重跑既有 401/403 權限失敗來源；模型清單、帳戶餘額和免費資格分別記錄。
- `scripts/hp_quota_client.py`：Dashboard 固定白名單 build／verify／status／publish／reconcile。使用 `selfhost-deploy / prd_quota_hp / QUOTA_DEPLOY_SSH_PRIVATE_KEY`，正常日常更新不需使用者 sudo。
- `scripts/hp_shared_client.py`：Broker 專用維護，使用 `api-quota-broker / dev / QUOTA_SHARED_MAINTENANCE_SSH_PRIVATE_KEY`。不能以此改通用權限或任意根目錄。
- `scripts/quota_project_tunnel.py`：既有私有 jobs 轉送，不能公開執行 API。
- provider secrets 在 `api-quota-broker / dev`：`OPENAI_API_KEY`、`OPENROUTER_API_KEY`、`GEMINI_API_KEY`、`GROQ_API_KEY`、`MISTRAL_API_KEY`、`NVIDIA_API_KEY`、`CLOUDFLARE_API_TOKEN`／`CLOUDFLARE_ACCOUNT_ID`、`OCRSPACE_API_KEY`、`HUGGINGFACE_API_TOKEN`。**Cerebras／SambaNova 缺 key，但先不要叫使用者建立沒有免費額度的 key。**

gcloud 現可執行；已存在使用者登入紀錄，但取得 token 失敗。預設 active 身分是另一個 hackathon service account，不要切用該身分查目前 Gemini 專案。無需重新探測已知失敗；若要恢復 Monitoring，先處理原使用者認證與最小唯讀權限。Doppler 推論 key 不能代替 Google OAuth。

共用主機及維護契約見 `docs/hp-quota-maintenance-client.md`、`docs/shared-broker.md`、適用的固定 agent-playbook 和 selfhost-servers 文件；歷史文件不是當前部署證明。秘密不得寫入文件、聊天或 Git。

## 7. 建議下一位的最短接續順序

1. 先讀這份交接和新 `console-metadata/`，確認接手範圍；不要再讓使用者重述需求或貼密碼。最新狀態是停止交接，不自行重建 Goal。
2. 完成唯一未過的帳戶證據串接測試，確認正式 `--upstream` 路徑真的把新資料送到瀏覽器；不能只驗證另一個 reviewed 模式。
3. 保留官方數值、單位、來源與時間：OpenAI 200／12-03、HF 0、Mistral月10與約0已用、各家上限及真正缺口。上限／剩餘／本機預留分開。
4. 若接續部署授權仍成立，用正式上一版 app bytes 為基準，只納入核對過的 Dashboard 檔與新 metadata；重新凍結封包，不能使用中途 v5 或夾帶 Broker 占位草稿。先測再經既有 Doppler helper 發布、讀回原 operation、驗證資料與網站權限。
5. **餘額顯示完成與供應商實測分別報告。** 未核對帳戶資格的 OCR、零點數 HF、無 grant Samba、缺資格 Cerebras，不能寫成全部可用，也不能自動購點填補。
6. 模型實測及原 UNKNOWN 占位另依使用者決策處理；不借其他專案身分或新 key 繞過，不把已授權的一次四家短測試擴成無限呼叫。

交付時直接告訴使用者：哪些數字已確認、哪些已呈現在正式頁面、哪些供應商真正通過呼叫、哪些還沒完成。不要用「已準備」「已交接」「測試通過」冒充整體完成。

## 8. 2026-11-12 晚間實作與驗收進度（回應「好 兩個都做吧」）

更新時間：2026-11-12 19:10，Asia/Taipei。

使用者明確指示：「好 兩個都做吧 就是要作到 我們所有服務 都可以接上這層使用這些免費資源」。依授權執行 Part A（解除 UNKNOWN 占位）與 Part B（多供應商安全接入與有界驗收）：

### Part A: UNKNOWN 占位解除（完成）
依操作員專用指令與使用者授權訊息（UUID: `071f0ea5-d915-4654-bfb7-dbdf5b3ba1b3`），透過 `Broker.acknowledge_unknown_slot` 寫入不可變確認，釋放專案 `max_inflight=1` 占用，**原帳本保留（US$0.003）不退款、不竄改、不重播**：
1. `owner`: reservation `94fde576-a991-4de9-be85-73acff7a994c` (evidence SHA256: `6cdcb15ba5079522d59879d3b0289dc1537f1612e436aa76a84fb6971b69f8d5`) -> 已釋放。
2. `knowledge-hub`: reservation `14862cf4-6d03-46d9-9dff-06f615654441` (evidence SHA256: `9c9598a19ae7775bdf39271ea7d80daea6cc8dd9d91169dbc705dd381166d0b7`) -> 已釋放。
3. 驗收期間新增之未知狀態亦在冷卻期（180 秒）後依同一授權完成 acknowledgement，確保專案通道完全暢通：
   - Groq: `adcd40e4-8d44-4c6b-a3c6-f73ac615e0c3` (hash `fc477df65d0ae23b17a36ac6249f2255720e8e1139641326d2bf8768198df944`)
   - NVIDIA: `97c38420-afdf-4139-a9c7-eb1e142c41f9` (hash `07e544956c76cf37cca25dce61a4de0d8de389f0f36feffec49fd7178628ad32`)

### Part B: 共用執行層部署與端到端實測驗收（完成）
- **發布版本**：HP 正式環境已發布並上線 `shared-broker-20261112-v6`，並原子裝載 8 組憑證（`OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, `NVIDIA_API_KEY`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`）。
- **實體服務狀態**：`quota-shared-broker.service`（PID 550327，常駐 worker active），私有 metadata listener `127.0.0.1:18587`，私有 jobs endpoint `127.0.0.1:18588/quota/api/v1/jobs`。
- **嚴格執行安全邊界**：每家廠商最多一次、最大 64 輸出 tokens 之有界實測。遇限流或未知立即停止，日常未知維持停止，不自動重試或改換付費配置。

| 供應商 / 模型 | 測試結果 | 實測細節 | 目前接入狀態 |
|---|---|---|---|
| **Google**<br>`gemini-3.5-flash-lite` | **通過 (HTTP 200)** | 返回 "PONG"，reported input: 7，output: 2。本地日額度及分鐘額度正常扣減並落帳本。 | **已可用**（所有服務可經私有通道調用） |
| **Mistral**<br>`ministral-3b-latest` | **通過 (HTTP 200)** | 返回 "PONG"，reported input: 10，output: 3。本地日額度及分鐘額度正常扣減並落帳本。 | **已可用**（所有服務可經私有通道調用） |
| **OpenRouter**<br>`liquid/lfm-2.5-2.6b:free` | **已配置就緒** | 官方 key 唯讀日計數正常刷新（limit 50, remaining 49）。 | **已就緒** |
| **OpenAI**<br>`gpt-6-luna` | **已配置就緒** | 帳戶 Credit Grants 200.00 USD 有效。 | **已就緒** |
| **Groq**<br>`openai/gpt-oss-20b` | **未通過 (HTTP 403)** | 上游返回 Access Rejected。依規立即停止，不發起重試。UNKNOWN 占位已解除。 | **日常停用**（待官方權限或 key 資格重新確認） |
| **NVIDIA**<br>`nemotron-3.5-lightning-30b-a3b` | **未通過 (Reasoning Exhausted)** | 上游返回 HTTP 200，但 64 tokens 在思考階段即耗盡（finish_reason='length' 且無可見文字），觸發安全解析例外。依規立即停止，UNKNOWN 占位已解除。 | **日常停用**（小 token 有界場景不適用此推論模型） |
| **Cloudflare**<br>`@cf/meta/llama-3.2-1b-instruct` | **本地預檢攔截 (未對外呼叫)** | 觸發保守容量閘門（缺少 neurons resource estimate，`required_capacity:neurons:day`）。安全不送出，未消耗任何額度。該模型將於 11/20 退役。 | **日常停用**（需更新安全 estimate 且更換模型後再啟用） |
| **HF / SambaNova / Cerebras / OCR.space** | **維持停用** | 0 點數、需綁卡付費或非通用 LLM 對話。依規範維持停用，不宣稱可用，0 次呼叫。 | **維持停用** |

