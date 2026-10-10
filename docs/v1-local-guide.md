# v1 可擴充 API 資源池：本地操作指南

## 現況

同一個 Gateway、SQLite 與 CLI/API 處理七家既有代表能力，並支援可信 manifest 的同協定擴充、持久健康／quota observation與加密等待佇列。完整契約與離線驗證見 [v1-core-contract.md](v1-core-contract.md)；過去真API成功與未知問題仍見 [readiness](v1-seven-provider-readiness.md)。正常 1.0 已於2026-10-09部署至ASUS loopback；三筆固定live與服務重啟持久性驗證已通過，Cloudflare neurons仍未知並保留估計hold。最新部署／CLI入口與證據見[ASUS維運](asus-broker-ops.md)。無付費fallback。

## Registry 與帳戶設定

沿用 `uv sync --locked`、`gateway.example.json` 與既有account/shared bucket身份。所有example targets仍disabled、free_eligible=false；管理者核對當前Free資格、billing、model access、local caps及期限後才可啟用。本地caps不是官方remaining。

`gateway-serve --registry-file /protected/registry.json` 載入可信manifest，預設為既有builtin Registry。不修改全域MODELS。同名跨provider須在Task明確指定provider。第八家相容chat例子：

```json
{
  "schema_version": 1,
  "providers": [{"id":"eighth","adapter":"openai_chat","origin":"https://approved.example","endpoint":"https://approved.example/v1/chat/completions"}],
  "models": [{"provider":"eighth","model":"example-chat","capability":"text_generation","context_tokens":8192,"max_output_tokens":1024,"features":["text","json_output"],"input_parameters":["input"],"output_parameters":["max_output_tokens"]}]
}
```

另在target config設定provider/model、account/shared quota、secret_ref與Free證據。origin/endpoint是管理者固定設定；Task不能傳URL，redirect禁止。existing OpenAI providers只能保持原origin/endpoint；Google新generateContent及CF同schema可用`gemini_generate_content`／`cloudflare_workers_ai`與原origin、核對過的模型endpoint。固定模板只允許Google `{model}`、CF `{model}`及原`{account_id}`位置，model路徑安全驗證。新schema須在程式啟動時註冊受測Adapter implementation；manifest不能載入Python或任意proxy。

Adapter可提供admit及quota hooks；相容request schema不代表相同quota/error語意。缺hook保守unknown，generic429不會fallback。

### 需要 Neurons 的 target

可選以下**管理者已核對的固定最壞單次上界**；範例數字是fixture，不能拿去當真帳戶或官方價格：

```json
{"amount":500,"max_input_tokens":4096,"max_output_tokens":128,"source":"trusted_operator","verified_at":"2026-10-02T07:00:00+00:00","expires_at":"2026-10-02T08:00:00+00:00"}
```

填在target的`neuron_estimate`。只在有效期、input hold及output都符合時適用；來源標記`estimated_per_request_upper_bound`。一般Task不用知道provider-specific價格。缺設定或超出範圍回`estimation_unconfigured`；legacy `neuron_bound`可保留，在管理者估算適用時至少取其上界。不得把估算列為actual Neurons。

## 啟動與隱私設定

`gateway-serve`必要欄位仍為`--config --db --digest-key-file --client-token-file --doppler-token-file --doppler-project --doppler-config`，port預設18084、只bind loopback。HMAC key至少32bytes、client token至少32字元；重啟沿用HMAC key。runtime Doppler credential須另行妥善準備，五分鐘測試token不適合常駐。ASUS已由受控安裝準備；其他環境仍須自行準備，不複製實機憑證。

- `--secret-names-file`可載入已核對有期限的名稱清單，只接受project/config/names/verified_at/expires_at。current missing名稱在讀key前排除；unknown或過期不宣稱缺key；present不代表key有效。查詢不自動讀Doppler。
- 預設queue停用；`--queue-key-file /protected/queue.key`明確啟用。獨立持久Fernet key或32bytes raw key；不能與HMAC key共用。key/DB檔0600、兩者父目錄0700、owner必須是當前使用者、symlink/unsafe SQLite sidecar拒絕。preflight先檢查，現存unsafe DB不先遷移或chmod；新DB才建立0600空檔。
- 啟用queue會啟動一個背景worker；`--no-queue-worker`可改由CLI worker處理。`--queue-ttl-seconds`預設86400；terminal刪payload、到期刪result ciphertext，metadata tombstone保留防重播。
- wrong/lost key fail closed，不會重送。備份／復原應保護並保留獨立key。SQLite內容與結果用Fernet authenticated encryption；沒有自製cipher。同步run仍不持久化input/answer。
- `--admin-token-file`可選且必須與client token不同，只有此token可提交quota observations或reset health。未設定時管理端拒絕。ASUS仍只bind loopback，沒有對外網路入口。

## CLI／HTTP

所有API帶認證，回應no-store。Task input經stdin，token經受保護檔或查詢用token-stdin；不把secret或內容放argv。正常查詢不讀provider key或呼叫provider。

| 功能 | HTTP | CLI action |
| --- | --- | --- |
| 模型與當前最低admission | GET `/v1/catalog` | catalog |
| 目標health／quota／worker狀態 | GET `/v1/diagnostics` | diagnostics |
| 實際Task路由解釋 | POST `/v1/routes/explain` | explain |
| 同步執行 | POST `/v1/tasks` | run |
| metadata分頁／單筆／用量 | GET `/v1/tasks`、`/v1/tasks/{key}`、`/v1/usage` | recent、status、usage |
| 非同步提交／metadata列表 | POST／GET `/v1/queue` | submit |
| queue metadata／取內容／取消 | GET `/v1/queue/{key}`、GET `.../result`、POST `.../cancel` | queue-status、result、cancel、wait |
| 一次worker處理 | POST `/v1/queue/tick`，body `{}` | worker --once；無once則持續迴圈 |
| 管理者quota observation／health修復 | POST `/v1/admin/targets/{id}/quota`、`.../health/reset` | observe-quota、reset-health |

```sh
uv run --locked quota-broker gateway --token-file /protected/client-token diagnostics
uv run --locked quota-broker gateway --token-file /protected/client-token --json recent --state unknown
uv run --locked quota-broker gateway --token-file /protected/client-token --json usage --provider mistral
printf 'Hello.' | uv run --locked quota-broker gateway --token-file /protected/client-token --json explain --request-key dry-run-1 --capability text_generation --max-output-tokens 1024
```

以下動作會由worker／run派送真請求；帳戶與操作授權核對後才執行，這些是用法範例，不代表可重跑已使用的驗收key：

```sh
printf 'Hello.' | uv run --locked quota-broker gateway --token-file /protected/client-token --json submit --request-key opaque-job-1 --capability text_generation --max-output-tokens 1024 --priority 10 --wait-policy wait --max-attempts 4
uv run --locked quota-broker gateway --token-file /protected/client-token --json wait opaque-job-1 --timeout 60
uv run --locked quota-broker gateway --token-file /protected/client-token --json result opaque-job-1
uv run --locked quota-broker gateway --token-file /protected/client-token worker --once
```

`wait`只查狀態，不派送也不自動取answer；`result`明確解密內容。無queue時回queue_disabled。cancel僅能取消確證未送出的工作，不能宣称在途呼叫已取消。CLI polling interval預設1秒、上限60秒；client `--http-timeout`預設185秒、上限3600。同步execution總界限180秒，provider最長120秒、更短特例保留；task deadline可縮短。timeout永不自動replay，改查既有opaque key。

### Task

除以下legacy文字／小圖格式，也支援buffered typed input與discovery；完整family、媒體上界及尚未live驗證的範圍見[能力擴充契約](provider-capability-expansion.md)。

相同opaque request_key／payload不重播；不同payload回409。

- `capability=text_generation`：input非空、最多32768 UTF-8 bytes；max_output_tokens預設1024，顯式值1..65536，實際仍受model／target較小界限限制。
- `translation`：不同source_language/target_language；Riva adapter保留支援且含英文的pair／1952字限制。新翻譯adapter採自己的admission；通用Task仍有32768bytes上限。
- `ocr`：單圖PNG/JPEG base64，解碼最多36000bytes，max_output_tokens省略或1；不解析多頁PDF或檔案路徑。
- 可選provider/model、requirements.features（CLI重複`--require-feature`）、priority(-1000..1000)、帶時區deadline、wait_policy(wait/reject)、max_attempts(1..32)。同步run預設不等待；queue預設wait。max_attempts是整job所有Gateway attempts總量，送出前失敗通常也耗用額度。唯一可接續原slot的是租約已失效、同payload HMAC且完全沒有reservation／attempt／dispatch證據的preparing execution；沿原execution接續，不退款或建立新attempt。

`diagnostics`是最低input/output admission，不是完整輸入、key validity、持續配額或品質承諾。catalog的configured_available與當前available/admission_state分開。worker故障顯示enabled/running/stopped、固定error_code、last_tick_at，沒有原始例外內容；HTTP仍可查詢。修復後正常重啟worker，不以靜默fallback掩蓋故障。

## QuotaObservation與健康管理

以獨立admin token傳入`observe-quota`，JSON從stdin讀入；reset-health不改charges或unknown ledger。

```json
{"metric":"requests","window":"day","remaining":20,"limit":100,"as_of":"2026-10-02T07:00:00+00:00","valid_until":"2026-10-02T07:05:00+00:00","reset_at":"2026-10-02T07:05:00+00:00","provenance":"observed","source":"trusted_operator","confidence":"administrator_verified"}
```

```sh
uv run --locked quota-broker gateway --token-file /protected/admin-token observe-quota target-id < /protected/observation.json
uv run --locked quota-broker gateway --token-file /protected/admin-token reset-health target-id
```

來源必須受信任且有期限；remaining/limit為非負整數或null，remaining不可大於limit。metric支持requests/input_tokens/tokens/neurons；window支持rolling_minute/day/month/budget。內建Groq header parser與registered adapter觀測仍受schema與秘密反射檢查。較舊觀測不覆蓋新證據；同時間取保守值。HTTP管理入口不讀provider、不能代替官方帳戶用量查證。

## 路由、unknown與重啟

先核對能力、features、輸入／輸出、Free證據；合格short_renewable按免費budget refresh由短到長，再unknown、one_time_gift。其後health failures、可信remaining/headroom、local usage pressure、inflight、latency及target priority決定順序。RPM/TPM只是短窗口限流；無官方餘額維持null，沒有當成0或無限。

reserve、observed holds、quota及concurrency在同一SQLite transaction；dispatch重驗snapshot/lease/deadline/quota/health。所有busy時queue存next_retry_at；到期才claim，不忙輪詢。priority/deadline決定claim次序。auth/config問題需管理者修復，429/temporary circuit可等待；generic400/422不永久封鎖target。

派送後只有受測adapter、文件支持且無answer/usage的非執行quota拒絕可fallback。timeout、unknown、5xx、202及crash gap不重播。同一unknown不釋hold；另一provider有容量仍可接受新Task。

有效回答但缺quota metric為`completed_usage_unknown`。新的持久execution_completion證據只解除執行占位，保留估算charges及null actual；因此同CF target可處理下一個不同job，cap滿仍等待。沒有證據的歷史unknown／真正timeout仍保守占位，沒有回寫追認。對已完成target的設定變更不刪舊charges或更名歷史bucket。

first synchronous run才返回answer，status／同key只metadata。async只有認證result解密。length為partial，不等於完整回答；可信用量可正常結算，從不因此重送。queue lease過期先核對Gateway/ledger派送證據；確證未送出可恢復，可能派送則unknown。相同execution晚到有效answer可保存，replacement execution不能覆寫或派送。

## 驗證

目前部署與限定驗收摘要見[ASUS 維運指南](asus-broker-ops.md)；[核心契約](v1-core-contract.md)保留較早階段的驗證紀錄。使用標準`uv run --locked pytest -q`、Ruff／format／mypy與diff check。fixtures 離線，不提升七家歷史證據為當前全模型或持續免費配額保證。原始 live DB／操作封包留在本機，不納入 Git。
