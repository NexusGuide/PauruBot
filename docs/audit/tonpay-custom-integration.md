# Custom Tonpay checkout integration — draft / disabled by default

## Scope and dependency

This is the continuation of **the same PR #116**, stacked on #115 → #114 →
#113 → #112 → #96. It is not an independent main-branch patch and is not deployed.
The custom contract remains derived from Faoxima snapshot `a96ccf23`, not verified
against official documentation, a provider sandbox, real banking settlement or a
Telegram device. Standard Tonpay and custom Tonpay use separate credentials.

The creator still commits a durable uncertain intent before its single external
POST. Customer locking, scoped key/fingerprint conflicts, current-quote replay,
reference binding and preservation of concurrent closure remain unchanged.
The new adapters use those domain operations; they do not implement a second ledger.

## Customer API and UI

All `/api/webapp/tonpay` routes use the existing signed Telegram principal,
shop/customer scope, bans, join and maintenance policy. The adapter releases only
the auth upsert transaction before calling private domain transactions, avoiding a
self-deadlock on the customer's creation lock. No request supplies shop, customer,
role, callback URL, credential, provider reference or authoritative wallet credit.

- `POST /create`: strict requested IRT amount and scoped intent key.
- `POST /resume`: locate that exact owned amount/key locally; never another POST
  to the provider, never guessed reference binding.
- `GET /payments`: latest 20 own custom payments, no external request.
- `GET /{payment_id}`: local sanitized projection, no external request.
- `POST /{payment_id}/refresh`: bounded authenticated provider GET, with local
  version fencing and a fresh configuration/epoch recheck after the response.
- `POST /{payment_id}/consent`: exact current quote revision and payable total.
- `POST /{payment_id}/change-card`: durable mutation claim; no retry wrapper.
- `POST /{payment_id}/receipt`: small strict JSON operation header plus raw image
  body. Authentication precedes body decoding. Actual streamed bytes, not just
  Content-Length, are bounded to 10 MiB; JPEG/PNG/WebP signature/decode limits from
  #114/#115 remain authoritative. JSON bodies are bounded to 4096 bytes and reject
  duplicate keys, extra fields, booleans/floats substituted for integers. Body
  reads time out after 15 seconds. Per-owner and per-shop Redis admission applies.

The per-process image admission semaphore is not a distributed financial lock or
an absolute bound on cancelled background decode work. Proxy/body/header/timeout
controls and runtime load testing remain necessary.

Mini App `/me` advertises custom availability separately from global payment
providers. Only ordinary authenticated Wallet top-ups use this flow; guest buy,
order checkout and Bot callbacks do not silently acquire it. Wallet reuses its
synchronous gate and owner/amount/method intent. Unknown creation can be resumed
with the same key; an unknown outcome never clears the key automatically.

The payment screen displays requested wallet credit, fee and exact total, requires
explicit current consent, and exposes card only from a successful fresh authorized
check. Card/name are ephemeral component state, not DB/sessionStorage/URLs. An
operation or refresh immediately hides old card instructions. A failed read never
leaves a stale card. Changed fee/card revokes consent. Parent keys the subtree by
authenticated owner and local payment ID. Mutation keys include payment, quote,
operation and receipt digest; no raw image is persisted by this adapter. Receipt
upload alone never displays a payment-success claim. File validation in the browser
is a convenience; server validation remains mandatory.

## Secure settings and activation gates

The existing owner-protected `/api/admin/settings` flow stores
`tonpay_custom_api_key` through the existing `secretbox.encrypt("payment", ...)`
mechanism. Browser responses/audit use the existing set/unset secret mask. No key
is included in customer DTOs or this document. All `tonpay_custom_*` writes also
require `panels.secrets`; existing API-key restrictions still strip that right.
No new key-entry form or direct secret-read endpoint is introduced.

Defaults: `tonpay_custom_enabled=false`, `tonpay_custom_policy_approved=false`,
`tonpay_custom_confirm_enabled=false`, empty key/credential epoch, and **zero**
receipt wait/card cooldown/review window (unapproved, not usable timing defaults).
To advertise or create, owner must explicitly supply validated policy, key,
confirmation gate and existing top-up bounds/payment TTL. Timings are strict
positive integers, bounded to 30 days; TTL must exceed receipt wait. The synthetic
600/60/3600-second tests are NOT approved production policy.

Key changes generate a nonsecret credential epoch. Old bound attempts fail closed
on epoch mismatch; rotation does not magically reassign or reconcile old invoices.
Drain/reconcile pending invoices before rotation and retain a recovery plan. A
separate arbitrary credential-epoch edit must not be used to bypass this boundary.
The callback is built only from configured `PUBLIC_URL`, not client data; deployment
must approve that HTTPS origin, reverse-proxy routing and egress policy. This work
has not validated the real production origin or installed credentials.

## Confirmation is not receipt approval or bank-settlement proof

`GET/POST /api/pay/tonpay-custom` is an inert hint/acknowledgement endpoint. It
ignores all query/body paid/order claims and does not credit, create, enqueue or
trigger a provider call. There is no automatic custom provider poller in this PR.
Customer or authorized operator explicitly requests a status refresh.

Only a matching authenticated GET for the bound reference, exact local order ID
and requested amount, `paid=true` **and** `status=completed`, unchanged current
payable quote/consent, no uncertain operation, still-open unexpired local PENDING
payment, and still-enabled approved matching-epoch confirmation policy can call
the existing `engine.mark_paid`. It receives **requested wallet credit**, never
fee-inclusive payable total. Existing CAS, transaction uniqueness and transactional
fulfilment enqueue remain responsible for financial posting; the adapter does not
write a balance or call the ledger directly. Successful confirmation adds audit in
the same transaction. A receipt flag or earlier create response is not authority.

These checks are a gated implementation, NOT proof that the upstream `completed`
contract equals final bank settlement. Official contract/merchant verification,
sandbox reconciliation, fee/currency/refund rules and operational sign-off remain
release gates. Late/closed/changed-quote outcomes fail closed, not silently credited.
Local read-version fencing does not establish provider-global event ordering.

## Ambiguity and operator diagnostics — deliberately incomplete recovery

`POST /api/admin/payments/{id}/tonpay-reconcile` requires `payments.review`, exact
shop and non-scoped staff. A bounded reason and actor are audited and committed
before a GET-only check; diagnostic responses strip card. It has no override,
re-POST, arbitrary reference-binding or manual-credit action.

A known-reference GET **cannot attribute** a previous receipt/card mutation to its
exact intent or image hash. Thus uncertain operations remain uncertain, even if
provider paid/receipt flags are observed. No marker clearing, lease expiry or
automatic replacement key is introduced. Operator diagnostics are not full recovery.

For a lost create response there is **no verified lookup-by-order contract**.
`provider_lookup_unavailable` is surfaced and no reference is invented or accepted
from a customer. The existing uncertainty block on replacement invoices remains.
Official lookup/evidence attribution and a separately reviewed audited resolution
procedure are still required. This is the main unresolved recovery requirement.

## Validation evidence and limits

Original SQLite checkpoint: **132 passed, 2 deselected** (creation + mutation + runtime + API +
settings cases) on Python 3.13, SQLite and minimal ORM/import seams. The two real
PostgreSQL concurrency cases are excluded only in the local harness; committed
tests are not skipped or weakened. API cases execute the production router via
FastAPI/httpx ASGI with auth/rate/domain seams. Runtime confirmation cases mock
`engine.mark_paid`: they prove gate/delegation behavior, not actual ledger/outbox
integration or financial settlement. Settings tests verify encryptor wiring and
masked views, not production key recovery or a real encrypted database round trip.

Frontend: **27 passed** (existing 9 top-up tests plus 18 custom DTO/file/gate cases),
Node 24. Targeted strict typecheck of the changed screens/component/API passed with
older local dependency sources and receipt component declarations; it is NOT an
exact full-repository build. No screenshot/device/accessibility or live provider
QA is claimed. Targeted backend mypy follows imports in skip mode. Ruff lint with
explicit first-party package classification, formatter and compile passed for the
changed Python files. No workflow/check/branch protection was changed.

A valid exact-head GitHub Actions run and review for the whole stack are still
required. No merge, deploy, production enablement, bank transfer or real provider
request was performed.


## Follow-up checkpoint: PostgreSQL, financial rollback and committed risk changes

The same PR now rechecks `card_trust=blocked` inside the domain principal guards,
not only in the earlier API/runtime precheck. It refuses new creation claims,
late reference binding, quote acknowledgement, or a GET response/card disclosure/
confirmation when the blocked state was committed before that guard reload.
Five regression cases failed against the preceding code and passed after this
change (before-claim, before-bind, consent, in-flight unpaid GET and paid GET).
This is **not** a claim that ban/risk changes are globally serializable with every
external HTTP effect or financial mutation. No new medium/high/trusted policy is
invented and no uncertain marker is cleared.

Five further financial cases use the real `engine.mark_paid` and durable
`queue.enqueue` in the committed tests: PAID plus one fulfilment job, repeated
verification, concurrent verified refreshes, enqueue failure rollback, audit
failure rollback, verified retry and reused-provider-transaction rejection.
The concurrent case accepts exactly one paid result and three stale observations;
the durable job is checked in another committed session. Failure injection proves
that PAID, the provider observation and queued job roll back together before a
verified retry. These tests do NOT run the fulfilment worker, ledger posting,
provider banking settlement, or the migration chain locally.

**Current local result: 144 passed, zero deselected/skipped, on PostgreSQL 16.14
and Python 3.13.** The two previous PostgreSQL-only races now execute. Production
custom modules/router/settings and the exact repository `mark_paid` function AST
plus full queue module were executed against minimal ORM/import seams; unrelated
engine imports, auth, settings provider responses and audit recording remain seams.
This is materially stronger than SQLite evidence, not an exact full-app run.
The same 144 cases also pass with **one shared PostgreSQL database and session
loop** across the entire local custom suite (matching the shared fixture lifetime
and loop behavior relevant to CI), rather than resetting tables for each case.
Two pytest plugin assertion-rewrite warnings remain; no test failures or errors.

The shared-fixture experiment exposed an independent test defect: ten invalid-
configuration cases wrongly assumed that no earlier test had committed any
Payment. In a controlled shared run they failed after one successful creation.
Assertions now query the current test shop through session.info instead of
unfiltered whole-table reads. No destructive cleanup, skip, assertion removal or
workflow change was introduced. Runtime/financial modules explicitly import their
fixture dependencies so standalone pytest collection can resolve them.
This experiment is not claimed as the root cause of a specific GitHub run without
its log. Ruff lint/format and targeted import-skipping mypy pass for the changed
production modules; no frontend source changed in this follow-up.

Public documentation search did not yield a verified Tonpays recovery contract;
retrieving the known homepage returned 404 from the page loader. That is not a
live API/availability test and does not prove that a merchant-only lookup API does
not exist. Unknown-reference and uncertain-effect recovery remain unresolved until
official documentation or provider-attributed evidence supplies a valid contract.
There is still no guessed endpoint, arbitrary reference binder, automatic re-POST,
manual credit override, production activation, merge or deploy.


## Public documentation / reference follow-up

Official public custom Telegram documentation is now available at
https://doc.tonpays.online/?mode=custom&sub=telegram . This supersedes the earlier
public-search availability statement above; it is not sandbox or banking proof.
The shared client enforces the documented 20-character order ID bound. Fresh
original receipt claims and `can_receipt` require provider `pending`; amount-
mismatch/remainder flows are not implemented through that original endpoint.
No cancellation/refund/remainder endpoint, automatic retry or marker clearing is
added. Existing custom enablement and confirmation gates remain false by default.

The generated API/settings references had omitted this PR's eleven route-method
entries and eight settings. Refreshed using repository formatter/access rules,
source route constants/names and complete data-only SPECS with exact admin locale
labels. Removing only the eight custom specs reproduces the previous settings
reference byte-for-byte. The complete application/docgen check remains a CI gate.

Earlier Python runs on #114/#115/#116 concluded cancelled; their logs are needed
to attribute the original cancellation. These verified contract/reference fixes
must not be represented as proof of that missing cancellation root cause. Fresh
CI is required; no workflow, timeout, test assertion or check is weakened.
