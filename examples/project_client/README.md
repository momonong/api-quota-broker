# 獨立專案接入範例

完整設定與契約見 [開發者接入文件](../../docs/developer-api.md)。兩個檔案可以複製到任意目錄；不需要 Broker 的資料庫、設定或供應商 key。

- `client.py`：依接入文件從 GitHub 或 wheel 安裝 SDK 後執行，包含 `run`、`wait` 與非同步工作操作。
- `http_client.py`：只用 Python 標準函式庫，展示各語言都能採用的 HTTP 契約，每次命令只送一個請求。

先從秘密管理器注入 `QUOTA_PROJECT_TOKEN`，設定 `QUOTA_BASE_URL=https://momonong.me/quota/api/v1`。2026-11-13 已用 repo 外安裝的 SDK 與這兩個範例通過正式 HTTPS catalog／explain／結果讀取驗收，詳見 [交付紀錄](../../docs/developer-api-acceptance.md)。

兩個範例均送出真實應用名稱的 `User-Agent`。自行移植到其他語言時，保留自己的應用識別、TLS 驗證、原工作 key，以及不自動重送 POST 的行為。

```bash
python client.py catalog
python client.py explain --key example-order-001 --text '只回答 OK。'
python client.py run --key example-order-001 --text '只回答 OK。'
# 等待逾時或連線中斷後，用同一個 key 查詢。
python client.py wait --key example-order-001
python client.py result --key example-order-001
```

純 HTTP 版本：

```bash
python http_client.py catalog
python http_client.py explain --key example-http-001
python http_client.py submit --key example-http-001
python http_client.py status --key example-http-001
# status 的 result_available=true 時才取結果。
python http_client.py result --key example-http-001
```

每個 key 代表一個固定業務操作。不同內容使用新的業務 key；同一操作不因重啟、等待逾時或未知結果而換 key。範例會將內容輸出到你的終端，請自行保護終端記錄。
