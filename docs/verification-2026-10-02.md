# 2026-10-02 全面驗證紀錄

## 範圍與環境

- Ubuntu、Python 3.12.3；工作目錄為本 repo，起點 `4636eb3f30edd92cdcd7220b6088e7f76238152a`，分支 `feat/private-key-admin`。
- 本輪只修改本機 gateway 的 Groq/Mistral 固定文字模型接入、停用的範例設定、fixture 測試與文件；沒有合併、推送、部署或啟動正式服務。
- 真實請求上限為每 provider 2、總數 14、LLM 輸出最多 64 tokens、timeout 最多 90 秒、並行 1。每筆須使用新識別與獨立收據／SQLite；派送後不明的結果不重送。**目前本輪真實 provider 呼叫數為 0。**

## 已驗證

- `ruff check .`、`ruff format --check .`、`mypy src/quota_broker` 均通過；`pytest -q` **86 passed**（含本機 HTTP socket、CLI、SQLite、路由、同鍵不重送、429、unknown、金鑰／內容不落庫與認證測試）。原沙箱無法綁測試 socket 時曾有 9 項 `PermissionError`；在可綁 loopback 的 Ubuntu 環境重跑即通過，不是程式回歸。
- `quota-broker demo` 使用本機 fixture 回傳 HTTP 200、`completed`，重建後仍為 `completed`；`provider_calls=1`，沒有外部 provider 請求。
- Doppler `api-quota-broker/dev` 僅用 `secrets --only-names --json` 核對名稱；2026-10-02 共有 11 個名稱。`NVIDIA_API_KEY`、`GROQ_API_KEY`、`GEMINI_API_KEY`、`MISTRAL_API_KEY`、`CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`、`OCRSPACE_API_KEY`、`OPENROUTER_API_KEY` 均存在。沒有讀取值；名稱存在不代表金鑰、免費資格或模型權限有效。
- 官方當次資料：[Gemini 2.5 Flash-Lite 定價](https://ai.google.dev/gemini-api/docs/pricing)與[帳務級別](https://ai.google.dev/gemini-api/docs/billing)；[Cloudflare Workers AI 免費額度](https://developers.cloudflare.com/workers-ai/platform/pricing/)與[模型頁](https://developers.cloudflare.com/workers-ai/models/llama-3.2-1b-instruct/)；[Groq 免費方案限額](https://console.groq.com/docs/rate-limits)與[Chat API](https://console.groq.com/docs/api-reference)；[Mistral Free mode](https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key)與[訂閱／按量計費](https://docs.mistral.ai/admin/billing-usage/subscriptions)；[OCR.space 免費 API](https://ocr.space/ocrapi)。公開文件不能證明本帳號目前免費或可用。

## Provider／路徑矩陣

下表的 fixture HTTP 與用量完全由測試傳輸層產生；耗時為「未單獨量測」，不代表真實網路延遲。fixture 的 SQLite 依各自隔離的暫存 DB 核對。

| Provider／model | 方法 | HTTP／狀態 | 耗時 | Provider usage | SQLite／防重送 | 真實測試限制與下一步 |
| --- | --- | --- | --- | --- | --- | --- |
| Google `gemini-2.5-flash-lite` | gateway fixture，HTTP+CLI | 模擬 200／completed | 未單獨量測 | 模擬 input 11、output 3 | settled，狀態查詢與同鍵重送不再呼叫 transport | 帳號所屬專案的 Free tier／billing 未核對；尚無 live 證據。 |
| Cloudflare `@cf/meta/llama-3.2-1b-instruct` | gateway fixture，HTTP+CLI | 模擬 200／completed | 未單獨量測 | 模擬 input 11、output 3、Neurons 7 | settled；缺可信 Neurons 時保留 unknown hold | account ID 名稱已補齊；仍須核對實際帳號 Workers Free 與安全的 Neuron 上界，才可 live。 |
| Groq `openai/gpt-oss-20b` | gateway fixture，HTTP+CLI | 模擬 200／completed；另模擬 429／unknown | 未單獨量測 | 模擬 input 11、output 3；429 unknown | settled；429 留 hold，重建／同鍵不重送 | 固定官方 URL、低推理、最多 64 輸出可供未來 live；組織方案與計費未核對。 |
| Mistral `mistral-small-latest` | gateway fixture，HTTP+CLI | 模擬 200／completed；另模擬 429／unknown | 未單獨量測 | 模擬 input 11、output 3；429 unknown | settled；429 留 hold，重建／同鍵不重送 | 先前使用者回報 console 出現升級提示；Free mode、API 權限及按量計費未核對，停止 live。 |
| NVIDIA `google/gemma-4-31b-it` | gateway fixture | 模擬 200／completed | 未單獨量測 | 模擬 input 11、output 3 | settled、秘密及內容不落庫 | 本輪未重新核對帳號與 endpoint，未 live。 |
| NVIDIA `nvidia/riva-translate-4b-instruct-v2` | 歷史 gateway live，2026-09-30 | 200／completed | 1628 ms | input 22、output 3 | 舊 SQLite 有 settled 記錄；**本輪未重跑** | 歷史 profile 的免費資格於 2026-09-30 到期，須重新核對。 |
| NVIDIA `nvidia/nemotron-3.5-lightning-30b-a3b` | 歷史 gateway live，2026-09-30 | HTTP 未知／unknown | 約 60334 ms | unknown | 舊 SQLite hold 保留；**不得重送舊識別** | 先唯讀核對供應商紀錄與重新確認資格。 |
| OCR.space | direct-only，未進 gateway | 本輪未送出 | — | 不適用 | 無新收據或 SQLite | 自動批准審查拒絕讀取 `OCRSPACE_API_KEY` 並將其送往 OCR.space；須由使用者在本 task 直接批准才可繼續。 |
| OpenRouter | 名稱 metadata only | 未送出 | — | 不適用 | 無 gateway 契約 | 本輪未選定固定免費模型與帳號條件，未擴充介面。 |

無指定 provider 的 `routes/explain` fixture 已納入 Groq、Mistral、Google 候選，依已驗證的本地 capacity／priority 選擇；未驗證免費資格或已啟用計費的候選被排除。這是路由演算法證據，非目前帳號的 live 可用性。Groq/Mistral 只接受固定 HTTPS chat endpoint，拒絕未列入 allowlist 的 URL；新目標在 `gateway.example.json` 預設停用。

## 保留與限制

- 被 Git 忽略的 `.state/gateway-live-profile.json` 是舊實測 profile，兩個 NVIDIA target 的免費資格有效期都已過。其舊 project 字串只在帳務與共享 bucket 識別欄位；未改寫，以免改變舊帳本意義。歷史 `.state/gateway-live-once.sqlite` 與兩份 smoke receipt 未改動。
- 本 task 嘗試向指定 orchestrate thread 傳送中途回報時，自動批准審查因缺少可信的直接跨對話傳送授權而拒絕；沒有改用其他工具繞過。完成結果由本 task 直接回報，後續跨對話同步須走正式授權路徑。
- 有關真實 provider 測試，目前等待本 task 的直接憑證讀取／傳送授權與帳號免費／計費狀態。沒有把 fixture 通過標示為真實推論驗收。
