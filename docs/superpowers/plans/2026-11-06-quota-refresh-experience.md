# Quota refresh and usable overview

Scope authorized by the user: merge the accepted matching fix first (PR #6,
063ead2, complete), then implement repeatable quota updates and improve the UI.
No inference, purchases, credential rotation or renewal of manual qualification.
Preserve unrelated work. Existing HP maintenance and three-view dashboard remain.

## Implementation
1. Add a bounded, on-demand operator pipeline using Doppler CLI for existing
   credential references and official read-only endpoints. Each provider reports
   success, missing access, unsupported source or console review required.
   Preserve original timestamps on failure, counters separate from spending
   limits, public plan limits, actual credit expiry and evidence review dates.
2. Extend the existing metadata reader compatibly, validate imported evidence,
   keep observations advisory: new balances cannot enable execution by themselves.
   Use an immutable private output directory; never overwrite the input release.
3. Make the first view an account/allowance table: known remaining, last observed
   amounts, reset, actual credit expiry and refresh status visible before models.
   Provider selection leads to its models; detailed proof stays collapsible.
   Keep usage filters and task matching as separate existing views.
4. Offline regressions for unknown versus zero, stale evidence, collection failure,
   midnight reset, strict schema, secret redaction and UI data isolation. Check
   1440/768/390 layouts in the browser. Read-only live collection only after tests.
5. Package the exact reviewed output through the existing fixed HP publisher,
   reconcile durable operation receipts and verify public/private route boundaries.

Design: design-taste app draw selected achromatic-ledger; existing typography and
palette retained, blue-grey ground, tabular numeric columns and a neutral action.
Primary question: which provider has usable allowance, and when does it change?
No chart sums unlike units; no percentage for unknown denominators.
Sources lacking a supported counter API remain explicit manual/permission gaps,
not invented balances. CLI invocation and private report form the refresh contract;
website reload reads published results and does not pretend to query providers.
