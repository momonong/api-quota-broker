# HP 主機保管金鑰修訂

2026-11-14，Asia/Taipei。使用者明確回覆「採用自有主機保管（建議）」，取代原方案的離線根簽章私鑰要求。原 2026-11-13 設計保留為歷史；本修訂只改金鑰保管與必要維護，不改 HTTPS 入口、SDK、敏感資料僅限本地或原工作不得重派的契約。

HP 是受信任的解密及簽章主機。root／signer／接收種子儲存 key 與 worker credential 使用 systemd host-encrypted credentials；接收種子以獨立 key 加密後持久化，使用時還原到 `/run`。HP 未具備完整可用的 TPM2；這是主機保護，不宣稱 HSM、離線 root 或 root 入侵後仍安全。維護 helper 與 Broker 使用同一服務 UID，不宣稱二者有帳號隔離。

正常請求不需要使用者插 USB、輸入密碼或聯絡 Doppler。root credential 只由每週更新 delegation 的 oneshot 取得；delegation 有效 30 天。另一個每 5 分鐘執行的 oneshot 取得 signer 與儲存 key，更新短效 manifest，並在 recipient 接近一天期限時輪替，保留 20 分鐘接收重疊期。root 和 signer 本身不隨此 timer 自動換 key。

首次可信 profile 透過已驗證 SSH 發放，SDK 固定 root／audience／epoch 並保存版本狀態。公開 HTTPS 不能自行更換 root。缺狀態、簽章不符、過期或 key 維護失敗時停止加密呼叫，不降級明文、不轉雲端。

Doppler 不存這套新私鑰的明文或本輪敏感專案 token；原非敏感雲端專案的既有設定不因此更換。Cloudflare 只轉送應用層密文，但可見流量 metadata；HP／4090 仍是可信明文端點。詳見 [維護操作](../../hp-encrypted-api-operations.md)。
