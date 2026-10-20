# Durable hosted-invoice top-up attempts — partial S09

## Scope and release status

Follow-up to #96, based on its branch, not a second copy of that PR. Applies ONLY to
keyed ordinary top-ups through NowPayments and Plisio. Unkeyed clients, order/guest
checkout and all other providers retain #96's contract. No migration, new endpoint,
ledger change, automatic refund or workflow change.

This is a draft safety increment. It is NOT external exactly-once delivery and does
NOT complete provider-specific automated reconciliation. Do not deploy it as a full
S09 solution. Staging and operator recovery acceptance are release gates.

## Transaction boundaries

The owning principal must already be committed. A separate session reloads the owner,
validates the method/amount and creates a Payment under the existing scoped unique
key and PostgreSQL advisory transaction lock. Its fingerprint and an `uncertain`
attempt marker and outgoing USD amount/currency are committed BEFORE provider creation.
Creation consumes the frozen conversion instead of requoting after the commit. The request session is never
committed or rolled back by this service, and its unrelated writes remain its own.

Only the request which inserted the attempt may call invoice creation. Other requests
observe the durable marker and fail closed, including when the first request was
cancelled, timed out or terminated. There is no lease expiry permitting another POST.
No exception text, merchant configuration or raw provider response is copied into
metadata. Existing integer IRT amounts and provider amount/currency are unchanged.

A successful creation stores the checkout snapshot and reference in a second commit.
A row lock and refresh preserve concurrent expiration/rejection/dispute; no closed row
is resurrected. The successful snapshot is replayed by later identical requests.
A failure of the second commit leaves the first durable marker as recovery evidence.
No credit is made here: existing verified-payment/ledger paths remain authoritative.

## Client contract for ambiguity

An uncertain attempt returns the existing `gateway_error`, NOT
`payment_intent_closed`/`payment_intent_conflict`. Current Wallet clients clear keys
only on those explicit 409 codes, so uncertainty retains the same opaque key. An
in-flight duplicate also gets `gateway_error`; a later retry after success resumes
its snapshot. This deliberately favors safety over automatic availability.

A different explicit key, tab/device or different amount remains a distinct intent.
Cross-device deduplication and preventing every deliberate new invoice are not claimed.

## Recovery and deployment gates — still OPEN

- Locate uncertain payments using private metadata `hosted_attempt_v1.state` and the
  existing payment/shop/customer/provider identity. Do not expose merchant data or
  this metadata in customer responses. No public recovery or arbitrary-ref binding API
  is introduced.
- An uncertain marker does not prove a provider invoice exists: process death might
  precede its POST. Conversely, timeout does not prove rejection. Never automatically
  create a replacement, clear the key, credit funds, or treat expiry as proof of no charge.
- Implement and validate provider-authenticated lookup by original order/payment ID,
  exact amount/currency/reference matching and audited attach/replay before claiming
  automatic reconciliation. If lookup cannot prove identity, keep manual review.
- Existing callbacks require the stored reference and amount. After a crash before the
  second commit, callbacks alone cannot recover that missing reference. This limitation
  is not bypassed by trusting an incoming webhook's invoice ID.
- New customer/setup writes must commit in their owning workflow before hosted checkout.
  Independent sessions must use the correct database/tenant deployment configuration.
- Pool capacity, DB outage, actual provider sandbox, callback races and operator recovery
  must be exercised on staging. Keep the durable markers on rollback; reverting to #96
  must NOT remove uncertain rows or retry them with fresh keys.

## Validation

Regression tests use independent PostgreSQL sessions and committed isolated schemas:
pre-call visibility, caller rollback, provider failure/timeout/cancellation, in-flight
retry, terminal decision/expiry races and amount validation. Providers are fakes; no
live invoice, settlement or Telegram-device validation is claimed. Local static checks
are separate from the full GitHub Actions result recorded in the PR.
