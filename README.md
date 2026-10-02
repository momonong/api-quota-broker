# API Quota Broker

Local control plane for trusted, cooperative clients using official text-generation APIs. It selects a manually verified free target, atomically reserves every configured shared quota, authorizes one dispatch, and records actual usage. The original Google/Cloudflare client calls the official provider directly; its broker receives no prompt, response body, or provider secret. The separate NVIDIA executor accepts text, holds its provider key only in memory, and uses a fixed official endpoint. There is no paid fallback.

## Reproduce locally

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

    uv sync --locked
    uv run --locked quota-broker demo
    uv run --locked pytest -q
    uv run --locked ruff check .
    uv run --locked ruff format --check .
    uv run --locked mypy src/quota_broker

The demo uses a local HTTP broker, an in-process provider fixture, a temporary SQLite database, and a restart check. It sends no real provider request and needs no provider account, API key, or GPU. Mypy checks all package modules with explicit function signatures. Provider JSON, client requests and SQLite rows remain dynamic data at their boundaries; runtime validation and behavior tests cover those contracts, so a passing type check does not establish their correctness.

## Account setup

Copy config.example.json to config.local.json (ignored by Git). Every example target is disabled. Confirm current free eligibility, billing state and model access for the actual account/project before enabling one; record the source, verified_at and a short expires_at. Billing enabled or stale/unknown free eligibility closes admission.

`local_safety_caps` are positive limits chosen by the operator for this broker, not published or account-specific provider quotas. The example values are placeholders. Use shared bucket names for the same account/project scope across models; `concurrency_limit` is per target, while `shared_concurrency_scope` and `shared_concurrency_limit` must be set together for an account-level cap. All local caps are checked in one SQLite transaction. For modern Google/Cloudflare targets, request caps for rolling_minute and day are required; other dimensions may be added when their charge can be bounded. A cap does not prove that the provider has remaining allowance.

`provider_quota_facts` separately records each provider limit and remaining claim with provenance (`official`, `observed`, `estimated`, `unknown`), observation time, source, account scope and optional valid_until. Use null values with `unknown`; never turn unavailable data into zero or unlimited. Only a current official remaining=0 with an unexpired valid_until blocks admission. Known facts without a validity bound are displayed but cannot prove current exhaustion. `capacity.kind` is `short_renewable` (with a sourced refresh_seconds of at most one day), `unknown`, or `one_time_gift`; its expires_at is display metadata only. Among eligible Google/Cloudflare targets, the broker tries shorter renewable capacity first, then unknown, then one-time gifts; priority and ID break ties within each class. This is a routing preference, not evidence that capacity is available. In the legacy `serve` path, NVIDIA remains a separate executor and is not part of that ordering; the unified gateway below routes across all three providers.

Existing `quotas` configurations still load and appear in the catalog with `quota_basis=legacy_v1`. Their figures remain local ledger admission limits and must not be presented as current official remaining. Migrate a target by renaming `quotas` to `local_safety_caps`, adding `provider_quota_facts` and `capacity`, then checking the displayed `quota_basis=local_safety_cap` before enabling. Do not set both fields. No SQLite ledger rewrite is required; changing cap definitions on an active route is still subject to the existing route snapshot and reconciliation rules.

    uv run --locked quota-broker catalog --config config.local.json --db broker.db
    BROKER_TOKEN='a-local-secret' uv run --locked quota-broker serve --config config.local.json --db broker.db --port 18081

The server binds to loopback only. If BROKER_TOKEN is set, clients must send it as a bearer token; keep it outside Git and logs. Provider credentials stay in each client process. Use DirectClient from quota_broker.client; pass a stable request_key, the provider secret in process memory, a text prompt, and optional model. For Cloudflare, also pass a conservative neuron_bound established from account/model evidence. Do not treat the client's byte-based input estimate as a token guarantee. The legacy NVIDIA candidate has a separate command and security contract in [the phase document](docs/nvidia-executor-phase.md). Its proposed one-shot local smoke command is `uv run --locked python -m scripts.nvidia_smoke_once`; it needs a fresh human Doppler CLI login, live free-endpoint check, and no conflicting billing evidence, and leaves a non-secret no-replay receipt. Production admission remains disabled without verified account metadata. No external ingress is included.

## Contract

- GET /v1/catalog returns static author/host/model/official endpoint/capability/context/source data plus account eligibility, labeled local safety caps, separately sourced provider quota facts, and capacity class.
- POST /v1/reservations accepts request_key, capability: text_generation, optional model, input_token_bound, max_output_tokens, and optional neuron_bound. It returns one reserved target or 503 unavailable with wait_until when calculable. Same key and same arguments return the same reservation; conflicting arguments fail.
- POST /v1/reservations/{id}/dispatch moves a still-valid reservation to dispatched once, after rechecking eligibility, cooldown, quota windows, and the original provider/model/endpoint/quota contract. The client calls only the returned official endpoint. Repeating dispatch is rejected.
- POST /v1/reports accepts reservation_id, unique report_key, state completed, failed, or unknown, and, for known outcomes, a usage map containing every configured metric. Identical reports replay safely; conflicting reports and invalid transitions fail. Unknown can later be reconciled to a known outcome with a new report key.
- GET /v1/reservations/{id} reads the durable state. Only unsent reserved rows expire after 30 seconds and release their holds. Dispatched and unknown never auto-release or auto-replay; reconciliation is explicit. The original route snapshot, quota definitions and held charge amounts remain visible for sent requests even if configuration later changes or removes the target. An unsent reservation whose route contract changed cannot dispatch.

Rolling-minute and daily quotas, per-target and optional shared concurrent requests, and 429 Retry-After cooldowns (seconds or HTTP date, with persistent conservative backoff when absent or invalid) are enforced in short BEGIN IMMEDIATE SQLite transactions. Google RPD uses America/Los_Angeles midnight; Cloudflare daily Neurons use UTC midnight. Requests and input tokens are separate metrics. Cloudflare responses do not establish actual Neurons; without authoritative Neuron usage, the client reports unknown and retains the hold for manual reconciliation. A real 429 or disconnect does not trigger a provider retry. The direct HTTP adapter disables redirects and implicit SDK retries; its exact official endpoint allowlist is independent of broker configuration.

Existing SQLite files are migrated without inventing missing route history. A pre-snapshot unsent reservation cannot dispatch and expires normally. Pre-snapshot active sent/unknown records remain inspectable and reportable; new admission pauses while they remain active because their original routing scope cannot be proven. Settling those rows only removes that particular admission block. Before enabling a changed configuration, an operator must independently confirm prior provider usage, how old charges map to new buckets, and the applicable quota windows; the migration cannot infer or prove those facts.

The broker observes only cooperating clients. External usage, server-side metering differences, accounting delay, provider changes, and requests that overrun estimates can exhaust a provider before this local ledger detects it. This is neither third-party exactly-once execution nor a guarantee against charges. Keep billing disabled and reverify account facts before use. The NVIDIA executor is a small HTTP/SQLite process and needs no GPU. HP is the documented deployment candidate, subject to live host and port checks; no host deployment has occurred.

## Usage records and current evidence

The normal broker persists accounting in the SQLite file passed to `--db`: `reservations` records request lifecycle, `charges` holds per-bucket estimated amounts and later replaces them with reported usage, `reports` records idempotent settlements, and `cooldowns` records rate-limit waits. The separate NVIDIA executor uses its own `serve-nvidia --db` path and adds `nvidia_executions` with provider-reported prompt/completion tokens. Its authenticated `GET /v1/nvidia/requests/{request_key}` and admin recent-request table read these records; the admin also shows the ledger's accounted input tokens. These paths are implemented and tested locally, but require a configured, running service and verified account profile before they can record real admitted traffic.

The one-shot Gemma and Riva scripts bypass that formal admission and SQLite ledger. They write only Git-ignored private `.state/*.json` no-replay receipts. The Riva translation probe's actual provider usage (22 prompt, 3 completion tokens) is in its receipt, **not** in `nvidia_executions` or `charges`; the older Gemma attempt remains `unknown` with no provider usage. There is no evidence of a running production database or deployed usage monitor for this repository. A successful isolated translation proves only that the Riva endpoint responded to that one request, not that the formal executor or usage dashboard has accepted a real request.

## Source and scope

See [provider sources](docs/provider-sources.md) for primary source links and the 2026-09-25 review. Google Gemini Developer API and Cloudflare Workers AI retain their direct-client v0.1 behavior. NVIDIA now has a separate fixed-route, non-streaming executor candidate, disabled until verified metadata is entered. No prompt/answer persistence, provider key storage, Redis, LLM ranking, paid fallback, or remote deployment is included.

## Unified gateway candidate (this phase)

The new `gateway-serve` path joins routing and provider execution behind **one required-auth loopback API**. It is a local candidate, not a deployed service. The older `serve`, `DirectClient`, and `serve-nvidia` interfaces remain available for compatibility. The gateway's `gateway.example.json` has seven disabled targets: Google Gemini, Cloudflare Workers AI, Groq GPT-OSS 20B, Mistral Small, NVIDIA Riva translation, and NVIDIA-hosted Gemma and Nemotron Lightning text generation. A model's `author` is distinct from its `provider`; `google/gemma-4-31b-it` consumes NVIDIA API Catalog capacity, and `openai/gpt-oss-20b` uses Groq.

Copy `gateway.example.json` to an ignored local config and populate **only verified account facts** before enabling a target. Every target requires a `secret_ref` name in the explicitly chosen Doppler project/config; this is metadata, never the secret value. Keep official remaining unknown as null. `local_safety_caps` are operator bounds, not official remaining. A valid official remaining=0 blocks; unverified free eligibility, billing enabled, stale evidence, missing credential scope, exhausted local caps, cooldown and shared concurrency block dispatch. Eligible routes prefer the shortest sourced renewable capacity, then unknown free capacity, then one-time gifts; priority and target ID break ties. A request can constrain `provider` and `model`. Cloudflare needs an explicit positive `neuron_bound` chosen by the operator; the gateway does not invent a Neurons conversion.

`gateway-serve` takes `--config`, `--db`, `--port` (default 18084), `--digest-key-file`, `--client-token-file`, `--doppler-token-file`, `--doppler-project`, and `--doppler-config`. The digest key must be at least 32 persistent bytes, and the separate client bearer token at least 32 characters. Use restricted runtime credentials outside Git. The Doppler service credential must be provisioned separately for the selected config; the one-shot 5-minute validation token is **not** a durable runtime credential. Provider keys are fetched by the gateway executor from the fixed Doppler HTTPS secret API immediately before dispatch; no provider key, prompt, or answer is stored in SQLite, argv, environment, or normal logs. This phase does not provision a service or network ingress.

All gateway routes require `Authorization: Bearer <client token>`:

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/v1/catalog` | Models, provider/author, account and quota evidence |
| POST | `/v1/routes/explain` | Dry-run route reasons without a provider request |
| POST | `/v1/tasks` | Reserve, dispatch once, execute and report |
| GET | `/v1/tasks/{request_key}` | Durable content-free task status |
| GET | `/v1/usage?provider=&model=&from=&to=` | Aggregated usage, unknown counts, UTC-normalized offset times |

`POST` task JSON requires `request_key` (opaque URI-safe ID), `capability` (`text_generation` or `translation`), `input`, and `max_output_tokens` (1–4096). Optional fields are `provider`, `model`, and `neuron_bound`. Translation additionally requires `source_language` and `target_language`: currently Riva's supported language codes with English on one side. Riva uses the documented language-pair system message, nonstreaming response, and a local 1952-character input policy. The fixed catalog endpoints use HTTPS with redirects disabled. The 4×UTF-8-bytes+256 input hold is deliberately conservative local admission, **not** provider tokenization or an official quota claim.

On the first successful task call, the response includes `answer`; status and same-key calls return metadata only. A changed payload with the same key is 409. `estimated_input_tokens` is the local hold, `reported_input_tokens`/`reported_output_tokens`/`reported_neurons` are provider values or null, and `ledger_charges`/`ledger_basis` show the actual SQLite hold or settlement separately. `completed_usage_unknown` means an answer was received but at least one provider usage value is absent; a missing quota metric retains the SQLite hold as unknown. An uncertain send, timeout, 202 or incomplete response is never retried or automatically rerouted. Before dispatch, an unavailable credential or stale dispatch admission may move to another eligible target. The gateway task record and broker reservation survive restart; the old private smoke receipts are neither imported nor replayed.

Groq's normal `provider_http` branch uses the packaged bounded curl transport at its fixed official HTTPS chat endpoint. It requires the existing `/usr/bin/curl`, passes credentials and JSON through stdin, discards stderr, bounds response/deadline, verifies TLS, and does not retry, follow redirects, change client identity or fall back to urllib. Other providers retain their existing transports.

Groq responses expose `finish_reason` (fixed known values, `missing`, `unclassified`, or null) and `response_truncated` (true for `length`, false for known other reasons, otherwise null). **`completed` describes execution and usage settlement, not answer completeness.** A visible partial answer ending in `length` is returned only on the first call and settles trustworthy actual input/output usage normally; it never triggers fallback or replay. Missing, invalid or conflicting Groq token counts retain `completed_usage_unknown` and the ledger hold. Usage includes `truncated_count`; historical rows without completion metadata remain unknown. The two metadata columns are added transactionally and repeatably to both task/attempt tables, with existing rows left null; no historical results or reservations are rewritten. Inspect `finish_reason=stop` when requiring a complete answer.

The CLI uses the same HTTP API. Pass task text on standard input so it is not exposed in shell arguments; `--json` selects machine output, otherwise a concise text view is printed:

```sh
uv run --locked quota-broker gateway --token-file /protected/client-token catalog
printf 'Hello.' | uv run --locked quota-broker gateway --token-file /protected/client-token --json explain --request-key dry-run-1 --capability translation --source-language en --target-language zh-cn --max-output-tokens 16
printf 'Hello.' | uv run --locked quota-broker gateway --token-file /protected/client-token --json run --request-key unique-task-1 --capability translation --source-language en --target-language zh-cn --max-output-tokens 16
uv run --locked quota-broker gateway --token-file /protected/client-token --json status unique-task-1
uv run --locked quota-broker gateway --token-file /protected/client-token --json usage --provider nvidia
```

For content-free `catalog`, `status`, and `usage` queries, `--token-stdin` can replace `--token-file`; supply the client token over standard input without an argv or file value. Task `run`/`explain` use standard input for task text and therefore require the client token file.

Do not run the example `run` command until its account facts, credential, local cap and real request authorization have been checked. The authenticated HTTP and CLI fixture tests exercise all five gateway providers, two models under NVIDIA, no replay, missing usage, and secret-free SQLite storage. Fixture success does not establish live provider eligibility or acceptance. Groq and Mistral account plans, billing and model access must be checked before enabling either target. [Phase contract and historical limits](docs/core-routing-phase.md).

## Private loopback key entry

The separately authenticated `key-admin-serve` page accepts a key only when the owner submits its form. It has fixed NVIDIA and Groq Doppler destinations; it does not call a provider or validate free eligibility. The management CLI login must be separate from the gateway's read-only runtime credential. See [private key admin](docs/private-key-admin.md) for the exact scope, local start command, status meaning, and safety limits.

The [2026-10-02 verification record](docs/verification-2026-10-02.md) separates fixture results, historical live requests, current credential-name checks, and live tests blocked by missing account evidence or approval.
