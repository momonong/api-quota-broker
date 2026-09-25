# API Quota Broker v0.1

Local control plane for trusted, cooperative clients using official text-generation APIs. It selects a manually verified free target, atomically reserves every configured shared quota, authorizes one dispatch, and records actual usage. The client calls the official provider directly. The broker receives no prompt, response body, or provider secret and does not proxy streams. There is no paid fallback.

## Reproduce locally

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

    uv sync --locked
    uv run --locked quota-broker demo
    uv run --locked pytest -q
    uv run --locked ruff check .
    uv run --locked ruff format --check .

The demo uses a local HTTP broker, an in-process provider fixture, a temporary SQLite database, and a restart check. It sends no real provider request and needs no provider account, API key, or GPU.

## Account setup

Copy config.example.json to config.local.json (ignored by Git). Each target starts disabled with unknown account eligibility and placeholder limits. For each account, manually check its current plan, billing state, model access, account/project quota pages, shared bucket scope, and remaining allowance. Then set enabled, free_eligible, verified_at and a short expires_at in timezone-aware ISO 8601 form. Enter your confirmed limits for every applicable dimension and a descriptive source. Bucket names must identify the actual shared scope, e.g. the Google project, not an individual API key; reuse an identical bucket definition across models sharing that quota. The sample 1 limits are placeholders, not published provider limits. When billing is enabled, admission fails closed even if a platform advertises a free allocation.

    uv run --locked quota-broker catalog --config config.local.json --db broker.db
    BROKER_TOKEN='a-local-secret' uv run --locked quota-broker serve --config config.local.json --db broker.db --port 18081

The server binds to loopback only. If BROKER_TOKEN is set, clients must send it as a bearer token; keep it outside Git and logs. Provider credentials stay in each client process. Use DirectClient from quota_broker.client; pass a stable request_key, the provider secret in process memory, a text prompt, and optional model. For Cloudflare, also pass a conservative neuron_bound established from account/model evidence. Do not treat the client's byte-based input estimate as a token guarantee. There is no deployment profile, external ingress, or real-account configuration in this repository.

## Contract

- GET /v1/catalog returns static author/host/model/official endpoint/capability/context/source data plus manually confirmed target status and quota definitions.
- POST /v1/reservations accepts request_key, capability: text_generation, optional model, input_token_bound, max_output_tokens, and optional neuron_bound. It returns one reserved target or 503 unavailable with wait_until when calculable. Same key and same arguments return the same reservation; conflicting arguments fail.
- POST /v1/reservations/{id}/dispatch moves a still-valid reservation to dispatched once, after rechecking eligibility, cooldown, and quota windows. The client calls only the returned official endpoint. Repeating dispatch is rejected.
- POST /v1/reports accepts reservation_id, unique report_key, state completed, failed, or unknown, and, for known outcomes, a usage map containing every configured metric. Identical reports replay safely; conflicting reports and invalid transitions fail. Unknown can later be reconciled to a known outcome with a new report key.
- GET /v1/reservations/{id} reads the durable state. Only unsent reserved rows expire after 30 seconds and release their holds. Dispatched and unknown never auto-release or auto-replay; reconciliation is explicit.

Rolling-minute and daily quotas, concurrent requests, and 429 Retry-After cooldowns are enforced in short BEGIN IMMEDIATE SQLite transactions. Google RPD uses America/Los_Angeles midnight; Cloudflare daily Neurons use UTC midnight. Requests and input tokens are separate metrics. Cloudflare responses do not establish actual Neurons; without authoritative Neuron usage, the client reports unknown and retains the hold for manual reconciliation. A real 429 or disconnect does not trigger a provider retry. The direct HTTP adapter disables redirects and implicit SDK retries; its exact official endpoint allowlist is independent of broker configuration.

The broker observes only cooperating clients. External usage, server-side metering differences, accounting delay, provider changes, and requests that overrun estimates can exhaust a provider before this local ledger detects it. This is neither third-party exactly-once execution nor a guarantee against charges. Keep billing disabled and reverify account facts before use. On the intended future Ubuntu laptop (2 cores / 4 threads, 3.4 GiB RAM), this control flow is designed to be lightweight and needs no GPU; this hardware has not been tested or deployed in this phase.

## Source and scope

See [provider sources](docs/provider-sources.md) for primary source links and the 2026-09-25 review. Google Gemini Developer API and Cloudflare Workers AI are the only dispatchable providers in v0.1, and both are disabled until account facts are confirmed. NVIDIA is tracked only as a trial candidate requiring further review. No prompt/answer persistence, provider key storage, Redis, proxy, LLM ranking, paid fallback, or remote deployment is included.
