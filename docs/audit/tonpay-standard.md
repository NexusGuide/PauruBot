# Tonpay standard invoice adapter — draft

## Identity and evidence

This adapter targets **tonpays.online**, the standard Tonpay gateway used by Faoxima,
NOT TON blockchain Ton Pay, a Jetton SDK, or a similarly named gateway.

Contract evidence is source-derived from Faoxima snapshot
`a96ccf23a6693bf52313e1f0f78ea000ef702111`:

- [standard create/check helpers](https://github.com/Mmd-Amir/Faoxima/blob/a96ccf23a6693bf52313e1f0f78ea000ef702111/re/rx/function/database_helpers_2.php)
- [server-verified callback](https://github.com/Mmd-Amir/Faoxima/blob/a96ccf23a6693bf52313e1f0f78ea000ef702111/payment/tonpay.php)
- [standard poller](https://github.com/Mmd-Amir/Faoxima/blob/a96ccf23a6693bf52313e1f0f78ea000ef702111/cronbot/tonpaycheck.php)
- [separate custom receipt mode, NOT implemented here](https://github.com/Mmd-Amir/Faoxima/blob/a96ccf23a6693bf52313e1f0f78ea000ef702111/re/rx/function/tonpay_custom.php)

Official documentation probes did not yield a usable contract. No live provider,
sandbox invoice, bank settlement or production credentials have been tested. Keep
this disabled by default and the PR draft until the release gates below are met.

## Standard contract

Fixed origin `https://tonpays.online/api/v1/invoices`, no configurable API URL.
`X-API-Key` authenticates both calls:

- POST `/create`: integer Toman `amount`, local Payment ID as string `order_id`,
  public `/api/pay/gw/tonpay?pid=...` callback. Optional buyer identity is omitted.
- Response: bounded opaque `invoice_id`, HTTPS `invoice_url`. Only
  `tonpays.online` or `t.me` host, no credentials/nonstandard port. Never fetch the
  checkout URL internally. Additional documented checkout hosts require review.
- GET `/check/{stored_invoice_id}` verifies payment. Callback fields only locate
  the local payment; any supplied conflicting order/invoice/key blocks the read.
  Missing callback key still requires the authenticated GET. A matching key alone
  never pays. No caller-provided API endpoint or reference-binding endpoint.
- The GET order must equal the local Payment ID. If remote invoice_id is present,
  it must match too. Require JSON boolean `paid: true`, `status: completed`, exact
  requested amount and a positive integer final amount. No truthy string/boolean
  numeric coercion, decimal rounding, missing-final fallback or Rial guessing.
  Optional currency, when returned, must be `IRT`.
- Actual final amount is passed to the existing engine. Both underpayment and
  overpayment become DISPUTED for staff; they do not silently change wallet credit.
  Exact settlement uses the existing unique provider transaction/CAS/outbox path.
- Polls use the same authenticated GET. Pending/need_action/unknown/expired remote
  states never grant money or independently close a local payment. Existing local
  expiry/late-credit window remains authoritative. HTTP errors/429 wait for a later
  poll, with no create retry; no provider-specific Retry-After scheduler is claimed.

The new adapter streams at most 64 KiB of response body, does not follow redirects,
and uses existing finite HTTP timeouts. It does not log response bodies or keys.
This is not a claim that every existing provider or ASGI/proxy body cap is hardened.

## Configuration and channel integration

Per-shop `tonpay_enabled` defaults false; `tonpay_api_key` is a secret setting,
stored encrypted and redacted by the existing settings service. PUBLIC_URL and
key are required for availability. Admin settings and Bot/Mini App method names
have English/Persian labels. The existing generic gateway callback, discovery,
poller and financial engine are reused; no new public route, schema migration,
balance write, authorization bypass or workflow exception.

## Durable creation boundary (depends on draft #112)

Keyed ordinary Tonpay top-ups use #112's independently committed attempt before
POST. Their frozen amount is Toman (IRT), not a USD FX conversion. Only the creator
sends POST. Same-key retries replay a saved checkout or retain the uncertain
attempt with gateway_error; they do not recreate it or clear Wallet's intent key.

Unkeyed clients, order/guest checkout and different keys/devices are NOT covered
by that guarantee. Process death after provider acceptance but before reference
persistence leaves an uncertain attempt: callbacks cannot bind an unknown ref.
No authenticated lookup-by-order/reconciliation/operator recovery was invented.
Do not resolve this by manually substituting a fresh idempotency key or re-POSTing.

## Release gates and exclusions

- Confirm API contract, JSON types/units, checkout hostname(s), invoice expiry,
  status values and API-key/callback behavior with official docs/provider sandbox.
- Test delayed callbacks, rate limits, provider outages, late payment, amount
  discrepancies, account/key rotation and operator recovery in isolated staging.
- Complete authenticated uncertain-attempt recovery from #112, with audited exact
  order/reference/amount/currency matching. No automatic rebinding or credit.
- Integration-test the stack (#96 then #112 then this PR); preserve receipt guard
  #92, streaming callback cap #93 and independent Wallet/history/alert fixes when
  combining branches. Passing this stack's CI does not verify all other PRs together.
- No custom API/Telegram receipt upload, card change, custom review UI, automatic
  refund or external exactly-once guarantee. Faoxima's custom route is a separate
  feature and must not be emulated with the local manual-card receipt workflow.
- No merge, enablement or deployment in this change. Do not weaken existing checks.
