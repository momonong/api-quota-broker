# HP 加密 API 維護

公開入口保留 `https://momonong.me/quota/api/v1`；新路由是 `GET /crypto-config` 與 `POST /encrypted`。Caddy 將它們交給既有 loopback 18588 listener，清除外層 Cookie／Authorization；專案認證在加密正文內。既有 dashboard、metadata listener 18587 與雲端專案保留。

## 信任與檔案

| 位置 | 用途 |
| --- | --- |
| `/etc/credstore.encrypted/quota-private-*` | systemd host 加密的 root、signer、storage key 與 worker credential |
| `/var/lib/quota-private-crypto/` | 公開 profile、delegation、manifest、單調版本 DB，以及加密的 `recipients.fernet` |
| `/run/quota-private-crypto/recipients.json` | 僅 runtime 的接收私鑰；0700 目錄／0600 檔案 |
| `/etc/quota-shared-broker/` | root 管理的加密入口、固定 local target、敏感 access overlay 與 worker CA |
| `/var/lib/quota-shared-broker/` | 原權威帳本、加密工作內容、信任與 replay 狀態；不可清空來解除 unknown |
| `/home/morris/.local/share/quota-private-api/` | HP 管理者私人目錄中的 `project.token` 與 `profile.json`；用已驗證 SSH 安全發放 |

維護 helper 為 `/usr/local/libexec/quota-private-crypto.py`，固定相依及程式位於 `/opt/quota-private-crypto`。helper 禁網路、只寫指定 state/runtime；與 Broker 同一 UID。host 加密防止只取得密文檔案者直接使用金鑰，不能防止 HP root 入侵；HP 沒有完整可用 TPM2。兩端設備、可信 SDK 與 profile 發放仍是信任前提。

## 自動維護

- `quota-private-delegate.timer` 每 7 天簽發 30 天 delegation；只有這個 oneshot 載入 root credential。
- `quota-private-publish.timer` 每 5 分鐘發布新版本；manifest 最長 15 分鐘、SDK 快取最長 10 分鐘。recipient 接近一天期限時輪替，舊 key 接收重疊期 20 分鐘。
- `quota-private-publish.service` 從加密持久檔恢復 `/run` 接收種子；版本 DB 或加密種子遺失時停止，不建立新 root、不歸零版本。
- root／signer 的換 key 與撤銷是獨立管理操作。signer 外洩風險可持續至 delegation 結束；15 分鐘 TTL 不是攻擊上限。

唯讀檢查：

```bash
systemctl is-active quota-shared-broker.service quota-private-delegate.timer quota-private-publish.timer
systemctl show quota-private-delegate.service quota-private-publish.service -p Result -p ExecMainStatus
```

oneshot 執行完成後 inactive 是正常狀態，需核對 `Result=success` 與 timer active。不要把私鑰、token、完整 runtime credential 或內容寫入 journal、聊天或 Git。

## 4090 私人路徑

4090 的 `quota-hp-worker-tunnel.service` 使用獨立 SSH key／`quota-local-tunnel` 帳號，只在 HP loopback 開 18443，轉至 4090 loopback 18443 的 `quota-local-worker-tls.service`。worker TLS 固定 CA 並驗證主機名，再轉到既有 loopback scheduler 18080。沒有把模型 port 開放到 LAN／公網。

HP 帳號只允許這一個 remote forward，禁止 session、PTY、agent、X11、其他轉發與 shell；root-owned authorized_keys 與 sshd Match 共同限制。HP adapter 開啟 `exact_lookup=true`，僅使用登錄 deployment 與既有 worker credential。模型容器沿用 internal network、固定 image、離線權重及受管 executor。

目前 4090 API／executor／controller、TLS 與 tunnel 為明確啟動的 user service；尚未安裝或驗收整機開機自動啟動。重開機／登出後需由管理者先核對原 state、未知工作與 GPU，再恢復原服務。接入方不可自行啟動模型、換 key 或下載權重；worker 離線時原工作保持原身分，不改送雲端。

## 部署與回復

一次性 root gate 是 `deploy/hp_encrypted_bootstrap.py`。固定 8 檔 bundle 與 gate 雜湊、比對原 release／manifest／unit／Caddy，再建立加密憑證與私人通道。它只經既有發布通道切換 app，保留原帳本。備份與收據在 `/var/backups/quota-shared-broker/quota-encrypted-20261114-v1/`。

失敗或回覆遺失先讀同一 operation 收據；不得重跑 gate、刪除 state 或換 operation 迴避未知結果。正常發布拒絕仍有本地未完成／未知工作；加密配置存在時也拒絕一般舊版 rollback。退回舊版須由 root 同步回復 unit／helper／設定；`publish` 回 not_started 時不得重啟忙碌 Broker。回復只撤回本次建立且雜湊／UID 仍相符的資源，不停用既有其他通道。

目前保留了部署回復材料，**未完成異機災難復原演練**。僅備份 encrypted credentials 不足以在另一台主機解密，還涉及 host 解鎖材料；正式備份須採獨立加密與保管權限，不能把 host 解鎖材料與密文一起公開或明文存入 Doppler。遺失根信任時由管理者發放新 epoch／profile，不從公開 API 自動取得新 root。
