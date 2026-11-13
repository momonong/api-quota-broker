# Encrypted Local API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 其他專案用同一個 ProjectClient／HTTPS 入口，以受驗證的加密封裝執行 local_only 工作；敏感資料在任何錯誤或恢復路徑均不改送雲端。

**Architecture:** 使用標準 OHTTP／bHTTP 核心，Python SDK 與自有 Broker 處理簽章設定、封裝及結果驗證。專案政策在認證、接受工作及實際派送時檢查；本地持久工作與雲端帳本明確分流，沿用既有專案 ownership 與安全儲存能力。

**Tech Stack:** Python >=3.12、現有 SQLite／cryptography／pytest；原生擴充候選 ohttp=0.8.0、bhttp=0.8.0、PyO3=0.30.1，Rust 1.90.0 隔離工具鏈。相依確認通過前不建立自製替代加密協定。

**Spec:** [4090 設計](../specs/2026-11-13-rtx4090-encrypted-local-api-design.md)。原方案的「現在不開發」已由使用者本輪「訂定目標和驗收標準……先設計測試……開始」取代；正式部署等界線不變。

## Global Constraints

- 公開 API root 維持 `https://momonong.me/quota/api/v1`；HP 是可信解密端。
- 新加密 SDK client 預設 encrypted，不提供 plaintext fallback；專案 token 只能在密文內。
- 敏感專案 `allowed_execution=[local_only]`、`default_execution=local_only`、`encryption_required=true`；請求只能收緊政策。
- local_only 的前處理、模型呼叫與結果處理不得呼叫雲端；UNKNOWN 不換 key／目標／模型重派。
- 所有 key／token／輸入／回答不進一般日誌；測試只產生暫存測試憑證，禁止使用正式憑證。
- 維持舊資料及未提交工作；不修改任何消費端專案。不為接入啟動、下載或呼叫真實模型。
- 同一現有 worktree，基準 `cf79ec08e3fbee968a44f4a8660c19e178e2b818`，工作分支 `feat/encrypted-local-api`；本輪不提交、合併、推送、部署或變更正式網路。
- 初始完成證據為 Linux x86_64／Python 3.12 的離線實作與安裝驗證。其他 OS／架構、GPU 品質及正式入口另列未驗收，不以 CI 設定存在冒充通過。

## Review Focus

1. 舊 SDK／server 或未配置信任根：在傳送 token／正文前失敗；相依缺失不能退回 JSON。
2. 接收 key／簽章 key 輪替、舊設定、偽造高版本、過期委派：不得替換信任根或延長 root 授權。
3. 已提交但回覆被截斷／偽造：沿原身分唯讀查回，不能重新 POST 或釋放未知容量。
4. 政策在排隊後被收緊或 token 更新：沿專案與提交時約束檢查，不放寬舊工作。
5. 排隊、結果保存、GC 及程序重啟：正文可逾期，但防重派證據與唯一恢復資料不得被一般 TTL 刪除。

## 完成標準與證據

| 驗收 | 必須證明 | 證據方式 |
| --- | --- | --- |
| A1 標準封裝 | 請求／回應使用 RFC 9458，拒絕調包及錯誤上下文 | 上游測試向量／獨立互通向量、原生邊界測試 |
| A2 零雲端外送 | local_only 在離線、滿載、OOM、timeout、不支援、政策變更時均不進雲端 transport | 真實路由／policy＋可計數的邊界 fixture |
| A3 雙向內容保護 | token、prompt、回答、內層 URL 不出現在外層；可信業務錯誤也加密 | HTTPS fixture／代理觀測，真實封裝而非假加密 |
| A4 金鑰信任 | 簽章、用途、origin、有效期、版本及撤銷邊界均驗證 | 固定時間與測試 key 的正反案例、重新啟動讀回 |
| A5 防重播與恢復 | 相同密文不重執行；不同密文的同業務 key 不重派；未知只查原工作 | 真實 SQLite、丟回覆／重啟／並行測試 |
| A6 專案隔離 | 同 key 跨專案無法讀取；敏感 token 不能使用原 JSON routes | 既有 access 測試與加密入口整合測試 |
| A7 獨立接入 | repo 外安裝 wheel，只依文件與受控 profile 完成提交、等待、重取 | 暫存乾淨環境＋合成模型 fixture，不改其他專案 |
| A8 回歸 | 既有可重用測試、ruff、mypy 通過；基準問題分開記錄 | 保存指令與結果，不把 sandbox 失敗列為產品通過 |

以下不屬於本輪完成聲明：正式金鑰發放、Cloudflare／HP 部署、真實 4090 推論、Spark 可用容量、安全稽核證書與使用者品質驗收。

### Task 1: 加密相依與封裝邊界

**Files:** 新增 `native/ohttp/{pyproject.toml,Cargo.toml,Cargo.lock,src/lib.rs}`、`tests/test_ohttp_native.py`；更新主專案依賴／lock 及忽略檔。

**Interfaces:** 原生模組 `quota_broker_ohttp` 提供 `ClientRequest(config: bytes)`、`Server(seed: bytes, key_id: int)`，使用一次性的請求／回應上下文；bHTTP 的 encode/decode 由原生標準元件處理，不自行重寫 KDF／AEAD／nonce。

- [x] 寫並執行失敗測試：真實封裝 round-trip、回應綁定、調包／截斷／錯 key 拒絕；assert 外層不含已知合成正文。
- [x] 固定相依、核對授權及已知安全公告；編譯封裝並通過相容向量。候選不合適時留下明確 gate，不以假實作過關。
- [x] 用預建 wheel 在另一個 Python 環境安裝，無 cargo 仍可執行；Linux 測試通過後才接後續封裝。

Expected: 原生測試全綠，安裝證據可重現；尚未驗證的平台保持未驗收。

### Task 2: 執行政策與保守相容

**Files:** `src/quota_broker/{access.py,gateway.py,gateway_server.py}`；新增 `execution_policy.py` 與 `tests/test_execution_policy.py`。

**Interfaces:** `execution_policy` 僅接受 `local_only|cloud_only`；Project principal 保存 `allowed_execution`、`default_execution`、`encryption_required`。沒有新設定的既有政策維持 cloud_only。管理者登錄的 local target 另接本地 adapter，不能把雲端 Target 改名偽裝本地。

- [x] 先寫失敗測試：非法 policy／auto、相同專案 token 政策不一致、缺省政策、改 policy 的同 key 衝突、排隊後收緊、明文敏感 token、local_only 無本地 target 時 cloud calls=0。
- [x] 在接受工作前解析生效政策，加入持久 payload 身分；dispatch 重驗最新約束。
- [x] 沿用既有 queue／access 用例核對舊專案、撤銷及配額行為。

Expected: A2、A6 的政策邊界成立；本地未配置時明確不可用，不代送雲端。

### Task 3: 信任設定、接收金鑰與防重播

**Files:** 新增 `src/quota_broker/crypto_config.py`、`encrypted_transport.py`、`tests/test_crypto_config.py`、`tests/test_encrypted_transport.py`。

**Interfaces:** `TrustProfile` 由受信任檔案載入 root 公鑰／精確 audience；`ConfigVerifier` 驗證 root 委派與 signer manifest，保存公有版本狀態；`ReplayStore` 在 SQLite 原子記錄已接受的標準封裝識別。私鑰檔案與公開設定分開。

- [x] 先寫失敗測試：錯 root／audience／alg、重複 JSON 欄位、過期設定、過長有效期、回退／偽造更新、重新開啟 cache；固定 15 分鐘 manifest、10 分鐘快取、30 天最長委派。
- [x] 使用成熟 JWS／Ed25519 元件驗證，不接受遠端 key locator。根撤銷與版本重設只透過獨立管理設定。
- [x] 先寫並行／重啟的 replay 失敗測試，再實作 2 分鐘接收時間窗及有界持久拒絕重播；容量滿或狀態失效即停止接受。
- [x] 測試每日 key 切換與 20 分鐘舊 key 窗口、到期刪除／停用；結果保存不依賴原請求 key。

Expected: A4、A5 通過；使用測試 key，沒有正式金鑰或系統服務變更。

### Task 4: 同一入口與 SDK 的加密流程

**Files:** `src/quota_broker/{client.py,gateway_server.py,cli.py}`；新增 `tests/test_encrypted_api.py`，更新 SDK 測試及 OpenAPI。

**Interfaces:** 公開 `GET /quota/api/v1/crypto-config`、`POST /quota/api/v1/encrypted`；內層只允許既有 project catalog/explain/jobs routes。新版 `ProjectClient(..., security_profile="encrypted", trust_profile=...)` 封裝全部操作；既有非敏感用法保留明確的 legacy 客戶端，不讓新 client 暗中降級。

- [x] 先寫 HTTPS fixture 測試：輸入／token／回答只出現在內層；錯 authority、管理路由、redirect、巢狀 wrapper、偽造來源 header、回應調包被拒絕。
- [x] 在既有 dispatcher 中加入封裝入口；解封裝前只有固定錯誤，解封裝後的業務錯誤加密。建立受限 handler／密文與明文上限。
- [x] SDK 在送出前驗證信任設定；每個 HTTP 呼叫有新上下文；任何可能送出後的外層錯誤只導向查原工作。
- [x] 驗證 catalog、explain、submit、status、result、cancel、wait/run 與憑證更新、程序重啟；雲端模式同樣封裝。

Expected: A3、A4、A5、A6 全部通過；原 HTTPS root 不變。

### Task 5: 本地持久工作 adapter

**Files:** 新增 `src/quota_broker/local_jobs.py`、`tests/test_local_jobs.py`；接入 `access.py`／`gateway_server.py` 的專案工作路由，補精確 selfhost 契約文件。

**Interfaces:** 固定 HTTPS 目標、deployment ID、模型與 capability 由管理設定提供；adapter 保存 dispatch intent、selfhost idempotency identity／job id。新提交只一次，下游查回為唯讀；狀態與結果分開處理，`result_state=available` 才提供結果。

- [x] 先寫真實 SQLite＋下游 HTTP fixture 失敗測試：提交回覆遺失、重啟、同 key 不同內容、結果 pending/storage_failed、GC 後查不到、token 撤銷、下游離線。
- [x] 實作本地上限全域 8／每專案 2、15 分鐘未派送期限與完成後 1 小時結果期；不刪 UNKNOWN／唯一恢復證據騰空間。
- [x] 嚴格轉換既有 Qwen chat／單圖契約，不加入未驗證模型或外部前處理；拒絕未知欄位及不支援能力。
- [x] 核對 selfhost 缺少精確 idempotency lookup 的限制。若必須修改服務端契約，僅提出最小後端變更及對應測試，先核對該 repo 規則與本輪範圍；未取得端到端證據不得宣稱已能自動恢復。

Expected: A2、A5 及容量／保存條件成立；缺少服務契約的組合禁止宣稱正式可用。

### Task 6: 接入交付與獨立驗收

**Files:** 更新 `docs/developer-api.md`、`docs/openapi.json`、`examples/project_client/`、`README.md`；新增日期化驗收紀錄。

- [x] 補新版 SDK 安裝、信任設定發放、local_only／cloud_only 範例及斷線後查回；文件標示未部署與平台限制。
- [x] 在 repo 外乾淨 Python 環境安裝 wheel，執行文件範例；加密、local/cloud 分流及失敗案例使用本機合成 fixture，不借用正式憑證。
- [x] 跑相關測試後執行必要完整 regression、ruff、mypy；保存基準與新結果。
- [x] 由新的獨立 reviewer 檢查實際完整 diff 與驗收證據，修正重大問題並補回歸測試。
- [x] 核對 Git 變更、其他專案狀態與未驗收清單，直接回報使用者；不因完成開發就部署或推送。

Expected: A1–A8 有明確通過／未通過證據；不把缺失 gate 隱藏成通過。

## 實作確認與邊界

- SDK major version 為 2.0.0；舊 cloud canonical HMAC 不變，舊明文行為明確名為 LegacyProjectClient。
- 本地輸入依實際 selfhost schema 收斂：PNG ≤1 MiB／1024x1024，輸出 ≤1024 tokens；不假設 JPEG 支援。
- 管理 publish 只讀 recipient 公開設定；根私鑰、signer、接收種子分開保管。
- 審查三項 Important 均有 RED→GREEN 回歸；PyO3 跨執行緒 GC 與精確封裝清單的新增回歸亦已修正。
- 下游 exact idempotency GET、正式入口、GPU、其他平台 wheel 為已知後續 gate，不屬本輪離線通過聲明。
- [最終驗收紀錄](../../encrypted-local-api-acceptance.md) 已逐項列 A1–A8：本次功能 106 項、獨立安裝 7 項通過；完整回歸 3418 passed、40 skipped、20 failed、64 errors，失敗／錯誤身分與開發前基準完全一致。不能把打勾當成完整 suite 全綠、正式部署或人工接受。
