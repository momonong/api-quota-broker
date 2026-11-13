# Quota 4090 精確查回與端到端驗收

> 使用既定 executing-plans／測試先行流程；承接已批准的加密本地設計，不重啟選型討論。

**Goal:** 其他專案照接入文件，以同一 HTTPS／SDK 完成真實 4090 文字與單圖工作；敏感資料不送雲端，首次提交回覆遺失仍只查原工作。

**Architecture:** selfhost-models 在既有受認證 jobs API 增加唯一 idempotency 索引的唯讀查詢；Broker 沿用既有 local adapter、OHTTP、專案政策及原工作身分。沿用固定 Qwen3.5-4B 權重／worker image、原權威帳本與模型排程。

**Spec:** [已確認方案](../specs/2026-11-13-rtx4090-encrypted-local-api-design.md) 與 [精確查回契約](../../encrypted-local-api.md)。本輪使用者已授權補下游並接真正 4090 驗收；仍不含 Git 合併／推送或無關服務變更。

## 範圍與保護

- Broker 沿用原工作樹；selfhost-models 由 `45ebfd57db5313292ae8f38e733f3e2894a99e5f` 建隔離 worktree，原 security-dependencies 分支及四個 dirty 檔案不變。
- 固定介面 `GET /v1/jobs/by-idempotency/{key}`，沿用 public bearer 與 public job projection；不建立 job、不讀 prompt、不觸發 GPU、不從列表猜測。404 不授權重送。
- worker 只經私人加密路徑接入；原 API／SDK root 保留。Caddy／服務必要調整僅限 Quota，先固定候選、備份、驗證及最小權限範圍；需人工認證時最後才交付具體 gate。
- 不下載模型、不變更 GPU driver／全域 Docker、不停止其他容器、不清理歷史 unknown／receipt／gate。
- 真 GPU 使用合成資料，單次新窗口最多 1 次模型 load（含最多 6 次 warmup）與總共 12 次 generation attempts；最多 30 分鐘，遇未知派送或資源身分不符即停止新提交。需超出時保留原收據並交使用者決策。
- 不聲稱 GPU 合成驗收等於私人業務品質／所有平台可用；正式可用須有公開入口、獨立 SDK、真模型及原工作重取共同證據。

## Review Focus

認證先於 key lookup；非法／超長 key 不進 SQL；查回不碰 admission、lease 或 generation budget；GC／錯 key 不重派；部署沿用原 DB 與未知工作，不能以新 state 規避恢復。

## 工作與驗收

1. [x] selfhost lookup：9 個精確查回測試通過；完整 CPU 契約 389 passed、2 skipped。新隔離工作樹保留原分支與 dirty 檔案。
2. [x] 真實下游契約整合：文字、單圖、截斷首次下游回覆三個真 GPU 工作均完成；每工作只有一個 attempt，精確 GET 恢復原 job，合成 Broker 的雲端呼叫為零。
3. [x] 部署前核對：固定 Qwen 資產、原 control/executor DB、TLS、Docker internal network 與 51 檔 HP 部署包已核對。201 項本輪整合測試通過；一次獨立審查的實質問題已修正並驗證。
4. [x] 真 GPU／入口驗收：正式 HTTPS 的獨立 wheel consumer 完成文字 OK、單圖 Red、新 client 原 key 查回 OK；錯 token／敏感 cloud_only 拒絕，HP loopback 明文回 encryption_required。三筆 worker job 各一個 attempt；每秒全卡記憶體最高取樣 17,955 MiB。未宣稱完整封包稽核。
5. [x] 更新接入文件／範例／實測收據；交付含兩個 wheel、相依版本與公開信任 profile 的接入 zip。token 另行私人保管。列明平台、能力與未完成項；selfhost-servers 新增本次日期化部署紀錄／Caddy 片段，未覆寫既有草稿。

原對話直接執行並作一次最終獨立審查。使用已批准設計與本輪明確範圍，不另要求規劃批准；有實際資源／權限阻礙才交回具體決策。

## 2026-11-14 接續紀錄

- 使用者明確選擇「採用自有主機保管」，取代離線 root 設計；詳見 [修訂](../specs/2026-11-14-hp-key-custody-amendment.md)。不向 Doppler 寫入新加密私鑰或敏感專案 token。
- 第一窗口 `quota-encrypted-20261114-01` 已關閉：1 load／9 generation budget（包含 6 次 warmup 預留與 3 個工作）。模型已正常卸載；準備 HP 期間不占用模型 GPU。
- HP 操作 `quota-encrypted-20261114-v1`／Broker 操作 `52b2f6d3-5b60-4f2e-882c-c9224b93bfc3` 收據為 complete。正式 release 為 `shared-broker-20261114-encrypted-v1`，原四項服務 active；公開 GPU 驗收仍需另外取得證據。
- 第二窗口 `quota-encrypted-20261114-02` 專供正式 HTTPS／獨立 SDK 驗收，最多 1 load、12 generation attempts、30 分鐘；先核對第一窗口已關閉及沒有未知工作，再開啟。沿用同一帳本，不重設 lifetime 計數。
- 第二窗口已完成並關閉，使用 1 load／9 generation budget；正常卸載模型，再恢復 controller 按需服務。啟動流程中一筆未執行的排隊工作已明確取消，attempt=0，完整對帳記於驗收文件。

## 後續 Git 交付授權

2026-11-14 使用者要求「好 先合併和推送」。本階段只交付 Broker 的加密 API／SDK／文件、本地模型的精確查回，以及 selfhost-servers 的兩份本次部署紀錄／片段；原工作樹、憑證、`.state`、模型權重與其他任務變更保留。Git 交付不重跑現場部署、模型驗收或發布 images／PyPI 套件。
