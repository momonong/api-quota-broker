# NVIDIA 執行層階段規格與驗證界線

狀態：2026-09-29 本地候選實作；已驗證一次短時唯讀 Doppler executor 秘密讀取，尚未驗證 NVIDIA 帳號、金鑰有效性、人工驗收或部署授權。起始版本 `0f2ad46529f0a22db35541bc02c737a5c046635e`，工作分支 `feat/nvidia-executor-admin`。本階段保留原 Google / Cloudflare 直連客戶端與配額服務，新增獨立 NVIDIA 執行服務及同源管理頁。

## 行為契約

- 客戶端只帶 broker 的獨立 bearer token，呼叫 `POST /v1/nvidia/text`，JSON 為 `request_key`（穩定不含敏感資料、只用 URI unreserved 字元的 opaque ID）、`prompt`、`max_output_tokens`。固定模型 `google/gemma-4-31b-it`、關閉推理（`enable_thinking=false`），固定 `https://integrate.api.nvidia.com/v1/chat/completions`；客戶端不能提供網址、模型、金鑰或標頭。僅支援單則純文字 user message、非串流。成功時回傳文字及供應商 usage；後續同鍵只回傳狀態，不重送或保存回答。
- `GET /v1/nvidia/requests/{request_key}` 回傳狀態、非內容用量與帳本 input token 數，不回傳 prompt/answer。`usage` 在供應商回報可核對時含 `prompt_tokens`、`completion_tokens`，未收到可信供應商用量時為 `null`；若已收到用量但帳本核銷失敗，狀態可仍為 `unknown` 且用量已知。`accounted_input_tokens` 在未核銷狀態是保留估算，完成後為帳本入帳值。不同 payload 使用同一鍵回 409。HMAC 摘要金鑰須跨重啟保留；請求內容、供應商金鑰及回答不進入 SQLite、一般日誌或管理頁。
- 輸入 token 預留使用 `4 × UTF-8 位元組數 + 256`，其中 256 為 chat 模板與特殊 token 的緩衝；輸出上限為請求 `max_output_tokens`、profile 上限及本地政策 4096 三重約束。這是保守估算政策，不是 NVIDIA tokenizer 或實際計量保證；服務回報的 prompt usage 仍用於帳本核銷，外部使用與供應商計量差異仍可能使帳號超限。
- 承襲 broker 的 reserve → dispatch → report。派送前取得 Doppler 金鑰並檢查資格；派送可能已發生後遇到逾時、斷線、無 usage 或不明回應，保留 `unknown` 與占用額度，不自動重試。`preparing`/`dispatched` 若因程序中斷而留下，也不能自動重送。營運人員應對照供應商紀錄後再決定如何另行處置。
- 執行服務只接受 loopback，client 與 admin 是不同長 token。管理員經同源登入取得 HttpOnly、SameSite=Strict 記憶體 session；修改與測試需 Origin、Host、CSRF 都通過。管理頁只保存單一 profile 的中繼資料；初始表單可貼入 `nvidia-profile.example.json` 再填入已驗證事實。中繼資料包括：Doppler `secret_ref`、金鑰 ID/名稱、到期狀態（`unknown`、`never`、`at`）、scope、免費資格驗證時間與來源、啟用與計費旗標、本地安全上限（RPM/RPD/input TPM）、供應商配額證據、容量類別、並行與最大輸出。`local_safety_caps` 不代表官方限額；`provider_quota_facts` 的 limit/remaining 分別帶 provenance、as_of、source、scope 與 valid_until，未知值保留 null。未知到期或未驗證免費資格一律拒絕執行。金鑰值只在 Doppler UI 編輯。
- 管理頁提供唯讀供應商列表：Google/Cloudflare 的帳號狀態明標「未知（本頁無帳號資料）」；NVIDIA 顯示待設定、有效、金鑰到期未知／已過期、免費資格待驗證／已過期等可辨識狀態。頁面顯示服務啟動時指定的 Doppler project/config 與 secret ref 名稱，供管理者核對編輯目的地；不顯示秘密值。此狀態摘要以本地中繼資料計算，並非即時查詢 provider 或 Doppler 成功的證據。
- 管理頁「手動連線測試」會送出真實請求，可能消耗額度或計費；介面直接顯示此副作用。本階段只以 fixture 測試，沒有按下真實測試。

## 啟動與秘密來源

`quota-broker serve-nvidia` 需指定 SQLite DB 路徑、port（預設 18083）、`--digest-key-file`、`--client-token-file`、`--admin-token-file`、`--doppler-token-file`、`--doppler-project`、`--doppler-config`。這些路徑需由受限制的 service user 讀取，金鑰檔不得進 Git、指令參數值或一般日誌。digest key 至少 32 bytes，client/admin token 各至少 32 字元且不同。不能輪替 digest key 而仍期待同鍵辨識原內容；輪替程序需另行設計與驗證。

Doppler 讀取僅使用明確 project/config/name 的 HTTPS API `GET /v3/configs/config/secret`，不使用 CLI 快取、`.env`、環境變數掃描、重導向或離線 fallback；失敗即停止送出。優先考慮經核實可用的身份聯邦；HP/ASUS 尚未證實有可供 Doppler 信任的 OIDC issuer，因此部署候選為 config 範圍內 read-only Service Token，從 systemd 加密 credential 啟動載入。只給 executor 讀取，broker 核心與 admin UI 不讀取金鑰。正式部署前須核對 service token 權限、credential 路徑/ACL、輪替和失效流程。

## 憑證導入候選（尚未在任何主機執行）

`scripts/import_doppler_credential.py` 是互動式 Linux 導入候選。人工先審核目標主機、service owner 與既有 root-owned `0700` credential 目錄；腳本再次要求輸入主機名稱、`root` 與絕對目錄，檢查 systemd system manager、`systemd-creds` 和 TPM2，缺任一條件即停止。它從 TTY `getpass` 隱藏輸入 Doppler Service Token，以標準輸入交給 `systemd-creds --with-key=tpm2 --name=doppler_service_token encrypt - ...`；命令 argv、shell history 和聊天均不包含 token。暫存與目標檔只有密文，目標已存在時拒絕覆寫；腳本不建立 service、不修改 Caddy、不啟動服務。若主機無 TPM 或權限不符，需重新審查方案；不自動改用 plaintext 或 host-key 模式。Python 與 systemd 在導入過程仍會短暫持有明文記憶體，應在受信任的本機 TTY 執行，避免 terminal 錄影。

候選 systemd unit 應用 `LoadCredentialEncrypted=doppler_service_token:/受審核絕對路徑/doppler_service_token.cred`，並將 `--doppler-token-file` 指向 `%d/doppler_service_token`；client token、admin token、digest key 也需分開以受保護 systemd credential 提供。unit、服務帳號、DB 路徑、ACL、啟動順序與外部入口尚待目標主機審查，故本階段不提供可直接部署的 unit。Doppler Service Token 在 executor 記憶體保持到程序重啟；每次執行會重新向 Doppler 取 provider key，不保留 provider key 跨請求快取。撤銷 Service Token 後，新查詢應失敗關閉，但已進行中的請求仍可能用已載入的 provider key 完成；遠端撤銷生效時間不可承諾立即。輪替時需安全更新 credential 並重啟 executor，且不能重置未確認的請求狀態。

## 管理端 Doppler CLI 候選

官方 Doppler CLI 可讓具權限的人在**獨立管理者 OS 帳號**完成 `doppler login`，並針對已審核的 project/config 管理秘密；它不屬於 runtime executor，也不供任意 agent 使用。可用 `doppler login --scope /管理者專用目錄` 與 `doppler configure --scope /管理者專用目錄` 核對登入配置，但 `--scope` 只決定 CLI 本機設定的適用目錄，不是 OS 或 Doppler 權限隔離。實際操作仍須對管理者帳號與 Doppler 角色授權進行審查。不要把 personal login token、CLI 設定檔或輸出傳給 executor。

在管理者帳號已完成登入、目的地 project/config 已由人工核對後，可審查執行 `python scripts/set_nvidia_doppler_secret.py`。腳本要求輸入並再次確認 project/config，固定設定 `NVIDIA_API_KEY`，先以 `doppler --no-read-env --silent --project ... --config ... secrets --only-names` 檢查存取，再以相同顯式目的地執行 `secrets set NVIDIA_API_KEY`。金鑰由隱藏 TTY 提示取得、只經子程序 stdin 傳送；不在 argv、shell history、一般 stdout/stderr 或本地檔案。更新失敗只回報錯誤類別，不印出 CLI 輸出。此 helper 對未來 provider 可依相同安全模式另行擴充，但本階段不批量建立秘密，也不實際呼叫 Doppler。

不使用 `doppler run` 作為執行層秘密來源。官方文件指出其 encrypted fallback 快照可能在 token 撤銷後繼續供應舊資料；runtime 採每次向 Doppler API 直取、失敗關閉。管理端 CLI 的 project/config 指定也不代替 Service Token 的 config-scoped read-only 權限。本機於 2026-09-29 在使用者範圍安裝 Doppler CLI v3.76.6，官方 installer 完成 `gpgv` 發行簽章驗證，公鑰指紋核對官方 INSTALL.md；使用者後續已在本機完成 CLI 互動登入。管理端 set helper 仍只有 subprocess fixture 驗證，不宣稱遠端更新成功。

## 本機短時唯讀驗證

為先驗證 Doppler 而不依賴目標主機 TPM，管理者可在本機完成 Doppler CLI 互動登入（只限管理者；scope 不代表 OS 隔離）。先執行 `secrets --only-names --json` 並顯式指定 `api-provider-nvidia/dev`，只判斷 `NVIDIA_API_KEY` 名稱是否存在；不得執行會顯示值的 `secrets`、`secrets get --plain` 或 `doppler run`。網頁登入不等於 CLI 已登入。本機於 2026-09-29 使用上述顯式 project/config、`--no-read-env` 的唯讀名稱查詢，CLI exit=0；回應可解析為 4 個名稱，含 `NVIDIA_API_KEY`。原始 CLI 輸出已攔截而未顯示，也未讀取秘密值。這證實當次管理端 CLI 可查該 config 的名稱 metadata。

`scripts/verify_doppler_executor_read.py` 是須經 main／使用者核准的一次性工具：要求互動 TTY 再次確認；以已登入的管理端 CLI 明確指定 project `api-provider-nvidia`、config `dev`、`--access read`、`--max-age 5m` 建立單一短時 Service Token（名稱帶隨機後綴），CLI stdout 只被 Python 捕獲於記憶體，不出現在 shell argv/history/log/chat 或明文檔。接著用與 executor 相同的直接 HTTPS API adapter 讀取 `NVIDIA_API_KEY`，只印成功／失敗，絕不印 token 或秘密值，也不呼叫 NVIDIA。程序退出後不保留 token；該 access 在最多 5 分鐘內自動到期。若建立結果不明，必須於 Doppler Access 的 metadata 核對是否出現短時 token，不以重試建立新 token 代替核對。這個工具不是常駐服務的 credential bootstrap，也不變更前述 TPM 部署候選。

使用者於 2026-09-29 明確核准本機一次性建立及讀取。本機執行工具 exit=0，僅回報 `metadata_name_present: yes`、`executor_secret_read: success`、`temporary_access_expiry: 5m from creation`。隨後只查 Doppler access metadata：符合本次 one-shot 名稱的紀錄恰為 1 個，`access=read`、project/config 為 `api-provider-nvidia/dev`、`expires_at=2026-09-29T09:08:37.000Z`。token 與秘密值均未輸出或保存；沒有呼叫 NVIDIA，也沒有部署。這只證明當次短時憑證可經 executor 的直接 HTTPS API adapter 讀取指定秘密，不能證明 NVIDIA 金鑰有效、免費資格或真實推論品質。此 Service Token 五分鐘到期，不能作為常駐服務 credential。

## 本機一次性 smoke（與正式 admission 隔離）

2026-09-29 核對 NVIDIA 官方 [Gemma 4 31B IT 頁面](https://build.nvidia.com/google/gemma-4-31b-it)：`google/gemma-4-31b-it` 的 Free Endpoint 顯示 Available，範例使用固定的 `https://integrate.api.nvidia.com/v1/chat/completions`；[API reference](https://docs.api.nvidia.com/nim/reference/google-gemma-4-31b-it-infer) 支援純文字 message，並明示 `chat_template_kwargs.enable_thinking=false` 可關閉推理。原固定模型 `meta/llama-3.1-8b-instruct` 的官方 Free Endpoint 已顯示 Deprecated，因此改用此仍有免費端點的固定模型。這是產品頁的公開證據，不代表本帳號可用、剩餘額度或不會收費。

`uv run --locked python -m scripts.nvidia_smoke_once` 是僅供已授權、在本機 TTY 執行的一次性檢查。它沿用已登入的 Doppler 管理端 CLI，先只查 `NVIDIA_API_KEY` 名稱；以 `api-provider-nvidia/dev` 的整個 config 建立 5 分鐘到期唯讀 Service Token，記憶體中經 executor 的 HTTPS adapter 讀取秘密一次，再用相同固定路由 transport 送出 `Reply with OK.`，關閉推理，`max_tokens=16`。此路徑不使用 `NvidiaExecutor` 的正式配額 admission，也不填入猜測的 RPM、RPD 或 input TPM；程序只送一次、同時最多一個請求、無重試或付費 fallback。發送前建立忽略 Git 的 `.state/nvidia-smoke-once.json`，僅保存模型、狀態、時間、HTTP status 與可核對的 usage/request ID；不保存 token、供應商金鑰、prompt 或回答文字。檔案存在即拒絕再跑；超時、斷線或 HTTP 202 保留 unknown/pending，絕不再 POST。若 202 回傳可辨識 request ID，僅保存該 ID 供人工另行核對。CLI 只回報安全狀態與用量，不輸出秘密或原始 provider 回應。

使用者提供的 NVIDIA API Keys 頁截圖顯示名稱 `API Quota Broker`、狀態 `ACTIVE`、到期日期 `2027-09-29`；完整 key ID、精確到期時區、模型權限、帳號計費狀態及剩餘額度仍未知。目前無已知帳號證據與官方 Free Endpoint Available 矛盾，但這不等於已核定帳號免費資格。日期離本次測試逾一年，僅用於排除「顯示日期已過」的情形，不寫成正式 profile 的精確 expiry。若執行前出現計費或免費資格相矛盾的證據，必須在 provider 呼叫前停止。正式服務保持預設停用，沒有完整帳號專屬 profile 仍拒絕執行。

## 目前可驗證與未知

離線 fixture 可驗證固定路由、配額、生命週期、重啟去重、認證、CSRF/Origin、資料庫無 prompt/answer/key 明文，並以真實 HTTP 表單和 HTML 回應檢查登入後列表、狀態、合法 profile 儲存、用量摘要及手動測試流程。它本身不能證明 NVIDIA 帳號免費資格、真實計量、Doppler 權限、真實網路行為、跨主機瀏覽器路徑或人工 UI 驗收；上述一次性真讀只補足特定 Doppler project/config、Service Token 和秘密讀取路徑的證據。官方 NVIDIA 文件描述 API Catalog hosted preview 為 prototype 用途；不得把它等同正式生產免費承諾。正式 profile 的免費資格與計費狀態須有當下帳號證據；本地安全上限由操作者設定，不宣稱為 NVIDIA 官方限額。官方限額與剩餘額度若無證據就保持 unknown；只有仍在有效期內、官方來源確認的剩餘零額度才阻擋。舊版 profile 的 `rpm`/`rpd`/`input_tpm` 仍可讀取，但管理頁標示為 legacy；遷移時改置於 `local_safety_caps` 並加入 `provider_quota_facts`、`capacity`。本 repo 不預設公開限額。

## 部署候選（僅文件評估）

selfhost-servers 文件於 2026-09-29 的快照：HP 是 Cloudflare Tunnel + Caddy 入口，Caddy loopback 18080；ASUS 的 OrderFlow 使用 loopback 18081，HP 另有 ASUS forward 18082。文件記錄 HP 可用約 2.8 GiB、ASUS 可用約 1.6 GiB，皆為當時快照而非即時容量。建議初次候選為 HP 的獨立低資源 systemd 服務、獨立 DB/credential/port 18083；管理頁維持內網或受控入口，對外路由和認證方案需另行審查。正式部署前仍需現場唯讀核對主機、port、程序、資源與現有 Caddy/Tunnel 配置。此階段未 SSH、未改 Caddy、未新增公開 URL、未觸碰共用服務。

## 來源

- [NVIDIA LLM API](https://docs.api.nvidia.com/nim/reference/llm-apis)、[Gemma 4 模型與 Free Endpoint 狀態](https://build.nvidia.com/google/gemma-4-31b-it)、[Gemma 4 API 與關閉推理參數](https://docs.api.nvidia.com/nim/reference/google-gemma-4-31b-it-infer)、[Run Anywhere / hosted preview](https://docs.api.nvidia.com/nim/docs/run-anywhere)
- [Doppler 單一秘密查詢](https://docs.doppler.com/reference/secrets-get)、[Service Tokens](https://docs.doppler.com/docs/service-tokens)、[Service Account Identities](https://docs.doppler.com/docs/service-account-identities)、[CLI Guide](https://docs.doppler.com/docs/cli)、[官方 CLI 安裝與簽章](https://github.com/DopplerHQ/cli/blob/master/INSTALL.md)、[Secrets Setting](https://docs.doppler.com/docs/setting-secrets)、[CLI scope](https://docs.doppler.com/docs/multiple-workplaces)、[CLI fallback](https://docs.doppler.com/docs/automatic-fallbacks)
- [systemd-creds 手冊](https://www.man7.org/linux/man-pages/man1/systemd-creds.1.html)
- 共用基礎設施：`selfhost-servers/AGENTS.md`、`README.md`、`docs/infrastructure.md`、`docs/operations.md`，僅唯讀參考其文件快照。
