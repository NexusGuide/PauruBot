# Durable custom Tonpay invoice creation — original design record

> This records the original unwired creator commit `8c0ef8b4`. Its statements
> about missing adapters describe that commit only. The same PR #116 now adds
> gated API/UI/settings/confirmation and GET-only diagnostics. See
> [current integration scope and remaining recovery gates](tonpay-custom-integration.md).
> The creator invariants below remain applicable; production is still disabled.


## Scope and dependency

Stacked on #115 (which depends on #114/#113/#112/#96), not an independent main
branch patch. Adds `tonpay_custom_create.create_once` for **ordinary custom Tonpay
wallet top-ups only**. It produces the bound state consumed by #115. No provider
registry/settings entry, encrypted-key configuration, customer route, callback,
poller, UI, settlement, operator recovery or production activation is added.

Contract is still the source-derived Faoxima snapshot a96ccf23 described in
[custom contract](tonpay-custom-contract.md), NOT an official API/sandbox test.
The fee model separates `Payment.amount` (requested wallet credit in IRT) from the
provider's payable amount (including fee). No financial entitlement or settlement
rule is inferred from a create response. A paid flag is observation only.

## Trusted creation configuration

`Owner` comes from an authenticated principal. The future adapter MUST load that
shop's approved policy, encrypted custom API key and configured public callback;
construct the client with the matching key; and keep all of these off request
bodies. The configuration object is not authentication, RBAC or secret storage.
`enabled` defaults false and the configuration shop must exactly match the owner.
`credential_epoch` is a nonsecret configuration revision, NEVER the key itself.
No actual application configuration flag or secret field is installed here.

An explicit immutable `CreationPolicy` supplies minimum/maximum requested amount,
invoice TTL, receipt wait, card cooldown and review window. There are no product
policy defaults. Tests use synthetic values, not approved production timings.
Integers reject bool/float, amount must satisfy the approved bounds, timing values
are bounded to 30 days and invoice TTL must exceed receipt wait. This does not
establish the provider's actual expiry contract. Callback syntax is checked by the
contract client; future deployment origin policy must constrain it further.
The callback URL must be server-configured, never an arbitrary customer URL.

## Persist before POST; fail closed after uncertainty

1. Reload the committed active shop and its exact non-banned, positive-Telegram-ID
   customer. Lock the durable customer row inside a private transaction. This
   serializes initial claims even when the requests use different keys.
2. A scoped SHA-256 key identifies the request. Fingerprint includes requested IRT
   amount, explicit frozen policy, nonsecret credential epoch and callback hash.
   Changed same-key fingerprint conflicts; completed replay never calls create.
3. A bounded existence query also blocks **different** keys if any custom creation
   for this owner is uncertain/missing a ready marker, regardless of local status
   or expiry. No history rows are loaded/evicted en masse and no marker is cleared.
   This is intentionally conservative: an unresolved attempt blocks future custom
   creation for that customer until a separately designed audited recovery process.
   It does not serialize all other payment providers or dedupe across devices that
   intentionally use new keys after a known completed creation.
4. Commit CREATED Payment + independent uncertain token/request snapshot BEFORE
   sending the single HTTP POST. The returned local payment ID becomes order_id;
   buyer_chat_id comes from the reloaded database principal. No caller session is
   accepted or committed. Release DB locks before HTTP, with no retry wrapper.
5. Recheck principal and lock the original row. Validate identity/request amount,
   token and types. Serialize binding per shop and reject a reference already bound
   to another custom payment in that shop. A changed/unknown reference cannot be
   supplied through a new API; the only binder is this private creation completion.
6. Persist the exact create response's reference, quote and provider observations,
   initialize #115 state with no fee consent, and switch CREATED to PENDING only
   if still open/unexpired. Concurrent admin closure/expiry and unrelated metadata
   are preserved; closure/expiry/paid/unexpected state signals needs_review.
   A late successful binding retains evidence without resurrecting a closed row.

The contract client permits an omitted order_id on create; a present ID must match.
This binding trusts only the in-process reply to the original POST, not a callback
or customer-provided reference. Future authenticated GET checks require order_id.
A receipt/card quote revision advanced since creation is read from current state
on replay; replay cannot restore the initial fee or implicitly acknowledge it.

Timeout, error, cancellation, crash or failed final commit leaves a durable uncertain
marker and no safe automatic re-POST. Concurrent customer ban/shop suspension may
also intentionally prevent binding, leaving the same blocked state. This is not a
serializable authorization-vs-already-in-flight-HTTP guarantee. No watchdog/lease
expiry can replace the original attempt. Unknown-reference recovery remains open.
A new key after a known ready creation can intentionally create another invoice;
future adapters need explicit UX, rate budgets and active-invoice product policy.

## No financial or sensitive-data side effects

Never set PAID, credit balance, post journal, enqueue fulfilment or treat receipt
acknowledgement as settlement. The requested amount is not replaced by payable.
A successful create response is not authoritative bank verification.
The result exposes only local payment ID, current quote revision/request/payable
amounts and needs_review. No card, key, callback URL, receipt bytes, raw response,
remote invoice reference or provider error body is returned/persisted by this module
apart from the bound reference stored in scoped Payment.provider_ref/state.
Only callback hash and nonsecret credential epoch enter the request fingerprint.
These hashes/references still require scoped projection and retention controls.

No migration/index/unique constraint is introduced. The per-shop binding lock is
an application invariant for this creator, NOT a database uniqueness guarantee for
arbitrary future writers. Generic financial/polling APIs must not be assumed ready
for an unregistered custom provider. It remains internal and unwired.

## Evidence and release gates

- New file: 45 test cases, including one real PostgreSQL initial-claim race with
  four different keys. Committed tests have no skip marker for that race.
- Local: **44 creation tests + 40 existing mutation tests = 84 passed**. Two
  PostgreSQL races (one in each test file) were excluded ONLY in the local runner.
  This uses exact production creator/service/client code and repository enums, but
  SQLite/minimal ORM/import seams. NOT full app, migrations, actual row-locking,
  Redis, provider sandbox, deployment or staging evidence.
- Tests cover owner/tenant/ban/guest/suspension, disabled/invalid configuration,
  exact fingerprint conflict, committed pre-call visibility, same/different key
  ambiguity, expiry, caller rollback, initial/final commit failure, concurrent admin
  changes, invalid response, duplicate reference, quote revision/consent and paid
  flags. One HTTP-level test uses the actual client with httpx.MockTransport only.
- Ruff lint/formatter, compile and targeted mypy with imported modules skipped are
  separate local checks, not full-project CI or aggregate suite totals.
- Base #114 Python was CANCELLED, cause not established from check metadata;
  #115 Python was still IN_PROGRESS at the implementation checkpoint. Their
  frontend jobs were successful. No assertion that the stack is all-green.

Still needed before customer use: approved shop policy/key/callback settings and
capability wiring; bounded authenticated API/multipart ingress; GET observation
ordering/reconciliation and audited uncertain-operation recovery; Bot/Mini App
card/fee disclosure and explicit consent UX; permission-scoped admin diagnostics;
financial settlement/refund decisions; sandbox/staging/Telegram-device QA and
successful review/CI for the entire dependency stack. No merge or deploy performed.
