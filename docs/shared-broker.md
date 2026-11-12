# 共用 Quota Broker：專案接入與額度判讀

本文件描述 2026-11-11 的 HP 共用實作與接入契約。Broker、Dashboard 串接、Doppler 專用維護入口與私有 SSH 轉送已安裝；私有接入與拒絕邊界已實測。**新增模型端到端呼叫仍待本輪授權，不能把獨立提供者測試當作共用路徑驗收。** 版本及原操作收據見 [交付紀錄](shared-broker-delivery-2026-11-11.md)。

## 三個工作台與共用執行層

`momonong.me/quota` 提供三個工作台：供應商／模型與配額、可篩選用量、任務配對建議。網站登入只授權查看與試算，不授權執行模型。瀏覽器取得的資料會移除提供者 key、內部憑證名稱與帳戶識別等私有欄位。

Broker 負責接收專案任務、確認任務與模型相容、檢查免費資格及成本界限、預留共用額度、排程、取回結果及記錄用量。同一帳戶的額度由專案共用，不能替每個專案複製一份餘額。任務配對先過濾能力與輸入／輸出需求，再依有效免費容量、到期／重置時間與既有確定性排序規則選路；缺證據時停止該路由，不自動切至付費方案。

型錄盤點與執行路由分開：看到某家提供者或某模型，代表已盤點到能力，**不代表該帳戶有免費資格、可用 key、剩餘額度或已配置可執行路由**。

## 網路與權限

| 入口 | 用途 | 憑證邊界 |
| --- | --- | --- |
| 公網 `https://momonong.me/quota` | 三個工作台、唯讀資料、配對建議 | 既有網站認證；不能提交執行 |
| HP `127.0.0.1:18587` | 內部模型／診斷／用量／選路試算 | 獨立 read/plan 身分；普通專案 token 不能讀取全域診斷與用量 |
| HP `127.0.0.1:18588/quota/api/v1/jobs` | 私有非同步專案任務 | 專案 token；每個專案只能讀取自己的工作與結果 |

遠端專案經授權的 SSH 本機轉送連入 `18588`；提供者 API key 留在 Broker。客戶端接受的目的地為 `http://127.0.0.1:<本機轉送埠>`，不接受公網 URL、LAN IP 或任意路徑。轉送的身分與權限需由維護流程配置；既有僅限部署的 `quota-deploy` 身分不能逕自當作通用 shell 或轉送帳戶。

一個專案使用一個 token，存取政策把 token 綁定專案 ID 與 scopes。網站的 read/plan token、一般專案 token、維護管理權限互不替代。取得內網連線也不等於取得 API 權限。應用程式不要持有其他專案 token 或提供者 key。

以下 Doppler references 已建立並綁定正式 HP 存取政策；兩個專案身分均已通過私有入口的認證唯讀驗證：

| 專案身分 | Doppler project / config / secret |
| --- | --- |
| Owner 專案客戶端 | `api-quota-broker / dev / QUOTA_SHARED_OWNER_TOKEN` |
| Knowledge Hub 專案客戶端 | `api-quota-broker / dev / QUOTA_SHARED_KNOWLEDGE_HUB_TOKEN` |

透過專案既有的秘密注入機制，只向該應用注入其所屬 token。不要將 token 貼入聊天、原始碼、命令列參數或日誌，也不要為取得單一 token 將整份含提供者 key 的設定注入其他專案。

## 專案客戶端

`quota_broker.client.ProjectClient` 使用非同步 jobs API。先把 SSH 轉送與專案 token 注入配置好，再由應用程式執行；以下是程式介面範例，不是要求手動貼上部署指令。

```python
import os
from quota_broker.client import ClientError, ProjectClient

broker = ProjectClient(
    "http://127.0.0.1:18588",  # 呼叫端的已建立轉送目的地
    os.environ["QUOTA_SHARED_OWNER_TOKEN"],
)

# 從應用的業務工作 ID 衍生並持久保存；不要在每次網路重試產生新 ID。
request_key = "my-project-document-123-summary-v1"

try:
    accepted = broker.submit_text(
        request_key,
        "請以一句話摘要這段公開文字。",
        max_output_tokens=64,
    )
except ClientError as exc:
    # 如果 detail.error == transport_unknown，只查這個原始工作。
    # 不在這裡重送 POST、換 request_key 或換提供者。
    raise

status = broker.status(request_key)
# 應用依狀態稍後繼續查同一工作；不是重送生成請求。
if status["state"] == "completed":
    result = broker.result(request_key)
```

`submit_text` 會產生 `text_generation` 與 `input.messages` 的結構化契約。可選填 `provider`／`model` 限定路由；省略時交由 Broker 在通過檢查的候選中選擇。指定模型也不能跳過成本、餘額、資格與到期檢查。

| 方法 | 私有 HTTP 介面 |
| --- | --- |
| `submit(task)` / `submit_text(...)` | `POST /quota/api/v1/jobs` |
| `status(request_key)` | `GET /quota/api/v1/jobs/{request_key}` |
| `result(request_key)` | `GET /quota/api/v1/jobs/{request_key}/result` |
| `cancel(request_key)` | `POST /quota/api/v1/jobs/{request_key}/cancel` |

每次請求須附 `Authorization: Bearer <該專案 token>`。`request_key` 長度為 1–120 字元，只接受英數字與 `._~-`，與 Gateway 契約一致。同一業務工作的 key 與任務內容需持久保存；不可用新 key 規避未知狀態。同專案相同 key 不產生第二次模型呼叫，不同專案的同名 key 仍各自隔離。

`cancel` 用於尚未執行的排隊工作。已開始執行的工作不能藉由取消證明提供者停止，也不能因此釋放未知消耗。結果尚未就緒時應查狀態；`unknown`／`transport_unknown` 只允許核對原工作、原操作與收據。逾時不代表未送出，不能重送、換 key 或換廠商繼續同一請求。

## 本版執行範圍

正式配置只啟用兩條已具獨立測試證據的文字路由；共用入口的新模型呼叫仍須另外完成有界驗收：

| 提供者 | 模型 | 本版契約 |
| --- | --- | --- |
| OpenAI | `gpt-6-luna` | Responses、純文字、`store=false`、無串流／工具 |
| OpenRouter | `liquid/lfm-2.5-2.6b:free` | 純文字、提供者 fallback 停用、prompt／completion／request 價格上限皆為零 |

兩個專案 token 的正式存取政策為 **最多 4,096 input bytes、512 output tokens**。部署包已讀回；實機提交 513 output tokens 的請求回傳 403，隨後查不到該工作，沒有派送模型。input bytes 依 `ProjectAccess` 將 `input` 與 `options` 正規化序列化後的 UTF-8 bytes 計算；messages 等結構本身也占用界限。OpenAI 的保守成本估價另外限定同樣的 4,096 bytes／512 tokens 包絡；OpenRouter 沒有這個美元估價界限，不能將專案政策誤稱為全域 runtime 固定上限。這些皆不是供應商模型的完整能力上限。OpenAI Responses 輸出下限為 16 tokens。客戶端可要求更短的上限；新增正式模型驗收尚待使用者確認；提案界限為每條路由最多一次、64 output tokens，不因本文件自動授權。

其他供應商保留盤點資料與逐家阻擋原因；缺 key、免費資格、成本估算或可用容量時維持停用／不允許執行，不以型錄存在或 API models 查詢成功替代免費資格確認。

## 餘額、保留、用量與期限

介面與帳本須同時保留數值單位、來源、`as_of`、證據有效期限，以及重置／點數到期時間。三種金額不可混為一談：

- **帳戶最後觀測**：使用者回報 OpenAI 官方 Free trial 畫面為 **200.00 USD**，於 **2026-12-03** 到期，無需付款方式；其觀測時間記在來源檔。這不是重新查到的即時餘額，也不能推論每月自動刷新。
- **Broker 保守可用量**：建立器從上述觀測扣除先前獨立測試的完整保守預留 `1,000 credits_usd_micros`；後續再扣同配額池中已保留、已派送及結果／用量未知的界限。每個本版 OpenAI 任務保守預留 `3,000 credits_usd_micros`（0.003 USD），不是每次實際計費金額。`1 USD = 1,000,000 credits_usd_micros`，帳本以整數記錄。
- **提供者實際用量／費用**：輸入與輸出 tokens 依提供者實際回覆記錄；未回報實際美元費用時維持未知。不能把價目表估算或上述預留寫成實際帳單，也不能用「200 減估算費用」宣稱新的官方餘額。

點數到期、資格複核期限、餘額讀值有效期限、配額重置是不同的時間。帳戶只提供日期時，頁面維持「僅日期、時間與時區未知」；Broker 使用最早可能跨時區截止作保守停止時間，不將此時間冒稱官方精確到期時刻。讀取或重啟服務不會延長舊證據。

OpenRouter 的免費日計數可由已核對的唯讀 key API 更新；必須保留原始觀測時間與 UTC 日視窗，不能跨日沿用舊餘額。路由要求 15 分鐘內的有效計數，HP 每 5 分鐘更新一次；遇到錯誤或拒絕會持久記錄停止狀態，重新啟動也不盲目重試。key 的美元 limit／remaining 不等於免費模型每日次數。

OpenAI 一般推論 key 可查模型及執行已授權任務，不能因此假定可取得帳戶信用餘額。現有普通 key 查詢管理費用介面權限不足，且費用統計也不能直接替代逐筆贈送點數餘額與到期日。CLI／pipeline 會使用來源真正提供的能力；無法自動查到的帳戶證據維持原觀測與期限，過時即停止該路由。

## 歷史資料與完成標準

此共用執行層使用獨立 ledger，明列 `historical_gap=true` 與新紀錄起點。它不宣稱已恢復 ASUS 舊帳本，也不與 Receipts 的既有 `18993` 服務混用資料或 key。舊的 UNKNOWN 請求與收據仍留在其原操作紀錄，不能以新實例、新資料庫或新工作 key 重播。

本機測試通過、部署安裝成功、服務啟動、真實模型完成與使用者接受是分開的證據。共用層正式交付須確認：專案提交／查結果、跨專案拒絕存取、網站不能執行、未知結果不重播、帳本與 `/quota` 顯示一致，以及部署／有界驗收收據。本文不替代該紀錄。

## 日常維護與新專案接入

`scripts/hp_shared_client.py` 提供離線建包／語義檢查、status、publish、reconcile 與明確 rollback；使用 `api-quota-broker/dev/QUOTA_SHARED_MAINTENANCE_SSH_PRIVATE_KEY`。SSH 僅能進固定的 JSON 維護程式，不能執行 shell、修改憑證政策、網站密碼、Caddy 或其他服務。此身份不需要人手輸入 sudo。所有更新保留原 intent、版本與收據；未知結果先查原 operation，不能換 UUID 繞過。

`scripts/quota_project_tunnel.py` 使用同一 Doppler config 的 `QUOTA_SHARED_TUNNEL_SSH_PRIVATE_KEY` 建立前台轉送，只有 HP `127.0.0.1:18588` 可達。應用使用自己的專案 token；tunnel 憑證與 API token 是兩道獨立權限。新專案須由維護者登錄獨立 token／project ID，不能共用 Owner token 當作所有專案的身分。

Dashboard 沿用 `scripts/hp_quota_client.py` 與既有 `selfhost-deploy/prd_quota_hp` 的專用部署身分。Broker 與 Dashboard 各有固定封包、版本比較及收據，日常更新不使用通用 sudo 密碼。

路由排序是可重現的演算法：先排除任務／模態不合、資格或容量不足、估價不明、過期與不健康路由，再比較最近的已驗證額度重置／點數到期，其後以剩餘比例、健康、壓力、併發、延遲與固定 ID 排序。不是按供應商名稱硬排；新增提供者可沿用相同規則，但仍須配置可驗證證據與安全 transport。

### 十一家盤點邊界（2026-11-11）

| 提供者 | 已確認的內容 | 共用執行現況／尚缺資料 |
| --- | --- | --- |
| OpenAI | 擁有人轉貼 200 USD Free trial、12/03 到期；key 與模型清單可用；獨立文字測試成功 | 已配置；保守可用量與帳戶最後觀測分開。一般 key 無權讀管理費用端點，不能冒稱自動取得即時點數餘額 |
| OpenRouter | 官方免費日計數 50 次上限；本輪最後讀值 49 次剩餘；指定免費模型獨立測試 cost=0 | 已配置；正式 runtime 持續唯讀更新日計數，頁面以實際 as_of 為準 |
| Google / Gemini | key 與模型清單可讀，62 筆型錄 | 停用；缺此帳戶目前免費資格／可用容量證據 |
| Groq | key 與模型清單可讀，11 筆型錄 | 停用；缺目前方案及安全可用容量證據 |
| Mistral | key 與模型清單可讀，55 筆型錄 | 停用；需區分免費內含額度與付費餘額 |
| NVIDIA | 84 筆公開型錄；同日有一筆既有成功結果 | 停用；另一筆舊品質測試 UNKNOWN 保留，且缺帳戶容量證據 |
| Cloudflare | key 與模型清單可讀，73 筆型錄 | 停用；既有用量查詢權限不足，沒有用上限冒充餘額 |
| OCR.space | key 存在，3 種文件列出的引擎 | 停用；未發送 OCR，方案與目前容量未確認 |
| Hugging Face | 身分 API 成功，free 帳戶 | 停用；free 身分不證明月點數還有餘額 |
| Cerebras | 此 Doppler config 未列 key | 停用；缺憑證與免費資格 |
| SambaNova | 此 Doppler config 未列 key | 停用；缺憑證與免費資格 |

型錄筆數僅表示本次盤點範圍，並非帳戶已驗收的可執行模型數。其餘九家不是零餘額，也沒有被宣稱可自動執行。唯讀 refresh pipeline 可更新官方有提供的模型／帳戶欄位；缺權限或沒有官方餘額介面時保留已知事實及精確阻礙，不捏造數值。
