# ASUS Broker 1.0：使用與維運

2026-10-09 的工程與限定實機驗證已完成：ASUS loopback Gateway 已部署 1.0，固定維運入口可用，SQLite connection lifetime 修復已部署。三筆核准的供應商呼叫均 HTTP 200，並通過一次服務 restart 的持久性驗證。這是當日快照，不代表本文件每次更新都重新檢查主機。

日常任務以 CLI／API 為主要入口。契約與本機示例見 [使用指南](v1-local-guide.md)，多模態支援與限制見 [能力擴充](provider-capability-expansion.md)。可選的 legacy [key-admin](private-key-admin.md) 保留在套件中；ASUS 未啟動該管理服務，也未作真 key 表單寫入驗收。

## 已驗證結果

| 項目 | 2026-10-09 證據與界限 |
|---|---|
| 部署版本 | `release-510b514833ce06ee491128a8c12d5ab2acb2666d0f9839ee79cdc82e0ec4dda8` |
| Groq | 原 queue execution 與 attempt 1 保留；HTTP 200、836 output tokens、未截斷、依 provider usage 結算 |
| Google | 原有 key；HTTP 200、434 output tokens、STOP、依 provider usage 結算 |
| Cloudflare | HTTP 200、869 output tokens；未回報 neurons，狀態 `completed_usage_unknown`、保留 `held_estimate` |
| 歷史資料 | 原歷史列保留，沒有還原資料庫；Cloudflare 共三筆估計 hold、ledger neurons 145 |
| 持久性 | 一次服務 restart 後 keys／recent tasks／usage／catalog 快照一致；不是整機 reboot 驗證 |
| 閒置健康 | restart 後 182.19 秒、五個健康樣本通過，worker tick 持續前進 |
| FD 修復 | 原失敗達 FD 128；修復後本機 native fixture 400 ticks／20 HTTP 成功，FD 5–6；實機 restart 前 FD 4，NOFILE 仍為 128 |

非秘密摘要可直接在 checkout 閱讀：

- [當日驗收摘要](verification/asus-v1-2026-10-09/final-acceptance.json)
- [restart 持久性摘要](verification/asus-v1-2026-10-09/persistence.result.json)
- [閒置健康摘要](verification/asus-v1-2026-10-09/idle-180s.result.json)

這些 JSON 保留原始快照；其中 `git_commit`／`git_merge`／`git_push` 為 false 是實機驗收當時尚未 Git 收尾的狀態。後續 Git 交付以提交與 PR 為準，不改寫歷史收據。

三筆 POST 額度已用完。這次沒有重新驗證七家供應商，也沒有驗證完整 host reboot。GET-only 診斷的 `ready_targets=0` 反映尚未取得 credential inventory，不能解讀成 keys 不存在。Cloudflare neurons 未知仍須保留 hold，不得以文字生成成功推定已結算。

## 使用入口與限制

ASUS 已配置 ordinary-user `aqb` client；日常查詢不需 sudo 或在 argv 傳 token。Gateway 僅 loopback，應用 client credential、provider credentials 與維運 credential 分開管理。其他主機須依本機指南自行配置受保護的憑證與狀態目錄，不能沿用 ASUS 的路徑或身分假設。

文字請求的預設輸出上限為 1024 tokens。Schema 接受 1–65536，但仍受 family、provider、model 與已核定 pool target 的較小上限約束；ASUS 一般 target 上限 4096，Google 8192，Cloudflare 2048，legacy OCR 為 1。這些是本地安全界限，不是供應商保證。

已送出而結果未知的任務保留 reservation，不自動重新 POST。`preparing` 的嚴格恢復僅能接續同一 execution／原 attempt，不能退回 attempt 額度；孤立的 dispatch ledger 仍視為 unknown。SQLite connection 現在明確關閉，不能只依賴 transaction context manager 或垃圾回收釋放 FD。

## 維運邊界

固定入口採 Unix socket 與短命 worker，核對 peer 身分、固定 package／unit／policy pins、期限、systemd credential 與 resource limits。Doppler 唯讀 credential、runtime provider keys 與 ordinary-user client credential 各自隔離；worker 只允許政策核定的固定操作，不接受任意 root shell、來源路徑或 command。

已部署 maintenance 流程先驗證候選封包及應用身分，候選應用在降權後才載入；root 控制流程不直接匯入候選應用。部署與 restart 保留固定 request ID、intent 與結果收據；不明結果不盲目重送。操作前仍需核對當次授權、pins、policy expiry 及服務狀態。可讀狀態入口存在不代表已授權新部署、重啟或 provider 呼叫。

`deploy/asus/` 保留已測試的安裝、維運、歷史診斷與修復原始碼，方便審查與回歸。部分 builder／once helper 固定依賴當年的本機封包與 hash，**不是通用安裝精靈，也不應直接重跑歷史入口**。新的主機操作須產生當次審查封包並獨立驗證，不得為了讓舊 builder 通過而重造或覆寫歷史 seal。

## 本機重現

在 repo 根目錄執行：

```sh
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src
uv run --locked quota-broker demo
```

測試使用 temporary directories 與 `tests/fixtures/asus-history/` 中的非秘密文字 fixture；source archive／wheel 由目前 source 在測試目錄生成。fixture 的來源與 SHA 見 [來源表](../tests/fixtures/asus-history/README.md)。測試不需要本機 `data/` 或 `docs/evidence/`，也不呼叫真 provider 或操作 ASUS。

`data/`、`docs/evidence/` 與一次性 command drafts 為本機歷史／操作資料，不納入這次公開交付。原始封包與收據持續保留；上述三份摘要只是有界證據入口，不能替代原生操作前的最新核對。
