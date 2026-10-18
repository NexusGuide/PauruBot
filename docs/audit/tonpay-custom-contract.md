# Tonpay custom contract client — not an enabled payment method

## Scope and evidence

This is the first isolated implementation increment for Faoxima's **custom** Tonpay
flow, distinct from standard redirect invoices in #113. It is stacked on #113 and
adds only a low-level client, tests and this document. It adds NO registry entry,
setting, public callback/upload endpoint, UI, database mutation, money movement or
production activation. Existing Bot/Mini App/Admin behavior is unchanged.

Source-derived from Faoxima snapshot `a96ccf23a6693bf52313e1f0f78ea000ef702111`:

- [custom helper and lifecycle](https://github.com/Mmd-Amir/Faoxima/blob/a96ccf23a6693bf52313e1f0f78ea000ef702111/re/rx/function/tonpay_custom.php)
- [owner-scoped API handler](https://github.com/Mmd-Amir/Faoxima/blob/a96ccf23a6693bf52313e1f0f78ea000ef702111/api/handlers/TonpayCustomHandler.php)

No official/sandbox contract validation or real invoice/receipt upload has occurred.
Do not describe this as full Tonpay custom support or a completed checkout flow.

## Important financial difference

In this custom flow `request_amount` is the requested value and `final_amount` is
the customer-payable total INCLUDING the provider fee. The client exposes these
as separate immutable `request_amount` and `payable_amount`, with derived `fee`.
It never returns a financial engine Verification or changes the wallet amount.
A future product/ledger decision must explicitly determine how fees are quoted,
consented to and represented. Do not reuse standard Tonpay's final-amount dispute
rule or credit the fee into the wallet just because the response says completed.

`receipt_received: true` means upload acknowledgement, NOT bank settlement or an
instruction to run local trusted-card autoapproval. All statuses and paid flags
are typed provider data, not independent settlement authority.

## Implemented contract and guards

Fixed origin `https://tonpays.online/api/custom/v1`, independent custom API key:

- POST `/invoices/telegram/create`: caller's original order, integer Toman amount,
  authenticated Telegram buyer ID and backend-configured HTTPS callback.
- GET `/invoices/check/{stored_ref}`: exact remote order AND invoice ID required.
- POST `/invoices/{stored_ref}/change-card`: returns updated card/fee data; does
  NOT silently persist or accept a new fee on the customer's behalf.
- POST multipart `/invoices/{stored_ref}/receipt`: generated filename, verified
  image MIME, explicit boolean receipt acknowledgement required.

All calls use bounded streamed JSON, finite timeouts, no redirects, no automatic
HTTP/mutation retries, no raw response/key logs, strict opaque path identities,
strict positive integer amounts and strict boolean flags. Duplicate JSON keys,
wrong references/orders/currency/amounts, fee below zero and malformed data fail
closed. Create/change/upload may omit remote order_id in the source-derived
contract; the returned value remains None (not fabricated). A server GET requires
that identity before any future financial decision. Paid/status consistency and
unknown states must still be handled by the future domain state machine.

429 has a sanitized rate-limited reason with optional bounded numeric Retry-After
hint (0–3600 seconds); this is not a scheduler or permission to replay a mutation.
After any ambiguous mutation, GET/reconciliation must precede another POST.

Receipt bytes are limited to 10 MiB, decoded/verified as one-frame JPEG/PNG/WebP,
and limited to 20 million pixels before raster decode. Validation runs off the
async event loop; cancellation during validation cannot start the HTTP upload.
No URL/path/filename input or persistent file storage. Card fields and image bytes
are excluded from diagnostic reprs. Original validated bytes, including possible
image metadata, are transmitted; this is NOT metadata scrubbing, malware scanning,
receipt authenticity verification or a bounded-concurrency upload service.

Callback URL validation is structural, not DNS/SSRF enforcement. Future callers
must derive the callback from approved deployment configuration, not customer
input, and enforce public-origin/network policy. This module is not a public API.

## Required next implementation increment — before registration or enablement

1. An authenticated shop/customer-owned service must load the original Payment,
   Telegram principal, provider reference and immutable quote. Never accept buyer,
   shop, order, fee, amount or arbitrary invoice reference as authority from clients.
   Explicit capability handling is needed for guest/non-Telegram customers.
2. Persist custom lifecycle state, fee consent and mutation intents BEFORE remote
   create/change/upload; use database locking/CAS, scopes and auditable recovery.
   The client itself cannot enforce cooldown, dedupe or receipt-state permission.
   A failed upload/change may already have succeeded remotely; do not blindly retry.
3. Implement server-side readiness gates, poll budget, card-change cooldown and
   receipt/review/deadline transitions. Faoxima's 600s receipt wait, 60s card-change
   cooldown, 8s UI poll throttle, 24h review/unpaid lifetime and 14-day late-reconcile
   window are source observations, NOT automatically adopted product policy here.
4. A separate customer-only receipt endpoint must enforce owner/tenant/resource
   scope, body/part/file caps during ingress, image validation, concurrency limits,
   safe temporary retention/deletion, audit redaction and provider rate backoff.
   Do not wire this into ordinary manual-card review or expose provider API keys.
5. Bot/Mini App controls must render the same domain state, disclose payable fee,
   prohibit stale/duplicate actions and never imply receipt upload means paid.
   Admin support needs permission-scoped diagnostics/recovery, not raw card/media
   secrets or an arbitrary reference-binding/money-posting endpoint.
6. Integrate authenticated settlement through existing financial invariants, define
   fee/refund policy, and exercise callback/poll/recovery races on PostgreSQL and
   provider sandbox. Unknown-reference recovery from #112 remains open.

Keep draft/unregistered. No migration, merge or deployment was performed.
