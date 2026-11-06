# HP Quota 專用維護入口

2026-11-06：**HP 專用入口已安裝，新配對版本已經由 Doppler 專用身分部署到正式 `/quota`，收據為 complete 且 active=true。** Home 完成共用主機初始化，本對話完成維護協定、封包與發布驗收。不借用 `/sites/` 靜態部署身分，日常固定範圍發布不再要求 sudo 密碼或搬運指令。

## 已實作

`scripts/hp_quota_client.py` 提供 build、verify、status、publish、reconcile。前兩者純離線；後三者只走專用 `quota-deploy@192.168.68.67:22`，不提供 shell、PTY、密碼 fallback、static identity 或一般 sudo。

Doppler 範圍固定為 `selfhost-deploy/prd_quota_hp`，唯一秘密名稱 `QUOTA_DEPLOY_SSH_PRIVATE_KEY`。專用 config、key 與 read-only config Service Token 已建立，並以該 token 即時讀回核對一致；未覆寫其他 config。客戶端只接受本機 `.state/hp-quota-maintenance/doppler-token` 的 config-scoped service token，檔案 owner-private、目錄0700。Doppler 必須即時取得，停用 cache/fallback、核對 project/config 與完整秘密名稱集合；不使用環境中的個人 Doppler 登入。SSH key 只暫存於使用者私有 `/run/user/<uid>`，不寫原始碼/參數/日誌；補足 secret storage 可能省略的結尾換行，已用真 OpenSSH 解析測試驗證。

日常封包只含精確白名單的 Python、四個 Dashboard assets、五個 metadata 檔。網站認證、unit、固定launcher、Caddy與其他服務不在封包。新增 module 必須經 allowlist 審查。payload使用 Home 的 `hp-quota-release-v1`，傳輸envelope為 `hp-quota-maintenance-v1`。

發布依序 status核對CAS、validate、fsync本機intent、單次publish、以原operation讀回status。共用本機flock覆蓋整段流程；任何未解決intent阻擋所有新UUID。相同UUID不重送。reconcile 以原 ID、release、manifest 一致的 complete 收據解除未知狀態；rolled_back 額外核對原 baseline，記錄回復而不報發布成功。歷史操作成功與當前 active 版本分開，原紀錄保留。

`deploy/hp_quota_maintenance.py` 已實作伺服器端共用鎖、持久收據與未解決交易阻擋，涵蓋不同 checkout 與管理主機；client 不能替代 server 端保護。伺服器回應約定：

- status：current（release ID）、manifest_sha256 為實際目前版本；指定 operation 另包含 operation_id、status、release_id、release_manifest_sha256、previous、previous_manifest_sha256。
- validate：status=validated、manifest_sha256。
- publish：operation_id；客戶端仍需獨立status讀回才報完成。

## 固定應用啟動需求

root-owned 固定 launcher 綁定 current 指向的實際 release，再把該 release 的 app 與既有 `/opt/momonong-quota-readonly/site` 加入 Python 路徑。它以服務身分核對 manifest 與五份 metadata SHA，將原 bytes 複製到私有 0700 暫存目錄／0600 檔案，供舊、新版 owner-only reader 使用；不複製認證檔、不修改證據期限。root 不 import 應用封包，應用只以既有專用服務身分執行。系統程序 CLI 保持固定：

```text
dashboard-serve
--reviewed-config /opt/momonong-quota/current/metadata/reviewed.json
--accounts-file /opt/momonong-quota/current/metadata/accounts.json
--provider-metadata-file /opt/momonong-quota/current/metadata/provider-observations.json
--dashboard-password-file /etc/api-quota-dashboard/browser-auth.json
--public-base-url https://momonong.me/quota/
--port 18586
```

Home 首次初始化收據 `quota-maintenance-bootstrap-20261106-v1` 已 complete，先將原 44 檔版本保留為 `hp-inventory-20261106-baseline`，驗證後啟用專用身分。實機固定 unit SHA 為 `4eca3f6ff46d70c3299f51acfd13170f0076a14c120d2d04fcfb8ee97a66eb70`。日常應用封包不能替換此 unit、launcher、root helper 或政策檔。

## 本次配對行為

`POST /quota/api/advice` 的 `mode=match` 先檢查同一模型、同一任務variant的全部輸入/輸出與功能。跨adapter能力不可拼接；retired模型不復活。文字／翻譯完整任務才進入原Gateway成本及資格dry-run，媒體需求不會偽裝成文字工作。

任務能力建議 `recommended_model`／`shortlist` 與可免費執行的 `selected_target` 分開：不填提示詞仍可選模型，先按同一完整 variant 的能力核對，再按有效免費方案證據排序；相同證據等級才提供供應商多樣性。只有通過既有免費資格、有效餘額、成本與本機狀態檢查的路由才能進入 `selected_target`；其後先依已核對的實際credit到期／精確quota重置，再依各視窗最小有效餘額比例排序。週期長度與證據期限不當倒數；分母未知不算100%。型錄列名、API key存在、供應商上限均不代表剩餘額度或可執行。模型能力不是品質benchmark。

工作台仍是供應商／模型、總用量、任務建議三頁。第三頁加入完整型錄的任務/模態選單與阻擋原因，保留原詳細文字/翻譯表單。所有回應仍 `execution_allowed=false`、`paid_allowed=false`。本次未恢復Broker派送、不執行模型、不付費。

## 驗證與正式發布證據

- 配對、排序、Dashboard/session/API、Gateway family、framework與來源封裝：233 passed。
- 專用 client、root helper 與 launcher：53 passed；包括未知操作換 UUID、並發鎖、收據錯放、歷史收據／回復核對、故障接續、檔案權限、私有 metadata 快照與真 OpenSSH 私鑰格式／暫存清理。另用舊版 44 檔 app 與真實 metadata 完成離線啟動 smoke。
- Ruff通過；mypy 36個source檔通過。瀏覽器390/768/1440寬度核對，無水平溢出，沒有JS error；合成資料只用於本機QA。
- 完整suite首次有效執行為3202 passed、40 skipped、19 failed、43 errors。與本次改動有關的固定封包檔數及舊排序預期已修正並於233項範圍重驗；其餘舊ASUS維護案例有固定授權日期到期、歷史pin不符和user namespace環境限制，**不宣稱全套通過**，未改退役ASUS權限去迎合測試。
- 凍結候選 `.state/quota-matching-maintenance-20261106/frozen/candidate.json`：45檔、1,225,709 bytes、manifest `38f53d9a3b321fc7a1ed074f30da1fff1cf62d8035c8a43f4444df3df5f6eb41`。metadata五檔沿用上一成功版本原bytes，不延長證據期限、不把上限當餘額。
- 使用凍結app+真實metadata離線smoke通過：11供應商、OpenAI餘額未知；文字700、文件OCR9、圖片生成84筆能力候選均停止、零執行。
- 原 frozen 候選的 baseline 只屬歷史證據，未直接送出。正式候選位於同目錄 `production-v1/candidate.json`，經實際 scoped SSH status 核對後綁定 baseline `hp-inventory-20261106-baseline`／`edef84c98a173d6ec25208857cd84fdcc58b140fc364f467bea6bd960785aff5`。
- 正式發布 operation `7c5d4469-a489-48ae-9321-d14bdc29db69`，release `quota-matching-20261106-v1`；只送出一次 publish，獨立收據讀回為 `complete`／`active=true`，manifest 與凍結應用相同。首次兩次唯讀 status 因 key 尾端換行格式失敗，修正前未建立發布 intent、未送出 publish。
- root helper 已驗證 origin HTML/CSS/JS 完整 SHA、服務程序／固定引數、401 私有資料與404 執行／維護路由。外部 HTTPS 的 11 項狀態／權限檢查符合預期，CSS/JS bytes 一致；HTML 原始 hash 不同，只有一段367字元的 Cloudflare Insights edge script 插入，移除該單一差異後應用 HTML 完全一致。保留 `production-v1/public-html-diff-proof.json`，不把原始 hash 檢查假稱通過。
- 瀏覽器已刷新正式新版，正常顯示登入保護；服務切換後舊 session 失效。未要求重新輸入密碼，未聲稱完成新版登入後人工驗收；三工作台與配對互動沿用同 bytes 的本機驗證。
- 未提交、推送或合併 Git，未更動無關草稿；未建立未授權的每日排程。這是日常受限發布通道，不代表所有供應商已能即時自動回報餘額。

Doppler解決憑證供應，不會使沒有官方餘額API的廠商突然具備該介面。既有 `provider_metadata_fetch.py` / `dashboard_refresh.py` 繼續負責可用的唯讀來源；無法自動取得的餘額保留unknown，不能靠發布新版本刷新證據時間。

### Home 接收確認

Home 已核對本次人類授權，將已審首次初始化列為其既有維護批次的第一個獨立步驟並完成。它只替換現有 unit 的 ExecStart、保留認證與 hardening，先確認原 app 在新目錄正常，再啟用專用 SSH public key。本對話已接續完成一次真正的受限發布；原已 complete 的操作不重跑，本次 metadata 原 bytes 保留不變。

## 2026-11-06 任務建議修正

正式版 `quota-matching-20261106-v2` 已核對 complete/current，manifest `3fe3e4eae633b8ba38b9d475094426b356edd257a65dbc68d0e83256ea785394`。修正預設未附提示詞時所有模型被誤標不可推薦，以及快照停用被誤當 live 配置。輸入／輸出選單隨任務調整，候選附官方文件與分開的免費執行條件。114 項受影響離線測試、11 項正式 HTTP 路徑檢查通過，公開 JS/CSS 等於部署包；五份私人 metadata bytes 不變。使用者已確認問題解決。沒有模型呼叫或付費。

## 2026-11-06 額度總覽與按需資料更新

接受的配對修正版已合併 PR #6，`main=063ead242fb038bf6701cb556e7fdde4c65bf3c4`。後續額度 pipeline／介面工作位於 `feat/quota-refresh-experience`，與該 PR 分開。

額度總覽 app release `quota-refresh-20261106-v1`（operation `9d24e609-9ada-4a36-9c78-da2fb9f1f66d`）以現有入口發布成功。再執行 `quota_refresh.py run --base-package ... --publish`，已實際驗證單一流程的 Doppler 取值、OpenRouter GET、資料封包、發布及成功收據讀回。當前資料 release `quota-data-9e52a939-92cc-4f60-8e28-dcb26a08182a`，operation 同 UUID，manifest `f899442e34036af61267fe9e0da46559a0d1cafcebc2c1080d9a4750a98756ee`，`complete` 且 `active=true`。第二次只變動 `metadata/provider-observations.json`，40份 app 檔完全沿用；資格、路由、registry 不變。

351 項相關離線測試通過，最後新增的供應商觀測／模型額度區分另通過19項介面回歸；Ruff、mypy及獨立審查通過。凍結45檔封包以真實metadata驗證，`execution_allowed=false`。1440/768/390視窗檢查及三工作台操作通過，後兩尺寸無水平溢出。正式HTTPS11條既有權限檢查全通過，CSS/JS bytes等於凍結包；正式瀏覽器確認新總覽DOM與登入保護，未聲稱完成正式登入後人工驗收。

OpenAI於本次帳戶頁重新核對200 USD、2026-12-03僅日期到期；OpenRouter日計數於同步時剩50次、已用0／上限50。這是觀測，不保證日後仍相同。過時後歷史數字可見，當前可用值回未知；其他無支援介面／缺權限者明列缺口。0模型呼叫、0付費；76份其他草稿維持原SHA。Home已收到新的baseline與發布完成通知，本次不改其18993 Broker或共用密碼流程。

操作契約與官方來源見 [quota-refresh.md](quota-refresh.md)。私人證據保存於 `.state/quota-refresh-20261106/`，憑證不在封包或Git。
