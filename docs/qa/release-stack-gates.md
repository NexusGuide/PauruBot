# Remaining release gates — observed 2026-10-19

This is an execution checklist, not a completed release certificate. No production merge,
configuration activation, provider call, Telegram staging session or migration rehearsal was done.

## Dependency inspection

Observed open stack: #96 → #112 → #120 → #122 → #123 → #124 → #125.
#91 is a separate password-hashing prerequisite for independent recovery.
#105 → #108 is another line; #117 wallet transfers, #118 notifications and #119 ticket media
are sibling feature branches from #108, NOT automatically included in the checkout stack.
Tonpay #113–#116 is a separate gated branch line; keep disabled until its own approval/QA.
Several older audit/fix PRs appear overlapping (#83–#88 vs #89–#94); compare patches before
choosing canonical commits. Do not merge both copies blindly or close them without review.

Live parent branches have moved: e.g. #120 head 866c199 differs from the older base snapshot
032d035 recorded in #122. #108's base snapshot also differs from the listed head of #105.
A child's earlier CI success is not evidence that all current parent fixes are included.

## Last checked CI evidence

- #91, fabc846: python + frontend success, run 39107269413.
- #119, 5f8f40f: python + frontend success, run 39218141739.
- #123, 3bc281b: python + frontend success, run 39225739643.
- #124, 20e9771: python + frontend success, run 39229051643.
- #125, 7b98341: frontend success; **python failure**, run 39233739049.
  Failure logs were not exposed by current MCP tools; no root cause or fix is claimed.
- Other PRs: current-head CI was not revalidated in this pass. Pending is not success.

## Required execution order

1. Obtain #125 failure logs; reproduce/fix the precise error and rerun its exact-head checks.
2. Freeze chosen head SHAs and compare every stack delta, not only titles or green badges.
3. Build a disposable integration branch containing selected canonical security/payment/UI commits
   and the three sibling features. Resolve shared webapp routes, API client, locales, package.json,
   worker registration and migration-number conflicts deliberately; preserve all regression tests.
4. Run full lint/types, security/full API suites, frontend build/UX suites and a combined migration
   rehearsal from both empty DB and a restored production-shaped isolated snapshot. Validate a single
   Alembic head, data/backfill defaults, tenant scope, rollback-compatible application versions,
   and Redis/session/token compatibility. Do not infer this from separate branch CI.
5. QA real Telegram/mobile/staging with nonproduction credentials and disposable funds/services.
   Include wallet transfer replay, notification roles, support file ownership/retention, card verification,
   interrupted guest/normal checkout, callback-before-response, delayed/duplicate provider events,
   unresolved checkout recovery, renewal quote changes, reset expiry/revocation and clock skew.
6. Only after evidence and policy sign-off: review/merge in dependency order; no blind bulk merge.
   Production activation/deploy is separate from merging and requires a rollback/runbook checkpoint.

## Still requires implementation or an explicit decision

- Guest/Bot/legacy/unkeyed checkout deduplication across real caller paths.
- Provider-authenticated recovery when a creation result/reference is unknown. Do not trust a user
  supplied transaction ID, retry a possibly-created invoice, or infer success from amount/address alone.
- A binding renewal contract through delayed payment and queue execution: decide price/plan/usage
  drift behavior and who bears provider fees/refunds before freezing targets or auto-refunding.
- Trusted hard cap: exact amount, currency/unit, per-transaction vs rolling period and shop/customer scope.
- Privileged step-up: protected actions, approved independent factor, freshness/replay window,
  permission separation, recovery for unlinked admins and emergency operational authorization.
- Forced legacy credential rotation / restrictions on other privileged routes (see password-recovery.md).

Never mark these done merely because a Draft PR exists. Never run the test fixture that drops the
public schema against staging/production; it is only for a disposable test database.
