# Mini App order creation replay

Stacked on #112, not on the Tonpay branches. No migration is required.

`POST /api/webapp/orders` accepts an optional `intent_key`. The validated body
(including defaults, excluding the key) is hashed. A PostgreSQL transaction advisory
lock serializes the shop/customer/key. Existing `Order.params.order_intent_v1` stores
only the scoped key hash and body hash. Replay happens before catalog/discount
validation, returns the original order without repricing, and never creates another
order for a closed/expired key. Changed bodies conflict (409). Paid/provisioning/
completed orders can be read back, never paid again by this endpoint. Unkeyed legacy
callers retain existing behavior. The server's normal request commit owns persistence;
this helper never commits unrelated caller writes.

The checkout URL persists an opaque draft key and its applied label/code. Reload,
Strict Mode effect reruns and explicit retry keep the same key. Deliberately applying
a label/code cancels the old order first and installs a new URL/key. Stale responses
are ignored; synchronous action gating blocks overlapping edits/pay clicks.

## Not covered

Order invoice creation (`/orders/{id}/pay`), guest purchase, legacy bot paths, gateway
unknown-reference reconciliation, and automatic refunds are unchanged. This PR is
order **creation** replay protection, not end-to-end payment exactly-once behavior.
Clearing/replacing the URL draft deliberately creates a new intent. Metadata must be
preserved by any writer replacing `Order.params`; no raw key is exposed in order views.

## Verification / release gates

Run Ruff, mypy, the full security suite and frontend build/tests in CI. Added tests
cover retries, body mismatch, ownership scope, terminal states and independent-session
concurrency. PostgreSQL integration and full browser/Telegram staging verification
must pass before marking this draft ready. Test reload and ambiguous HTTP responses,
label/code edits, paid replay, custom orders and panel-account orders in staging.
