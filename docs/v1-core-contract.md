# v1 核心資源池契約與驗收

本文件保存核心架構與原始驗收契約；後續正常1.0部署、輸出上限、FD修正與live證據以[ASUS維運](asus-broker-ops.md)及[使用指南](v1-local-guide.md)為準。

起點 `54f21d4`，沿用 `feat/seven-provider-v1`。本階段補全可擴充資源池、共同路由、持久健康狀態與加密等待佇列。人類來源：main `01a0de5f-8306-71a3-9738-7ac6eb4d7746`，UTC 2026-10-02 07:50:25.365；既定 main → orchestrate →原 task。七家為首批 adapter；同協定新供應商及模型可透過管理者設定加入。

## 驗收矩陣

所有下列證據使用離線 fixture、臨時私有 SQLite 與暫時 loopback HTTP，沒有供應商或 Doppler 呼叫。

| 驗收 | 實作 | 可重現行為證據 |
| --- | --- | --- |
| A 可擴充 Registry | 每個 Gateway 獨立 Registry；provider＋model 身份；可信 manifest 與註冊 Adapter Protocol；admit/request/transport/interpret、非執行拒絕與 quota observation hooks | `test_registry.py`：第八家同名、不變全域 catalog、固定 origin／redirect／路徑注入、既有 OpenAI/Gemini/Cloudflare 新模型、新協定與新翻譯政策；`test_framework.py::test_manifest_same_name_config_only_through_http_cli_queue`、`test_registered_adapter_error_usage_and_observation_need_no_provider_branch` 經 HTTP／CLI／SQLite |
| B 共用路由與資源 | explain/reserve 共用排序；能力/features/限制，budget refresh、健康、可信 headroom、local pressure、inflight、latency、priority；觀測有來源／時間／期限；原子觀測 holds | `test_ranking_uses_observed_headroom_health_and_budget_refresh`、`test_explain_and_atomic_reserve_use_remaining_and_holds`、`test_observation_changed_after_reserve_fences_dispatch`、`test_quota_evidence_stale_contradictory_and_groq_dimensions`；unknown remaining 為 null，短窗口不冒充免費 budget |
| C 跨任務故障隔離 | SQLite circuit/health、429 冷卻、401/403 修復、有限 half-open；generic400/422不永久封鎖；整個 job 的 max_attempts | `test_circuit_half_open_auth_repair_and_generic_task_errors`、`test_stale_completion_does_not_overwrite_newer_health_or_observation`、`test_attempt_policy_can_use_fourth_unsent_target`；四目標送出前失敗可到第四個，timeout 不 fallback |
| D 等待／重啟 | queued/waiting/running/completed/failed/expired/cancelled/unknown；原子 claim、lease fencing、priority/deadline、bounded execution lease；同步 run 與非同步 submit/result 並存 | `test_queue.py`：多 worker、claim／dispatch crash、舊 worker 禁派送、晚到已知結果、quota refusal reset 與總 budget；真 Gateway lifecycle；`test_queue_busy_reset_restart_and_cancel_deadline`；unknown ledger不擋其他 provider |
| E 隱私 | 顯式 queue key；cryptography Fernet authenticated encryption；獨立持久 key、私有目錄0700/檔0600及sidecar preflight；terminal移除payload、TTL移除result；metadata tombstone防重播 | `test_queue.py`：wrong/lost key、ciphertext tamper/swap、TTL、拒絕 unsafe DB 時 hash 不變；HTTP result 需認證；DB／正常 log無 input/answer/key；同步輸入／回答仍只在記憶體 |
| F 正常入口與相容 | API/CLI/服務生命週期、獨立 admin quota/health 操作、可見 worker failure、有界client wait；已知完成與未知用量分離；歷史 unknown不追認 | `test_service_worker_runs_and_stops_with_server_lifecycle`、`test_admin_auth_and_worker_failure_are_visible_without_secret_logs`、`test_cli_slow_provider_and_worker_wait_bound_is_not_old_15_seconds`（兩次超15秒回應）、`test_gateway_serve_loads_manifest_and_private_queue_before_start`、`test_legacy_schema_has_no_invented_execution_completion` |

### 已完成但用量未知

`test_generic_cloudflare_estimation_unknown_usage_frees_only_execution_slot` 驗證同 CF target/shared concurrency=1，兩個不同 job 依序取得有效回答但缺 Neurons：持久 execution-completion 證據解除執行占位，兩筆 estimated charges 累計1000，reported Neurons為null，cap滿後仍等待。設定變更保留舊 bucket charges，同 key不重播。歷史 unknown及真正 timeout沒有完成證據，保留占位與帳務。

管理者可在任意需要 Neurons quota 的 target 設定有期限、input/output適用範圍的 per-request upper bound。通用 Task不用傳provider價格；沒有設定時`estimation_unconfigured`。legacy `neuron_bound`保留，估算適用時至少取管理者上界，client傳1不能低報。這是操作估算，不是官方單價或actual usage。

## 契約與限制

- 相同模型名跨 provider 時，caller必須給provider；target/account/shared bucket仍各自設定。manifest不含secret或可執行code，只有管理者可修改。新的不同schema需已註冊及受測 adapter。
- 通用 Task：request_key/capability/input/max_output_tokens；可選provider/model、requirements.features、priority(-100..100)、deadline、wait_policy(wait/reject)、max_attempts(1..32)。Riva特定語言與1952字政策由其adapter管理；其他翻譯adapter不被套用。
- 同步 execution總界限180秒；各provider仍保留更短界限。CLI HTTP等待預設185秒，可明確設定0..3600秒；逾時不自動重送。佇列單次execution lease預設180秒，下一次派送仍核對lease/deadline/TTL與quota。
- QuotaObservation受嚴格schema、非負值、remaining<=limit、時間／來源與秘密反射驗證。舊觀測／舊request晚到不覆蓋較新證據；同時間採更保守remaining。Groq documented headers：requests=RPD/day，tokens=TPM/rolling_minute，[官方文件](https://console.groq.com/docs/rate-limits)。其他未提供者維持unknown。
- Adapter缺quota hooks時不推定其429未執行；相容OpenAI schema也不自動繼承其他provider quota語意。只有受測adapter明確證明未執行，且沒有answer/usage，才可釋hold並改派。
- Queue使用[Fernet](https://cryptography.io/en/latest/fernet/)。key遺失／不匹配會拒絕；備份DB必須另行妥善備份獨立key。沒有建立或讀取任何真秘密。TTL是邏輯ciphertext清除，metadata tombstone保留no-replay；不是磁碟安全抹除。
- server關閉會停止新claim；已在途有界呼叫可完成，durable lease保護崩潰。背景worker停止時diagnostics顯示固定安全error code；HTTP仍可查詢/取消/取結果，修復後需正常重啟服務才能恢復背景worker。
- API/CLI指南：[v1-local-guide.md](v1-local-guide.md)。七家歷史真實成功與未知問題：[v1-seven-provider-readiness.md](v1-seven-provider-readiness.md)。離線工程驗證不替代新真API驗收、持續配額或使用者接受。

## 驗證與交付狀態

Ubuntu／Python 3.12，凍結程式版本完整 `pytest -q` **483 passed in 96.63s**。原baseline312項加本階段171項；完整suite包括兩次超15秒CLI/worker成功fixture。最後受影響回歸另為175 passed／1個未變慢回應case重用完整suite證據。`ruff check .`通過、`ruff format --check .`（68 files）通過、`mypy src/quota_broker`（18 source files）通過、`git diff --check`通過；`uv sync --locked --offline`確認lock/environment一致。沒有遠端CI，未驗證其他OS。

11份既有live DB SHA-256全部與本階段起點相同，未對它們建構Gateway或回寫完成標記。本地工程驗證已完成；本地提交ID以Git／最終報告為準，未推送。A–F為工程驗證完成，人工接受與部署仍未進行。

本階段零新live呼叫、零Doppler、零真key讀取；無部署、常駐服務變更、push/merge/tag/release。必要測試只啟動臨時loopback fixture，結束時關閉。成熟cryptography依賴由`uv.lock`固定；其他服務／worktree／歷史資料保持。
