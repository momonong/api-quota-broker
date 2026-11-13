# 獨立專案範例

`client.py` 預設使用 SDK 2.0.0 的加密流程。依 [接入文件](../../docs/developer-api.md) 安裝兩個 wheel、注入專案 token、設定可信 profile 與 state 後，可以複製此檔到任意目錄執行，不需 Broker checkout 或私鑰。

正式入口已支援此版加密 SDK，4090 使用固定 Qwen3.5-4B deployment。公開 HTTPS 與真模型的驗收範圍見 [紀錄](../../docs/encrypted-local-api-acceptance.md)；不要把服務端啟動或合成 fixture 當成所有設備／品質的驗收。

```bash
python client.py catalog
python client.py explain --key saved-report-001 --execution-policy local_only --text '只回答 OK。'
python client.py submit --key saved-report-001 --execution-policy local_only --text '只回答 OK。'
python client.py wait --key saved-report-001 --wait-timeout 650
# 新程序仍可沿原 key 取回，不用原加密上下文。
python client.py result --key saved-report-001
```

允許雲端的專案可改成 `--execution-policy cloud_only`；省略時採伺服器預設。需要一次提交及等待可用 `run`。不因 timeout／unknown 重複 `run` 或換 key，改用 `status`／`wait`／`result`。取消用 `cancel`；派送後不能把取消要求當成已停止。

範例讀取 `QUOTA_BASE_URL`、`QUOTA_PROJECT_TOKEN`、`QUOTA_TRUST_PROFILE`、`QUOTA_TRUST_STATE`。私有測試 CA 可指定 `QUOTA_CA_FILE`，仍驗證 TLS 與主機名，不接受跳過驗證。輸入／結果會出現在自己的終端，應用需依原資料政策處理。

維護者以私人管道提供 token 和可信 profile；目前敏感專案 `private-development` 僅准 `local_only`。日常加解密及金鑰更新由 SDK／HP 處理，不需要 USB、root 私鑰或供應商 key。完整單圖 PNG 範例也在接入文件中。冷啟動需要載入模型；`wait` 逾時後保存 key，稍後仍只執行 `status`／`wait`／`result`。

非敏感舊系統才明確使用 `python client.py --legacy catalog` 或 `http_client.py`。兩者沒有應用層內容加密，不能拿敏感 token／資料測試；SDK 不會自動切到這兩條路徑。原 SDK 1.3.0 的正式驗收保存在 [歷史紀錄](../../docs/developer-api-acceptance.md)。
