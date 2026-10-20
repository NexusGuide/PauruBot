# Pending payment inbox / saved resume

Stacked on #120 -> #112; no Tonpay dependency and no new migration.

Authenticated Mini App GET `/payments` lists only this shop/customer's CREATED,
PENDING and VERIFYING payments, with descending-ID cursor pagination (20 + 1).
It returns a minimal projection, not provider refs, transaction IDs, metadata,
receipt data or checkout secrets. Responses are `Cache-Control: no-store`.

GET `/payments/{id}/resume` rechecks ownership, current status, payment/order
expiry and order ownership/status. It returns only the existing server-saved
checkout snapshot. It never starts/verifies a payment, performs provider I/O,
credits a wallet, reprices, or creates a Telegram Stars invoice link. Malformed,
missing, mismatched-provider/ref and unsafe redirect snapshots fail closed (409).
Only HTTPS URLs without credentials, whitespace, backslashes or malformed ports
are accepted. Uncertain durable hosted attempts have no resume path and are
marked for support, including those awaiting expiration processing.

Wallet has refresh/retry, older/latest pages and synchronous resume click gating.
An unavailable resume does not fall back to top-up. The same payment ID and saved
card/crypto instructions are carried to the existing payment screen. Stale list
responses are ignored. Opening a gateway remains an explicit click, never an
automatic redirect on page load.

## Boundaries / release gates

Legacy payments without a saved checkout can be inspected but are not reconstructed
from changed shop settings. Stars is status-only. Unknown refs remain manual/provider
reconciliation work: accepting arbitrary callback/client references is not recovery.
This is read-only resume, NOT provider reconciliation or payment exactly-once. State
may change after response delivery; status is rechecked on each request, and no new
invoice is created. Provider sandbox, mobile/Telegram, PostgreSQL route tests and full
CI must pass before marking ready. No merge/deploy or financial policy change.
