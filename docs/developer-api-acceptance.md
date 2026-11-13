# Developer API 1.3.0 交付與正式驗收

日期：2026-11-13，Asia/Taipei。**已部署，且 repo 外的獨立客戶端已透過 HTTPS＋專案 token 取得真實模型答案。** 本輪沒有修改消費端專案。

入口：`https://momonong.me/quota/api/v1`。接入請讀 [開發者文件](developer-api.md)、[OpenAPI](openapi.json) 與 [可執行範例](../examples/project_client/README.md)。

## 正式 HTTPS 驗收

SDK 安裝在 repo 外的 `/tmp/quota-consumer-20261113`；範例複製到 `/tmp/quota-https-consumer-20261113`，沒有 PYTHONPATH，也不依賴 source checkout。正式 Owner token 由既有 Doppler reference 注入子程序記憶體，未寫入文件、參數或收據。

| 驗收項目 | 實際結果 |
| --- | --- |
| SDK 與純 HTTP 範例查 catalog | 成功；列出 11 家，當時 6 家 `available=true` |
| SDK explain | 成功，選出當下可用的 Google 免費路由；不派送推論 |
| 真實工作 | `quota-sdk-live-20261113-01`，2026-11-13 12:16 完成 |
| 模型／答案 | `google / gemini-3.5-flash-lite`，`completed`，`OK。` |
| 資源界限 | `max_output_tokens=32`、`max_attempts=1`、`wait_policy=reject`，免費 admission |
| 提供者實報用量 | input 5、output 2、total 7 tokens，requests 1；`settled_provider_usage` |
| 原 key 取回與已完成後同內容再次提交 | 結果相同，`attempt_count=1`、`attempt_budget_used=1`，沒有第二次派送 |
| 純 HTTP 範例取回結果 | 與 SDK 完整 outcome 相同 |
| 匿名／無效 token | catalog 401 `unauthorized` |
| 另一既有專案 token 查 Owner 結果 | 404 `not_found`；沒有建立或修改另一專案工作 |
| 管理路徑／queue tick | 公開入口 404 |
| 既有網站登入邊界 | `/quota/` 200，匿名 snapshot 401 |

這一筆證明正式共用 API 路徑可用；它不是所有供應商的逐家 live 驗收。當時可用者為 OpenAI、OpenRouter、Google、Mistral、NVIDIA、Cloudflare；Groq 當時不可用，其餘四家停用。每次呼叫仍須查最新 catalog／explain，不把既往八家直接 `Gateway.run()` 結果當作目前公開路由狀態。

首次公開探測收到 Cloudflare `403 / 1010`。查明是客戶端識別限制後，依 [Cloudflare 官方接入說明](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/) 為 SDK 加上真實的 `api-quota-broker/1.3.0`，純 HTTP 範例加上 `quota-http-example/1.0`。修正有 RED→GREEN 回歸測試，隨後正式 HTTPS 驗收通過；Cloudflare 安全設定沒有變更。

## 正式部署與修復收據

目標主機為 `hp-ubuntu2604-server`。共用設施規則採用已向 GitHub 核對的 `selfhost-servers` main `955b4f8cd73c4eeaaf894445afbdbc919326d6ed`。該 repo 的 dirty 工作沒有修改。

先前 fixed publisher 的 `installed_extra_file` 已處理：使用者明確授權私人終端管理並完成 sudo。唯讀比對確認 manifest 正式檔案沒有遺失或變更，額外的 19 個檔案皆是 root-owned Python 3.14 bytecode。整個快取目錄以 rename 保存在 root 專用備份，未刪除、未重建帳本、未修改憑證；原受限 publisher 隨後回到 `ready`。

| 項目 | 收據／值 |
| --- | --- |
| root 維護 operation | `quota-api-20261113-6d689e3b2ad2` |
| root 備份 | `/var/backups/quota-shared-broker/quota-api-20261113-6d689e3b2ad2` |
| 最終 Broker release | `shared-broker-20261113-api-v2` |
| 最終程式 publish operation | `46490cc9-2ef5-4c31-8f98-5377cc813baf` |
| 最終 manifest SHA-256 | `f6975d83a5614bdfedf9b15b5d99d5a34de1bdc7d633b42a34c7e2f2e790ee5a` |
| 前一 API release | `shared-broker-20261113-api-v1`，operation `46d2702c-14d6-4d12-bdb7-b30998ebee6d` |
| 修改前 Caddy SHA-256 | `9339b15eb69ff0c2df6448605d55fb5899ee023c1f291413abc1ecfc2ff8d8ec` |
| 修改後 Caddy SHA-256 | `75c2509d1d1c6b4d9a0cce8821aa87eb027fdb0649f7c7c2355c6f66d4260d00` |

程式部署使用原受限 publisher，兩次皆讀回 `status=complete`；v2 相對 v1 只變更 `client.py` 的真實套件識別。五份正式設定沿用已核對的 v6，不採用 v7 未證實的 remaining 值。原 access policy、credentials、ledger、queue 保留，未擴充模型／專案權限。

[Caddy 入口片段](../deploy/hp_quota_project_api.caddy) 已插入唯一 `handle @quota_app` 的既有 `route`，位於網站 handler 前。備份後比對基線，使用 HP 的 Caddy validate，再原子替換與 reload；刪除本次插入區塊即可逐位元還原舊檔，其餘應用區塊未變。

入口只允許 GET catalog／jobs／status／result、POST explain／jobs／cancel。上游固定 `127.0.0.1:18588`，移除網站 cookie，POST 上限 1 MB；專案自己的輸入限制仍另行檢查。HP 本機實測九個網站／API 邊界均符合預期，HTTP guard 308、錯誤 Host 404。管理、全域 usage、tick、同步 tasks 與原始 `/v1` 沒有公開。

root 修復與 Caddy 安裝經本次私人終端完成，不能假稱屬一般 publisher 權限。管理步驟結束後已執行 `sudo -k` 並結束專用 SSH／tmux 工作階段。

## SDK、文件與離線驗證

- HTTPS ProjectClient、catalog／explain、submit／status／result／cancel、wait／run 及 `call_free_api`。
- 原 key 恢復、不自動重送 POST；工作未知與用量未知分開處理，options 完整傳遞。
- 查詢／試算／派送共用專案容量檢查；有效絕對時間證據支援非 UTC 視窗，必要證據仍 fail closed。
- 專案 catalog 移除供應商帳戶、secret reference 與操作來源位置；其他專案工作維持 404。

最終相關驗證 **82 passed**（API／SDK／文件範例／Caddy／OCR），獨立 wheel consumer／規格測試 **6 passed**。Ruff check／format、Mypy 36 個模組、lockfile 與 diff check 通過。

獨立審查找到的 3 項問題均先重現再修正：截斷 HTTP 回應保留原 key 的可恢復錯誤；code completion 正確傳遞輸出上限；OpenAPI priority 對齊 Gateway 的 -100..100。正式部署期間另補上上述應用識別回歸。

完整 repo 的先前回歸觀測為 **3,583 passed、40 skipped、17 failed、64 errors**。保留原工作狀態、在 `/tmp` 還原本次產品原始碼至起點後，重現其中 **16 項 failure 與全部 64 項 error**：ASUS 舊固定 pin、已過期 fixture、pypdf runtime 固定值、主機 namespace 權限與 strict umask fixture。餘下一項寫死 1.2.0 的測試已改讀實際套件版本，單獨重跑 **1 passed**。最終小幅 HTTP header 修正後重跑上述相關測試；不宣稱整個 repo 全綠。

可安裝成品：`dist/api_quota_broker-1.3.0-py3-none-any.whl`，281,368 bytes，SHA-256 `81d5d56d6d7cee7ce4887cc289b1fb07c49fa26987779d353871eb7c2cbbaca4`。尚未上傳 PyPI／套件索引。

fixture 驗證只用暫存 ledger 與假供應商；真實用量僅為本頁記錄的一筆 HTTPS 工作。證據與原操作保存在忽略追蹤的 `.state/developer-api-20261113/`：`evidence.json`、`hp-preflight.json`、`hp-repair.json`、`hp-publish-v2-result.json`、`hp-caddy-result.json`、`https-live-result.json`、`https-boundaries.json`、`https-http-result.json` 與最終測試紀錄。憑證與 token 值不在收據中。

## Git 與其他專案

起點 HEAD `0d81f79b5cbbce7b02700c333204f0d19f9b7741`，沿用既有工作目錄。**部署已完成；本次另經使用者授權將 API／SDK、接入文件、範例與相關驗證整理為 GitHub PR。** 套件索引發布、tag 與 GitHub Release 不在這次範圍。交付使用獨立 worktree；原開發目錄的 ASUS dirty／untracked 工作保留，未接入或修改其他專案。

既有 untracked `tests/test_quota_code_update.py` 的版本參數修正只保留在原開發目錄；因該檔案其餘內容屬先前維運工作，整份未納入本次 Git 交付。原檔另存 `/tmp/quota-test-code-update-before-20261113.py`。新增專案仍需由維護者登錄其專屬 token／限制，不能共用其他專案身分；Owner 自己的工具可直接使用既有 Owner token。

### 本次 Git 交付樹驗證

交付 worktree 僅含 22 個指定檔案。完整回歸為 **3,311 passed、40 skipped、20 failed、64 errors**；它與含大量既有 untracked 維運檔案的原開發目錄是不同測試集合，數量不相加。失敗項目與修改前基線對應；額外五個案件已在乾淨 `origin/main`（`0d81f79`）重現，原因為過期 Cloudflare fixture，以及既有測試引用尚未追蹤的 `deploy/quota-web/gate.py`。這些先前維運工作未納入本次交付。

全目錄 Ruff check、Mypy 36 模組、lockfile、wheel 建置、差異與敏感內容檢查通過；本次 12 個程式／範例／文件格式檢查通過。全目錄格式檢查另有五個舊區塊未通過，全部也在 `origin/main` 重現，保留原狀。交付程式及重建 wheel 的 bytes 與已完成正式 HTTPS 驗收的版本一致。
