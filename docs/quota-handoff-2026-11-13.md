# Quota 專案成果報告與交接文件（2026-11-13）

更新時間：2026-11-13 02:40，Asia/Taipei。

---

## 1. 核心成果摘要

1. **8 家具有有效憑證與免費額度的供應商已全數真實跑通（100% Live 實測通過）**：
   - 透過統一的 `Gateway.run()` 與新配置 `.state/shared-broker-20261111/runtime-config-v7`，已完成 **OpenAI、OpenRouter、Google (Gemini)、Groq、Mistral、NVIDIA、Cloudflare Workers AI、OCR.space** 共 **8 家供應商**的端到端真實 API 呼叫驗收（皆回傳 HTTP 200 與預期輸出）。
2. **其餘 3 家供應商停用原因已由官方登入頁面實證確認**：
   - **Hugging Face**：已登入官方 Billing 頁面確認 `Credits: 0.00 USD`（現行 Free 方案不再附贈每月 Inference Providers 免費額度）。
   - **SambaNova**：已登入官方主控台確認 `No commits or credits`（需綁定付款方式並儲值），且 Doppler 無對應 API key。
   - **Cerebras**：官方免費試用需先驗證信用卡（`$5 / 30 天`），不符合免綁卡原則，且 Doppler 無對應 API key。
3. **封裝統一易用的呼叫介面（`call_free_api` / `ProjectClient`）**：
   - 於 [`src/quota_broker/client.py`](../src/quota_broker/client.py) 新增單一函式 `call_free_api(...)`，讓任何內部服務或專案只需一行 Python 即可透過共用 Broker 自動選路或指定供應商呼叫免費 API。
4. **公網 `/quota` 三工作台與真實帳戶證據串接完成**：
   - 公網 `https://momonong.me/quota/` 已部署 `quota-console-audit-20261112-v6`，正確顯示 11 家供應商的核對餘額、刷新規則、到期日與觀測時間。

---

## 2. 八家可用供應商真實呼叫驗收結果（2026-11-13 實測）

以下為使用 Doppler (`api-quota-broker / dev`) 真實憑證、經由本專案 `Gateway.run()` 執行的端到端實測結果：

| # | 供應商 (`provider`) | 模型 (`model`) | Target ID | 任務類型 | 狀態 (`state`) | HTTP | 延遲 (ms) | 實測 Token / 資源用量 | 實際回傳內容 |
|---|---|---|---|---|---|---:|---:|---|---|
| 1 | **OpenAI** | `gpt-6-luna` | `openai-personal-luna` | `text_generation` | `completed` | 200 | 4,087 | Input: 11, Output: 6 | `"PONG"` |
| 2 | **OpenRouter** | `liquid/lfm-2.5-2.6b:free` | `openrouter-personal-liquid` | `text_generation` | `completed` | 200 | 1,494 | Reported via OpenRouter | `"PONG"` |
| 3 | **Google** | `gemini-3.5-flash-lite` | `google-personal-gemini` | `text_generation` | `completed` | 200 | 11,859 | Input: 9, Output: 2 | `"PONG"` |
| 4 | **Groq** | `openai/gpt-oss-20b` | `groq-personal-gpt-oss` | `text_generation` | `completed` | 200 | 571 | Input: 80, Output: 19 | `"PONG"` |
| 5 | **Mistral** | `ministral-3b-latest` | `mistral-personal-ministral` | `text_generation` | `completed` | 200 | 653 | Input: 12, Output: 3 | `"PONG"` |
| 6 | **NVIDIA** | `nvidia/nemotron-3.5-lightning-30b-a3b` | `nvidia-personal-nemotron` | `text_generation` | `completed` | 200 | 541 | Input: 25, Output: 3 | `"PONG"` |
| 7 | **Cloudflare** | `@cf/meta/llama-3.2-1b-instruct` | `cloudflare-personal-llama` | `text_generation` | `completed_usage_unknown` | 200 | 769 | Input: 53, Output: 4 | `"Pong."` |
| 8 | **OCR.space** | `ocr.space/engine2` | `ocrspace-engine2-unverified` | `ocr` | `completed` | 200 | 1,456 | Conversions: 1 | `"OK"` |

> **備註**：Cloudflare Workers AI REST API 不在回應內回報精確 `neurons` 數值，因此 Gateway 按安全規範標記為 `completed_usage_unknown` 並以保守預估上限計入本機安全帳本，但模型輸出（`"Pong."`）完全正常回傳。

---

## 3. 本輪定位並修復的關鍵問題（Root Causes & Fixes）

為了讓上述 8 家供應商全部跑通，本輪解決了以下 6 個實質阻礙：

1. **Cloudflare WAF 阻擋 Python 預設 User-Agent（修復 Groq HTTP 403 `error_code: 1010`）**
   - **位置**：[`src/quota_broker/family_transport.py`](../src/quota_broker/family_transport.py)
   - **原因**：`family_transport.py` 的獨立子程序使用 `urllib.request`，預設送出 `User-Agent: Python-urllib/3.12`，被 `api.groq.com` 前方的 Cloudflare 直接以 HTTP 403 (`text/html`) 拒絕。
   - **修正**：在 `_HTTP_CHILD` 預設補上 `'User-Agent': 'curl/8.5.0'`，與 `gateway_providers.py` 保持一致。

2. **推理模型（Groq `gpt-oss-20b` 與 NVIDIA `nemotron-3.5-lightning`）思考 Token 耗盡問題**
   - **位置**：[`src/quota_broker/family_adapters.py`](../src/quota_broker/family_adapters.py)
   - **原因**：Typed family adapter (`openai_chat_family`) 先前未像 `gateway_providers.py` 一樣抑制推理模型的預設長思考，導致小 `max_output_tokens` 全數耗盡於 `<think>` 區塊而使 `content` 為空。
   - **修正**：
     - 針對 `groq` 自動帶入 `"reasoning_effort": "low"` 與 `"include_reasoning": False`。
     - 針對 `nvidia/nemotron-3.5-lightning-30b-a3b`（未指定 `reasoning_effort` 時）自動帶入 `"chat_template_kwargs": {"enable_thinking": False}`。

3. **Groq 速率限制 Header 短重置時間誤覆蓋每日配額有效期**
   - **位置**：[`src/quota_broker/routing.py`](../src/quota_broker/routing.py) (`groq_quota_observations`)
   - **原因**：Groq 的 `x-ratelimit-reset-requests` 代表「距離恢復 1 次請求的時間」（例如 `1m26s`），當 `remaining > 0`（如 999/1000）時，原本直接把 `valid_until` 設為 86 秒後，導致 86 秒後觀測過期而阻擋後續請求。
   - **修正**：當 `window == "day"` 且 `remaining > 0` 時，將觀測有效期維持至少 24 小時（或至下一個重置窗口）。

4. **過期分鐘級觀測與 Cloudflare Neurons 成本估算連動阻擋**
   - **位置**：[`src/quota_broker/gateway.py`](../src/quota_broker/gateway.py) (`_target_resources`、`_capacity_reasons`、`_permit_dispatch`、`run`)
   - **修正**：
     - `_target_resources`：將 `target.neurons(...)` 納入 `costs["neurons"]`，解決 `cloudflare-personal-llama` 在多供應商並存時因缺少 `neurons` 成本鍵而被判 `estimation_unconfigured` 的問題。
     - `_capacity_reasons` / `_permit_dispatch`：僅合併 `current == True` 的非必備動態觀測，避免過期的 `rolling_minute` 觀測永久卡住 target。
     - `run`：只要包含 `resource_bounds` 即傳遞給 `self.broker.reserve(request)`，使純文字請求與結構化請求皆能正確通過容量預留。

5. **已確認（Acknowledged）歷史 `unknown` 槽位解除阻擋**
   - **位置**：[`src/quota_broker/gateway.py`](../src/quota_broker/gateway.py)、[`src/quota_broker/core.py`](../src/quota_broker/core.py)
   - **原因**：先前已透過 `unknown_slot_acknowledgements` 標記確認的舊 `unknown` 紀錄，仍被 `legacy_active` 與 `changed_active` SQL 查詢算入，導致 `openai-personal-luna` 持續被 `active_configuration_changed` 阻擋。
   - **修正**：在 `gateway.py` 與 `core.py` 的 `legacy_active` / `changed_active` 查詢中排除已存在於 `unknown_slot_acknowledgements` 的 `reservation_id`。

6. **Dashboard 任務配對建議對本地時區配額（Google Pacific Midnight）的相容性**
   - **位置**：[`src/quota_broker/dashboard.py`](../src/quota_broker/dashboard.py)
   - **修正**：僅在 `target["required_capacity"]` 為空時檢查本地窗口對齊警告，確保具有明確容量或本地安全上限的路由正常參與配對。

---

## 4. 統一呼叫介面與使用說明（How to Use）

本專案已在 [`src/quota_broker/client.py`](../src/quota_broker/client.py) 提供 **`call_free_api`** 單一函式與 **`ProjectClient`** 類別，任何服務皆可直接引入使用。

### 方式 A：一行函式呼叫（透過共用 Broker HTTP 介面）

無論在本機或透過 SSH tunnel 連接至 `http://127.0.0.1:18588`：

```python
from quota_broker.client import call_free_api

# 1. 自動選路（由 Broker 自動挑選當前可用且免費額度最充裕的供應商）
res = call_free_api(
    "請用一句話解釋什麼是 API Quota Broker",
    token="<PROJECT_ACCESS_TOKEN>",
    max_output_tokens=128,
)
print(res["provider"], res["model"], res["result"]["text"])

# 2. 指定特定供應商與模型（例如指定 Groq 或 NVIDIA）
res_groq = call_free_api(
    "Reply with PONG only.",
    token="<PROJECT_ACCESS_TOKEN>",
    provider="groq",
    model="openai/gpt-oss-20b",
    max_output_tokens=64,
)
print(res_groq["result"]["text"])

# 3. 呼叫 OCR（OCR.space Engine 2）
res_ocr = call_free_api(
    "<BASE64_ENCODED_PNG>",
    token="<PROJECT_ACCESS_TOKEN>",
    capability="ocr",
    provider="ocrspace",
    model="ocr.space/engine2",
)
print(res_ocr["answer"])
```

### 方式 B：Python 內部直接透過 `Gateway` 呼叫（不經 HTTP Server）

若在具備 Doppler 存取權的環境內直接使用 `Gateway`：

```python
import subprocess, uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from quota_broker.config import SecretInventory, load_gateway_config
from quota_broker.gateway import Gateway
from quota_broker.registry import Registry

def get_secret(ref: str) -> str:
    return subprocess.check_output(
        ["doppler", "secrets", "get", ref, "--project", "api-quota-broker", "--config", "dev", "--plain"],
        text=True,
    ).strip()

cfg_dir = Path(".state/shared-broker-20261111/runtime-config-v7")
reg = Registry.load(cfg_dir / "registry.json")
targets = load_gateway_config(cfg_dir / "gateway.json", registry=reg)
now = datetime.now(UTC)

gw = Gateway(
    db=Path(".state/local-broker.sqlite3"),
    targets=targets,
    digest_key=b"0123456789abcdef0123456789abcdef",
    secret_resolver=get_secret,
    secret_inventory=SecretInventory(
        names=frozenset({
            "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
            "MISTRAL_API_KEY", "NVIDIA_API_KEY", "CLOUDFLARE_API_TOKEN",
            "CLOUDFLARE_ACCOUNT_ID", "OCRSPACE_API_KEY", "HUGGINGFACE_API_TOKEN",
        }),
        verified_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=7),
        project="api-quota-broker",
        config="dev",
    ),
    registry=reg,
)

result = gw.run({
    "request_key": f"job-{uuid.uuid4().hex[:12]}",
    "capability": "text_generation",
    "provider": "nvidia",  # 可省略，省略時自動依免費配額選路
    "model": "nvidia/nemotron-3.5-lightning-30b-a3b",
    "input": "Reply PONG only.",
    "max_output_tokens": 64,
})
print(result["state"], result.get("answer"))
```

---

## 5. 測試、靜態檢查與部署交接事項

1. **自動化測試與型別檢查狀態**：
   - `ruff check src/quota_broker`：**All checks passed!**
   - `mypy src/quota_broker`：**Success: no issues found in 36 source files**
   - 核心與新增單元測試（`test_family_transport.py`、`test_family_adapters.py`、`test_gateway.py`、`test_dashboard*.py`、`test_unknown_slot_acknowledgement.py`、`test_shared_*.py`、`test_project_client.py`）：**466 passed**。
2. **8 家供應商完整配置位置**：
   - 位於本機 `.state/shared-broker-20261111/runtime-config-v7/`（含 `gateway.json`、`registry.json`、`runtime.json`），所有 8 家可用供應商皆已設定 `enabled: true` 並對齊安全上限。
3. **HP 共用主機（`hp-server`）維護注意事項**：
   - 切勿在 `hp-server` 的 `/opt/quota-shared-broker/current/app` 下直接以未加 `-B` (`PYTHONDONTWRITEBYTECODE=1`) 的 Python 匯入模組，否則產生的 `__pycache__` 會被 `deploy/hp_shared_maintenance.py` 的 `ALLOWED` 檔案白名單視為 `installed_extra_file`。
   - 部署更新至 `hp-server` 時，請統一使用 `scripts/hp_shared_client.py` 維護入口。
