# Broker reliability and expiry-aware allocation

User authorization: repair accounting, visualizations, model inventory, provider allocation and overload retry; verify complete behavior. Preserve live data, existing dirty work and local-only/cloud-only boundaries. Base: d03ee867c04bb7ed7039183db0b2ac4374ff5f91. No push, merge or deployment performed by this plan itself.

## Accepted design

- Fresh capacity expires at the earlier of evidence TTL and its actual reset; stale data stays visible but never authorizes a new dispatch. No invented daily balances or release of UNKNOWN charges.
- Reports join the same filtered ledger, distinguish known lower bounds / unknown / not-applicable, and show daily requests and provider distribution.
- Model discovery records official catalogs, pagination and freshness separately from registered adapters and account eligibility. Add current NVIDIA GLM and replacement Cloudflare model only with documented protocol support.
- Allocation is constrained numerical optimization: all free-only, capability, credential and quota gates remain mandatory. Weighted expiry urgency is dominant; task-specific evidence, posterior quality and reliability, remaining capacity and latency contribute continuously. Missing evidence is neutral/uncertain, never best. Same scoring for explain, dispatch and dashboard recommendations. No claim that HTTP success proves answer quality.
- Retry complete synchronous overload refusals (documented 429/503 error shapes) with bounded exponential delay and deterministic jitter; honor Retry-After and deadlines. Every dispatch is independently accounted and durable. Complete refusal may leave unknown usage held. Incomplete bodies, transport timeout, asynchronous202, ambiguous502/504, accepted results and partial results are never automatically replayed. Queue retry survives restart and has a total attempt budget.

## Work and evidence ledger

1. Baseline: isolated fix/quota-reliability-expiry; 92 existing gateway/quota/queue/usage tests passed (2026-11-16).
2. Accounting: independent quota_accounting agent owns routing.py and dashboard.py.
3. Usage UI: independent usage_ui agent owns dashboard assets.
4. Official catalog: provider_inventory agent owns discovery/catalog metadata; execution readiness remains separate.
5. Root: common numerical allocation, explicit quality evidence, safe retry/queue integration, usage timeline and runtime candidate configuration.
6. Integrate patches, review complete diff, run full tests/static/browser checks and bounded live verification only within available access/authorization.

## Completion checks

Cross-reset refusal, partial and unavailable accounting, equivalent route ordering, quality versus urgency scenarios, 503→success/429→success, exhausted budget/deadline/restart/dedup/concurrency, unknown no-replay, credential/privacy boundaries; full Python/JS checks and actual dashboard rendering where supported. Record live evidence and unverified deployment separately.

## Current implementation and verification status (Taiwan 2026-11-17)

- Accounting, usage UI, official catalog, numerical expiry utility, profile-scoped quality evidence and safe durable overload retry are integrated.
- Original three missing canonical gate failures are fixed. Full final suite including launcher/direct ChatCompletion/malformed finish reason: 3748 passed, 41 skipped, zero failures (272.64s); skips are unavailable legacy ASUS namespace mutation checks.
- Python lint/type checks and JS syntax pass. Real isolated Chrome verified dashboard with synthetic ledger data at 1440/768/390 pixels, filtering, distribution geometry and logout.
- Official catalog records 1195 fetched provider/model identities. Cloudflare 75 IDs independently match its entire Workers AI documentation index, while the account API's total334 discrepancy remains partial; HF remains chat-only.
- User authorized HP read-only audit, Analytics Read, bounded free inference, deployment, push and merge. HP cache discrepancy was preserved in backup; restricted publishers/control pins updated. First broker switch rolled back on launcher readiness, then v2 published complete/active after the precise launcher fix. The final direct-schema patch is now broker v3/dashboard v2, both complete/active with independent current/manifest readback.
- Existing private-development encrypted local_only acceptance completed with Qwen LOCAL_OK. Cloudflare first HTTP200 response remains UNKNOWN with all holds, no replay; exact official direct ChatCompletion parser and malformed-field guard are tested and deployed. NVIDIA original key was never accepted (read-only ownership count0).
- Finish Cloudflare parser/live acceptance, remaining consumer readbacks and authorized Git delivery; record final evidence in docs/reliability-expiry-2026-11-17.md. No quality benchmark or blanket device-availability claim.
