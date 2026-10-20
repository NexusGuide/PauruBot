# Scoped ordinary top-up checkout intents

## Scope

This change prevents repeated keyed **ordinary top-up** requests from creating
multiple committed payment rows/provider checkout requests in the normal retry and
concurrent-request paths. It is separate from fulfillment/ledger idempotency.
Order checkout, guest purchase and pre-existing unkeyed clients keep their current
behavior. It does not implement external exactly-once delivery or replace provider
verification, receipt review, the ledger, or authorization.

## Server contract

`POST /api/webapp/wallet/topup` accepts optional `intent_key`, 8–128 ASCII characters
from letters/digits/`._:-`. The authenticated shop/customer namespace and SHA-256
hash produce a bounded key for the existing unique Payment.idempotency_key column.
No schema migration. A transaction-scoped PostgreSQL advisory lock serializes the
same scoped key until the caller commits/rolls back; no Redis lease timeout.

The request fingerprint uses requested method, original IRT amount (before random
card tail) and currency. A repeated key must match it. A pending/verifying/paid
payment replays its existing customer checkout snapshot without calling provider
creation again. Closed/expired intents return HTTP 409 `payment_intent_closed`;
changed/corrupt intents return 409 `payment_intent_conflict`. Invalid API input is
422. No server-side automatic creation of a replacement invoice on these errors.
Legacy requests without a key still create a new top-up as before.

The snapshot contains only existing Checkout customer instructions/redirect fields,
not a settings dump or merchant credentials. It stays in private payment metadata;
the customer response remains the existing allowlisted payment view. Do not log
snapshot URLs/contents. Resuming an existing intent does not apply new pricing/card
rotation settings to the old invoice; current authentication and ownership checks
still apply. Paid is replayed as the same payment, never credited a second time here.

## Bot and Mini App

Bot ordinary top-up keys derive from source chat/message, method and amount, scoped
server-side to customer/shop. Different callback IDs from the same button reuse the
payment. A new menu message or changed amount/method is a new deliberate intent.
A callback without a Message falls back to callback ID: no cross-click guarantee in
that unsupported/inline context. Repeated responses may still resend Telegram
instructions/invoice presentations; those are not additional Payment rows.

Mini App Wallet guards both top-up and gift actions synchronously before the first
await (React state alone is not sufficient). All method, amount and gift controls
are disabled while busy, including the already-selected method. An opaque key is
kept per customer/amount/method in sessionStorage, with in-memory fallback when
storage is restricted. Network/5xx/ambiguous failures retain the key, including a
screen reload in the same browser tab. There is no timer-based silent key rotation.
Success clears the key for an intentional new purchase. A 409 closed/conflict shows
localized feedback and clears for a subsequent **manual** click; no automatic retry.
Separate tabs/devices or unavailable sessionStorage after a full reload are not
cross-device intent deduplication. Client keys are not authorization credentials.

## Known boundary / recovery work

A provider can accept invoice creation and the process can die before the local DB
transaction commits. The invoice may then be orphaned and a later retry may create
another external invoice. Durable pre-call intent/outbox plus provider idempotency
and reconciliation are needed to close that crash window; this PR does not claim
that guarantee. Provider/Telegram invoice-link presentation may also run again on
response construction; payment identity/settlement remains the existing engine's
responsibility. Existing payment max-age and webhook verification are unchanged.

## Validation

Local PostgreSQL tests include real four-contender commits (same payload and changed
amount), one provider creation, scoped customers/shops, changed payload conflicts,
expiry/terminal states, card random-tail/instruction replay, legacy behavior,
malformed/corrupt keys/snapshots, HTTP ownership/response privacy and Telegram
same-message/new-callback behavior. Frontend tests execute the actual Wallet
handlers with controlled hook/API seams, plus storage, retry-key and gate tests.
No live gateway/Telegram or staging validation is implied. Preserve the separate
receipt-review fix (#92) when merging the overlapping engine.py changes.
