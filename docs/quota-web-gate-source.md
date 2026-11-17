# Quota gate 來源與驗證界線

`deploy/quota-web/gate.py` 是 Quota 雙主機 gate 的完整來源，包含公開 Caddy 路由、dashboard／tunnel service unit，以及封裝、preflight、安裝與回復函式。不是測試替身；公開路由與共用 selector 測試直接核對此來源。

## 來源補齊（2026-11-17）

`tests/test_quota_public_boundary.py` 與 `tests/test_quota_shared_selector.py` 已在 `31bc06ddc001ce628b5733e65de909ec63671e72` 納入 Git，但它們引用的 gate 未提交；`d03ee867c04bb7ed7039183db0b2ac4374ff5f91` 及可讀取的 Git refs 均不含該檔案。主要工作目錄保留了 regular、未追蹤的 `deploy/quota-web/gate.py`。本次完整審閱 1,189 行來源後納入，沒有改寫 deployment pins 或以虛構常數取代實作。

捕捉來源的 SHA-256：`a8dc5ca31b4c8e880be397e31efbbfb03ff9ee8ddacf108ae18cd7cf43419caa`。此值識別本次完整審閱的原始本機來源，不證明它等同任何已安裝或已發布 gate。

主要工作目錄原有 builder 讀取這份來源，再附加固定主機角色的 entry point，形成 standalone gate。這次只補入 gate 來源，不將其他未追蹤 builder、私人 payload、憑證或收據混入本次修復。

## 路由修正

真 Caddy 合成測試發現：原來源在同一 reverse_proxy 使用 `header_up -X-Forwarded-*` 與固定 `header_up X-Forwarded-Proto https`，會將固定 scheme 也刪除。僅將 wildcard 清除移到 proxy 前的 `request_header` middleware，保留 proxy 的固定 HTTPS 設定；測試確認偽造 forwarding header 不會穿透，upstream 實際收到 HTTPS scheme 與 loopback peer IP。此修正會改變 gate 來源 bytes，發布時須重新凍結及核對，不沿用原始來源 SHA 當作成品 seal。

## 契約

- 公開 Caddy matcher 僅轉送 dashboard shell／CSS／JS 與 login、logout、snapshot、usage、advice；執行、管理、維護與其他路徑拒絕。轉送時清除外來認證與身分 headers，固定 Host、HTTPS scheme 與 no-store。
- Dashboard 使用 `/opt/api-quota-broker/current/runtime/bin/python` 這個共用程式 selector；服務身分仍是 `api-quota-dashboard`，資料、browser verifier 與 read-plan credential 保持獨立，不取得 Broker 私有目錄或 supplementary groups。
- 模組只定義 stdlib 函式與常數，沒有可直接執行的角色入口。匯入測試禁止後續檔案、socket、subprocess、OS 與 resource 操作；這不等於安裝函式已做現場驗收。

## 部署界線

檔案仍保留既有的主機、release、Caddy 與 Home core pins；未驗證的現場狀態不能被當成新的 baseline。納入來源及離線測試通過，不代表適用於再次初始安裝，也不授權執行 `apply`／`apply_buffer`。正式更新須另依已授權維運路徑核對 installed bytes、當前 selector、資料與存取隔離，不能直接執行未凍結的 user-owned 來源。

本次 focused tests 使用真 Caddy 與合成 loopback upstream，驗證 redirect、精確公開路由、GET／POST 與認證 header 清除；另以 AST 核對 unit selector／隔離，並驗證 source import 不接觸主機。沒有讀正式帳本、憑證或收據，沒有執行 gate 安裝／回復。
