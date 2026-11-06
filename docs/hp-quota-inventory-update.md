# HP Quota 型錄整合更新（2026-11-06）

## 範圍與現況

本次沿用既有三工作台，將 11 家供應商、65 組 API 介面及 1,215 筆供應商／模型資料整合到「供應商與模型 → 供應商 API 與型錄」。預設排除退場與僅開放權重項目，可按供應商、模型、任務及輸入／輸出模態篩選。型錄、帳戶資格與 Broker 路由各自標示。未知額度與不可取得的歷史活動保持未知。

現役主機是 HP，來源契約為 selfhost-servers `5b35b847785c933ee8a52c42b1d00ef2a2adc718` 的 `docs/asus-retirement-2026-11-05.md`。ASUS 舊部署工具與未決操作保留作歷史，不重播。此次不恢復 Broker 派送或另開正式帳本。

## 已完成驗證

- 207 項 Dashboard、帳戶、metadata 匯入、報表、封裝及公開路由離線測試通過。
- 本機 Chrome 在 1440、768、390px 核對登入後型錄、分頁、同能力項目的複合篩選、退場狀態與來源展開；無水平溢出或瀏覽器例外。
- HP 用實際 Python 3.14 與現有相依套件，在非特權暫存目錄驗證新快照；11 家型錄、1,215 項模型、5 筆目標的型錄觀測、OpenAI 歷史帳戶快照均可讀。過時餘額維持非 current；派送及付費為 false。
- HP 舊版程式、有效 systemd 設定、實際程序參數及匿名路由與固定基線相符。
- 更新 gate 的五項合成測試覆蓋成功、切換前失敗、啟動失敗回復、外部 unit 漂移拒絕覆寫，以及封包越界／雜湊拒絕。
- 獨立審查核對第二版封包及 46 筆 payload 雜湊，無剩餘阻擋。

## 凍結候選與切換界線

私人候選在 `.state/quota-hp-inventory-20261106/frozen-v2/`；PINS SHA-256 為 `2adda19fbfcf77d681884e66b35e1a1e6be2ba8df7fc264e023602de7f7998fc`。此 pin 指定凍結 bytes，不以未提交分支名稱或版本號代替。HP 暫存檔已讀回核對，原操作已完成切換，驗證如下。

固定更新只新增獨立程式目錄與版本化 metadata，替換 `api-quota-dashboard.service` 的 ExecStart，保留相同資源及網路限制。保留 `/etc/api-quota-dashboard/browser-auth.json` 的身分、路徑和密碼 revision 語義，沿用 18586 與既有 Caddy 路由；不改首頁、其他應用或 Broker。首頁密碼管理工作由既有 selfhost 對話承接，兩邊互相等待操作完成，避免中途驗證衝突。

root 執行先全讀核對固定封包，再以服務帳號在無網路的暫態 unit 驗證快照，通過才切換。收據固定為 `/var/lib/quota-hp-inventory-20261106-v1.json`，存在即拒絕重播；原 unit 保存在 `/var/backups/quota-hp-inventory-20261106-v1/`。啟動失敗且 unit 未漂移時回復；有漂移或結果不明則保留現場對帳。

## 正式更新驗證

2026-11-06 06:26（台灣時間）獨立核對：

- 原收據 `quota-hp-inventory-20261106-v1` 為 `complete`，`provider_calls: 0`、`paid_allowed: false`。新程式為 `/opt/momonong-quota-inventory-20261106-v1`，新版 metadata 為 `/etc/api-quota-dashboard/metadata-inventory-20261106-v1`。
- 41 個安裝程式與資產雜湊吻合凍結 payload。有效 systemd 設定、active/running 與程序命令列均符合新版；unit SHA-256 為 `96d1da37db6870ccf678292036642b2afa2ef7b3266376010cf0691b7b009233`。
- loopback 的新版資產、401/404 權限邊界通過。外部 HTTPS `/quota` 回 308、`/quota/` 回 200；JS/CSS 雜湊吻合，頁面含新版型錄。匿名 snapshot／usage 回 401，執行、管理及直接型錄資產路徑回 404；原首頁回 200。
- 實際瀏覽器可載入正式登入頁。登入後的正式畫面及人工接受尚未驗證，不以匿名驗證或隔離 smoke 代替。
- Home 對話已接收成功收據與新 metadata 路徑，解除原等待。Home 的密碼管理更新另行核對與驗收；本次不宣稱它已完成。

外部 HTTPS 非秘密結果保存於 `.state/quota-hp-inventory-20261106/public-verification.json`。初次 Python HTTP 用戶端受到 403 拒絕，隨後標準 curl 完整路由核對及實際瀏覽器均成功；未因此變更伺服器或保護設定。此更新不讀 Doppler 或供應商金鑰，不呼叫模型、不付費，未提交、推送或合併 Git。
