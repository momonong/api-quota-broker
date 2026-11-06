# 三家供應商擴充：本機設定與離線驗證

2026-10-18。本階段新增 SambaNova、Hugging Face routed inference 與 Cerebras 試用候選，沿用 instance-local Registry、`openai_chat` adapter、Gateway、SQLite 與 durable queue。沒有修改 runtime、依賴、內建七家目錄或正式設定，也沒有呼叫真實推論、啟用 target、部署或發布。

## 交付與載入方式

- [`registry.expansion.example.json`](../registry.expansion.example.json)：三個固定 HTTPS endpoint 與模型；由既有 `gateway-serve --registry-file` 載入。它會擴充該 instance 的 Registry，不改全域 builtin。
- [`gateway.expansion.example.json`](../gateway.expansion.example.json)：只含三個新 target，全部 `enabled=false`、`free_eligible=false`，驗證日期為空。不是既有七家設定的替代品；正式整合前需把經核對的新 target 加入受保護的本機設定，保留原帳戶 scope／bucket／active reservation。
- [`tests/test_provider_expansion.py`](../tests/test_provider_expansion.py)：直接載入上述兩個範例，經現有 adapter 與 Gateway 驗證；只替換 provider HTTP I/O 與憑證來源。

| provider | 固定 endpoint | 模型 | secret reference |
| --- | --- | --- | --- |
| `sambanova` | `https://api.sambanova.ai/v1/chat/completions` | `Meta-Llama-3.3-70B-Instruct` | `SAMBANOVA_API_KEY` |
| `huggingface` | `https://router.huggingface.co/v1/chat/completions` | `openai/gpt-oss-120b:cerebras` | `HUGGINGFACE_API_TOKEN` |
| `cerebras` | `https://api.cerebras.ai/v1/chat/completions` | `gpt-oss-120b` | `CEREBRAS_API_KEY` |

HF 使用 HF token 與帳戶額度，明確固定 Cerebras host，不使用 `:auto` 或自備 provider key。它與 Cerebras 直連是不同的認證與計費 scope。表中只有 reference，沒有任何真實憑證；仍沿用既有 Doppler runtime resolver。現有 private key admin 僅支援固定 NVIDIA／Groq 目的地，本次沒有擴充它或建立新秘密。

## 能力與會計邊界

初始範圍是 `capability=text_generation`、字串 `input`、單輪 buffered 回答。既有 generic adapter 送出 `model`、單一 user message、`max_tokens`、`stream=false` 與 Bearer 認證。新 provider 的 typed input、媒體、tools、structured output、streaming 與自動 discovery 不在這次交付範圍。

Manifest 的 context 8192／output 1024 是本機保守 profile 上限，並非供應商最大能力；target 另限 output 256。Manifest 的 `verified_at` 表示公開協定資料核對日期，不能代替帳戶資格驗證。

每個 target 的預設 local safety caps 為每分鐘 1 次、每日 1 次、每日 input tokens 8192（UTC），並行上限 1。這些是 disabled 範例的本機防線，不是供應商實際配額，也不是已批准的真實呼叫次數。`provider_quota_facts` 保持空白、`capacity.kind=unknown`；未確認的剩餘額度不填成零或無限。

Generic adapter 要求有效、相符的 prompt／completion／total usage；成功時既有 Gateway 結算 requests 與 input_tokens，另呈現 output tokens。此字串路徑沒有 output／total-token bucket 結算或美元預算帳本，因此不加入無法正確結算的 cap，也不能用這份設定宣稱費用有保證。缺漏或矛盾 usage 保留 hold，結果為 `completed_usage_unknown`。

這三家尚未加入 provider-specific quota/error hook。429、503、timeout 都保守處理為 `unknown`，保留 hold；不因錯誤文字或 Retry-After 就推斷未執行、不轉送另一家、不自動重試。這個限制符合現有非重播契約，之後若需要 proven quota fallback，必須另補官方證據與驗證。

## 公開資料與啟用前檢查

- SambaNova 官方 [quickstart](https://docs.sambanova.ai/docs/en/get-started/quickstart) 與 [API URLs](https://docs.sambanova.ai/docs/en/get-started/api-keys-urls) 支持上表協定。2026-10-18 重新直接讀取官方 [rate limits 的 Free Tier](https://docs.sambanova.ai/docs/en/models/rate-limits#free-tier)：未綁付款方式適用 Free Tier，Llama 3.3 70B 的公開上限為 20 RPM、20 RPD、200000 TPD。這是公開限額，不是帳戶剩餘；Credits 頁沒有餘額與購買提示不能單獨證明免費層不存在。帳戶實際套用 tier／剩餘／模型可用性仍未經 API 驗證，故維持 disabled／unknown，不綁卡、不購買、不以付費 tier 繞過。
- HF 官方 [GPT-OSS guide](https://huggingface.co/docs/inference-providers/guides/gpt-oss) 提供固定 `:cerebras` route；[chat completion](https://huggingface.co/docs/inference-providers/tasks/chat-completion) 要求 Inference Providers token 權限。2026-10-18 最新 [Pricing](https://huggingface.co/docs/inference-providers/pricing) 經網頁工具與瀏覽器核對：Free Users 的 included monthly credits 為 **None**，需購買 credits 或訂閱付費方案；本次因此更正 manifest，不再標記為免費月額度候選。先前提到的免費月額度不能作為當前資格依據。Token 存在只證明憑證名稱已設定，不會產生免費額度；目前不符合免費池的啟用證據。
- Cerebras 官方 [OpenAI compatibility](https://inference-docs.cerebras.ai/resources/openai) 與 [chat completion](https://inference-docs.cerebras.ai/api-reference/chat-completions) 支持此協定。當日 [rate limits](https://inference-docs.cerebras.ai/support/rate-limits) 說明為需驗證付款方式的 $5／30 天試用，故僅作 disabled 試用候選，不能當每日再生免費池。實際獲贈時才依證據標記 `one_time_gift`。

真實呼叫前需確認：帳戶／模型存取、免費餘額及有效期、付款與超額扣款狀態、正確 shared scope、憑證安全存放，以及明確批准的每家呼叫次數與輸入／輸出上限。填入有來源且短效的 `verified_at`／`expires_at`；保留 `billing_enabled` 的真實語意，不能為通過 admission 而填假。若與免費原則衝突，就維持 disabled。

同日直接核對 SambaNova 登入後的 [Plans](https://cloud.sambanova.ai/plans)：帳戶為 **Free / Active Plan**，但方案文案同時要求新增付款方式、購買 credits 才能執行首次請求，Includes 也標為 pay-as-you-go credits。這與 rate-limit 文件的 Free Tier 語意有落差；可確認「存在 Free 帳戶與公開速率表」，不能據此保證「這個帳戶有可用的免費推論額度」。維持候選停用，不透過測試呼叫解決此矛盾，也不擅自購買。

建立 HF token 時只需獨立 fine-grained token 的 `Make calls to Inference Providers`；不增加私有 repo、write、endpoint 管理或組織權限。建立持久憑證需使用者確認，秘密不得貼進聊天、文件、Git 或日誌。帳戶註冊完成也不等於完成金鑰設定或取得免費資格。

## HF token 存入後的唯讀準備

2026-10-18 使用者回報存入 Doppler 後，只執行一次 `api-quota-broker/dev` 的 `secrets --only-names` 查詢，確認 `HUGGINGFACE_API_TOKEN` 名稱存在。沒有取得 token 值、修改 Doppler、建立 Service Token、呼叫模型或部署。這不驗證 token 值、scope、有效性或免費資格。

名稱快照以既有 `SecretInventory` schema 保存於 Git 忽略的 `.state/provider-expansion-20261018T015721Z/secret-names.json`，檔案 0600、目錄 0700，效期一小時；只供本輪本機預檢，沒有接到執行中服務，也不修改既有 inventory。到期後不得視為現況。

使用這份快照載入既有 Gateway，於臨時 SQLite 執行 `diagnostics`／`explain`，並禁止網路與 secret resolver／provider transport：HF credential metadata 為 `present`，admission 仍為 `blocked`、沒有選中 target，原因含 disabled、免費資格未驗證及缺少免費證據。`.state/provider-expansion-20261018T015721Z/hf-preflight.json` 保存不含秘密的結果；secret value reads 0、provider requests 0。

更正 HF manifest 分類後，重跑 `tests/test_provider_expansion.py`：**33 passed**；JSON 載入與 `git diff --check` 通過，原有 33 個歷史未追蹤文件 hash 不變。沒有重複整套驗證，也沒有變更 runtime 或依賴。

## 離線證據

以既有已安裝依賴執行，不同步或下載套件：

```sh
UV_CACHE_DIR=/tmp/aqb-expansion-uv-cache uv run --locked --offline --no-sync pytest -q tests/test_provider_expansion.py tests/test_registry.py tests/test_framework.py --tb=short
```

結果：**170 passed**。其中新增 33 個參數化案例涵蓋三家 disabled／帳戶資格 gate、真正 adapter payload、HTTP/CLI 同步請求、同 key 不重送、queue/worker/result、重啟恢復、usage 缺漏／矛盾、429／503／timeout 不轉送、truncation、固定 endpoint／model 與 SQLite 不含明文測試內容或憑證。新增測試明確禁止外部 DNS／連線，只允許 loopback；provider response 及 secret 均為 fixture。

另通過 `ruff check .`、`ruff format --check .`（240 files）、`mypy src/quota_broker`（27 modules）、`uv lock --check --offline` 與 `git diff --check`。依賴／lockfile 未改；既有 33 個未追蹤歷史文件逐一 hash 核對未變。沒有執行全套 pytest，以上為本次相關範圍的回歸驗證。

這是本機協定與整合證據，不能證明真實帳戶可呼叫、免費餘額足夠或線上部署可用。所有新 target 維持 disabled；本階段停在本機成果與帳戶資格核對。
