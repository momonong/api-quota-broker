# 加密本地 API：維護者操作與契約

狀態（2026-11-14）：SDK 2.0.0、下游精確查回與正式 HTTPS 到真實 4090 的文字／單圖／回覆遺失恢復已驗收；HP 已部署 `shared-broker-20261114-encrypted-v1`。結果及限制見 [驗收紀錄](encrypted-local-api-acceptance.md)。金鑰保管採使用者已批准的 [HP 主機保管修訂](superpowers/specs/2026-11-14-hp-key-custody-amendment.md)；完整維護入口見 [HP 操作指南](hp-encrypted-api-operations.md)。

## 路徑與界線

`SDK → HTTPS /quota/api/v1/encrypted → Cloudflare（密文）→ HP 解密／政策 → 已登錄私人 HTTPS worker → 4090`。回應沿標準上下文加密。未配置本地 adapter 時 local_only 拒絕；cloud Gateway 在 provider I/O／countTokens 前拒絕 local_only。

使用 [RFC 9458](https://www.rfc-editor.org/rfc/rfc9458.html)、[bHTTP RFC 9292](https://www.rfc-editor.org/rfc/rfc9292.html)、[HPKE RFC 9180](https://www.rfc-editor.org/rfc/rfc9180.html)。固定 X25519／HKDF-SHA256／AES-128-GCM；`ohttp=0.8.0`、`bhttp=0.8.0`、PyO3 0.30.1；不自製 KDF。JWS 只接受 Ed25519、固定用途／audience，拒絕遠端 key locator。

解封裝前只回固定錯誤；之後的業務錯誤也加密。內層限 catalog／explain／jobs、固定 authority，拒絕管理 route、任意 URL、巢狀封裝與重複 headers。密文 ≤16 MiB＋64 KiB，正文 ≤16 MiB，回應沿用 32 MiB。每個加密 listener 同時最多 8 個 handler、body 接收總期限 30 秒；外層連線防護仍由受控入口負責。

## 建置

維護者用 Rust 1.90.0、Python 3.12，消費端用預建 wheel：

```bash
uv sync --all-extras --locked
uv run --no-project --with maturin==1.16.0 maturin build --release --locked \
  --manifest-path native/ohttp/Cargo.toml --out dist --interpreter .venv/bin/python
uv build --wheel
.venv/bin/python -m pytest -q tests/test_ohttp_native.py tests/test_ohttp_interop.py \
  tests/test_crypto_config.py tests/test_encrypted_transport.py tests/test_crypto_admin.py \
  tests/test_execution_policy.py tests/test_encrypted_api.py tests/test_local_jobs.py \
  tests/test_encrypted_examples.py
```

測試用暫存 keys、TLS、SQLite 與合成模型，不需正式 token／GPU；HTTPS fixture 需允許 loopback sockets。相依公告與互通向量檢查不等於外部安全稽核。

## 金鑰操作

正式 HP 已由固定 root gate 建立金鑰，並以 systemd host-encrypted credentials 保管。一般接入不執行以下命令；root／signer 不需移到應用或 4090。入口 `python -m quota_broker.crypto_admin` 僅供受控維護與測試，工具只輸出 operation/status；秘密只寫指定的 0600 檔案。

1. 初始化：`keygen --private ROOT.pem --public ROOT.pub`。正式 gate 在 HP 的 `/run` 由非 root 服務帳號產生 root／signer，再用 host key 加密持久化，移除暫存明文；不是離線 root。
2. 建立可信 profile：`{"audience":"https://momonong.me/quota/api/v1","epoch":1,"roots":{"hp-root-1":"ROOT_PUBLIC_BASE64URL"},"minimum_version":1}`。只填公鑰，經獨立管道發放應用。
3. 簽發：`delegate --root-key ROOT.pem --root-id hp-root-1 --signer-public SIGNER.pub --signer-id hp-config-1 --profile PROFILE.json --out DELEGATION.jws --days 30`。正式每週 oneshot 由 systemd 提供 root credential；輸出為公開 delegation。
4. HP 接收端：`recipient --out RECIPIENTS.json --key-id 1 --public RECIPIENT-PUBLIC.json`。新 key 有效一天；輪替保留舊 key 20 分鐘接收期，ID 不得撞到重疊期的 key。再次輪替會移除已過期項目；即使尚未清檔，runtime 仍拒絕過期 key。種子不納入一般備份。
5. 發布：`publish --signer-key SIGNER.pem --delegation DELEGATION.jws --profile PROFILE.json --recipient-config RECIPIENT-PUBLIC.json --out MANIFEST.json --state PUBLISHER.sqlite --version NEXT_INTEGER`。版本單調遞增。HP 只把公開的 RECIPIENT-PUBLIC.json 交受控簽章端；publish 不需接收私鑰，signer key 也不需放進推論 worker。

manifest 最多有效 15 分鐘，SDK 快取最多 10 分鐘。正式已安裝每 5 分鐘 publish 與每 7 天 delegation timer；recipient 約每日輪替，先持久化加密種子，再發布。root／signer 本身不隨 timer 自動換 key。版本 DB／加密種子遺失、維護失敗或到期即停止，不能降級或自行延長。

signer 外洩可持續簽到 delegation 結束，15 分鐘 TTL **不是**該攻擊上限。撤銷須獨立更新可信 profile／epoch，重新建立狀態；網路 manifest 無權重設。接收種子外洩可能解開被錄下的 bearer，需同時撤銷相關 token。HTTP TLS、OHTTP、簽章、儲存 key 各自管理；transport key 淘汰不影響既有結果查詢。

## Broker 設定

敏感 access principal 新增三個一致欄位：

```json
{"allowed_execution":["local_only"],"default_execution":"local_only","encryption_required":true}
```

三欄必須一起設定，同 project 的所有 token 必須一致。舊 policy 保持 cloud_only。既有 cloud canonical payload 不變；local_only 明確參與 HMAC，新增欄位不重寫歷史。缺舊 payload 指紋的工作仍禁止派送。

`--encrypted-config-file` 指向如下物件（各路徑必須為絕對路徑且不同）：

```json
{"profile":"/private/quota/profile.json","manifest":"/private/quota/manifest.json",
 "recipients":"/private/quota/recipients.json","state":"/private/quota/service-trust.sqlite",
 "replay":"/private/quota/replay.sqlite"}
```

`gateway-serve` 沿用既有參數，另需 `--private-api-port`、access policy、encrypted config。本地另加 `--local-config-file` 與既有 queue key。管理 helper 與服務使用各自的版本 state；replay DB 不得任意刪除／還原舊快照。

本地設定格式（worker/deployment 為不能直接使用的佔位）：

```json
{"version":1,"target":{"target_id":"rtx4090","base_url":"https://APPROVED-PRIVATE-WORKER",
 "deployment_id":"dep_VERIFIED_64_HEX","model":"Qwen/Qwen3.5-4B"},
 "token_file":"/private/quota/worker.token","ca_file":"/private/quota/worker.crt","exact_lookup":true}
```

管理者先驗證主機、TLS、deployment、模型與環境。worker token 在 0600 runtime 檔案，每次讀取以支援輪替；拒絕 HTTP、redirect、環境 proxy、未驗證 TLS。沿用原 Qwen deployment capacity=2、context 8192；本輪驗收一次只執行一個工作，不能據此宣稱雙併發品質已驗收。輸出最多 1024 tokens、單張 PNG ≤1 MiB／1024×1024。GPU 實測與 isolation 見驗收紀錄。Spark 128 GB 尚非可用 target；兩台記憶體不可相加。

## Durable job 與下游精確查回

POST 前原子保存 ownership、payload 指紋、原 target/deployment、downstream idempotency key 與 intent。intent 存在即不再 POST，即使程序在真正送出前崩潰；這個保守空窗可能需人工處理。

正常路徑用既有 `POST /v1/jobs`、`GET /v1/jobs/{id}`、`GET .../result`、`POST .../cancel`。收到 job ID 先保存再下載結果；運算 succeeded 與 result available 分開。未知不釋放容量、不轉其他本地或雲端重做。

selfhost-models 已在隔離工作樹補齊並部署以下契約；原 security-dependencies 分支與既有未提交變更保留：

```text
GET /v1/jobs/by-idempotency/{Idempotency-Key}
Authorization: existing worker bearer
200: 與 GET /v1/jobs/{id} 相同的 public job
404: 保留資料中找不到，不能解讀為允許重新 POST
```

後端以唯一 index 查原工作，認證後讀取、不建立工作、不從列表猜測；GC 後 404 仍保留 Broker unknown。key 限 `[A-Za-z0-9_.:-]{1,128}`，非法輸入在查 DB 前拒絕。真 4090 已驗證「下游接受後截斷回覆 → GET 精確查回 → 原結果」，只有一個 job／attempt；正式 HP 設定 `exact_lookup=true`。

owner 與同一 local job 只計一次，含 worker 已接受的工作；全域 8、每 project 2。其他直接呼叫 worker 的工作由 worker 全域容量管理，Broker 不宣稱能管制所有消費者。結果保存 1 小時；pending/storage_failed／unknown 保留恢復資料。內容用 queue 的獨立儲存 key 加密，防重派 tombstone 不刪除。

## 證據與剩餘界線

本輪已完成固定 HP 部署與真 GPU 的公開路徑驗收，結果以 [驗收紀錄](encrypted-local-api-acceptance.md) 為準。跨 OS／ARM wheel、Spark、雙併發品質、整機重啟自動恢復與異機災難復原未驗收。Git 提交、合併、推送與部署分開記錄，不把本機 dirty 工作樹當成已發布版本。
