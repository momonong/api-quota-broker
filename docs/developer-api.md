# Quota 開發者 API／Python SDK 2.1.0

其他專案使用同一個 `ProjectClient`、HTTPS 入口及自己的專案 token，選擇 `local_only` 或 `cloud_only`。SDK 自動完成內容加解密、設定簽章驗證及每次請求的短期上下文管理。

正式 `https://momonong.me/quota/api/v1` 已部署加密介面，使用相同入口即可接入。實機範圍與限制見 [驗收紀錄](encrypted-local-api-acceptance.md)。SDK 1.3.0 操作與先前正式驗收保存在 [歷史文件](developer-api-legacy.md)／[部署紀錄](developer-api-acceptance.md)。

## 安裝與一次性設定

消費端驗證 Linux x86_64、Python 3.12、glibc ≥2.34；HP Python 3.14 也已通過服務端原生相依檢查。wheel 尚未發布到 PyPI；維護者需交付同版兩個 wheel、[已驗證的相依版本](../examples/project_client/constraints-linux-py312.txt)，以及從獨立可信管道發放的 `trust-profile.json`。本次接入 zip 已包含這些非秘密檔案，token 另走私人管道。不能信任同一公開 API 自稱的 root。

```bash
python -m venv .venv
.venv/bin/python -m pip install --constraint ./constraints-linux-py312.txt \
  ./quota_broker_ohttp-0.1.0-cp312-abi3-manylinux_2_34_x86_64.whl \
  './api_quota_broker-2.1.0-py3-none-any.whl[encrypted]'
```

消費端不需要 Rust、供應商 key、Broker 資料庫或模型權重。維護者的建置與金鑰操作見 [服務指南](encrypted-local-api.md)。其他 OS／架構尚未驗收，不能要求消費端自行編譯來冒充支援。

由私人憑證檔或已批准的秘密管理方式注入 `QUOTA_PROJECT_TOKEN`，不要放進程式、URL、Git、前端 JavaScript 或一般日誌。敏感專案不要把 token 放進經同一代理解密的外部秘密服務。本輪專案 `private-development` 僅准本地且強制加密；管理者以已驗證 SSH 發放 token 與 profile，不需各專案持有解密私鑰。另設定非秘密資訊：

```bash
export QUOTA_BASE_URL=https://momonong.me/quota/api/v1
export QUOTA_TRUST_PROFILE=/absolute/managed/trust-profile.json
export QUOTA_TRUST_STATE=/absolute/private-state/quota-trust.sqlite
```

範例程式讀取以上變數。直接建立 client 時可傳 `trust_state`；未傳則用 `~/.cache/quota-broker/`。目錄由應用帳號管理，狀態檔須為 0600；保留它才能在重啟後拒絕版本回退。root／epoch 改變時，管理者重新發放可信 profile 與獨立的新狀態檔，不能靠刪 cache 解決不明錯誤。

若應用直接讀私人檔案，可用 `Path("/absolute/private/project.token").read_text().strip()` 作為 `ProjectClient` 的第二個參數；檔案及父目錄分別設為 0600／0700。公開 profile 仍需可信來源，不從 API 回應自動採信新 root。HP 的金鑰更新由主機排程完成，日常呼叫不需要 USB 或人工輸入密碼。

## 同一個 client 選本地或雲端

```python
import os
from quota_broker.client import ClientError, ProjectClient

client = ProjectClient(
    os.environ["QUOTA_BASE_URL"], os.environ["QUOTA_PROJECT_TOKEN"],
    trust_profile=os.environ["QUOTA_TRUST_PROFILE"],
    trust_state=os.environ["QUOTA_TRUST_STATE"],
)
# 先把業務 key 保存到應用自己的工作紀錄，再提交。
task = {
    "request_key": "report-20261113-001",
    "execution_policy": "local_only",
    "capability": "text_generation",
    "input": {"messages": [{"role": "user", "content": "請整理這份內部筆記。"}]},
    "max_output_tokens": 512,
}
plan = client.explain(task)  # 不推論、不保留配額。
try:
    outcome = client.run(task, wait_timeout=650)
    answer = outcome["result"]["text"]
except ClientError as error:
    # unknown / wait_timeout 時保存原 key，稍後只查原工作。
    # 不重新 run、不換 key、不自動切雲端。
    problem = error.detail
```

允許雲端的專案把 policy 改成 `cloud_only` 即可；同樣加密到 HP，HP 再將必要明文交給所選雲端供應商。省略 policy 時採伺服器的專案預設，沒有 `auto`。

敏感專案固定 `allowed_execution=["local_only"]`、`default_execution="local_only"`、`encryption_required=true`。呼叫端不能放寬；模型離線、OOM、滿載、timeout 或結果未知均不改送雲端。模型名稱含 local／4090 不構成本地信任依據。

方法包括 `catalog`、`explain`、`submit`、`submit_text`、`status`、`result`、`cancel`、`wait`、`run`。`submit_text(..., execution_policy="local_only")` 與 `call_free_api(..., trust_profile=..., trust_state=..., execution_policy="local_only")` 使用相同流程；helper 必須提供已保存的 request key。

單張 PNG 使用同一 client，只有 capability／input 改變：

```python
import base64
from pathlib import Path

image_task = {
    "request_key": "saved-image-job-001",  # 提交前先持久保存
    "execution_policy": "local_only",
    "capability": "vision",
    "input": {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "這張圖片的主要顏色是什麼？"},
        {"type": "image", "mime_type": "image/png",
         "data": base64.b64encode(Path("sample.png").read_bytes()).decode("ascii")},
    ]}]},
    "max_output_tokens": 512,
}
client.submit(image_task)
answer = client.wait(image_task["request_key"], wait_timeout=650)
```

## 中斷、重啟與取消

```python
status = client.status("report-20261113-001")
if status["result_available"]:
    outcome = client.result("report-20261113-001")
# wait 只查原工作，不再次提交。
outcome = client.wait("report-20261113-001", wait_timeout=120)
```

新程序使用同 project 的有效 token 與原 key 就能查回，不需保存原 HPKE 上下文。同 key 不同內容／policy 會 conflict。已接受的 local_only 不會因之後放寬政策而改成雲端。

`unknown` 不能當成失敗重做。未派送時 cancel 可確定取消；派送後僅要求取消，直到下游證實停止前保留工作及容量。等待逾時、取消要求、模型真正停止，分別處理。

本地未派送期限最多 15 分鐘，完成結果保存 1 小時。pending/storage_failed、unknown 與唯一恢復身分不由一般 TTL 清除。內容逾期後仍保留同 key 的防重派證據；長期保存由應用在期限內取回到已授權儲存。

## 首版本地能力與限制

- 管理者登錄的 `Qwen/Qwen3.5-4B` deployment：文字與一張 inline PNG，圖片 ≤1 MiB 且寬高 ≤1024；輸出最多 1024 tokens。Spark 128 GB、9B 量化、ASR、embedding、rerank 尚未列為可用能力。
- 不接受外部圖片 URL、工具執行、雲端前處理、stream／SSE。context 採保守 bytes 上界加圖片／訊息預留，總上限 8192；測量值見驗收紀錄，不把短合成工作推廣為最大輸入／品質保證。
- Broker 本地未完成工作全域 8、每專案 2。沿用固定 deployment 的 capacity=2，本輪驗收一次一個工作；未宣稱雙併發品質已驗收。catalog 的 `loaded`／`can_execute_now=null` 表示沒有即時設備證據。
- 精確 idempotency lookup 已接上真 selfhost-models。首次下游 POST 回覆遺失時，Broker 以唯讀 GET 找回原工作；應用仍只用原 key 查詢。查不到或結果未知不重新提交。
- 模型冷啟動需要載入及 warmup，應用需容許排隊；逾時後查原 key。4090 主機的開機自動恢復、異機復原與長期可用性尚未驗收，不提供 uptime 保證。

內容加密終點是自有 HP Broker，HP 與本地 worker 必須看到明文推論。Cloudflare 仍可見 IP、時間、大小等 metadata；不宣稱匿名、流量隱藏、端點入侵仍安全，或能限制應用自己在其他系統外送資料。

## 舊版遷移與其他語言

2.0.0 的 `ProjectClient` 沒有 plaintext fallback。缺 profile、簽章失敗、舊 server 或缺原生加密依賴，都在傳送 token／正文前停止。

非敏感舊程式可明確用 `LegacyProjectClient` 維持原 JSON 行為；它沒有應用層內容加密。敏感遷移時更換舊 token，僅配置給新 client。伺服器拒絕明文請求不能撤回代理已看過的 token。

[OpenAPI](openapi.json) 描述 encrypted/config 外層與內層工作契約。其他語言須提供相同標準封裝及信任驗證；目前未交付 JS／瀏覽器 SDK。`http_client.py` 僅供非敏感 legacy 使用。新版範例命令見 [examples](../examples/project_client/README.md)。


## 到期優先與過載重試（2026-11-17 未部署候選）

以上正式 2.0.0 接入資訊不變；本節為此次程式候選，尚未完成正式切換。文字任務可加 `"requirements": {"profile": "coding"}`，也支援 `general`、`reasoning`、`translation`、`extraction`。省略時 `code_completion` 用 coding、translation 用 translation，其他用 general。profile 只參與適配與品質評分，不會賦予模型未核對的能力。可先用同一 `explain` 看分數、各因素及品質樣本數。

符合免費資格、能力、憑證、有效配額、健康與並行限制的路由，再最大化 `expiry_utility_v1` 效用。權重為到期急迫性 60%、任務適配×品質 17%、歷史穩定性 15%、免費餘額比例 5%、延遲 2%、負載 1%。這些權重反映本產品偏好，不是假稱訓練得出的最優參數。品質採有時效的任務評測，無評測顯示 unmeasured；HTTP 成功只作穩定性證據。同分沿用 Gateway 的 selection_order，建議與派送一致。

新工作的 `max_attempts` 預設 3，顯式上限 32；既有省略參數而已保存為 32 的工作保留原身份與預算，不能靠重送取得新的預算。Broker 只重試已完整接收且可識別的同步過載拒絕，包括既定 503 error envelopes，以及固定 `rejectIfBusy` Cloudflare 429/code 3040。可用其他合格供應商時可切換；指定 provider/model 的請求仍保留該限制。

直接執行使用有上限的指數退避與 jitter；佇列用持久化業務 key、累計 attempt 計算並保存下次時間，重啟不歸零。兩者遵守 Retry-After、deadline 與總 attempt 預算。每次實際嘗試獨立入帳；過載回覆未提供 tokens/Neurons 時保留未知用量，不以重試成功沖銷。timeout、部分回應、202、含糊 502/504 或結果 unknown 不自動重送。消費端沿原 request key 查 status/result，無須自行換 key 重試。

供應商列表、計費資格與模型品質是不同證據；完整性與已知缺口見 [官方盤點](provider-inventory/README.md)，操作及驗證狀態見 [本次交付紀錄](reliability-expiry-2026-11-17.md)。
