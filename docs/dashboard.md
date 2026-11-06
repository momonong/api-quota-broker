# 本機配額工作台

Quota Broker 工作台提供中繼資料快照與**單一任務的選路建議**。它不會執行、排程、保留配額、更新證據、修改配置或開啟計費。所有建議固定回傳 `execution_allowed: false` 與 `paid_allowed: false`。來源可選合成示範、私人本機人工核對快照或明確指定的 Gateway；人工核對模式只讀本機中繼資料，不會登入供應商、同步帳戶或驗證憑證。

## 離線示範

預設為離線示範；也可明確指定 `--demo`。合成 Google、Groq、Mistral 路由分別展示完整餘額、未知餘額與資格證據過期。示範使用實際 Gateway 與臨時 SQLite；憑證解析器、一般與 typed provider transport 一旦被呼叫就會失敗。關閉程序後，臨時資料庫由程序清理。

先準備**獨立工作台權杖**，存放在你擁有、權限為 `0600` 的一般檔案；不得沿用 Gateway client/admin token，不要把權杖放入命令參數、Git 或截圖。可在私人終端建立權杖檔，不輸出值：

```bash
python3 -c 'import os,secrets; fd=os.open("/tmp/quota-dashboard.token",os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,(secrets.token_urlsafe(32)+"\n").encode()); os.close(fd)'
quota-broker dashboard-serve --demo --dashboard-token-file /tmp/quota-dashboard.token
```

在同一台主機的瀏覽器開啟 `http://127.0.0.1:18086`，透過密碼欄位輸入工作台權杖。程序只綁定 literal IPv4 loopback；另一台裝置的 localhost 指向它自己。啟動範例不是部署指示，也未提供其他裝置的可達性保證。瀏覽器使用 HttpOnly、SameSite=Strict cookie；session 最長八小時，重啟後失效。工作台權杖與 upstream 權杖都不能透過 URL 或 Bearer header 當作瀏覽器 session。

## 私人本機人工核對快照

使用 `--reviewed-config` 明確選擇人工核對模式，與 `--demo`、`--upstream` 互斥；未指定模式時仍使用合成示範。從專案根目錄啟動：

```bash
uv run --offline --locked quota-broker dashboard-serve \
  --reviewed-config .state/dashboard-reviewed/reviewed.json \
  --dashboard-token-file /private/path/dashboard.token
```

`reviewed.json` 與同目錄的 `gateway.json`、`registry.json` 都必須是目前使用者擁有的私有一般檔案（`0600`），目錄為 `0700`，不接受 symlink、重複 JSON keys、不支援的欄位或不合法證據。`gateway.json` 沿用 Gateway 設定與 Registry 驗證；所有目標必須明確停用。三份實際帳戶檔案只放在 Git 忽略的 `.state/dashboard-reviewed/`；既有 live profile、帳本與 expansion 範例不會被覆寫。不要將帳戶識別碼、電子郵件、秘密或原始帳戶畫面加入顯示 metadata。

程序為這個來源建立獨立臨時 Gateway SQLite，沿用現有 metadata、diagnostics 與 explain 邊界。憑證解析器、一般 provider transport 與 typed provider transport 都直接拒絕呼叫；不載入或更新名稱清單。所有資格與上限都是**人工核對帳戶快照**，不代表即時同步、憑證目前有效或 provider 已接受 API 請求。此模式的任務建議固定保守為 `ask_user`；`execution_allowed`、`paid_allowed` 均為 `false`。停用目標不代表其已核對的免費資格為 false。

快照的 `audit_session_at` 是稽核時段使用的共用時鐘，`time_precision` 固定為 `session_clock_not_per_provider_observation`，不能解讀為每家帳戶各自精確的觀測時間。2026-10-25 本地稽核對應的 UTC session anchor 是 2026-10-24T18:42:18+00:00；重新整理或重啟只更新本機讀取時間，不更動原始 anchor、資格時間、上限時間或剩餘值有效期限。`review_due_at` 固定為 anchor 加上既有本機 14 天再核對政策；這是本機複核期限，**不是供應商點數到期**。

`reviewed.json` schema 1 的固定來源種類為 `manually_reviewed_account_snapshot`，逐目標顯示 metadata 包含：

- `qualification_basis`：帳戶方案證據、公開免費條件適用或未核對。公開條件不等於實際 API 驗證。
- `billing_state`：已核對停用或未知；未知不能標為已啟用或已確認停用。Gateway 的布林值只能表達保守 admission，因此未知在私人 runtime config 使用阻擋值；工作台另以三態顯示並把阻擋原因投影為「計費狀態未核對」。
- `limit_context`：限額屬於帳戶／專案或公開方案，另列重置視窗；原始 limit 的來源、provenance、核對時間與本機複核期限取自 `provider_quota_facts.limit`，不借用 remaining 的觀測時間。
- `display_facts`：allowlist 數字 metadata，保留 requests/second 的小數、USD 與畫面四捨五入／歷史期間。RPS 不轉換為 RPM；USD、歷史請求與 rounded cost 不變成 requests、tokens、精確剩餘或執行上限。
- `notes`、`sources`：有界文字及核准官方網域的 HTTPS 來源，不接受電子郵件、credential 標記或帶 query／認證的 URL。一般未經此 loader 驗證的 Gateway 來源不會公開其 limit source 或 scope。

未知 remaining 必須保持 `null`，不由已知 limit、local cap、空帳本或歷史使用量倒算。唯一支援的派生快照形式是 Cloudflare 日 Neurons 的 limit 減已用，標為 `estimated` 與 `derived_from_used_and_limit`，原始有效期不得超過 15 分鐘或 UTC 午夜重置。到期後保留原始歷史推算值並標示已過時，`effective_remaining` 為 `null`；重新載入不能續期。未核對的擴充供應商必須保持停用、免費資格 false、資格時間 null、provider limits 空與 capacity unknown。

## 真實 Gateway 模式（需另行授權驗證）

必須明確指定 `--upstream` 與 `--upstream-token-file`，不能與 `--demo` 或 `--reviewed-config` 同時使用。兩個權杖檔皆檢查擁有者、私有檔案權限與 symlink；兩個值必須不同。上游只接受 `http://127.0.0.1:<port>`，沒有其他主機、DNS、proxy、redirect 或路徑前綴支援。

```bash
quota-broker dashboard-serve \
  --upstream http://127.0.0.1:18084 \
  --upstream-token-file /private/path/gateway-client.token \
  --dashboard-token-file /private/path/dashboard.token
```

上游 client token 只保留在 Python 程序，瀏覽器不會取得。上游存取固定為 `GET /v1/catalog`、`GET /v1/model-inventory`、`GET /v1/diagnostics`、`GET /v1/usage`、`GET /v1/tasks?limit=6` 及 `POST /v1/routes/explain`。歷史報表只允許在 `/v1/usage` 加入經驗證的 `provider`、`from`、`to`、`task`；沒有任意 URL／query 轉送。無 execution、queue 或 admin 路由。每次上游回應有 2 MiB 上限與有限讀取時間；單次 HTTP 連線 timeout 為五秒。來源失敗或無效形狀回傳固定錯誤，介面會隱藏舊快照，不把快取資料當目前讀值。

## 建議 API

`POST /api/login` 接收 `{"token": "<dashboard-only token>"}`，取得 session cookie；`POST /api/logout` 撤銷目前 session。每個 POST 都要求 `Content-Type: application/json`、正確 `Content-Length`、與工作台一致的 `Origin: http://127.0.0.1:<port>` 及 `X-Dashboard-Request: 1`。請求上限 64 KiB、JSON 深度上限 16；不接受 transfer encoding。登入連續失敗五次後有一分鐘內的節流。

持有 cookie 的用戶可呼叫 `GET /api/snapshot`；`POST /api/advice` 使用[既有 Gateway Task JSON](client-integration.md)。回傳包含來源／快照時間、`decision: recommend | ask_user`、可空值的 `selected_target`、依 Gateway 順序排列的 `candidates`、原因與可空值的 `recheck_at`。此 API 與瀏覽器共用一份政策。文字表單是其中較小的操作入口。

推薦必須同時滿足：最新 diagnostics 就緒；供應商、模型、target 身分一致；憑證名稱清單目前有效；免費資格目前有效且未啟用計費；Gateway 候選適合此任務；每個配置與觀測的 metric/window 都有目前有效、權威且足夠的餘額證據。保留 Gateway 候選順序，只跳過較嚴格安全門檻未通過的候選。

成本分別檢查 requests、input tokens、output tokens、total tokens 與已知 neurons；`tokens` 僅與 total tokens 同義化。未知資源成本、缺少讀值、過時、局部證據、空值、付費或不足都必須 `ask_user`。不將 input 與 output 的分開觀測相加來虛構 total token 餘額。重複或矛盾觀測按保守方式檢查，不採最後一筆覆蓋。日／月本機 cap 使用非 UTC 時區，而觀測無法證明視窗對齊時，也停止推薦。尚未提供完整批次規劃、throughput 保證或自動執行。

## 數字與期限的界線

- **本機上限**是設定的保護值，不能代替供應商剩餘額度。Gateway catalog 另外提供 `local_ledger_usage`：沿用 admission 的當前 metric/window/timezone 帳本讀值，包含保留與輔助 request charges。介面顯示同維度的 used/limit 與比例；舊版上游缺少此欄位時保持未知，不由歷史聚合倒算。catalog 讀取不清理到期 reservation，因此仍未清理的 charge 可能被保守計入。
- **本機記錄用量**是已送出生成嘗試的聚合，與供應商帳單、其他帳號用量、輔助請求與當前視窗分開。保留量屬於本機紀錄；觀測有效餘額使用 Gateway 已扣保留量的值。不同單位或視窗不合計為「總配額」。
- **憑證清單有效**只代表所需名稱存在且清單未到期，未讀取或驗證憑證值，更不保證 provider 接受請求。
- **免費資格證據到期**是本機需要重新確認；不是帳號或點數到期。**餘額觀測到期**表示讀值不再可用；不是重置。**reset_at** 是供應商視窗重置資料；只有明確提供才顯示。獨立帳戶 metadata 的 grant.expires 可提供實際點數到期；僅日期時保留原日期與未知時間／時區，不製造午夜或倒數。
- `recheck_at` 只是可重新核對的估計時間。Gateway 可能從觀測期限推導它，不能當保證重置、可自動重試或自動排程的許可。
- catalog、diagnostics、usage 與 recent 不會讀秘密或呼叫 provider。`Gateway.explain` 可能清理已到期且未送出的 reservation，會有帳本 bookkeeping 寫入；工作台不能宣稱零資料庫寫入。它不建立 reservation、dispatch 或 queue。

最近工作僅投影 provider、model、state、建立／完成時間與 latency；不提供 request key、提示詞、回覆、provider request ID、secret ref 或原始 diagnostics。頁面只用 DOM 文字節點顯示來源值，且沒有外部字型、腳本、資產或前端建置工具。

驗證指令：`uv run --offline --locked pytest -q`、`uv run --offline --locked ruff check src tests`、`uv run --offline --locked ruff format --check src tests`、`uv run --offline --locked mypy`、`node --check src/quota_broker/dashboard_assets/dashboard.js`。離線測試涵蓋真實 Gateway 選路、未知／過時／不足／付費阻擋、不同成本與單位、session/origin/route/body 邊界、權杖反射、redirect 與回應大小；真實供應商、正式服務與人工接受仍需各自驗收。


## 獨立點數帳戶與活動來源

`--accounts-file /private/path/accounts.json` 可在原模式旁加入 schema 1 帳戶觀測。此檔與上層目錄必須是目前使用者擁有的 `0600` 一般檔案／`0700` 目錄，不接受 symlink、重複鍵、不支援欄位、credential 標記與非核准來源。檔案上限 128 KiB、JSON 深度 16、帳戶上限 8；目前只接受 OpenAI 帳戶欄位。真帳戶值只存在 ignored 私有檔，不能寫入原始碼、資產或此文件。

顯示 ChatGPT 訂閱附帶的 OpenAI API credits 使用已核對的帳戶快照，不需要 API key。若之後接入 OpenAI 模型呼叫，請在與 ChatGPT 連結的 OpenAI API 組織選擇專案，將該專案的 API key 存在 Doppler 的 `api-quota-broker` 專案、`dev` 設定下，名稱為 `OPENAI_API_KEY`。目前帳戶顯示與這個金鑰獨立。

Broker 的 [OpenAI 模型目錄](../registry.openai.example.json)和[停用路由設定](../gateway.openai.example.json)提供 `gpt-5.4-mini` 的純文字候選。它引用 `OPENAI_API_KEY`，固定使用官方 Chat Completions，並設每次最多 64 個輸出 tokens 的本機上限。樣板不合併現有 Gateway 設定。實際啟用前須核對該專案的模型權限、最新點數與計費狀態，並確認有明確的呼叫額度。未知計費在樣板裡使用阻擋值，不能當成已確認啟用計費。

頂層為 `schema_version: 1` 與 `accounts`。逐帳戶包含 `provider`、`qualification`、`billing`、`currency`、`balance`、`grants`、`benefit`、`model_limits`、`evidence`、`usage_page`、`notes`。金額為非負十進位字串或 null，最多 6 位小數，不以 binary float 進行扣除或換算 tokens。grant 的 `received`／`expires` 為 null 或 `{value, precision}`；`precision: date` 必須是原始 YYYY-MM-DD，`instant` 必須帶時區。`benefit` 分開保存條件式訂閱補充、可空的下次金額／時刻與是否結轉。這些帳戶不是 Gateway targets，不能授權或推薦執行。

`evidence` 具有獨立 `as_of`、`valid_until`（最多 24 小時）、來源與觀測精度，不借用舊人工稽核時間。snapshot 回傳原始 `balance` 與只在證據有效時提供的 `current_balance`；過時仍保留歷史值、明確標示，重新整理／重啟不續期。每個模型的 RPM／TPM／IPM 與 Batch queue tokens/day 是**觀測上限**，不是一般每日可用量或剩餘；同筆 USD grant 不會按模型重複計入。官方 Usage 頁 `no_data` 與該期間／篩選範圍獨立顯示，摘要的零不當成已確認帳戶零消耗。

人工核對模式未接入實際活動時，`usage`、`recent` 為 null，`activity_source.state: unavailable`；不拿臨時空帳本顯示實際用量為零。可另加成對的 `--activity-upstream http://127.0.0.1:<port>` 與 `--activity-token-file /private/path/client.token`，僅適用 `--reviewed-config`。此來源只允許 `GET /v1/usage`（含已驗證的歷史報表篩選）與 `GET /v1/tasks?limit=6`，不讀 catalog、不解釋路由或派送。服務端讀私有 client token，且不得等於工作台 token。活動不可用或形狀不完整時，只把活動降為未知；原帳戶證據仍可閱讀。接入後的空陣列只表示該本機帳本無記錄，不表示全部供應商帳戶零用量。`source`（供應商快照）與 `activity_source`（本機活動）的來源／本機取得時間分開；活動的 as_of 是本次帳本讀取時間，不是官方帳單觀測時間。

## 模型目錄與歷史報表

在「供應商與模型」選擇「供應商 API 與型錄」可按供應商、任務、模型 ID、輸入及輸出模態查詢。模態與任務在同一個能力項目上匹配。展開供應商可查看 API 家族、文件與盤點範圍。預設顯示可參考型錄，也可查閱退場模型或開放權重。

`GET /api/snapshot` 的 `provider_inventory` 來自隨應用出貨的 `dashboard_assets/provider-inventory.json`，沿用工作台 session。每一組清單有來源、原始核對時間及涵蓋範圍，包含公開目錄、官方文件和已取得的金鑰清單。後者只投影模型 ID 及能力。型錄只用來瀏覽，帳戶證據與 Broker 配置用精確的供應商和模型 ID 對照。重新整理只讀取本機快照，更新盤點時需核對來源後更新隨附資料。

`GET /api/snapshot` 的 `models` 投影目前 instance Registry；`model_inventory.basis` 固定為 `instance_registry_not_provider_complete_catalog`。同一家可有多個已註冊模型，不代表已列出其全部官方模型、目前帳戶有權使用或已完成 API 驗證。每個模型提供 `capabilities`、`features`、`input_modalities`、`output_modalities`、context／output envelope；這些能力依既有 Registry 與 adapter 契約，沒有另外查供應商或啟用模型。`model_scope` 區分 `configured_gateway_target` 與 `registered_unconfigured`，後者 `configured_target_ids` 為空、不可執行且餘額未知。模型本身沒有 quota／balance 副本；只可用相同 provider/model 的 target 讀取其既有配額，不把同廠其他模型的剩餘額度移用。舊上游未支援 `GET /v1/model-inventory` 或身分關聯矛盾時，目錄標為 unavailable，既有帳戶與 target 快照仍可閱讀。

持有工作台 session 可呼叫 `GET /api/usage?from=<ISO帶時區>&to=<ISO帶時區>&provider=<id>&task=translation`，所有條件皆可省略；公開子路徑模式為 `/quota/api/usage`。`from` 含、`to` 不含，依完整帳本的 `dispatched_at` 查詢並按 provider/model/task_type 彙總，不只篩選最近六筆，也不將舊總量拆分到猜測日期。這是已送出生成嘗試，含多次候選嘗試；不是使用者工作數、供應商全部帳單或未送出的保留量。未接入、上游失敗或形狀不完整時 `rows: null`，不能當成零；有效來源回傳空陣列只表示該本機帳本、期間與篩選無記錄。不同資源單位分欄，不相加為一個總用量；部分 provider 回報未知由相應 unknown count 與 null 保留。

回應保留 `filters`、`source`（本機取得時間與來源）、`coverage`、`task_metadata` 與有界 `rows`，每次最多 256 個彙總列、2 MiB；不含 request key、提示詞、回覆、provider request ID 或秘密。Gateway 在既有 task／attempt 表加入可空的 `task_type`；只有新工作通過 Task 驗證後才保存內建 capability，不能保存自訂標籤、專案名或內容。舊紀錄保持 NULL，報表投影為 `unknown`，不從模型反推、也不回填。不同 capability 即使使用同一模型也分列；`task=unknown` 只查未記錄類型，其他 `task` 值只接受已支援的內建 capability。

新版來源 `task_metadata.state: available_for_new_records`，basis 為 `validated_capability_for_new_records_legacy_unknown`，不表示歷史紀錄已分類。上游使用 `task=all` 明確核對任務維度支援；舊版拒絕新參數時，只有未指定 task 的報表可降級讀舊彙總並標為 `task_metadata.state: unavailable`、全部 `task_type: unknown`。明確 task 篩選在舊版仍回來源不可用與 `rows: null`，不能把不支援篩選誤報為零。此版不新增活動資料庫，也不自動同步官方帳戶餘額。

## 明確 HTTPS 子路徑模式

### 集中管理的獨立網站密碼（2026-11-01 實作候選）

正式 Quota 入口使用 `--dashboard-password-file /etc/api-quota-dashboard/browser-auth.json`，與舊 `--dashboard-token-file` 互斥。首頁 `/#passwords` 的站主管理只負責更新這個服務的網站密碼；Quota 有自己的 cookie，不會取得首頁、Notes 或 Slides 的登入權限。此段記錄程式契約，實際部署與本人改密碼／登入驗收另外記錄。

canonical 檔案必須為 `root:api-quota-dashboard 0640` 的單一連結一般檔案，上層目錄由 root 擁有且不可由群組／其他使用者寫入；拒絕 symlink、hardlink、重複 JSON 欄位、超過 8 KiB 或讀取期間漂移。Schema 沿用 access 文件：`schema_version=1`、`kind=scrypt`、`verifier` 為固定 `scrypt:32768:8:1` 格式、`revision` 為 canonical UUID。不要將實際 verifier 加入 Git、日誌、聊天或部署報告。服務只讀此檔；只有受限 root worker 可透過驗舊密碼、CAS、備份與同目錄原子替換更新，無須重啟服務。

新密碼為 12–1024 字，合法 Unicode 與首尾空格原樣保留，拒絕 CR／LF／NUL 與無效 Unicode。登入用標準庫 scrypt（固定 N/r/p、64 MiB maxmem、64-byte digest），先限型別／長度及節流，再以全域單一登入 KDF 執行。登入在 KDF 前後重讀文件；revision 或 verifier 變更即拒絕，不發 cookie。每個受保護 GET／POST 及資料產生後都重新核對目前 revision；缺檔、格式／權限錯誤均 fail closed，舊 session 不會使用快取 verifier 繼續存取。

`POST /quota/api/login` 保留既有 `token` JSON 欄位以相容前端協定，在此模式代表網站密碼；它不是 Broker client/admin token 或 provider key。舊 loopback demo token 模式仍可使用相同登入欄位。HTTPS cookie、精確 Host／Origin 與 advice-only 資料契約維持。

已授權反向代理部署時，可使用：

```bash
uv run --offline --locked quota-broker dashboard-serve \
  --reviewed-config /private/path/reviewed.json \
  --accounts-file /private/path/accounts.json \
  --dashboard-password-file /etc/api-quota-dashboard/browser-auth.json \
  --public-base-url https://momonong.me/quota/ --port 18586
```

程序仍只綁定 `127.0.0.1`。public base 必須明確為無 port／帳密／query／fragment 的 HTTPS `/quota/` URL；不由 Forwarded 或 X-Forwarded-* 推導。代理須保留設定的 Host。`/quota` 固定 308 至 `/quota/`，只有此前綴下的靜態資產與 API 有路由；`/api`、根資產或其他 prefix 不會取得資料。HTML、brand、JavaScript fetch 使用相對路徑，保留前綴。登入頁為通用頁；所有帳戶與 snapshot 都需要 session。

public 模式精確核對設定的 Host、Origin（POST 必須提供 Origin），使用獨立的 `__Secure-aqb_quota` cookie，`Path=/quota/; Secure; HttpOnly; SameSite=Strict`，最長八小時，登出撤銷同一路徑 cookie。預設 loopback 模式維持原 Host／Origin、`aqb_dashboard` cookie 與根路徑，不接受公用 Host。兩種模式都維持 body/session/no-store/CSP 與無執行端點邊界。反向代理、正式憑證、HTTPS 可達性、服務持續性與人工接受須由部署階段另外核對。
## 2026-11-03 Quota 產品整合（本機實作）

`/quota/api/snapshot`、`/usage`、`/advice` 是網站 session 保護的 facade；不是 raw Gateway 的公開代理。用量支援 `provider`、`model`、`task`、`from`、`to`；時間仍以送出時間、起點含／終點不含查完整本機帳本，不等於供應商全帳戶帳單。

三個工作台沿用供應商／模型、總用量、任務建議。人工核對檔為防止執行而 `enabled:false`，與真正 Broker 開關分開：`runtime_configuration` 唯讀比對 live catalog 的 provider/model；單一匹配才顯示啟用狀態，無資料或有歧義保持未知，不據此轉移帳戶資格／餘額。

排序的 `capacity.expires_at` 仍是證據期限；可選 `capacity.credit_expires_at` 才是精確、帶時區的 credit 到期時間。日期精度的 grant 保留於獨立監看帳戶，不轉成精確執行期限。已確認到期的 credit 拒絕使用；合格路由優先依已核對的精確 credit 到期／quota 重置時間，再比較有效剩餘比例；週期長度不能充當下一次重置倒數。建議列出資料因素，沒有模型呼叫。

新的 Dashboard upstream 身分必須由 Broker `AccessPolicyFile` 授予獨立 read/plan；不得再用 legacy execution client token。部署模板已使用獨立 encrypted credential 及同 UID 的 `/run/api-quota-dashboard/read_plan_token` 0600 副本。這一節描述 source 行為，不代表正式主機已啟用。
