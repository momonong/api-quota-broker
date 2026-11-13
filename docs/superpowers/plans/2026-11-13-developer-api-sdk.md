# Developer API and SDK Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans in this chat. Preserve unrelated working-tree changes. Steps use checkbox syntax for tracking.

**Goal:** Other projects and temporary tools can use the shared Broker through HTTPS plus their own project token, following the supplied API/SDK documentation and examples.

**Architecture:** Extend the existing scoped jobs facade and Python ProjectClient. Keep one Gateway, queue and ledger. HTTPS ingress forwards only documented project routes; website sessions and private administration remain separate.

**Tech Stack:** Existing Python 3.12, urllib, SQLite, Fernet, pytest, uv and HP/Caddy deployment tools. No new runtime dependency.

**Spec:** User-approved scope in this chat, 2026-11-13: finish API/SDK, integration docs and examples; validate an independent caller; do not change consuming projects. The user explicitly selected HTTPS URL plus project token for use from elsewhere.

## Global Constraints

- Preserve provider credentials inside the Broker; never expose them in examples, outputs or metadata.
- Preserve project ownership, free-only admission, original job keys, quota holds and unknown outcomes.
- No provider inference solely for metadata, no paid fallback and no blind POST retries.
- Retain existing loopback clients and asynchronous submit/status/result/cancel methods.
- Fix the helper's queued/result mismatch and dropped options without duplicating provider adapters.
- Do not publish unverified v7 remaining values as account evidence or silently extend their freshness.
- Preserve unrelated dirty files and legacy ASUS work. No commit, push or merge is included in implementation.
- Prepare and validate the exact HTTPS route/deployment change before applying it through the authorized service mechanism. Do not modify other applications.

## Accepted Interface Design

Use `https://momonong.me/quota/api/v1` as the documented HTTPS API root. Existing loopback host URLs remain accepted for local testing/private connections. Authentication uses a project-scoped bearer token, never a website password or provider key.

Expose bounded read-only catalog and route explanation through the same project facade, together with jobs submit/status/result/cancel. Existing raw `/v1`, queue tick, global usage and admin/maintenance stay inaccessible through the public route.

`ProjectClient.wait(key, wait_timeout=120, poll_interval=1)` follows only the original job and returns its retained outcome when available. A local wait deadline raises a structured ClientError containing the original key; it neither cancels nor resubmits. Terminal unknown/failure is not silently treated as success. `run(task, ...)` composes submit and wait. `call_free_api(..., request_key=...)` is the convenience equivalent and passes options correctly. Document normalized result text for text/OCR while preserving raw provider-independent metadata and usage-unknown states.

Publish a concise developer guide, machine-readable API contract and runnable Python/HTTP examples. Verify examples from a separate working directory through actual HTTP with fixture providers. A local fixture does not establish live provider or public deployment acceptance.

## Review Focus

- A lost POST response must retain the key and never submit again automatically.
- A local wait timeout must not consume the job or start another provider request.
- A valid token cannot access another project's result, raw admin or queue tick.
- Options, non-text tasks and completed_usage_unknown must preserve their actual meaning.
- Public availability and dry-run recommendations must not invent remaining quota or bypass scoped dispatch checks.

### Task 1: Developer SDK

**Files:** `src/quota_broker/client.py`, `tests/test_project_client.py`.
**Interfaces:** HTTPS/loopback API roots; `catalog()`, `explain(task)`, existing jobs methods, `wait(...)`, `run(...)`, and `call_free_api(...)`.

- [x] Add behavior tests for HTTPS URL validation, option forwarding, completed result retrieval, bounded waits, failures and no POST replay.
- [x] Run the new tests and record expected failures.
- [x] Implement the smallest compatible client changes; keep provider I/O out of the SDK.
- [x] Run focused tests, Ruff and Mypy.

### Task 2: Scoped API contract and admission

**Files:** `src/quota_broker/gateway_server.py`, `src/quota_broker/access.py`, relevant access/admission tests.
**Interfaces:** Project-authenticated catalog/explain/jobs; normalized text results; accurate rejection reasons; existing ledger and ownership semantics.

- [x] Add HTTP tests for project metadata routes, scope boundaries, cross-project access and uniform text/OCR results.
- [x] Add regression cases for supported non-UTC quota windows and expired non-required dynamic observations, retaining fail-closed required evidence.
- [x] Verify failures, implement fixes, and run related access/framework/queue tests.

### Task 3: Independent integration and delivery

**Files:** `docs/developer-api.md`, `docs/openapi.json`, `examples/project_client/`, README and current integration pointers; scoped deployment candidate files as needed.
**Interfaces:** One installation path, HTTPS base URL, project token, stable key and documented job/result/error schemas.

- [x] Add executable Python and plain HTTP examples with no embedded credential or automatic resubmission.
- [x] Exercise the documented examples from a temporary independent directory against real fixture HTTP endpoints, including result recovery and foreign-token rejection.
- [x] Build/install the package into an isolated consumer environment and verify SDK imports without checkout/PYTHONPATH reliance.
- [x] Run repository verification; report inherited failures separately and preserve their files.
- [x] Review the combined change, inspect exact HP/Caddy candidate and current service state, then complete the authorized deployment and bounded public acceptance where the available maintenance mechanism permits it.
- [x] Record code, offline integration, public deployment and live-provider evidence separately. Deliver directly in this chat.

## Execution Record

- Starting HEAD: `0d81f79b5cbbce7b02700c333204f0d19f9b7741`; existing branch `feat/quota-refresh-experience`.
- Existing dirty files are unrelated ASUS deployment/test work; retain them. Main implementation files start clean.
- User has authorized implementation and selected HTTPS; no additional design-approval round is needed.
- Existing source and tests already establish jobs ownership and retention. Extend them in place; no new worktree or second service/ledger is needed for sequential work.
- Public infrastructure source selected from GitHub main on 2026-11-13: selfhost-servers `955b4f8cd73c4eeaaf894445afbdbc919326d6ed`; local dirty drafts are not authoritative deployment instructions.
- Task 1: SDK RED 8 failed / 14 passed, then 22 passed. Final related API/SDK/examples/ingress suite after review: 80 passed (`/tmp/quota-review-green-20261113.log`).
- Task 2: added metadata/admission regressions failed before implementation; 64 API/SDK tests passed after implementation. Separate actual Caddy route matrix and legacy OCR result test passed.
- Task 3: independent wheel consumer verified in `/tmp/quota-consumer-20261113`, outside checkout, no PYTHONPATH; final example/schema suite 4 passed (`/tmp/quota-wheel-final-20261113.log`).
- Final review: read-only `developer_api_review`, Sol 6.1 High, found three P2 issues. All fixed with observed RED→GREEN: truncated HTTP responses retain the original key; code completion output bound forwarded; OpenAPI priority matches Gateway -100..100. No deferred minor findings.
- Final review boundaries: production deployment and real providers require the current host path, credentials and separate bounded evidence; unrelated ASUS work remains outside this change. Full suite and baseline comparison are handled by this parent chat.
- Earlier deployment stopping condition (resolved below): fixed publisher status returned `installed_extra_file`; root-owned release cannot be inspected by management user, `sudo -n` is unavailable. No publication, deletion, forced rollback, credential policy change or Caddy reload performed.
- Full regression: 3583 passed, 40 skipped, 17 failed, 64 errors. A pre-change source snapshot reproduced 16 failures and all 64 errors. The remaining hard-coded 1.2.0 fixture was adjusted to the actual package version and passed alone after editable metadata sync. Original untracked fixture preserved at `/tmp/quota-test-code-update-before-20261113.py`; no unrelated lines changed.
- Final checks: Ruff check/format, Mypy 36 source files, lock and diff checks pass. At this earlier checkpoint production was blocked; the authorized continuation and final evidence are recorded below.

### Authorized HP continuation and production acceptance

- User explicitly accepted private-terminal HP inspection/repair and opening the API, then confirmed READY. Exactly 19 generated root-owned Python bytecode files were preserved in a private backup; original manifest files were intact. The fixed publisher returned to ready.
- Broker release `shared-broker-20261113-api-v2`, manifest `f6975d83a5614bdfedf9b15b5d99d5a34de1bdc7d633b42a34c7e2f2e790ee5a`, published by the existing fixed channel under operation `46490cc9-2ef5-4c31-8f98-5377cc813baf`; final status readback is complete. Five v6 config files, credentials, ledger and queue preserved.
- The reviewed API allowlist was inserted only inside the existing Quota Caddy route, after backup and target-host validation. Caddy reload and nine origin checks passed; HTTPS and Host guards preserved. Private sudo ticket invalidated and dedicated terminal closed.
- Cloudflare 1010 was diagnosed using its official documentation. The SDK and HTTP example now send their actual application names/versions; 2 header regression cases failed before the fix and passed after it. No Cloudflare policy change.
- Independently installed wheel and copied examples passed public HTTPS catalog/explain. One free job, max_output_tokens=32/max_attempts=1, returned `OK。` via Gemini; actual usage input 5/output 2. Same-key readback and completed same-content submission kept attempt_count and attempt_budget_used at 1. Plain HTTP returned the same outcome; cross-project result 404, anonymous/invalid token 401, admin/tick 404, website/snapshot boundaries preserved.
- Final related tests: 82 passed; isolated wheel/schema tests: 6 passed. Ruff/format, Mypy 36 modules, JSON/docs links, wheel/source/deployment byte matching and git diff checks passed. Earlier full-suite inherited failures remain recorded separately.
- Docs and local wheel updated; no Git commit, merge, push, PyPI publication or consumer-project modification. Final receipts: `.state/developer-api-20261113/`; readable closeout: `docs/developer-api-acceptance.md`.

### Authorized Git delivery

- The user explicitly authorized committing, merging and pushing only this API/SDK, documentation, examples and related validation to GitHub. Source `feat/quota-refresh-experience` and all prior dirty/untracked files are protected by a hash snapshot.
- Fresh origin/main matched source HEAD `0d81f79b5cbbce7b02700c333204f0d19f9b7741`. A managed release worktree and `feat/developer-api-sdk` contain only the explicit 22-file allowlist; the pre-existing untracked maintenance version fixture is excluded.
- Installation docs now support direct GitHub installation with a locked commit, plus reproducible wheel builds; no generated wheel, credential, raw receipt or runtime configuration is committed. Git publication does not redeploy HP or modify consuming projects.
- Clean release-tree regression: 3311 passed, 40 skipped, 20 failed, 64 errors. All failure nodes match pre-change evidence; five newly visible cases were reproduced on a clean origin/main archive. Root cause: prior expired Cloudflare fixtures and tests referencing untracked quota-web maintenance code. They stay outside this release.
- Whole-tree Ruff check, Mypy, lockfile, wheel and sensitive-content checks pass. Changed-format scope passes; five unrelated formatting failures reproduce on origin/main. The rebuilt wheel matches the production acceptance artifact SHA exactly.
