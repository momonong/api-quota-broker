# 2026-11-16～17 官方模型目錄

本目錄記錄來源目錄的所有 model identities，不把少量已配置路由誤當供應商完整清單。首次公開清單由 `fetch_public_snapshot(provider, output_modalities="all")` 正規化；後續已授權的帳戶清單沿既有 `quota_refresh.doppler_secret` 與 metadata GET helper 取得。每份保留官方 `source`、實際 UTC `checked_at`、`complete` 與所有模型。憑證及帳戶識別只在記憶體，沒有推論、啟用目標、變更配額、權限或部署。

| 供應商 | 實際記錄模型數 | 完整性及範圍 |
| --- | ---: | --- |
| NVIDIA | 33 | 該 `/v1/models` 回應；一頁，無後續頁標記 |
| OpenRouter | 724（743 capability rows） | `output_modalities=all` 回應；一頁，無後續頁標記 |
| Hugging Face | 132 | **partial**：router 只有 chat-completion models，非所有 Hub inference 任務 |
| SambaNova | 5 | 該 `/v1/models` 回應；一頁，無後續頁標記 |
| Cerebras | 2 | public catalog 回應；一頁，無後續頁標記 |
| Google | 64（84 capability rows） | 帳戶 models.list HTTP 200；一頁，沒有 nextPageToken |
| Groq | 11 | 帳戶 model list HTTP 200；完整於該 listing |
| Mistral | 53（58 capability rows） | 帳戶 model list HTTP 200；完整於該 listing |
| Cloudflare | 75 | 官方 Workers AI 文件索引 75/75 身份一致；帳戶 API **partial**：page 2 空、分母聲稱 334，範圍未解 |
| OpenAI | 96 | 帳戶 model list HTTP 200；完整於該 listing，沒有 private fine-tuned identity 需隱藏 |
| OCR.space | 文件限定三引擎 | 既有 `ocrspace_snapshot` 由官方 API reference 建立，非帳戶推論證據 |

`complete` 只涵蓋該來源的 listing，不表示所有帳戶能跑、全部免費、完整模型品質測試或 route activation。解析器會因 next cursor、has_more、總筆數不符等保留 partial；此次沒有自行跟隨任意 provider URL 或 pagination link；僅沿已核對的 Google pageToken 與 Cloudflare page 參數，既有 helper 最多五頁，總筆數上限 10,000。OpenAI、SambaNova、Cerebras、HF 尚無逐型號 protocol/capability 證據時保持 unknown，不從名稱猜 coding/vision。

## 已查核的重要型號

- NVIDIA：目前匿名 API 列出 `z-ai/glm-5.3-flash`；官方 [model card](https://docs.api.nvidia.com/nim/reference/z-ai-glm-5-3-flash) 將 coding、tool agents 列為用途，並記錄 text/image、reasoning/tool calling 和 1,048,576-token context。[chat API](https://docs.api.nvidia.com/nim/reference/z-ai-glm-5-3-flash-infer) 明定 exact ID、OpenAI-compatible endpoint。這是官方能力說明，沒有把廠商 benchmark 宣傳換成 Broker 自測分數。其預設高 reasoning 預算可能影響延遲；帳戶免費資格另驗。
- Cloudflare：`@cf/meta/llama-3.2-1b-instruct` 已公告 **2026-11-20** 停止回應，HTTP 410／code 5028，見 [官方 changelog](https://developers.cloudflare.com/changelog/product/workers-ai/)。[Gemma 4 profile](https://developers.cloudflare.com/workers-ai/models/gemma-4-26b-a4b-it/) 是官方推薦較小替代之一，256,000-token context，支援 messages。Catalog 4096 output 是本地 profile cap，不是假稱官方最大值。
- Cloudflare [官方 pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)：每日免費額度 10,000 Neurons、UTC 午夜重置。Gemma 4 每百萬 input/output 為 9091/27273 Neurons；Qwen3.8-27B 為 40909/290909。GLM5.3、DeepSeek frontier 等指定型號需要付費 billing method；listing 不證明免費。Paid plan 超額可計費，須維持 broker 帳戶額度與禁止付費 gate。

## 安全繁忙處理證據

Cloudflare [rejectIfBusy](https://developers.cloudflare.com/workers-ai/features/reject-if-busy/) 在 options 啟用時，可因 capacity 在排隊前拒絕，HTTP 429／code 3040。此語義比僅憑 429／503 狀態更強；不可把 transport timeout、斷線或未知結果當作安全重送。本目錄不修改重試或 runtime target 行為。

## 官方入口

- [NVIDIA public list](https://integrate.api.nvidia.com/v1/models)
- [OpenRouter all-modalities list](https://openrouter.ai/api/v1/models?output_modalities=all)
- [HF router list 文件](https://huggingface.co/docs/inference-providers/hub-api)，[public list](https://router.huggingface.co/v1/models)
- [SambaNova public list](https://api.sambanova.ai/v1/models)
- [Cerebras public list](https://api.cerebras.ai/public/v1/models)
- [Cloudflare models](https://developers.cloudflare.com/workers-ai/models/)

JSON 可供離線 review／匯入；匯入仍是管理操作，目錄檔案本身不會自動更新既有服務。所有 fetched timestamps 在各 JSON 內；無法取得者明示缺口，不將 unavailable 寫成 all。

## 相容性修正範圍

Gemma 4 支援 wrapped `result.choices[0].message.content` 及官方 [sync-output schema](https://developers.cloudflare.com/workers-ai/models/gemma-4-26b-a4b-it/sync-output.json) 定義的 direct ChatCompletion；direct 分支僅對精確型號啟用，檢查 object、id、created、model、單一 index0／assistant choice 與 stop/length。舊型號 `result.response` 保留。兩條 broker parser 均拒絕混合封裝、錯誤、重複欄位、多 choices、空文字、tool/refusal/audio。Typed parser 保留 finish reason、truncation 與 request id。沒有 usage 就維持 unknown，內外 usage 相衝時不採用，token total 矛盾也不可結算；未從文字長度製造用量。

Builtin native transport 僅接受 catalog 中既定 Cloudflare endpoint 的合法 account slot，拒絕未知 model／query／path escape。Builtin 和 typed native text serializer 固定加 `options.rejectIfBusy=true`；這不是使用者任意可指定 options，也沒有把所有 429／503 認定為安全重送。Registry extension adapter 如另有自己的 serializer，整合者須同步加同一固定旗標與精確驗證。

GLM5.3-Flash 的 builtin／typed request 使用其官方文件 `chat_template_kwargs.clear_thinking=true`，不再套用其他 NVIDIA 型號的 `enable_thinking=false`；保留明確 output cap，typed caller 已支援的 reasoning_effort 原值保留。模型盤點階段沒有推論呼叫或效果測量；後續已授權正式驗收另見[部署與驗證紀錄](../reliability-expiry-2026-11-17.md)。

## 帳戶清單與 Cloudflare 用量核對（台灣 2026-11-17）

`2026-11-17/` 保存新取得的五家 snapshot 與 `coverage.json`；實際 UTC 時間仍為 2026-11-16，詳各 `checked_at`。Google、Groq、Mistral、Cloudflare 與 OpenAI 各做一次官方 model-list GET，Cloudflare 因 partial 再沿同 endpoint 查 page 2。所有六次 GET 均 HTTP 200，沒有生成請求。OpenAI 的 `owned_by` 等帳戶資料不輸出，private fine-tuned IDs 如存在會隱藏並降為 partial；此次為零筆。

Cloudflare `cloudflare-pagination.json` 僅保留公開數字：page 2、per_page 100、count 0、total_count 334。已看到的 75 筆全數保存，總數不一致尚無官方解釋；沒有以空頁擅自把 complete 改成 true，也沒有繼續查空頁。

2026-11-17 另外比對[官方 Workers AI 文件索引](https://developers.cloudflare.com/workers-ai/llms.txt)及[模型索引](https://developers.cloudflare.com/workers-ai/models/index.md)：75 個 model ID 與上述帳戶 snapshot 完全一致，雙向差集皆空。`cloudflare-doc-index-scope-proof.json` 保存這個獨立範圍的完整性；沒有將帳戶 API `complete` 改為 true，也沒有把包含第三方 AI Gateway 的統一 AI 目錄混入免費 Workers 清單。文件列出不等於帳戶可執行或免費。

另外只沿既有 `quota_refresh.py` 的精確 `cloudflare_graphql` query 做一次當日 aggregate 查詢，不執行整個 collect、不查其他 billing、不新增權限或換 key。`cloudflare-usage.json` 記錄：HTTP 200，但 GraphQL 回報 analytics 權限拒絕，固定狀態 `missing_permission / analytics_permission_required`。因此 `reported_neurons` 與 `remaining_neurons` 都是 null。這證明 model-list 可讀與 usage analytics 可讀是兩道不同權限，**尚未恢復自動讀取 Cloudflare 用量／餘額**。

上述權限失敗是歷史證據。使用者另行明確批准後，2026-11-17 已對原帳戶、原 token 增加 Account Analytics Read，保留 Workers AI 權限且不變更方案或計費。新查詢 HTTP 200、GraphQL `errors=null`，但當日 groups 為空；這只證明權限恢復，不代表實際消耗為零或剩餘 10,000。採樣分析與帳戶權威餘額仍分開呈現。

查詢窗口從 2026-11-16 00:00 UTC 起；查核時間為 16:06:34 UTC。依[官方每日重置說明](https://developers.cloudflare.com/workers-ai/platform/pricing/)，下一免費額度重置為 2026-11-17 00:00 UTC（台灣 08:00）。文件的 10,000 Neurons 免費額度不是帳戶目前餘額，也未被拿來填補缺失讀值。

官方分頁契約：[Google models.list](https://ai.google.dev/api/models)、[Cloudflare models/search](https://developers.cloudflare.com/api/resources/ai/subresources/models/methods/list/)。HTTP 錯誤只保存狀態及固定 reason，原始回應、分頁 token、帳戶識別及秘密不落檔。`coverage.json` 不代表帳戶免費資格、可執行容量或人工驗收。
