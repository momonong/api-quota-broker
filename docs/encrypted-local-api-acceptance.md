# 加密本地 API 正式路徑驗收 — 2026-11-14

已完成 `https://momonong.me/quota/api/v1` → 加密 Python SDK → HP → 私人 TLS／SSH 通道 → 真實 RTX 4090 的文字、單圖及回覆遺失恢復。其他專案可依 [接入文件](developer-api.md) 安裝兩個 wheel、取得專案 token 與可信 profile 後使用；不需 Broker checkout、Rust、供應商 key 或自己實作加解密。

工程驗收沒有修改消費端專案；HP 與 4090 的必要服務已實際更新。使用者其後授權本次成果合併與推送，Git 交付以本文件所在提交及對應 PR 為證據，與既有部署收據分開核對。下方 2026-11-13 紀錄是開發階段歷史，當時的「未部署／下游缺口」由本節後續證據更新。

## 正式案例與實際邊界

| 案例 | 實測結果 |
| --- | --- |
| 按文件執行範例 | repo 外 `/tmp/quota-public-consumer-20261114-02`，獨立 wheel 環境、`python -I`；原樣 `client.py` 執行 catalog／explain／submit／wait／result，文字回覆 `OK` |
| 單張 PNG | 64×64 紅色合成圖，正式加密 SDK 回覆 `Red` |
| 公開提交回覆遺失 | 實際 POST 完成後刻意丟棄回覆，SDK 回 transport_unknown；新 client 只用原 key 查回 `OK`，不重新 submit |
| 下游首次回覆遺失 | 另一個真 GPU 案例在 selfhost 接受 POST 後丟棄回覆；Broker 精確 GET 原 idempotency key，只有一個 job／attempt；此故障注入位於合成 Broker＋真 worker，與公開入口案例分開記錄 |
| 敏感政策 | 正式 token 指定 cloud_only 得 forbidden；錯 token 得 unauthorized。HP 本機 loopback 的明文敏感請求回 403 encryption_required，沒有為負面測試把真 token 明文送經 Cloudflare |
| 密文觀測 | 正式 SDK 兩個直接呼叫案例觀測 22 次傳輸；外層無 Authorization，未含 token 或合成 private sentinel。這是封裝邊界觀測，沒有聲稱做全網路封包稽核 |
| 自動金鑰發布 | HP 兩個 timer active、oneshot Result=success；SDK 已驗證到 publisher version 4，超過首次版本，確認排程有持續發布 |

正式三筆 worker job 皆 `succeeded`／result `available`，每筆 `attempt_count=1`：

- 文字：`job_469cd3f6914848f1bfe6af8c4eede6eb`。
- 圖片：`job_38c614180bb9446c8bb861567d4484ab`。
- 公開回覆遺失：`job_5b00e3dcbc4b41419f354e66cb6ac1ad`。

驗收啟動時曾把 GNOME 遠端桌面的 GPU 使用誤判為推論程序，前置檢查中止後的 shell 仍啟動 consumer，留下 `job_12a82bf5d73947b2aee3864d81a882c1`。已停止該測試、查明未 dispatch、經正式 API 取消，帳本確認 canceled／attempt_count=0，預算未增加；保留收據後才開新有界窗口。沒有停止桌面程序或重派未知工作。

## 版本、資源與交付

- HP release：`shared-broker-20261114-encrypted-v1`；root gate 收據 `quota-encrypted-20261114-v1` 為 complete，Broker operation `52b2f6d3-5b60-4f2e-882c-c9224b93bfc3`。
- 部署 bundle SHA-256：`5394069dbf4c2871cbb6e9f6f98e3d3ed56f84c3364b1a784c74cd86e9b3df09`；Caddy 修改後 SHA-256：`a05392af79ff8dd18be87e6bf12a266f08a529819e9edbd2c646674ac844e321`。
- selfhost API candidate：`quota-lookup-a29559e428fd089a`，以原現役 `6f1046531c08fa56be7f14be4e5b0ea4067e9a53` 加入兩個 lookup 模組，保留原 control／executor DB。來源改動在隔離的 `feat/quota-idempotency-lookup`，原 security-dependencies 工作樹四個 dirty 檔案未改。
- 模型：既有 `Qwen/Qwen3.5-4B` revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`；固定 vLLM image SHA-256 `fdebb477430924ab768ce6db0d2294f2f8d5c693abd5589275fb9fcfb2819655`。未下載新權重或更動 driver／全域 Docker context。
- 模型容器只有 internal network、無 published port，權重 readonly，HF／Transformers offline；relay 只發布 loopback 18089。HP worker forward 只聽 loopback 18443；其他 KaChing 容器維持 healthy。
- 兩個真 GPU 窗口各用 1 load／9 generation budget（含 6 次 warmup 預留＋3 個工作），都已關閉，lifetime 計數未歸零。正式文字含冷啟動約 116 秒。每秒取樣的全卡最高記憶體 17,955 MiB（約 17.5 GiB，包含桌面等其他使用），不是所有輸入的最壞峰值。
- 驗收後正常卸載模型，恢復 controller 等待按需工作；API、executor、TLS 與 tunnel 保留。未安裝／驗收 4090 整機開機自動啟動。

| 本輪 wheel | SHA-256 |
| --- | --- |
| `api_quota_broker-2.0.0-py3-none-any.whl` | `198a261d8e60947a4607c7ff9036e72ab0b5d3a14a558e3a17d1669eb1a05160` |
| `quota_broker_ohttp-0.1.0-cp312-abi3-manylinux_2_34_x86_64.whl` | `574344beb64f11fa37e44559ace9ebd568c8aa332c8eaddc9584032f31f2eecd` |

驗證：selfhost 新查回測試 9 passed、完整 CPU 契約 389 passed／2 skipped；Broker 本輪 201 項相關整合測試全部通過，ruff／mypy／diff check 通過。前輪全套既有失敗與基準逐項比較的結論仍保留在下方，沒有將它改寫為全套全綠。

本機私有收據在 Broker `.state/hp-encrypted-20261114/` 與 `.state/4090-e2e-20261114/run-01/`，完整 token／私鑰不放本文、Git 或接入 zip。自有主機保管的批准、信任界線及復原要求見 [修訂](superpowers/specs/2026-11-14-hp-key-custody-amendment.md)／[操作指南](hp-encrypted-api-operations.md)。

## 尚未驗收

Windows／macOS／ARM 消費端、Spark 128 GB、其他模型、雙併發品質、最大 context 的品質／峰值、整機重啟自動恢復、異機災難復原及長期 SLA。這次只用合成資料證明傳輸與恢復，沒有把幾個短答案當成私人業務品質接受。HP 與 4090 仍是可信明文端點；Cloudflare 可見流量 metadata。自動 root／signer 換 key、外部加密安全稽核與正式撤銷演練也不在本輪完成聲明內。

---

# 歷史：加密本地 API 開發驗收 — 2026-11-13

本輪目標是讓其他專案以同一個 Python SDK／HTTPS API 選擇 `local_only` 或 `cloud_only`，SDK 自動處理內容加解密與金鑰信任；敏感專案不能自動改送雲端。成果為 **SDK 2.0.0 與 Broker 的未提交開發版本**，尚未部署，不能直接拿正式入口當成新版測試環境。

依據：[已確認設計](superpowers/specs/2026-11-13-rtx4090-encrypted-local-api-design.md)、[開發目標／驗收標準與進度](superpowers/plans/2026-11-13-encrypted-local-api.md)。接入方法見 [開發者文件](developer-api.md)、[可複製範例](../examples/project_client/README.md)；管理設定與下游契約見 [服務指南](encrypted-local-api.md)。

## 範圍及版本

- Broker 基準 `cf79ec08e3fbee968a44f4a8660c19e178e2b818`，工作分支 `feat/encrypted-local-api`。HEAD 未變，沒有 commit、merge、push 或正式部署。
- 實作採原對話執行、測試先行、一次新的獨立完整審查。各層的失敗測試、修正結果與裁定保存在本機 `.superpowers/sdd/2026-11-13-encrypted-local-api/`；這是工作證據，並未隨 Git 發布。
- 測試僅使用暫存金鑰、專案 token、SQLite、loopback TLS 與合成模型。沒有發放正式金鑰、呼叫真實模型、下載權重或變更服務／網路。
- selfhost-models 僅唯讀核對，基準 `45ebfd57db5313292ae8f38e733f3e2894a99e5f`；原有四個 pyproject／lock 變更保留，未修改該 repo 或其他消費端專案。

## A1–A8 驗收對照

| 項目 | 已取得證據 | 結果與界線 |
| --- | --- | --- |
| A1 標準封裝 | 12 個原生邊界測試、3 個獨立互通測試；RFC 9458 Appendix A 向量、PyHPKE 雙向互通 | 通過；固定 HPKE suite、一次性上下文、調包／錯 key 拒絕；不是加密安全認證 |
| A2 僅限本地 | execution policy、local lifecycle、admission 測試；可計數雲端／countTokens 邊界 | 離線通過；離線、滿載、未知、GPU 錯誤合成狀態與政策變更均不改送雲端；實際 worker 外連限制待部署驗收 |
| A3 雙向內容保護 | 真實 HTTPS／OHTTP fixture；token、正文、回應、內層路徑及業務錯誤封裝 | 通過 fixture；拒絕 redirect、錯 authority、任意代理／管理路由；未經正式 Cloudflare 路徑驗收 |
| A4 金鑰信任 | crypto config 22 個測試、操作工具與 key lifecycle 測試 | 通過；簽章、有效期、版本回退、root／epoch、signer 輪替、獨立 epoch 復原；正式 timer／撤銷演練尚未進行 |
| A5 防重播與恢復 | SQLite 並行／重啟、首次 POST 回覆遺失、結果 pending/storage_failed、取消、TTL 測試 | Broker 通過；原 intent 永不重送。已知 job ID 可查回；遺失 job ID 的 exact lookup 只驗證合成 fixture，現有後端未提供時保持 unknown |
| A6 專案隔離 | 既有 access 用例、新 encrypted API／policy 測試 | 通過；跨專案不可取結果，同 project token 政策一致，明文入口無法取得 encrypted provenance，派送前重驗政策 |
| A7 獨立接入 | repo 外 Python 環境安裝兩個 wheel，`python -I` 執行文件範例；7 項測試通過 | Linux x86_64、Python 3.12.3、glibc ≥2.34 通過；消費端不需 Rust 或 checkout；其他平台未驗收 |
| A8 回歸與品質 | 106 項本次功能／安全測試、84 項封裝回歸、ruff、mypy、cargo fmt/clippy | 上述通過；完整 suite 的最終基準比對見下節，不能將既有失敗列為通過 |

106 項包含 A1 的 15 項及 A4 的 22 項，不相加重複計數。獨立範例的 7 項同時保留 legacy 相容路徑；測試全部使用合成資料。

## 最終驗證

| 檢查 | 結果 | 本機紀錄 |
| --- | --- | --- |
| 變更前完整 pytest | 3311 passed、40 skipped、20 failed、64 errors | `/tmp/quota-encrypted-baseline-authorized.log` |
| 變更後完整 pytest | 3418 passed、40 skipped、20 failed、64 errors；與基準的失敗／錯誤 test ID 完全一致，沒有新增失敗 | `full-verified.log`、`final-baseline-comparison.json` |
| 本次功能／安全測試 | 106 passed；將跨執行緒 destructor warning 視為錯誤，仍通過 | `feature-final.log` |
| repo 外範例 | 7 passed，從獨立環境的 site-packages 載入 | `independent-final.log` |
| ASUS／HP 封裝 | 84 passed | `final-packaging-green.log` |
| 靜態檢查 | ruff check 通過、31 個變更 Python 檔案 format check 通過、mypy 42 modules 通過 | 本輪工具輸出 |
| 原生檢查 | cargo fmt、鎖定且 offline 的 cargo clippy `-D warnings` 通過 | 本輪工具輸出 |
| 交付一致性 | wheel 內 42 個 Python 模組與工作樹逐位元組相符；`git diff --check` 通過 | 本輪工具輸出 |

相對 log 名稱都位於 `.superpowers/sdd/2026-11-13-encrypted-local-api/`。基準與最終的 84 個失敗／錯誤身分已逐項比較，新增及消失皆為零；通過數增加 107。**完整 suite 仍非全綠。** 變更前已存在的問題包括維護 policy 到期、舊 runtime lock／版本固定資料、provider pool fixture、容器 namespace 權限及缺少 `deploy/quota-web/gate.py`；本輪不修正無關基準問題。標準沙箱不允許 loopback sockets，因此基準與完整整合測試均以正式批准的本機測試權限執行，沒有用沙箱失敗取代產品證據。

重現本次功能驗收：

```bash
.venv/bin/python -m pytest -q -W error::pytest.PytestUnraisableExceptionWarning \
  tests/test_ohttp_native.py tests/test_ohttp_interop.py \
  tests/test_crypto_config.py tests/test_crypto_admin.py \
  tests/test_encrypted_transport.py tests/test_execution_policy.py \
  tests/test_encrypted_api.py tests/test_local_jobs.py \
  tests/test_local_admission.py tests/test_local_http_backend.py

# 先依接入文件在 repo 外安裝同版 wheel，再指定其 Python。
QUOTA_EXAMPLE_PYTHON=/absolute/consumer/.venv/bin/python \
  .venv/bin/python -m pytest -q \
  tests/test_encrypted_examples.py tests/test_developer_examples.py
```

建置相依、原生工具鏈與授權說明見 [native README](../native/ohttp/README.md)。開發環境需安裝 encrypted extra 與 dev 相依；完整原生測試不應在未安裝擴充的基礎環境執行。

## 審查與修正

新的獨立 reviewer 檢查實際 working diff 與新增檔案，提出三項 Important，沒有 Critical 或待處理 Minor。三項均先重現失敗，再修正：

1. **本地輸入契約不一致：** 部分 worker 會拒絕的輸入原本可占用持久工作。改為建立 ownership 前驗證 PNG 尺寸、訊息 parts、取樣參數、stop 與輸出 token 上限；10 項回歸通過。
2. **閒置內容保存過期：** 原本沒有查詢就不會清除到期正文。worker tick 主動清理完成正文；未派送而 target 改變也清除正文，保留身分 tombstone；2 項回歸通過。
3. **畸形下游結果：** `choices=null` 等輸出可能中斷工作迴圈。逐層驗證結構，原工作保留 ID 並標示未知，其他工作繼續；5 項回歸通過。

實作方另修正實際發現的 PyO3 跨執行緒 GC 問題：以 Rust／PyO3 的 Send/Sync 與借用檢查取代 `unsendable`，未加入 unsafe 實作；跨執行緒測試及真實 HTTPS 整合通過。這項由原實作方核對，未宣稱經第二次獨立審查。新增六個 Python 模組也已逐一加入 ASUS 與 HP 的固定封裝清單，更新精確檔案數；未改成任意檔案放行。

## 交付物與裁定

兩個本機 wheel 位於 `dist/`，尚未發布：

| Wheel | SHA-256 |
| --- | --- |
| `api_quota_broker-2.0.0-py3-none-any.whl` | `64b150396ccc4a5040fd72012d74b092a357953d8a7edeebcb8536839de2debd` |
| `quota_broker_ohttp-0.1.0-cp312-abi3-manylinux_2_34_x86_64.whl` | `574344beb64f11fa37e44559ace9ebd568c8aa332c8eaddc9584032f31f2eecd` |

- SDK 升至 2.0.0：`ProjectClient` 預設且只走加密；非敏感舊用法改用明確的 `LegacyProjectClient`，沒有自動明文降級。API root 仍是 `/quota/api/v1`。
- 舊 cloud canonical payload 不增加多餘欄位，以保留既有 HMAC／歷史身分；local_only 明確參與工作身分。
- 首版本地能力依現有 worker 收斂為 `Qwen/Qwen3.5-4B`、文字／單張 PNG ≤1 MiB 且寬高 ≤1024、輸出最多 1024 tokens。未把尚未驗證的模型或 JPEG 當成可用。
- 審查以未提交 working diff 和新增檔案進行，因 HEAD 範圍沒有新 commit；保留工作樹與 ledger，不自動 commit 或清理。採一次最終獨立審查，沒有逐任務獨立 reviewer。
- recipient 私有種子、線上 signer、離線 root 分別管理；publish 只讀 recipient 公開設定。signer 洩漏風險可持續至 delegation 到期，不能宣稱被 15 分鐘 manifest TTL 限制。

## 尚未完成的正式驗收

1. **真正的服務接入：** HP／Cloudflare 路由、正式信任 profile、金鑰輪替與撤銷、worker 私人 HTTPS／外連限制、記錄／備份政策及實際設備排程。
2. **下游精確查回：** selfhost-models 缺少 `GET /v1/jobs/by-idempotency/{key}`。目前 `exact_lookup=false`；首次 POST 回覆遺失且沒有 job ID 時保留 unknown，不重新提交。這是明確功能缺口，需另獲授權修改該專案。
3. **真實 4090：** 文字／圖片品質、GPU 記憶體峰值、併行 1／context 8192 的實測，以及故障／重啟恢復；14–16 GiB 僅為預算。
4. **其他平台與設備：** Windows、macOS、ARM wheel，以及 Spark 128 GB target 均未驗收；不能把兩台裝置記憶體合併計算。

應用層加密保護使用者端到 HP 的內容；HP 與本地 worker 仍是可信明文端點，Cloudflare 仍可見流量 metadata。通過離線測試不等於正式服務安全認證、真實模型可用或使用者品質接受。
