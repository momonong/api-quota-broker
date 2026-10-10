# 七家供應商能力擴充：契約、證據與限制

起點：`f3c63fe`／`feat/seven-provider-v1`。人類來源為 main 對話 `01a0de5f-8306-71a3-9738-7ac6eb4d7746` 的 message `01a0fd4a-70a6-70b0-b0e0-0f8dfba97377`，原 task 已核對。沿 main → orchestrate `01a0d4cf-c625-7610-a50e-b9ff278ce901` → 原 task `01a0d64c-471b-7e70-b879-0a2ccfe8c890` 執行。

本階段交付本機能力發現、契約、協定、帳本與加密佇列。真帳戶可用性、免費資格與實測成功另列證據；原 A–F 的 483 項測試不是這次擴充的驗收結果。

## 已確認範圍

一般 AI：多輪對話／推理／程式、vision、OCR／PDF、ASR／audio translation／TTS、image generation、embedding／rerank、classification／moderation。NVIDIA 科學／3D／氣象列 inventory gap，其實作範圍等待 main 的人類決策。訓練、帳務／workspace 管理、遠端工具執行不在此推論階段。

main 已確認先交付同步 buffered 結果與加密佇列。Streaming／Realtime 明確拒絕；不取供應商回傳的遠端 URL，不新增 artifact 服務。本文件記錄能力開發階段；當時未授權的操作不構成後續操作指示。Git 收尾與 ASUS 部署狀態見[目前維運指南](asus-broker-ops.md)。

## 已實作路徑

1. `DiscoveryStore` 保存經驗證的 provider/model/capability 三元證據。CLI／API 可查 coverage、匯入 snapshot／attestation、產生 disabled candidates。完整刷新只適用同一官方來源範圍；partial 不移除未列項，消失不代表官方 retired，舊證據不覆蓋新證據。
2. `FamilyRegistry` 註冊輸入／選項／結果／對應關係與資源估算。舊 `input: string` 的正規化與 HMAC 保持；新 `input: object`／`options` 可帶 messages、inline 媒體、tools、JSON schema，以及各 family 的選項。
3. 27 個 packaged protocol profiles 經固定 endpoint → Gateway → reserve／dispatch → Result／Usage。`family_adapters` 讓同一 provider/model 分別使用 chat、FIM 等固定協定；不用更改 account 身份或猜模型名稱。
4. 非秘密 metadata 留在 SQLite；typed 內容只回傳第一次同步結果，或經加密 queue 的認證 result 取得。status、usage、diagnostics 沒有輸入／輸出內容。相同 request key 不重送；unknown 保留預留，不自動重播或釋放。
5. CLI 支援 `--task-stdin`、使用者明確指定的 `--input-file`／`--mime-type`，以及單一 image/audio 的 `--output-file`。輸出檔 exclusive 0600，不覆寫；HTTP 不讀 caller 提供的本機路徑。

### 協定覆蓋

| 供應商 | 本機已受測協定 |
| --- | --- |
| NVIDIA | Chat／vision、分開的 NV-Embed 與 GTE embeddings、rerank |
| Groq | Chat／vision、ASR、英文 audio translation、TTS |
| Mistral | Chat／vision、FIM、embeddings、OCR、ASR、TTS、classification／moderation |
| Gemini | generateContent 的 text／vision／ASR／image／TTS，embedContent |
| Cloudflare | 明確 schema 的 text、embeddings、translation、ASR／audio translation、TTS、image、classification profiles |
| OpenRouter | Chat／vision、embeddings、image endpoint |
| OCR.space | 三種 engine 的 form schema、語言／旋轉／scale／table／overlay 選項與完整幾何結果 |

這是協定 fixture 覆蓋，沒有宣稱每個模型／帳戶都可使用。Cloudflare task label 不能證明所有同族模型有相同 wire schema；其 discovery protocol 保留未知，需補模型 schema 證據。Mistral 模型名稱含 embed 也不能代替能力欄位。

## 輸入、輸出與管理者上界

main 已確認以下是可配置的開發預設，不是 ASUS 硬體、部署或併發驗收：

| 上界 | 預設 |
| --- | --- |
| 解碼後輸入媒體總量 | 8 MiB |
| HTTP JSON／送往供應商的 request body | 16 MiB |
| 序列化 result／HTTP response | 32 MiB |
| 內容 parts | 32 |

實際取本機／供應商較小的限制。Base64 長度與總量先檢查再解碼；MIME 與 magic 必須相符；JSON 深度、浮點有限值、batch 個數、vector 維度、rerank index、圖片個數與 PDF 頁數均驗證。PDF 在有時間／記憶體限制的隔離子程序中計頁，拒絕加密或無效文件。

只有 text_generation／vision／code_completion 使用輸出 token bound：typed 安全上界 65536，再受 model／target 上界限制；legacy 文字同樣接受 1..65536；正常預設1024，仍受model／target上限限制。OCR、語音、圖片、embedding 等無可設定 token 輸出的 family 使用 0 表示不適用，不填假 token=1。非 token model 的未知／不適用 token limits 可明確使用 0；mixed／token model 必須維持正值。

因此超出安全 bound 的文字輸出、大型媒體、Streaming／Realtime 等仍是功能邊界，不能稱模型能力 100% 利用。已存在 builtin model 的擴充必須在 manifest 明確指定 `replace_builtin: true`；固定協定與 host 驗證仍適用。active reservation 的 snapshot／mapping 變更會阻擋派送。

## 配額與用量

metrics：requests、input_tokens、output_tokens、total_tokens、audio_seconds、images、pages、conversions、neurons；windows：rolling_minute、rolling_hour、day、month。相同 account/shared bucket 的競爭在 SQLite `BEGIN IMMEDIATE` 內處理，預留與派送均核對觀測、估算 TTL 與 quota。

- WAV 秒數和 PDF 頁數只作本機保守預留估算，不當成 provider actual。
- 壓縮音訊秒數、Neurons 或其他缺少可信 bound 的 metrics，需有適用範圍／期限的管理者估算；未配置則 fail closed。
- 實際用量只取 provider 明確回報欄位；非整數 audio duration 在目前整數帳本中仍未知，不以 ceil 假造 actual。
- 音訊 total_tokens 可能含未列出的音訊 token，不能套文字 prompt+completion 的關係；關係由 protocol parser 決定。
- 有結果但缺必要 actual metric 時是 `completed_usage_unknown`：保留結果與 hold，可標記執行已結束，但不把估算冒充已結算用量。
- OpenRouter free-model daily request counter 與 credits 分開，不由 credits 或 `:free` 名稱推導免費資格。

## 官方證據與 discovery 狀態

每個模型／方法／feature 分別記錄上市、hosted/selfhost、free/paid/restricted/unknown、account allowed/blocked/unknown、adapter support 與歷史 live 結果。`source_catalog_completeness` 只表示該來源，`provider_capability_completeness` 仍未知；unique_models 與 model_capability_records 分開計算。

同一 account_scope 的當前 allowed 與歷史 passed 才能相交；不能混用 A 帳戶 allowed 與 B 帳戶 passed。`readiness_basis` 是證據交集，不等於 runtime admission。沒有完整免費／帳戶／能力分母時 coverage_percent=null，不以 7/7 連通稱 100%。

以下為 2026-10-03 的歷史公開 GET 快照；所列 data 檔案僅本機封存，不是 checkout 依賴：NVIDIA `/v1/models` 回傳 81 個驗證後 ID，snapshot 在 `data/catalog/nvidia-2026-10-03.json`；該清單缺能力／免費 discriminator，均保持未知，也不包含整個 NIM 科學或其他 host 的分母。經 orchestrate 明確授權的單次匿名診斷，OpenRouter 回 HTTP 200、646 筆模型；其中 19 筆字串 ID 被原本 slug 規則拒絕，使整批 parse_models/model_id 中止。安全計數見 `data/catalog/openrouter-diagnosis-2026-10-03.json`。已改為目錄／固定 JSON body 的 opaque ID 驗證，保留原值，拒絕控制字元、URL、過長值與已知秘密格式；Google／Cloudflare 放入 URL 的 ID 另用嚴格路徑驗證。之後依 main → orchestrate 的另一次公開補驗交辦，在臺灣時間 2026-10-03 01:52:27 做 1 次匿名 GET：HTTP 200、646 個唯一模型全部通過同版本正式 parser／validator，產生 661 筆 model/capability 紀錄。正規化目錄見 本機封存 `data/catalog/openrouter-2026-10-03.json`（不納入Git），安全計數／來源／UTC 時間見 本機封存 `data/catalog/openrouter-validation-2026-10-03.json`。來源 complete 僅表示此固定清單，不代表全部供應商能力或帳戶免費驗收；新發現的未支援能力仍只作 inventory。先前 raw 已丟棄，不能逐 ID 對照歷史 19 筆，但目前整份公開清單已通過；未保留本次 raw、未 enable、未讀秘密或執行推論。未來錯誤附固定 phase／細分 reason。

原始 body、description、account ID、認證值與自由文字錯誤不保存。候選 manifest／target skeleton 全部 disabled，沒有 secret/account/quota binding，不自動啟用、不複製帳戶 scope、不降到付費模型。

## 受限 metadata 批次

`scripts/provider_metadata_plan.py` 只列計畫或正規化 fixture；`scripts/provider_metadata_fetch.py` 預設 dry run，認證執行需明確指定 `--execute` 並取得精確批准。計畫：一個 api-quota-broker/dev、5 分鐘到期、整個 config 唯讀 token；每個必要 key／Cloudflare account ID 只取一次、留記憶體；NVIDIA／OpenRouter model list 不帶認證；不讀 NVIDIA key 或 OCR key。

最多 15 個 GET，Google／Cloudflare 各最多 5 頁；逐頁總 deadline 20 秒／5 MiB，批次 hard deadline 240 秒。`theoretical_page_ceiling_seconds=300` 是頁數乘以單頁上限，與 hard batch deadline 分開；開始前 token 必須剩至少 270 秒，保留 30 秒。沒有 retry、redirect、token renewal、自訂 UA/IP/proxy、推論或自動 account attestation。

這是歷史批次規格，不是待自動執行的工作。新的認證 metadata 執行仍需當次精確授權；過去診斷或 Git 收尾授權不涵蓋新批次。

## 續補成果、剩餘缺口與下一個必要動作

- Google signed tool continuation 已保留原 parts 順序、thoughtSignature 與 provider call ID，經 bounded typed Result／加密 queue 續接。必須明確指定相同 Google provider／model；拒絕跨模型、跨供應商或靜默刪除狀態。fixture 已驗證，真實續接尚未執行。
- OpenRouter 音訊輸出需要 Streaming，超出目前 buffered execution 契約。
- OCR overlay 已保存 lines／words／座標／尺寸與 has_overlay，並受數量、有限數值及 result bytes 限制；fixture 已驗證。
- Mistral classification 依官方 OpenAPI 的 target → scores map 實作，結果保留 target、label、有限 score；不將分數當成機率。官方 API 頁面範例與 OpenAPI 不一致，採 OpenAPI 型別並拒絕不符格式；fixture 已驗證。
- Gemini embedContent 一次只處理一筆；多筆不隱藏 fanout 成多次 API 呼叫。
- Cloudflare 各模型 schema、NVIDIA 專用模型的分母、七家各模型的真帳戶免費資格／quota 仍需官方與認證 metadata 證據。

本機 gate 已完成；剩餘 metadata／真實能力驗證是後續可選工作，須另行授權。fixture 通過不等於真實免費帳戶驗收。

## 管理者操作：CLI／API

以下命令從repo根目錄執行，沿用 `uv run --locked quota-broker`。既有服務／queue key 的準備見 [v1-local-guide 的啟動與隱私設定](v1-local-guide.md#啟動與隱私設定)；本節沒有啟動或部署服務。HTTP 範例的 `/protected/client-token`、`/protected/admin-token` 是已準備好的本機受保護檔案位置，token 值不放 argv／JSON。

### Coverage、refresh、attest 與 disabled candidates

| 操作 | HTTP 路徑 | CLI action／認證 |
| --- | --- | --- |
| 查證據 | GET `/v1/coverage` | `coverage`／client token |
| 匯入已驗證 snapshot | POST `/v1/admin/discovery/refresh` | `discovery-refresh`／admin token；JSON stdin |
| 匯入帳戶證據 | POST `/v1/admin/discovery/attest` | `discovery-attest`／admin token；JSON stdin |
| 產生停用候選 | GET `/v1/coverage/candidates` | `discovery-candidates`／client token |

已有 Gateway 時：

下列 discovery-refresh 範例須先備妥符合 schema 的非秘密 snapshot；checkout 不附帳戶或歷史目錄資料。

```sh
uv run --locked quota-broker gateway --url http://127.0.0.1:18084 --token-file /protected/client-token --json coverage --provider nvidia --limit 100
uv run --locked quota-broker gateway --url http://127.0.0.1:18084 --token-file /protected/admin-token --json discovery-refresh < /protected/snapshot.json
uv run --locked quota-broker gateway --url http://127.0.0.1:18084 --token-file /protected/admin-token --json discovery-attest < /protected/attestation.json
uv run --locked quota-broker gateway --url http://127.0.0.1:18084 --token-file /protected/client-token --json discovery-candidates --provider nvidia
```

`coverage` 可加 `--model`、`--capability`、`--limit 1..1000`；下一頁用回傳的 `next_before` 作 `--before`。API query 同名：`provider/model/capability/limit/before`。candidates 只接受 provider 篩選；CLI 自動取完整分頁。refresh 是本機證據匯入，不會 GET 供應商；attest 保存管理者提供的證據，不會讀 key 或自行驗證帳戶可用性。

不需要服務或認證的本機匯入方式如下。使用新的暫存資料庫，避免混入正式或歷史 ledger；`gateway.example.json` 的 targets 保持停用。可選 `--registry-file /protected/registry.json`，必須與對應模型的受測契約一致。本機 discovery 同時需要 `--config` 與 `--db`，不得帶 `--token-file`／`--token-stdin`。

```sh
BROKER_DOCS_DIR="$(mktemp -d /tmp/quota-broker-docs.XXXXXX)"
uv run --locked quota-broker gateway --config gateway.example.json --db "$BROKER_DOCS_DIR/discovery.sqlite" --json discovery-refresh < "$BROKER_DOCS_DIR/validated-discovery-snapshot.json"
uv run --locked quota-broker gateway --config gateway.example.json --db "$BROKER_DOCS_DIR/discovery.sqlite" --json coverage --provider nvidia --limit 100
uv run --locked quota-broker gateway --config gateway.example.json --db "$BROKER_DOCS_DIR/discovery.sqlite" --json discovery-candidates --provider nvidia > "$BROKER_DOCS_DIR/candidates.json"
```

snapshot 的最小 fixture schema 如下；存成 `$BROKER_DOCS_DIR/snapshot.json`，用同一 `discovery-refresh` 匯入。此為人工 fixture，source 僅符合官方 host policy；不代表網站或帳戶確認這個模型。

```json
{
  "schema_version": 1,
  "provider": "nvidia",
  "source": "https://docs.api.nvidia.com/models",
  "checked_at": "2026-10-02T12:00:00+00:00",
  "complete": false,
  "models": [{
    "model": "fixture-model", "capability": "text_generation",
    "hosting": "unknown", "endpoint": null, "protocol": null,
    "free_eligibility": "unknown", "free_source": null, "status": "listed",
    "context_tokens": null, "max_output_tokens": null, "features": []
  }]
}
```

attestation 最小 fixture 如下；先匯入上面的 model，再存成 `$BROKER_DOCS_DIR/attestation.json`。日期是已過期的固定 fixture，不可用來啟用真帳戶；真證據須填實際觀測時間、期限、非秘密 scope 與 receipt reference。`allowed`、`passed` 必須各有相應證據，同一 scope 才能相交。

```json
{
  "provider": "nvidia", "model": "fixture-model", "capability": "text_generation",
  "account_scope": "opaque-fixture-account", "account_availability": "unknown",
  "account_checked_at": "2026-10-02T12:00:00+00:00",
  "account_valid_until": "2026-10-02T12:05:00+00:00",
  "live_result": "unverified", "live_checked_at": null, "receipt_id": null
}
```

```sh
uv run --locked quota-broker gateway --config gateway.example.json --db "$BROKER_DOCS_DIR/discovery.sqlite" --json discovery-refresh < "$BROKER_DOCS_DIR/snapshot.json"
uv run --locked quota-broker gateway --config gateway.example.json --db "$BROKER_DOCS_DIR/discovery.sqlite" --json discovery-attest < "$BROKER_DOCS_DIR/attestation.json"
```

candidate 回應包含 `registry_manifest`、`target_candidates` 與 blocking reasons；候選保持 disabled、沒有 secret/account/quota binding，整個回應不是可直接啟動的 Gateway config。snapshot 上界 5 MiB、attestation 64 KiB；禁止加入 token、原始錯誤或其他未定義欄位。

### Typed media、structured result 與 queue result

下面的 JSON／命令描述既有 Gateway 介面。`explain` 只評估路由；`run`、`submit` 與 `worker` 在啟用的真 target 上可能派送供應商請求，須依當次操作授權使用。要完全離線重現，使用本節末列的 fixture tests；它們建立暫時 loopback Gateway、注入假 transport，無 Doppler／外部 API。

最小 embedding JSON（存為 `/protected/embedding-task.json`）：

```json
{"input":{"texts":["fixture first","fixture second"]},"max_output_tokens":0}
```

即使使用 `--task-stdin`，CLI 仍要求 `--request-key` 與 `--capability`。可加 `--provider`／`--model` 指定已配置模型；JSON 的同名欄位必須與 CLI 相符。

```sh
uv run --locked quota-broker gateway --token-file /protected/client-token --json explain --request-key docs-vectors-sync --capability embedding --task-stdin < /protected/embedding-task.json
uv run --locked quota-broker gateway --token-file /protected/client-token --json run --request-key docs-vectors-sync --capability embedding --task-stdin < /protected/embedding-task.json
```

同步回應的內容片段（其他 metadata 省略）為：

```json
{"state":"completed","result":{"vectors":[[0.1,0.2],[0.3,0.4]]}}
```

ASR 檔案輸入：`--input-file` 啟用 typed JSON；stdin 可供 options，或空輸入。CLI 把正規檔案編碼成 inline part，只傳 bytes／MIME，不把路徑送往供應商。OCR 用 `--capability ocr --mime-type application/pdf` 或支援的 image MIME；PDF 是否可用另受模型契約限制。

```sh
printf '%s' '{"options":{"language":"en"}}' | uv run --locked quota-broker gateway --token-file /protected/client-token --json run --request-key docs-asr-sync --capability audio_transcription --input-file /protected/sample.wav --mime-type audio/wav
```

HTTP 傳的是同一 typed body，媒體 shape 為 `{"type":"audio","mime_type":"audio/wav","data":"<base64>"}`，放在 `input.audio`；OCR 放在 `input.document`。`<base64>` 是位置示意，實際必須是符合 MIME／magic 的編碼；HTTP 不接受本機路徑或媒體 URL。ASR 結果為 `result.text`；OCR 為 `result.pages`，可帶完整 overlay。

Queue 用不同的 opaque key 提交相同 embedding fixture：

```sh
uv run --locked quota-broker gateway --token-file /protected/client-token --json submit --request-key docs-vectors-queue --capability embedding --task-stdin < /protected/embedding-task.json
uv run --locked quota-broker gateway --token-file /protected/client-token --json worker --once
uv run --locked quota-broker gateway --token-file /protected/client-token --json queue-status docs-vectors-queue
uv run --locked quota-broker gateway --token-file /protected/client-token --json result docs-vectors-queue
```

對應 HTTP：POST `/v1/queue`（完整 Task JSON）、POST `/v1/queue/tick`（`{}`）、GET `/v1/queue/{key}`、GET `/v1/queue/{key}/result`。CLI 將 required key／capability 合併進 JSON。queue 必須已啟用；內建 worker 已啟用時通常不需手動 tick。submit／status／worker 回 metadata，只有認證 `result` 解密完整 Gateway outcome，包含上述 structured `result`；pending 回 `queue_result_pending`。相同 key 不重新派送；同步內容只在第一次 run 回傳。

TTS 最小 JSON（存為 `/protected/tts-task.json`，voice／format 必須符合選定模型）：

```json
{"input":{"text":"fixture speech"},"options":{"voice":"troy","format":"wav"}}
```

```sh
uv run --locked quota-broker gateway --token-file /protected/client-token --json run --request-key docs-tts-sync --capability tts --task-stdin --output-file /protected/new-voice.wav < /protected/tts-task.json
uv run --locked quota-broker gateway --token-file /protected/client-token --json result docs-tts-queue --output-file /protected/new-queued-voice.wav
```

第二行要求該 TTS job 已提交並完成。`--output-file` 只接受單一 image/audio 結果、exclusive 0600，不覆寫；標準輸出移除媒體 data 並標記 `output_written=true`，保留其他結果欄位。省略此旗標時 `result.audio.data`／`result.images[i].data` 是 base64。

已存在的離線 fixture 範例與驗證命令（名稱中的 real 指實際 Gateway→HTTP→CLI 路徑，transport 仍是假資料）：

```sh
.venv/bin/pytest -q tests/test_discovery_api.py::test_client_coverage_admin_refresh_attest_and_auth_are_separate
.venv/bin/pytest -q tests/test_media_cli.py::test_real_embedding_http_cli_and_encrypted_queue_lifecycle
.venv/bin/pytest -q tests/test_media_cli.py::test_real_audio_file_http_cli_ingestion_uses_inline_part_no_local_path
.venv/bin/pytest -q tests/test_media_cli.py::test_real_tts_http_cli_export_and_encrypted_queue_result
```

fixture 原始碼見 [discovery API／CLI](../tests/test_discovery_api.py) 與 [typed media／result／queue](../tests/test_media_cli.py)。這些既有測試已包含在本輪 1286-pass gate。

## 續補契約的官方來源

- Google [thought signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures) 與 [function calling](https://ai.google.dev/gemini-api/docs/generate-content/function-calling)。
- Mistral [classification endpoint](https://docs.mistral.ai/api/endpoint/classifiers) 與 [OpenAPI](https://docs.mistral.ai/openapi.yaml)。
- OpenRouter [models schema](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties)：ID 是字串；本機固定 endpoint 的 body identity 與 URL path 分開驗證。

## 驗證紀錄

- 最終全套 `.venv/bin/pytest -q --tb=short`：**1286 passed，124.32 秒**；包括 CLI／HTTP、Gateway、資源帳本、固定傳輸與加密 queue fixtures。
- Ruff check／format：通過（79 files）；mypy：25 source files 通過；`git diff --check` 通過。
- `uv lock --check --offline` 通過；只新增 PDF 計頁依賴 pypdf，既有依賴版本保留。
- 11 份歷史 live SQLite 檔案 SHA-256 與階段起點一致，未改寫歷史結果。
- 新功能驗證使用本機 fixtures；本階段沒有建立 Doppler token、讀真 key、帶認證 GET 或推論。公開診斷與官方文件讀取另如上記錄。
- 變更留在既有工作分支；未提交、推送、合併、部署或變更服務。本機開發 gate 完成，七家真帳戶／免費資格與剩餘能力缺口仍未驗收。
- 文件續補：18 條 CLI 範例以實際 argparse parser 核對，5 個 JSON 範例以正式 family／discovery schema 驗證；4 個 fixture function 與 8 個 HTTP 路徑核對存在。只解析／驗證，未執行 HTTP 或 key 操作；純文件修改沿用以上全套回歸證據。

- OpenRouter 公開補驗：正式 parser／validator 接受 646 unique models／661 model-capability records，HTTP 200；1 GET、20 秒／5 MiB 上界、無 retry／redirect。只新增 normalized snapshot、receipt 與本文件；程式碼／測試檔未變，沿用 1286-pass 同版本證據。
