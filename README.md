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

Copy config.example.json to config.local.json (ignored by Git). Each target starts disabled with unknown account eligibility and placeholder limits. For each account, manually check its current plan, billing state, model access, account/project quota pages, shared bucket scope, and remaining allowance. Then set enabled, free_eligible, verified_at and a short expires_at in timezone-aware ISO 8601 form. Enter your confirmed limits for every applicable dimension and a descriptive source. Bucket names must identify the actual shared scope, e.g. the Google project, not an individual API key; reuse an identical bucket definition across models sharing that quota. The sample 1 limits are placeholders, not published provider limits. concurrency_limit caps one target; set shared_concurrency_scope and shared_concurrency_limit together on every target sharing an account-level concurrent-call cap. Targets with the same scope must use the same shared limit. Both caps are checked in one SQLite transaction. When billing is enabled, admission fails closed even if a platform advertises a free allocation.

    uv run --locked quota-broker catalog --config config.local.json --db broker.db
    BROKER_TOKEN='a-local-secret' uv run --locked quota-broker serve --config config.local.json --db broker.db --port 18081

The server binds to loopback only. If BROKER_TOKEN is set, clients must send it as a bearer token; keep it outside Git and logs. Provider credentials stay in each client process. Use DirectClient from quota_broker.client; pass a stable request_key, the provider secret in process memory, a text prompt, and optional model. For Cloudflare, also pass a conservative neuron_bound established from account/model evidence. Do not treat the client's byte-based input estimate as a token guarantee. The NVIDIA candidate has a separate command and security contract in [the phase document](docs/nvidia-executor-phase.md). No real-account configuration or external ingress is included.

## Contract

- GET /v1/catalog returns static author/host/model/official endpoint/capability/context/source data plus manually confirmed target status and quota definitions.
- POST /v1/reservations accepts request_key, capability: text_generation, optional model, input_token_bound, max_output_tokens, and optional neuron_bound. It returns one reserved target or 503 unavailable with wait_until when calculable. Same key and same arguments return the same reservation; conflicting arguments fail.
- POST /v1/reservations/{id}/dispatch moves a still-valid reservation to dispatched once, after rechecking eligibility, cooldown, quota windows, and the original provider/model/endpoint/quota contract. The client calls only the returned official endpoint. Repeating dispatch is rejected.
- POST /v1/reports accepts reservation_id, unique report_key, state completed, failed, or unknown, and, for known outcomes, a usage map containing every configured metric. Identical reports replay safely; conflicting reports and invalid transitions fail. Unknown can later be reconciled to a known outcome with a new report key.
- GET /v1/reservations/{id} reads the durable state. Only unsent reserved rows expire after 30 seconds and release their holds. Dispatched and unknown never auto-release or auto-replay; reconciliation is explicit. The original route snapshot, quota definitions and held charge amounts remain visible for sent requests even if configuration later changes or removes the target. An unsent reservation whose route contract changed cannot dispatch.

Rolling-minute and daily quotas, per-target and optional shared concurrent requests, and 429 Retry-After cooldowns are enforced in short BEGIN IMMEDIATE SQLite transactions. Google RPD uses America/Los_Angeles midnight; Cloudflare daily Neurons use UTC midnight. Requests and input tokens are separate metrics. Cloudflare responses do not establish actual Neurons; without authoritative Neuron usage, the client reports unknown and retains the hold for manual reconciliation. A real 429 or disconnect does not trigger a provider retry. The direct HTTP adapter disables redirects and implicit SDK retries; its exact official endpoint allowlist is independent of broker configuration.

Existing SQLite files are migrated without inventing missing route history. A pre-snapshot unsent reservation cannot dispatch and expires normally. Pre-snapshot active sent/unknown records remain inspectable and reportable; new admission pauses while they remain active because their original routing scope cannot be proven. Settling those rows only removes that particular admission block. Before enabling a changed configuration, an operator must independently confirm prior provider usage, how old charges map to new buckets, and the applicable quota windows; the migration cannot infer or prove those facts.

The broker observes only cooperating clients. External usage, server-side metering differences, accounting delay, provider changes, and requests that overrun estimates can exhaust a provider before this local ledger detects it. This is neither third-party exactly-once execution nor a guarantee against charges. Keep billing disabled and reverify account facts before use. The NVIDIA executor is a small HTTP/SQLite process and needs no GPU. HP is the documented deployment candidate, subject to live host and port checks; no host deployment has occurred.

## Source and scope

See [provider sources](docs/provider-sources.md) for primary source links and the 2026-09-25 review. Google Gemini Developer API and Cloudflare Workers AI retain their direct-client v0.1 behavior. NVIDIA now has a separate fixed-route, non-streaming executor candidate, disabled until verified metadata is entered. No prompt/answer persistence, provider key storage, Redis, LLM ranking, paid fallback, or remote deployment is included.
