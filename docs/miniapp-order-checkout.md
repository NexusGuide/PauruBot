# Durable Mini App order checkout

## Scope
The authenticated `POST /api/webapp/orders/{order_id}/pay` now reserves one provider-creation attempt per **already committed, owned order**. Wallet settlement retains its existing path. Bot checkout, guest checkout, unkeyed top-ups and other callers of `engine.pay_order` are not rerouted by this change.

The order ID is the durable operation identity; no browser key is needed. A private transaction locks the owned order and checks every existing payment before creating anything. A matching ready attempt replays its saved checkout. Changing method/order price, legacy or multiple payment rows, and closed attempts fail closed. There is deliberately no automatic replacement invoice for the same order, including after failure/expiry; this availability restriction must be reviewed before release, and the UI must not interpret an uncertain attempt as permission to pay again.

## Commit and crash boundaries
- Validate the current order/customer, provider, bounds and card-verification requirement before reserving. Reject uncommitted/edited caller orders; never commit unrelated caller changes.
- Compute the wallet shortfall once, persist the payment as `CREATED` with `order_attempt_v1.state=uncertain`, and extend a still-pending order's deadline to the payment deadline in the same private transaction.
- For NowPayments/Plisio, freeze USD units and the existing hosted-attempt marker before that commit. Other providers retain their existing conversion/creation contract; a conversion failure after reservation also fails closed.
- Commit **before** provider creation. Cancellation, a lost response or a failed result commit leaves an uncertain durable row. Retries and method changes do not issue another creation request.
- Save the returned checkout/reference under fresh order/payment locks. Preserve concurrent payment/cancellation/expiry decisions; do not reopen an order or credit funds. A ready response after order cancellation is retained as evidence but not returned as a payable order checkout.
- Telegram Stars invoice-link creation is part of the reserved attempt and uses the shop's bot. Successful retries reuse the persisted link instead of creating another link in response serialization. If link creation is ambiguous, no automatic re-creation occurs. This does not assert Telegram prevents a customer from reusing a known link; charge verification and fulfillment remain separate.
- No attempt is a distributed transaction with a provider. Unknown-reference recovery remains a separate task. No client-supplied reference, unsigned callback or synthetic successful response is accepted as recovery evidence.

## Pending payment inbox
The existing read-only inbox recognizes uncertain order attempts as `review`, without payment instructions. Known ready checkouts use the existing ownership/status/expiry checks. Stars remains status-only in the read-only inbox; this PR does not add a Stars resume feature there.

## Validation and release gates
Tests cover independently committed PostgreSQL reservations, cancellation/timeouts, pre/post-call commit failures, in-flight retries/method changes, existing legacy rows, ownership, changing wallet balance, order edits, terminal decisions, actual card metadata and cached shop-bot Stars links. A full-app API test covers authentication, cross-customer denial, card order replay and method conflict. Pure projection tests cover the new uncertain marker.

Local SQLite checks use actual Order/Payment classes, actual payment preparation/replay and the new module with controlled unrelated dependencies. They are **not PostgreSQL concurrency or full-app evidence**. Exact-head CI and real Telegram/mobile/reseller/staging QA are required before release. No schema migration is added here; this branch still inherits the card-verification migration and its combined-stack release gates from PR #123.

## Still open
- Guest/Bot/legacy checkout coverage, unkeyed top-ups and cross-channel races with those non-participating paths.
- Authentic provider recovery after unknown invoice/reference, including callbacks arriving before reference persistence.
- Operator resolution for ambiguous/closed attempts without duplicate charging; the PR does not create a manual force-retry button.
- Precise renewal preview/capability-constrained actions, legacy bcrypt reset/change hardening, and approved trusted-cap/permission/step-up policies.
