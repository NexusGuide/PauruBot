# Mini App renewal preview

## Scope

An authenticated read-only `GET /api/webapp/orders/{id}/renewal-preview` for an owned, unexpired pending **service renewal**. The service must also belong to the same customer and shop, be non-trial/non-deleted, and support renewal under the existing plan/panel gates. It refuses a forecast while an earlier paid/provisioning service modification is unresolved. Parent-shop plans in reseller shops are not rejected merely for having a different shop ID; existing panel matching and worker configuration still apply.

The projection calls `lifecycle.renew_mode` and the worker's `_target_for` calculator. There is no second client-side business-rule implementation. Plan > panel > shop mode selection, lapsed-service override, unlimited allowances, remaining data, expiry and counter reset follow the existing worker code. Current order price is returned unchanged, not repriced.

## Contract and honesty

- `basis=stored_service_snapshot`, `computed_at`, `valid_until` (at most 60 seconds and no later than order expiry).
- `before`: stored expiry, total limit, used counter and remaining allowance. A null remaining allowance means unlimited, **not zero**.
- Immediate: `after` is the calculated end state **if applied now**, including counter reset/retention. It is not a live panel reading or a reserved execution target.
- Queued: current expected queue time and `after=null`. Usage exhaustion can release the queue earlier; the eventual policy/state is determined at execution. The UI does not display current policy as a guaranteed future queued result.
- No credentials, private order params, usernames, subscription links, internal targets or predecessor IDs in the projection.
- No calls to panel/provider/Telegram, no commit, no target reservation, no payment, no provisioning, no refund, no cancellation or service transfer. Normal authentication bookkeeping remains unchanged.

Checkout renders the preview only for `kind=renew`. Loading, invalid/mismatched response, network failure and expiry disable wallet and every external-method button in this UI. A synchronous pay-handler guard also checks readiness. Refresh and abort/late-response handling cannot enable a different order. The 60-second UI freshness check is a presentation aid, **not a server authorization or execution lock**. Other API/Bot/reseller callers are unchanged.

## Validation

New full-app API regressions cover all four modes, active/lapsed services, unlimited/exhausted data, queuing, ownership/shop isolation (including inconsistent historical rows), capabilities, predecessor changes, mode precedence, response filtering and absence of domain writes. They run in the existing PostgreSQL test workflow; not claimed as locally executed.

Frontend tests execute the real component effect with controlled hooks/API/timer seams, plus response decoding, stale/mismatched results, expiry, abort, failure, bilingual placeholder parity and checkout wiring. Local Python matrix executes the real helper, policy and worker calculator using controlled DB/dependency seams, not PostgreSQL/full-app.

## Release gates and work not closed

Keep draft until exact-head CI and real Telegram/mobile/staging QA. Check preview readability in Persian/English, device clock skew, expiry refresh and delayed payments on the combined PR stack. This PR adds no migrations and inherits existing stack migration/release gates.

A guaranteed/binding quote through delayed payment/execution is **not implemented**: usage, service, plan or policy can change after preview and the worker recalculates. Paid/queued targets and reconciliation remain the worker's responsibility. Account-panel renewal, bulk reseller/Bot workflows, extras/upgrades previews and customer-facing cancel/refund/transfer controls are outside this slice. They require their own backend capability/policy review; this PR does not authorize new financial actions or mark the entire renewal backlog complete.
