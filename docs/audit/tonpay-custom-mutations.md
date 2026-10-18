# Unwired custom Tonpay mutation service

## Scope

Depends on #114 (and its #113/#112/#96 stack). This increment adds internal
owner-scoped receipt/card mutations for an **already bound, committed** custom
Tonpay top-up. It does NOT create an invoice, bind an unknown reference, register
a payment provider, expose a route, configure a key or render a UI. Test fixtures
seed the future creator's state; there is no production creator yet. Keep draft.

Contract context remains the source-derived Faoxima snapshot documented in
[custom contract](tonpay-custom-contract.md), not a provider-sandbox validation.
No additional product fee/timing policy is silently inferred from that source.

## Ownership and consent

A trusted adapter must derive Owner(shop_id, customer_id) from its authenticated
principal, NEVER from request body fields. Owner is an internal value, not proof
of login. The service reloads the exact customer/shop and locks only their Payment.
Inactive shop, banned/guest customer, wrong provider/purpose/currency or corrupt
state are denied. Fresh mutations require PENDING and a future local expiry.
Customer authorization is rechecked after image validation and at finalization.
This is not a serializable ban-versus-already-in-flight-HTTP guarantee.

The caller must construct the injected TonpayCustomClient with that shop's own
custom key. Client construction/key configuration and API authentication are NOT
implemented by this module. Do not accept client objects/keys/references/amounts
or Owner fields directly from a customer request.

Consent persists only the exact current quote revision and payable amount; it
never changes the price. A new card or changed provider fee invalidates consent.
Receipt readiness, review deadline, card-change cooldown and exhaustion are checked
server-side. Every invoice must already carry an explicit frozen policy; no global
default timing is introduced. Timing values in tests are synthetic examples only.
Resubmission cannot extend an existing review deadline. Receipt acknowledgement
is tracked against the quote/card revision; previous acknowledgements remain in
operation evidence when a new card later receives a new receipt.

## Durable mutation invariant

- A private transaction obtains a payment row lock, reloads owner/state/quote and
  checks action permissions. The request's unrelated session is never committed.
- Scoped hashed key + operation/quote/receipt SHA-256 fingerprint and a random
  claim token are independently committed as uncertain BEFORE external I/O.
- Only the inserting request sends the mutation. The row lock is released before
  that call. HTTP error, timeout, cancellation, process death or a failed final DB
  commit leaves uncertain evidence; same-key AND replacement-key mutations block.
  No lease expiry, background re-POST or automatic replacement key is introduced.
- Successful summaries replay by the original key without more provider calls;
  changed operation, quote revision or receipt bytes conflict. Replays still require
  current ownership and do not imply a current action capability or payment state.
- Completion requires the original token, matching identity/amount and unchanged
  quote revision. Preserve concurrent admin status and unrelated metadata. A newer
  paid/status observation is not downgraded by an older mutation reply. This is NOT
  a global ordering guarantee for future poll/callback/refresh implementations.
- Preserve remote acknowledgement without marking Payment PAID, posting a journal,
  changing a balance or enqueueing fulfilment. Fee changes, paid flags or concurrent
  closure/expiry require review; upload acknowledgement is not bank settlement.

State resides under Payment.meta[tonpay_custom_v1]. It includes original bound
identity, explicit policy, quote/consent revisions, readiness/review timestamps,
provider observations and at most 16 operation records. Do not evict old dedupe
keys to allow another mutation: at the cap, an audited operator workflow is needed.
The cap is a conservative technical bound, not a new financial entitlement policy.

No key, card number/name, original filename, raw provider body or receipt image is
persisted in this state. Receipt hashes and opaque references are still sensitive
metadata requiring scope and retention controls in future API/admin projections.
This operational evidence is NOT an immutable financial journal or a complete
operator audit trail. No clearing/rebinding/recovery endpoint is provided.

## Test evidence and limits

Tests cover owner/shop/ban/guest checks before decoding or HTTP, consent/quote/
time gates, committed pre-call evidence, replay/conflict, in-flight duplicate,
ambiguous errors/cancellation, both commit failures, concurrent rejection/expiry,
newer paid observations, corrupt metadata and bounded history. The explicit
multi-request claim race requires real PostgreSQL row locking in CI.

Local smoke tests use exact production service/client functions with SQLite and
minimal ORM/import seams. SQLite ignores FOR UPDATE; these runs are NOT evidence
of PostgreSQL locking, full application boot, migration or Redis behavior. The
PostgreSQL-only initial-claim race is excluded locally, not marked skip in CI.
Full CI status must be reported separately, including the still-unconfirmed #114
Python run; do not substitute local success for GitHub success.

## Still required before customer use

1. Real custom provider creation with durable pre-call intent, frozen quote/policy,
   per-shop encrypted key configuration, capability gating and customer fee consent.
   No arbitrary reference-binding API; unknown-reference recovery from #112 remains.
2. Authenticated refresh/poll/callback processing with transaction-safe observation
   versions, rate budgets, exact reference/order matching and audited reconciliation.
   A timeout must not be resolved by another blind POST. Service failure after a
   concurrent ban/tenant closure may intentionally leave an uncertain marker.
3. Route-level authentication, tenant/owner dependencies, bounded streaming multipart
   ingress, concurrency/CPU/backpressure controls, safe temporary receipt retention
   and deletion, localized stable errors and response minimization.
4. Bot/Mini App capability/state controls, fresh card/payable disclosure and fee
   consent; Admin permission-scoped diagnostics and reasoned recovery, without a
   receipt-trust bypass or secret/raw-card response.
5. Approved fee/settlement/refund rules through existing financial invariants and
   provider sandbox + staging/Telegram-device race/outage tests.

No migration, workflow weakening, merge, provider activation or deploy is included.
