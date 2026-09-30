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

`provider_quota_facts` separately records each provider limit and remaining claim with provenance (`official`, `observed`, `estimated`, `unknown`), observation time, source, account scope and optional valid_until. Use null values with `unknown`; never turn unavailable data into zero or unlimited. Only a current official remaining=0 with an unexpired valid_until blocks admission. Known facts without a validity bound are displayed but cannot prove current exhaustion. `capacity.kind` is `short_renewable` (with a sourced refresh_seconds of at most one day), `unknown`, or `one_time_gift`; its expires_at is display metadata only. Among eligible Google/Cloudflare targets, the broker tries shorter renewable capacity first, then unknown, then one-time gifts; priority and ID break ties within each class. This is a routing preference, not evidence that capacity is available. NVIDIA remains a separate executor and is not part of this ordering.

Existing `quotas` configurations still load and appear in the catalog with `quota_basis=legacy_v1`. Their figures remain local ledger admission limits and must not be presented as current official remaining. Migrate a target by renaming `quotas` to `local_safety_caps`, adding `provider_quota_facts` and `capacity`, then checking the displayed `quota_basis=local_safety_cap` before enabling. Do not set both fields. No SQLite ledger rewrite is required; changing cap definitions on an active route is still subject to the existing route snapshot and reconciliation rules.

    uv run --locked quota-broker catalog --config config.local.json --db broker.db
    BROKER_TOKEN='a-local-secret' uv run --locked quota-broker serve --config config.local.json --db broker.db --port 18081

The server binds to loopback only. If BROKER_TOKEN is set, clients must send it as a bearer token; keep it outside Git and logs. Provider credentials stay in each client process. Use DirectClient from quota_broker.client; pass a stable request_key, the provider secret in process memory, a text prompt, and optional model. For Cloudflare, also pass a conservative neuron_bound established from account/model evidence. Do not treat the client's byte-based input estimate as a token guarantee. The NVIDIA candidate has a separate command and security contract in [the phase document](docs/nvidia-executor-phase.md). Its proposed one-shot local smoke command is `uv run --locked python -m scripts.nvidia_smoke_once`; it needs a fresh human Doppler CLI login, live free-endpoint check, and no conflicting billing evidence, and leaves a non-secret no-replay receipt. Production admission remains disabled without verified account metadata. No external ingress is included.

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
