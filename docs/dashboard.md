# 本機配額工作台

Quota Broker 工作台提供中繼資料快照與**單一任務的選路建議**。它不會執行、排程、保留配額、更新證據、修改配置或開啟計費。所有建議固定回傳 `execution_allowed: false` 與 `paid_allowed: false`。本次只完成合成資料的本機離線驗證，尚未接入真實 Gateway 或讀取供應商帳號。

## 離線示範

預設為離線示範；也可明確指定 `--demo`。合成 Google、Groq、Mistral 路由分別展示完整餘額、未知餘額與資格證據過期。示範使用實際 Gateway 與臨時 SQLite；憑證解析器、一般與 typed provider transport 一旦被呼叫就會失敗。關閉程序後，臨時資料庫由程序清理。

先準備**獨立工作台權杖**，存放在你擁有、權限為 `0600` 的一般檔案；不得沿用 Gateway client/admin token，不要把權杖放入命令參數、Git 或截圖。可在私人終端建立權杖檔，不輸出值：

```bash
python3 -c 'import os,secrets; fd=os.open("/tmp/quota-dashboard.token",os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,(secrets.token_urlsafe(32)+"\n").encode()); os.close(fd)'
quota-broker dashboard-serve --demo --dashboard-token-file /tmp/quota-dashboard.token
```

在同一台主機的瀏覽器開啟 `http://127.0.0.1:18086`，透過密碼欄位輸入工作台權杖。程序只綁定 literal IPv4 loopback；另一台裝置的 localhost 指向它自己。啟動範例不是部署指示，也未提供其他裝置的可達性保證。瀏覽器使用 HttpOnly、SameSite=Strict cookie；session 最長八小時，重啟後失效。工作台權杖與 upstream 權杖都不能透過 URL 或 Bearer header 當作瀏覽器 session。

## 真實 Gateway 模式（需另行授權驗證）

必須明確指定 `--upstream` 與 `--upstream-token-file`，不能與 `--demo` 同時使用。兩個權杖檔皆檢查擁有者、私有檔案權限與 symlink；兩個值必須不同。上游只接受 `http://127.0.0.1:<port>`，沒有其他主機、DNS、proxy、redirect 或路徑前綴支援。

```bash
quota-broker dashboard-serve \
  --upstream http://127.0.0.1:18084 \
  --upstream-token-file /private/path/gateway-client.token \
  --dashboard-token-file /private/path/dashboard.token
```

上游 client token 只保留在 Python 程序，瀏覽器不會取得。上游存取固定為 `GET /v1/catalog`、`GET /v1/diagnostics`、`GET /v1/usage`、`GET /v1/tasks?limit=6` 及 `POST /v1/routes/explain`。無 execution、queue、admin 或任意轉送路由。每次上游回應有 2 MiB 上限與有限讀取時間；單次 HTTP 連線 timeout 為五秒。來源失敗或無效形狀回傳固定錯誤，介面會隱藏舊快照，不把快取資料當目前讀值。

## 建議 API

`POST /api/login` 接收 `{"token": "<dashboard-only token>"}`，取得 session cookie；`POST /api/logout` 撤銷目前 session。每個 POST 都要求 `Content-Type: application/json`、正確 `Content-Length`、與工作台一致的 `Origin: http://127.0.0.1:<port>` 及 `X-Dashboard-Request: 1`。請求上限 64 KiB、JSON 深度上限 16；不接受 transfer encoding。登入連續失敗五次後有一分鐘內的節流。

持有 cookie 的用戶可呼叫 `GET /api/snapshot`；`POST /api/advice` 使用[既有 Gateway Task JSON](client-integration.md)。回傳包含來源／快照時間、`decision: recommend | ask_user`、可空值的 `selected_target`、依 Gateway 順序排列的 `candidates`、原因與可空值的 `recheck_at`。此 API 與瀏覽器共用一份政策。文字表單是其中較小的操作入口。

推薦必須同時滿足：最新 diagnostics 就緒；供應商、模型、target 身分一致；憑證名稱清單目前有效；免費資格目前有效且未啟用計費；Gateway 候選適合此任務；每個配置與觀測的 metric/window 都有目前有效、權威且足夠的餘額證據。保留 Gateway 候選順序，只跳過較嚴格安全門檻未通過的候選。

成本分別檢查 requests、input tokens、output tokens、total tokens 與已知 neurons；`tokens` 僅與 total tokens 同義化。未知資源成本、缺少讀值、過時、局部證據、空值、付費或不足都必須 `ask_user`。不將 input 與 output 的分開觀測相加來虛構 total token 餘額。重複或矛盾觀測按保守方式檢查，不採最後一筆覆蓋。日／月本機 cap 使用非 UTC 時區，而觀測無法證明視窗對齊時，也停止推薦。尚未提供完整批次規劃、throughput 保證或自動執行。

## 數字與期限的界線

- **本機上限**是設定的保護值，不能代替供應商剩餘額度。Gateway catalog 另外提供 `local_ledger_usage`：沿用 admission 的當前 metric/window/timezone 帳本讀值，包含保留與輔助 request charges。介面顯示同維度的 used/limit 與比例；舊版上游缺少此欄位時保持未知，不由歷史聚合倒算。catalog 讀取不清理到期 reservation，因此仍未清理的 charge 可能被保守計入。
- **本機記錄用量**是已送出生成嘗試的聚合，與供應商帳單、其他帳號用量、輔助請求與當前視窗分開。保留量屬於本機紀錄；觀測有效餘額使用 Gateway 已扣保留量的值。不同單位或視窗不合計為「總配額」。
- **憑證清單有效**只代表所需名稱存在且清單未到期，未讀取或驗證憑證值，更不保證 provider 接受請求。
- **免費資格證據到期**是本機需要重新確認；不是帳號或點數到期。**餘額觀測到期**表示讀值不再可用；不是重置。**reset_at** 是供應商視窗重置資料；只有明確提供才顯示。現有 API 沒有獨立的實際點數到期欄位，因此不推算或宣稱。
- `recheck_at` 只是可重新核對的估計時間。Gateway 可能從觀測期限推導它，不能當保證重置、可自動重試或自動排程的許可。
- catalog、diagnostics、usage 與 recent 不會讀秘密或呼叫 provider。`Gateway.explain` 可能清理已到期且未送出的 reservation，會有帳本 bookkeeping 寫入；工作台不能宣稱零資料庫寫入。它不建立 reservation、dispatch 或 queue。

最近工作僅投影 provider、model、state、建立／完成時間與 latency；不提供 request key、提示詞、回覆、provider request ID、secret ref 或原始 diagnostics。頁面只用 DOM 文字節點顯示來源值，且沒有外部字型、腳本、資產或前端建置工具。

驗證指令：`pytest -q`、`ruff check src tests`、`mypy`。離線測試涵蓋真實 Gateway 選路、未知／過時／不足／付費阻擋、不同成本與單位、session/origin/route/body 邊界、權杖反射、redirect 與回應大小；真實供應商、正式服務與人工接受仍需各自驗收。
