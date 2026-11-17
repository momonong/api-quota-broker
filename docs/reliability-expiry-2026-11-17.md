# Broker 2.1.0 到期優先、可靠性與用量驗證

本次使用者明確授權修正帳務、用量視覺化、供應商模型盤點與排序、過載自動重試、HP 現場核對、Cloudflare Analytics Read、免費推論驗收，以及部署、推送和合併。工程驗證、部署及人工接受仍分開記錄。

隔離位置：`/home/ubuntu/.codex/worktrees/quota-reliability-expiry/api-quota-broker`，分支 `fix/quota-reliability-expiry`，基底 `d03ee867c04bb7ed7039183db0b2ac4374ff5f91`。原本 dirty checkout 與其他任務變更未覆寫；正式服務已切換最後通過完整測試的 2.1.0；Cloudflare／NVIDIA 後續推論驗收仍待限定 UNKNOWN 並行槽操作確認。Git 交付另以合併紀錄核對。

## 實作

- 配額觀測在 reset 與 TTL 較早者到期；日／月等窗口與不合理 reset 矛盾時不得派送。保留歷史讀值，沒有把已過期觀測重設滿額，也沒有釋放 UNKNOWN 消耗。
- `expiry_utility_v1` 在免費資格、任務能力、憑證、有效容量、健康與並行限制之內最大化連續效用。60% 到期急迫性；17% 任務適配×品質；15% 穩定性；5% 免費餘額比例；2% 延遲；1% 負載。`urgency=86400/(86400+seconds_until_loss)`，只接受已核對的 credit expiry／reset，不能把證據 TTL 當免費額度到期。
- 品質使用指定任務 profile 的 Beta(1,1) 後驗；穩定性使用最近 30 天、最多 100 次實際 attempt 的 Beta(2,1) 成功率及健康失敗修正。未量測品質維持 unmeasured，沒有用 API 成功冒充答案品質。模型官方用途只作 task fit。
- 所有排序使用同一分數與權威 selection_order。同分才沿用已配置 capacity/priority/ID；舊優先序不能蓋過分數差異。
- 完整同步過載拒絕可有界重試；預設最多 3 次實際 attempt。佇列以原業務 key 及累計 attempt 保存指數退避／jitter，尊重 Retry-After 與 deadline。每次嘗試獨立入帳；明確失敗可以釋放執行槽，但未回報 tokens/Neurons 的消耗仍保留。
- timeout、部分或不完整回應、202、含糊 502/504、未取得可靠拒絕證據的 UNKNOWN 不自動重送。此限制避免本來已執行的請求被重複消費，不是所有錯誤都可安全遮蔽。
- 用量圖表提供 UTC 每日生成嘗試／結果、明確回報 tokens、供應商分布與每日明細。完整回報、已知下限、未知、不適用與零分開。供應商區塊連結同一份已篩選帳本，不再錯接空資料；登出與來源錯誤清除舊圖。
- 總 tokens 只使用供應商明確 total；Google thinking 可能使 total 大於 input+candidate。缺少 total 時不以兩欄加總冒充完整消耗，input/output 的已知小計仍保留。
- [模型盤點](provider-inventory/README.md)及網站內模型清單已整合取得的 1195 個 provider/model identities。另保留 245 個官方文件／退場／weights 範圍項目；不是宣稱 1440 個唯一可執行模型。Cloudflare 75/334、HF chat-only 均 partial。新增 NVIDIA `z-ai/glm-5.3-flash` 與 Cloudflare Gemma 4 protocol；[候選配置](provider-inventory/route-candidates.disabled.json)保持 disabled，不能憑 listing 啟用帳戶。
- Cloudflare Gemma 4 Neurons bound 使用其 9091／27273 每百萬輸入／輸出係數；32768 input、4096 output 上界向上取整為 410。這是保守預留，不是實際使用。公式嚴格綁型號與證據時效。

## 管理介面

內部管理 token 可對 `POST /v1/admin/targets/{target_id}/quality` 提交：`profile`、整數 `passed`／`total`、非秘密 `evaluation_id`、帶時區 `as_of`／`valid_until`。`0 <= passed <= total <= 10000`、total>0，觀測目前有效且最長 90 天。同模型／profile／evaluation_id 重送相同值冪等，不同值拒絕；project token 沒有此權限。這個介面不允許一般服務自行把自己評成高品質。

管理者須使用有相同測試集合與判定標準的品質結果；本次只驗證演算法與證據保存，沒有真實模型品質 benchmark。因此目前不宣稱哪個供應商品質最好。

## 正式環境核對與部署

HP 原 release 的 51 個檔案全數符合 manifest；阻擋發布的是 17 個 Python 快取檔。限定 gate 已把快取保留於備份，沒有刪除或覆寫原程式。五份 runtime 設定以本機候選重建並逐份雜湊核對；保留 Google 503 修補、Knowledge Hub 32768 input／8192 output 上限、私人加密設定與持久帳本。沒有把 HP 設定或憑證匯出至 Git。

更新受限 Broker／dashboard 發布 helper 及其固定 hash 後，第一個 Broker release 因舊 launcher 拒絕明確 local-capacity 配置而未通過 readiness，原操作已完成安全 rollback。修正 launcher 的同一資格檢查後，以新操作發布 v2 成功。舊操作保留 rolled_back 收據；沒有將失敗改寫為成功。

目前 Broker `shared-broker-20261117-reliability-v3`、dashboard `quota-reliability-20261117-v2` 均取得 complete／active 收據。正式 [Quota 網頁](https://momonong.me/quota/) 可達且保留登入保護。服務僅更新 Quota，沒有更動其他網站路由或 Tunnel。Broker 最後 operation `092ad578-8bce-41e0-8223-84a6b4162801`，manifest `303b3efb33e4349be1ed9dd60f9b2801d427fbe789d76183c36d0160d3858264`；dashboard operation `6903ff72-ed79-4aa1-be5c-d0122cb67b8a`，manifest `7ae5a085eee1ab702d5e80fcd5457c8f5f81f6a49cb8d6a549db2e935abe9702`。兩者完整發布後均由受限通道獨立讀回 current／manifest。HP 四個 unit（Broker、dashboard、Caddy、cloudflared）均 active；正式瀏覽器重新載入顯示 Quota 登入頁。未帶認證的 Python urllib 網頁探測被 Cloudflare 回覆 403，沒有將它當作 app 身分驗證通過或 HTML hash 比對成功；未改動 edge 保護。

新路由 NVIDIA `z-ai/glm-5.3-flash`、Cloudflare `@cf/google/gemma-4-26b-a4b-it` 已使用原帳戶、原 bucket 啟用，且禁止付費。明確 `required_capacity: []` 表示由有效免費資格及本地保守上限管控，並不宣稱供應商即時剩餘額度已知。省略該欄不獲得此模式；Cloudflare 仍要求有效且型號吻合的 Neurons bound。資格過期會停止派送，不應令整個 Broker 無法啟動。

Cloudflare 原 token 已增加同帳戶 Account Analytics Read，保留 Workers AI 權限；沒有輪替 token、變更計費或升級方案。帳戶顯示 Free，每日 10,000 Neurons 是方案上限，不是目前餘額。新 GraphQL 查詢 HTTP 200、errors=null、groups=[]；權限已恢復，空資料仍不當成零消耗。NVIDIA 既有 key 與帳戶匹配、仍有效，免費 endpoint 的每分鐘限制可能依模型／流量變動，不當成每日 token 額度。

## 實際呼叫驗收

- 三個專案：owner、knowledge-hub、private-development 的正式加密 catalog 均可讀，分別有 13／13／14 個條目；private-development 只提供一個 local target、沒有可用 cloud target。這不代表每個應用自己的業務流程或每台設備都已逐一測試。
- 本機：既有 private-development 專案經正式 HTTPS 加密 API、local_only 呼叫 `Qwen/Qwen3.5-4B` 完成，取得 `LOCAL_OK`；未送雲端。只代表該服務路徑與這筆短文字工作成功，不外推所有設備或長時間可用性。
- Cloudflare 第一筆：HTTP 200，但 parser 無法辨識有效完成，原工作保持 UNKNOWN；一筆實際 attempt，保留 410 Neurons 和一天／一分鐘各一筆 request 計數。未重送、未偽造實際用量，官方 direct ChatCompletion 格式的精確解析與 malformed finish_reason 型別防護已通過完整測試並重新發布；原 UNKNOWN 未被改寫。
- NVIDIA 第一筆：同專案並行槽被 UNKNOWN 占用，唯讀資料庫查明該外部 key 的 ownership 為零；沒有被接受、沒有供應商呼叫。後續驗收保留原 key 與未接受證據。

目前限制：Cloudflare 及 NVIDIA GLM 都尚未取得最終成功推論證據，Knowledge Hub 的雲端派送受該 UNKNOWN 占位限制。限定操作已準備並通過 19 個新舊 acknowledgement 測試，但尚未取得此次人工確認，沒有套用。

結果未知的工作不能以新 key、重啟或釋放消耗繞過。既有 operator-only acknowledgement 可在明確人類授權後僅解除並行槽占用；原 UNKNOWN、預留及證據仍保留，這不等於完成或退款。

## 工程驗證

完整執行 `UV_CACHE_DIR=/tmp/quota-audit-uv-cache PYTHONDONTWRITEBYTECODE=1 uv run --locked --offline --extra encrypted pytest -q --tb=short -p no:cacheprovider`：**3748 passed、41 skipped、0 failed，272.64 秒**。包含 launcher、Gemma 4 direct ChatCompletion 及 malformed finish_reason 最後修正。

原先三個失敗已修好：恢復可審查的 `deploy/quota-web/gate.py`，公開入口、共用程式選擇及帳戶／資料／秘密隔離測試都通過，沒有 skip 原失敗項目。來源與範圍見 [gate 來源說明](quota-web-gate-source.md)。41 項 skipped 是受環境限制的 legacy ASUS user namespace 真變更測試；不是通過，也不是 HP production 操作。

56 個變更 Python 檔案的 Ruff check／format、全部 42 個 source files 的 mypy、前端 JS syntax 與 git diff whitespace 均通過。已修正 model inventory pretty JSON 超過前端 512 KiB 上限的問題，完整資料以 compact JSON 保存；未刪除歷史目錄來縮小檔案。

瀏覽器以真 dashboard_server、session、前端資產及合成帳本驗證 **1440／768／390** 三種寬度：登入、兩張日圖、篩選 9→6 筆及登出清除均通過，無水平溢出或 JS runtime error。供應商 SVG 長條比例 66.7%／22.2%／11.1%，誤差小於 0.2 個百分點；保留既有 CSP。圖表是合成資料驗證，不冒充正式帳戶用量或人工接受。

私人收據保留在隔離 worktree 的 `.state/reliability-release-20261117/`，含完整 suite、兩服務原操作、rollbacks、檔案 hash 與原工作讀回；不提交 Git。畫面證據保留於本機 `quota-repair/` 交付目錄。

## 驗證界線

尚未進行真實模型品質 benchmark；1,195 個目錄身份不代表全部免費、可執行或逐一實測。Cloudflare 與 HF 來源範圍仍有上述缺口。缺失的實際 tokens／Neurons 與供應商餘額保持未知，只有明確回報且一致的數字可用於結算。

Google 總用量語義依 [GenerateContent UsageMetadata 官方文件](https://ai.google.dev/api/generate-content?hl=en)：thoughtsTokenCount 與總數是獨立欄位，本版保留供應商 total，不以 prompt/candidates 兩項之和替代。
