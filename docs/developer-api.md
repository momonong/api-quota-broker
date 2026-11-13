# Quota 開發者 API／Python SDK

其他專案透過 **HTTPS 網址＋自己的專案 token** 使用同一個 Broker：查模型與額度、試算路由、送工作及取結果。供應商 key 留在 Broker，呼叫端不用自行接八家 SDK。

**API root：`https://momonong.me/quota/api/v1`**。網頁入口是 `https://momonong.me/quota/`；網站密碼與 API token 分開。這是 Broker 自有 JSON API，不能直接填入 OpenAI／Gemini SDK 的 `base_url`。

**1.3.0 已於 2026-11-13 部署並通過正式 HTTPS 驗收。** Repo 外獨立安裝的 SDK 取得真實模型答案，純 HTTP 範例讀回相同結果；匿名拒絕、跨專案隔離與原 key 冪等性均已核對。SDK 原始碼、文件與範例隨本次 Git 變更交付；尚未發布到 PyPI。詳見 [交付紀錄](developer-api-acceptance.md)。

## 1. 安裝與設定

SDK 需要 Python 3.12+。可直接從本 GitHub repo 安裝，不需啟動自己的 Broker。**不要從 PyPI 安裝同名未知套件**。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install 'api-quota-broker @ git+https://github.com/momonong/api-quota-broker.git@main'
```

使用 uv 的專案可執行：

```bash
uv add 'api-quota-broker @ git+https://github.com/momonong/api-quota-broker.git@main'
```

保存 `uv.lock` 固定實際 Git commit；不使用 uv 時，把安裝網址的 `@main` 換成已核對的完整 commit，讓部署可重現。

需要 wheel 時，在本 repo 執行 `uv build --wheel`，將產生的 `dist/api_quota_broker-1.3.0-py3-none-any.whl` 交給應用，再以 `pip install /absolute/path/api_quota_broker-1.3.0-py3-none-any.whl` 安裝。`dist/` 不納入 Git；既有本機 wheel 的雜湊記錄在交付紀錄。安裝後匯入 `quota_broker.client` 即可，不需要 Broker 資料庫或提供者 SDK。

在應用秘密管理器注入：

```text
QUOTA_BASE_URL=https://momonong.me/quota/api/v1
QUOTA_PROJECT_TOKEN=<這個應用的專案 token>
```

token 不放前端 bundle、瀏覽器 localStorage、URL、Git 或日誌。網站上的小工具應由自己的後端呼叫 Broker。未提供瀏覽器 CORS／cookie 執行入口。

維護者需先登錄獨立 project ID 與 token，設定 `read`、`plan`、`execute`、輸入 bytes、輸出 tokens、同時工作數與 deadline 上限。正式 Owner token 的既有安全取得位置為 Doppler `api-quota-broker/dev/QUOTA_SHARED_OWNER_TOKEN`；它只供擁有者自己的工具使用。新專案不可共用它冒充不同身分。token 值不出現在文件中。新專案登錄須透過已授權的政策更新；現有 code publisher 不具變更 credentials／policy 的權限。

SDK 接受 HTTPS 主機、`/quota` 或完整 API root；建議一律使用完整 root。既有私人連線及本機驗證可使用 `http://127.0.0.1:PORT/quota/api/v1`。TLS 憑證照常驗證，不跟隨 redirect，不沿用環境 HTTP proxy，不自動重送提交。

SDK 自動帶 `User-Agent: api-quota-broker/1.3.0`，純 HTTP 範例帶 `quota-http-example/1.0`。自行寫客戶端時，請使用自己真實的應用名稱／版本；本次正式入口對 Python 預設識別回傳 Cloudflare `403 / 1010`。這與 Broker 的 token 錯誤不同，處理方式見 [Cloudflare 官方說明](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/)。

## 2. 最短 Python 呼叫

先為業務操作保存一個穩定的 `request_key`（例如資料庫工作 ID），再呼叫：

```python
import os
from quota_broker.client import call_free_api, ClientError

key = "my-tool-document-001"  # 提交前保存；恢復時沿用
try:
    outcome = call_free_api(
        "請用一句話解釋這個工具的用途。",
        token=os.environ["QUOTA_PROJECT_TOKEN"],
        base_url=os.environ["QUOTA_BASE_URL"],
        request_key=key,
        max_output_tokens=32,
        wait_timeout=120,
    )
    print(outcome["result"]["text"])
except ClientError as exc:
    # 保存 exc.detail；不要記錄 token／敏感輸入，也不要換 key 重送。
    print(exc.detail)
```

`call_free_api` 送出一次、等待原工作並取回結果。`submit_text`／`submit` 只提交，回傳 `queued` 不代表模型已完成。`timeout` 是單次 HTTP socket timeout；`wait_timeout` 是輪詢期限，不是取消工作或硬性的伺服器執行 deadline。

1.3.0 修正先前 helper 回傳 queued 卻被當成答案的用法；現在必須傳入穩定 key，回傳完成 outcome。需要舊有立即排隊行為時，使用 `ProjectClient.submit_text`。`options` 和 `requirements` 會傳入 Broker；不保證每個 provider 都支援，應先 explain。

## 3. 查模型、額度與可執行性

```python
from quota_broker.client import ProjectClient
import os

client = ProjectClient(os.environ["QUOTA_BASE_URL"], os.environ["QUOTA_PROJECT_TOKEN"])
catalog = client.catalog()
task = {
    "request_key": "my-tool-document-001",
    "capability": "text_generation",
    "input": {"messages": [{"role": "user", "content": "只回答 OK。"}]},
    "max_output_tokens": 32,
}
plan = client.explain(task)
print(plan["selected_target_id"], plan["candidates"])
```

- `catalog` 列出已配置的模型、模態、能力、限制、額度證據及 `project_reasons`，不呼叫供應商。`available=true` 表示當下具備正的有效容量；仍須以完整請求 explain，派送時會再查一次。
- `quota_observations.remaining` 是最後觀測值；`as_of`、`valid_until`、`current` 表示其時間與效力。`effective_remaining` 已扣除本地保留量，仍不等於供應商剛查到的新餘額。
- `local_ledger_usage` 是 Broker 當前視窗的使用／保留；`quotas`、`local_safety_caps` 是本地限制。它們不可冒稱帳戶剩餘額度。無法取得的 remaining 保持 `null`，不是 0 或無限。
- `explain` 不提交模型、不占用新額度；`selected_target_id=null` 時讀候選的 `reasons`。`project_capacity:METRIC:WINDOW` 表示必要容量證據缺失、過期、未知或不足。額度回復與新證據不能由客戶端自行假定。
- 省略 `provider`／`model` 即自動選可用免費路由。指定時必須與 catalog 的值一致；`capabilities` 只表示已配置的協定能力，不保證當前帳戶能執行每個能力。

## 4. 非同步與恢復

```python
job = client.submit(task)  # 只送一次 POST，保存 request_key
status = client.status(task["request_key"])
outcome = client.wait(task["request_key"], wait_timeout=120)
same_outcome = client.result(task["request_key"])
# 尚未派送的工作可要求取消；已派送不保證能取消。
# client.cancel(task["request_key"])
```

狀態 `queued`／`waiting`／`running` 還未完成；以 `result_available` 判斷能否取結果。工作 `completed` 後，結果仍可能是 `completed_usage_unknown`：答案已取得，部分實際資源用量未回報，Broker 保留保守額度占用。`unknown` 表示派送結果不明，**不自動重播、不換 key、不換 provider 重做**。

文字與 OCR 的完成結果統一讀取 `outcome.result.text`；OCR 也可含 `pages`，其他能力使用其 typed result。保留 `state`、`provider`、`model`、`usage` 等實際回報，不把缺失的 usage 轉為零。舊字串型工作的 `answer` 保持相容。

相同專案＋相同 key＋相同內容具有冪等性；同 key 不同內容回 409。key 限 1–120 字元：英數及 `._~-`。其他專案無法查到此工作，回 404。撤銷 token 後不可再派送；同專案新 token 可查回原工作。

`wait_timeout` 到期不取消工作。`ClientError.detail.error=wait_timeout` 或 `transport_unknown` 時，保存 `request_key` 並查 `status`／`wait`，不要再次呼叫 `run`。503 `work_busy` 代表當下服務忙；先查原工作再決定下一步，SDK 不會自動重送 POST。`job_unknown` 交由維護者核對原帳本；`job_failed` 需讀 `state`／`error_code`。

伺服器回傳的 `deadline` 控制未完成工作的期限；`expires_at` 控制加密輸入／結果保存期（預設從提交起 24 小時，實例可縮短）。應用需在期限前保存必要結果；逾期後 `queue_content_expired` 不代表允許重播。工作 key 與帳本仍保留。`cancel` 僅能取消未派送工作；執行已開始時回 503 `invalid_transition`，已終止時回傳現有狀態，不會中止或重播原派送。

## 5. HTTP 契約與錯誤

所有請求帶 `Authorization: Bearer <project token>`。POST 使用 `Content-Type: application/json`，成功為 200 JSON，回應 `Cache-Control: no-store`。完整機器規格：[openapi.json](openapi.json)。

| 方法 | API root 後的路徑 | scope | 用途 |
| --- | --- | --- | --- |
| GET | `/catalog` | read | 模型／額度中繼資料 |
| POST | `/routes/explain` | plan | 試算完整工作 |
| POST | `/jobs` | execute | 提交非同步工作 |
| GET | `/jobs` | read | 此專案近期工作，`tasks`＋`next_before` |
| GET | `/jobs/{request_key}` | read | 原工作狀態 |
| GET | `/jobs/{request_key}/result` | read | 取回保留結果 |
| POST | `/jobs/{request_key}/cancel` | execute | 以 `{}` 要求取消 |

`/tasks` 是私人連線的相容同步路徑；HTTPS ingress 只開放表列路徑。管理、全域 usage、queue tick 與原始 `/v1` 不公開。沒有全域用量帳本讀取權限的 token 仍可從自己的工作結果及 catalog 取得所需用量資訊。

| HTTP／error | 處理方式 |
| --- | --- |
| 400 `invalid_request` | 修正輸入；不要沿用同 key 改內容 |
| 401 `unauthorized` | 檢查自己的 token 是否正確、有效；停止重試 |
| 403 `forbidden` | scope 或專案限制不足，聯絡維護者 |
| 404 `not_found` | 路徑錯誤、工作不存在或不屬於此專案 |
| 409 `conflict` | 同 key 內容不同等狀態衝突 |
| 429 `project_limit` | 此專案同時工作數已滿；查既有工作 |
| 503 `work_busy`／`queue_result_pending` | 稍後查同一個工作；不盲目重送 |
| 503 `queue_content_expired` | 保留內容已過期；不能以新 key 重播 UNKNOWN |
| 503 其他錯誤 | 保留原 key 與錯誤代碼，核對服務／原工作 |

錯誤 envelope 的固定欄位是 `error`，可有 `message`、`details`、`wait_until`；未知欄位應容忍。SDK 另將原 `request_key` 加入工作錯誤。所有 JSON 時間使用含時區的 ISO 8601，單位依 metric 判讀。

## 6. OCR 與其他能力

先檢查 catalog 是否有可用 `ocr` 路由。下例使用內建 OCR 相容的 base64 小圖輸入；圖片大小也受該專案的 input bytes 界限控制（base64／JSON 會增加大小）。不得假設文字 token 的配額足以提交圖片。內建小圖上限為解碼後 36,000 bytes，專案政策可能更小。

```python
import base64
from pathlib import Path

ocr_task = {
    "request_key": "my-tool-image-001",
    "capability": "ocr",
    "input": base64.b64encode(Path("small.png").read_bytes()).decode("ascii"),
    "max_output_tokens": 1,
}
print(client.explain(ocr_task))
# 只有已確認可用、符合專案大小限制時才提交。
# outcome = client.run(ocr_task)
```

進階 typed payload／provider feature 見 [能力契約](provider-capability-expansion.md)。Typed OCR 的 document 輸入需要實例另外啟用 `ocrspace_inference` adapter；不能只因 catalog 列出 OCR 就假設該路由接受所有輸入格式。先遵循 explain 結果；此 SDK 不宣稱所有已註冊能力都已於正式站啟用。

## 7. 可執行範例與驗證

[Python SDK 範例](../examples/project_client/client.py)、[純 HTTP 範例](../examples/project_client/http_client.py) 與 [執行說明](../examples/project_client/README.md) 可以放在另一個專案目錄直接使用。純 HTTP 範例不安裝本 SDK，不需要 Provider SDK。

維護者使用 `tests/test_developer_examples.py`，在另一個暫存目錄啟動 subprocess，透過實際 HTTP、隔離專案 token 及 fixture provider 驗證文件路徑；另將 wheel 安裝至獨立環境驗證匯入與相同範例。正式 HTTPS 驗收也使用 repo 外的 wheel 與範例：一筆 Google 免費工作回覆 `OK。`，用量為 input 5／output 2 tokens，同 key 重取與再次提交仍只有一次派送。當時 catalog 列出 11 家、6 家可用；每次開發仍以當下 catalog／explain 為準，完整證據見 [交付紀錄](developer-api-acceptance.md)。
