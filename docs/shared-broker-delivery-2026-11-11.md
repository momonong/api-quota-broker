# Quota 共用層交付紀錄 — 2026-11-11

## 目標與目前邊界

目標是讓專案透過同一個私有介面提交任務、共用免費容量、取得結果，並在 `/quota` 看到同一本帳本與相同選路證據。提供者型錄、獨立測試成功與共用正式路徑成功是三種不同證據。

HP 共用 Broker、專案隔離、Doppler 專用維護入口與私有 SSH 轉送已部署且完成介面／拒絕邊界驗證。2026-11-12 已取得真實試用授權並執行第一筆共用 OpenAI 任務，但結果 UNKNOWN；已修正重複 CORS 標頭解析。尚未完成共用端到端驗收，詳見下方最新紀錄。沒有付費、綁卡、儲值或重送舊 UNKNOWN。

## 已部署與驗證

| 項目 | 實際證據 |
| --- | --- |
| HP Broker | `shared-broker-20261112-v4`，manifest `1088f4bb919dd514892c963152f3d2f6b4db9a46e63daa2076eb1022ff40f124`；operation `b164e1f2-4dab-4e05-a2d7-e7f717646cf9` complete、active readback |
| Dashboard | `quota-shared-workbench-20261111-v4`，manifest `30fbd08e12e1eb2450bd7f1df5ec0662fd5a26c83fb338d357722514cd602174`；operation `bf868c44-7973-4f5c-9c55-44200a9d15b7` complete、active readback |
| 專用維護入口 | `quota-broker-deploy` forced-command，Doppler SSH key；一次實際 publish／readback 完整通過，無人手 sudo。Helper SHA `2bbbc96dbe155b72ab110adc801f375c660beea7373079cc46840dc09651b365` |
| 私有專案連線 | `quota-broker-client` 只能轉送至 `127.0.0.1:18588`；其他 port 與 shell 的實機請求被拒絕 |
| 存取隔離 | 匿名 jobs 401、網站 read/plan token 403、Owner 與 KH 各自 GET 200；超 512 output tokens 的任務 403，之後查無該工作，沒有送到模型 |
| 資料真實性 | OpenAI owner 最後觀測 200 USD；獨立測試保守預留扣除後本機可用 199.999 USD。OpenRouter 官方日計數最後讀值剩 49/50；runtime 每五分鐘查詢。兩者不可混稱即時帳單餘額 |
| 用量帳本 | 新共用帳本 1 筆 OpenAI UNKNOWN、實際 tokens 未知，保留 3000 credit micros；`historical_gap=true`，原 ASUS／KH 帳本與未知操作沒有被重建或覆蓋 |
| 網站源站 | HP Caddy `/quota/` 登入頁 200、JS/CSS 與候選 bytes 相同；snapshot 未登入 401，模型 jobs 公開路徑 GET/POST 都 404 |

公網 HTTPS 的本次程式化探測被 Cloudflare 1010 拒絕；它表示依客戶端簽章阻擋。沒有修改 Cloudflare 防護或更換身分重試。因此源站檢查不等於已完成外部瀏覽器驗收。參考 [Cloudflare 官方 1010 說明](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/)。本次 CUA 沒有可用瀏覽器表面，未宣稱完成視覺驗收。

## 更新失敗與實際修復

首次日常 v2 更新因新的 OpenRouter counter 時間被誤用來延長資格截止，超過既有 secret inventory 截止而遭 runtime 拒絕。原 operation `811afab1-a6be-401d-85d4-58cd1578a659` 保留為 UNKNOWN，先讀回失敗收據與檔案 metadata，確認無程序、帳本權限完整後，清除指定 unit 的 failed 標記並沿原操作 rollback；最終收據為 rolled_back。

修正後，counter 更新保留原資格截止，client 在任何 Doppler／SSH 動作前執行完整跨檔語義檢查，root helper 也獨立檢查期限與憑證／容量綁定。failed unit 只有在無 PID／Job、固定 cgroup 為空且沒有子群組時才允許清理。SQLite 唯讀核對明確關閉 connection。修正 helper 的更新不重啟服務、不開啟帳本；其後 v3 日常 publish 成功。

原 v2 的 intent、收據、回滾與修復紀錄均保留；沒有以新 UUID 規避尚未解決的 UNKNOWN。新 v3 只在原操作確定 rolled_back 後發起。

## 測試與接續

核心整合 fixture suite 524 passed；Dashboard／任務配對第一輪 180 passed；後續顯示真實性 117 passed；維護與配置修復合併組 71 passed。數量含重疊案例，不相加當成總測試數。Mypy 36 modules 與相關 Ruff 通過。最後日計數／用量／UI 回歸 102 passed，補充 UI 時間標示後 31 項再通過。這些測試使用合成 transport／資料，不能視為真實模型驗收。

Knowledge Hub 接入資訊已傳送至既有對話 `01a1c654-8db1-7e23-bf1d-4fcd3a003d6e`，僅包含文件、私有入口、專屬 Doppler reference 與舊 UNKNOWN 禁重播限制；未傳送秘密，沒有授權其新增模型呼叫。訊息送達不等於 KH 產品已改接、部署或驗收。

Home 既有指定對話已封存，未自行恢復；部署依已核對的 Home main `955b4f8cd73c4eeaaf894445afbdbc919326d6ed` 及現有 HP 契約，保留首頁密碼整合、Caddy、Receipts `18993` 與其他服務。此階段沒有提交、推送或合併 Git，原草稿保留。

尚未完成的驗收：OpenAI 原新測試 UNKNOWN，OpenRouter 被專案占位上限阻擋、沒有遠端工作；目前待使用者決定是否保留未知額度並解除占位以進行新的有界驗收；其他九家的帳戶資格／可用容量／缺憑證阻礙詳見 [接入文件](shared-broker.md)。期限不明與餘額不明維持原證據狀態，不能自動付費或把方案上限當餘額。

## 最終資料讀回

正式 Dashboard 組裝路徑驗證通過（含不指定廠商的自動試算：選擇 OpenRouter，與 Gateway 一致，沒有呼叫模型）：11 個 provider targets，9 個停用項目的付款狀態為未知；OpenAI／OpenRouter 兩條建議都與正式 Gateway 選擇一致。首頁與用量報告讀到同一個 OpenRouter 日計數：2026-11-11T13:00:10.515391Z、已用 1／上限 50／剩餘 49，UTC 翌日 00:00 重置，讀值有效至 13:15:10Z。實際服務繼續每五分鐘更新，以頁面當時 as_of 為準。

200 USD 是使用者核對的剩餘點數；原始核發總額未確認，已設為 null，因此沒有用 200−200 捏造已用 0。原始到期日期 2026-12-03 保留。帳戶讀值有效期限為 2026-11-12T09:59:49.597652Z，並不表示點數在那一刻到期；若仍無新證據，OpenAI 路由會停止，不能自動延長。

最後源站資產比對通過（JS `88063c12568fdfcffd65b1264d1a98108b3d5e4bb716335b9335b32a9a72e6a0`）；未登入 snapshot 401，公網 jobs GET/POST 404。這仍不替代外部瀏覽器視覺驗收。

## 本機收據索引

- `.state/shared-broker-20261111/`：bootstrap、access、helper repair、源站／私有邊界與 runtime readback。
- `.state/hp-shared-maintenance/`：原 v2、rollback 與 v3 daily publish durable receipts。
- `.state/hp-quota-maintenance/`：Dashboard 部署收據。
- `.state/provider-acceptance-20261111/`：先前獨立提供者測試與本輪核對的帳戶來源；不是新增共用模型驗收。

### 先前有界驗收準備紀錄（已由下方新進度取代）

`.state/shared-broker-20261111/accept-shared-once.py` 與 `shared-e2e/plan.json` 固定兩個新工作、每家最多一次、64 output tokens、`max_attempts=1`、`wait_policy=reject`；執行前需新的可查證人類授權紀錄，舊的獨立測試授權或 Goal 自動接續均不適用。尚未建立 authorization 或 run intent。

6 項離線測試通過：無授權不讀憑證／不連線、舊授權不適用、單次 POST、重跑原 intent 拒絕、UNKNOWN 只查原工作、錯誤答案不重送。預備流程會核對正式 release、當前資格／容量，再經 ProjectClient 執行並驗證回答、實際 tokens 及跨專案結果隔離。沒有新增真實模型呼叫。

同次重查 CUA inventory 仍是空的 apps／browsers；沒有可接續的外部瀏覽器驗收表面。此時剩餘真正模型驗收仍等待既有問題的使用者回覆，不以自動續跑或等待時間代替批准。

## 2026-11-12 真實驗收與修正

使用者訊息 `01a1cbe1-daff-73d0-9c99-bb37df4871e5` 明確允許真實試用。套用至預備計畫：兩家各一次、64 output tokens、OpenAI 贈額保守上限 US$0.003、OpenRouter 僅零價格。沒有付費、儲值或變更付款方式。

- `shared-accept-20261111-openai-v1`：經正式 ProjectClient／私有入口送出一次，約4秒後成為 UNKNOWN；Gateway記錄 phase=transport / ProviderError，沒有保留可確認的答案或 tokens。3000 credit micros hold 保留，原 key 不重送。
- 已用無憑證的 OpenAI `/v1/models` GET 重現合法回應包含大小寫不同的重複 `Access-Control-Expose-Headers`，舊 parent parser 會拒絕；這是已重現的相容性缺陷，不能據此聲稱已找回原模型回答。修正只合併明確的 list header，仍拒絕長度衝突及互相矛盾的 request ID。
- 修正通過 family/shared provider/credit fixtures及本機 client測試；HP唯讀 GET 驗證 normalized headers valid。發布包固定沿 v3 bytes，只替換 `family_transport.py`，避免把同目錄其他 Dashboard 草稿一併部署。v4發布收據 complete，帳本 inode與歷史紀錄保留。
- OpenRouter 原定工作已有本機 intent，但原 key在正式服務 GET status/result 均404；Owner專案 max_inflight=1，唯一占位由上述 UNKNOWN保留，因此工作沒有建立或送到廠商。不得把本機 intent當成模型呼叫成功，也不能盲目重跑 acceptance runner。
- 最新唯讀資料：OpenAI保守可用199996000 credit micros、hold3000；OpenRouter官方49/50日請求（as_of 2026-11-11T18:42:47.889945Z），runtime繼續每5分鐘更新。

### 等待決定的占位恢復

已向使用者明確詢問：永久保留原 UNKNOWN、以US$0.003保守計入預留，不重送舊工作，只解除執行占位，再各做一次新短測試。尚未收到批准。這不是解除付款保護或還原額度。

本機候選新增 `unknown_slot_acknowledgements` 獨立收據，只有明確operator方法可寫入；無HTTP入口、自動timer或startup動作。寫入需原reservation/attempt/task均UNKNOWN、唯一project關聯、執行等待期與queue lease結束、preview evidence hash不變及人類訊息ID。target/shared/project只解除已acknowledged UNKNOWN占位；配置變更與legacy snapshot guards維持。charges、observed_holds、reports、execution_completion、job/task狀態與原request key均不改。

聚焦9項測試通過，涵蓋錯誤owner、非UNKNOWN／缺失關聯拒絕、保留額度、重啟、queue不重播、多attempt缺口及並發claim仍遵守上限。相關原suite127通過；8項受沙箱loopback限制者另在授權本機測試環境全部通過；專案HTTP隔離／client46通過。獨立審查未發現blocking缺陷。Mypy4個來源與Ruff/diff check通過。

候選包 `.state/shared-broker-20261111/runtime-v5-acknowledgement-candidate.json`：operation `4d41ee0e-859c-46da-9ecd-7f4ba01c0755`，manifest `435d6251d045b25789978e4077ef53ddafd511d48614ca02a7484d72e861bbcb`，僅離線準備；沒有部署、沒有寫入正式acknowledgement、沒有額外模型呼叫。Operator CLI為 `scripts/quota_acknowledge_unknown.py`，日常維護仍透過Doppler，使用者不需貼指令或輸入sudo。

本次CUA再次回報apps/browsers皆空；仍未宣稱公網登入後的瀏覽器驗收完成。Goal未完成。沒有進行Git提交、推送、合併。
