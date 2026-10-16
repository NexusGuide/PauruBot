# F-15 — advertising preferences

## Scope

Preferences belong to the authenticated customer **in one shop**, including reseller bots.
Bot commands: `/marketing`, `/stop_ads`, `/start_ads`; Mini App: More.
`POST /api/webapp/me/marketing` takes only `{"marketing_opt_out": true|false}`.
Extra fields and coerced booleans are rejected. No staff override or request-supplied customer ID.
Changes are row-locked, audited and committed with the preference. Repeating the same explicit
choice does not toggle it or generate another audit entry.

All Broadcast jobs (text, copy or forward; main/child/admin/reseller entrypoints) exclude opt-outs.
Audience membership is re-read immediately before every send, not just when loading a batch.
Queued private discount-code offers also check the preference. Payment, wallet, service, support,
referral earnings and prize-result notifications remain unchanged; wallet gifts still credit everyone
eligible. A staff-requested preview sends only to that staff member and is not a customer campaign.

Admin API: `kind` supports all, resellers, buyers, active, inactive, never_bought. Existing legacy
resellers_only / buyers_only flags retain their precedence. Audience preview accepts those same
filters and locale and uses the exact worker predicate, including opt-out and non-guest checks.
Existing locale behaviour (null matches the selected locale) is intentionally preserved.

## Limits and rollout gate

Migration **0022** follows 0021. The false default preserves previous behaviour and is **not proof
of affirmative consent**. changed_at stays null until an explicit customer choice. Review the
privacy policy / whether an opt-in-only rollout is required before production deployment.

Use PostgreSQL's default READ COMMITTED isolation. A change committed before the final eligibility
read suppresses that recipient. A request already in flight to Telegram cannot be recalled; there
is inevitably a read-to-send race. Network failures retain existing queue retry/duplicate semantics.
No claim of exactly-once delivery. Total is a starting eligible estimate, not a frozen audience:
new choices can change it. skipped counts recipients removed after their batch was loaded, not all
historically opted-out customers; it is not a delivery failure. DONE/CANCELED have zero remaining.

Before release: apply migration on staging; run Python and frontend CI, then test both bot tenants,
scheduled/pause/resume campaigns, opting out after a batch loads, strict auth/input validation,
and real Mini App success/failure/reload. Keep service/payment messages enabled in those checks.
This PR does not deploy, merge, change notification settings or enable typed confirmations.


## Explicit web targeting contract (F-16 compatibility)

The additive `POST /api/admin/broadcasts/targeted` requires `kind`; legacy mirrors are strict
booleans and, if true, must agree with kind. Conflicts are rejected before scheduling, audit or
job creation. It delegates to the existing tenant-scoped create function and preserves all
button/schedule validation and permission checks. Legacy `/broadcasts` is unchanged.
`GET /api/admin/broadcasts/targeted/audience` requires a valid kind and bounds locale to eight
characters; it delegates to the same tenant-scoped audience predicate. Both require broadcast.send.

The #56 frontend uses these explicit paths without falling back to old APIs that could ignore
kind. Deploy/integrate this backend and migration before enabling customer queuing in that UI.
Preview is an estimate, not a frozen list, authorization token or durable idempotency key.

Added 21 API contract tests (creation/preview delegation, all six kinds, input conflicts,
required kind, locale bounds, permission denial, matching mirrors and legacy body default).
Local validation executes original endpoint/model AST nodes with injected auth/DB dependencies;
21 passed, with create/count mocked: no real campaign, audit, queue or Telegram request.
Full repository tests/imports, PostgreSQL isolation and staging still need GitHub CI/real validation.
