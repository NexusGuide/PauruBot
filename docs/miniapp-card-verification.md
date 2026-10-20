# Mini App card verification

## Scope

The wallet's **Card verification** screen registers card evidence for the existing shop staff review flow. A `card_verify_required` response from wallet top-up or order checkout opens the same screen. The checkout link retains the same owner-scoped draft URL; arbitrary/cross-owner return redirects fall back to the wallet. After approval the customer must deliberately return to checkout and retry; no payment is automatically created, resumed, credited or approved by registration.

- `GET /api/webapp/cards/verification`: current policy, independent approved flag, submit eligibility and up to 20 recent own masked rows.
- `POST /api/webapp/cards/verification`: strict JSON `{intent_key, number, image}`; `image` is base64, not a URL/data URI. Success returns `{card: {id, masked, status}}`.
- Both require existing signed Telegram initData, shop resolution, ban/forced-join/maintenance checks. No client-selected principal. Responses are `no-store`.
- Registration remains separate from receipt upload. The existing `payments.review` notification and card review handlers decide approval/rejection; there is no new customer approval endpoint or new staff permission.

## Input and privacy

Authenticate before reading the request body. Body is streamed with a 4 MiB + 1 KiB cap and 15-second read deadline; compressed bodies, duplicate/unknown fields and coerced numbers are rejected. Per-customer/shop registration rate is 10 requests per minute, in addition to the existing Mini App rate limit. The number accepts ASCII/Persian/Arabic digits with spaces/hyphens and must be 16 digits with a valid Luhn checksum. Errors never echo input fields.

Only JPEG/PNG, at most 3 MiB decoded, single-frame, 4096 pixels per side and 12 million pixels total. Pillow verifies and decodes the photo in a worker thread, then re-encodes pixels into a fresh JPEG without EXIF/text/profile metadata. Originals, filenames and raw numbers are not saved to the database or browser storage. The database keeps the existing masked number/shop-scoped digest and Telegram photo token, plus an opaque intent key and hashed request fingerprint. The customer API never returns photo tokens, digests, staff identities or cross-customer card matches.

The sanitized photo is sent to the customer through the authenticated shop bot, then attached to the existing staff notification. This is evidence of a requested review, **not proof of card ownership by itself**; staff must apply the shop's existing verification policy. Do not upload unrelated sensitive information. Telegram retains the sent photo according to its own behavior; this change does not claim to erase Telegram messages or implement a new evidence-retention policy.

## Retry and concurrency

A shop/customer-scoped unique intent key and exact request fingerprint give durable replay: same request returns the original card, including after staff review; changed payload with the same key returns 409. The common card registration service locks the customer row before checking pending cards so Telegram and Mini App submissions cannot race into two pending rows. POST checks ownership, current trust/policy and existing pending/approved state before Telegram upload.

The UI gates synchronous double clicks before file reads, keeps uncertain submissions unchanged in memory and ignores stale/unmounted responses. It never stores the number/photo in local/session storage. An uncertain result can be explicitly retried with the same request; locale changes do not clear it. Reload loses the in-memory photo; refresh status before deliberately creating another request. Server pending checks remain authoritative.

Telegram transmission and the database commit are not a distributed transaction: an upload timeout or database failure may leave a customer-visible photo without a registered card. Such a photo grants no approval or financial effect. Retry may resend that photo if no card transaction committed. An already committed request is replayed without another photo or notification. No automatic deletion/compensation guarantee is made.

## Migration and release gates

Migration `0026_card_verification` follows `0025`; existing Telegram rows retain NULL intent/fingerprint fields. Model and migration share `uq_verified_cards_intent`. This is a sibling of the announcement/ticket-media migrations in #118/#119, not a replacement or renumbering of them. Before integrating all branches, create/review the appropriate Alembic merge revision and run upgrade/rollback checks with the combined schema; do not rename migrations already applied to a database.

Stacked on #122 (#112 -> #120 -> #122). Keep draft until combined-stack review, exact-head CI and real Telegram/mobile/staging QA (including staff role checks, rejected/approved notifications, disconnect/retry and reseller bot isolation) succeed. No settings are activated, no real provider request is made, and no merge/deployment is part of this PR.

Local checks cover pure photo validation, actual route/core behavior with controlled SQLite/auth/settings/queue/Telegram seams, UI model/actual handler tests, limited new-screen typechecking, Python compile and changed-file Ruff. Full PostgreSQL concurrency, application auth integration, migration and full-app typing/build are CI/staging gates, not claims based on the local seams.

## Still outside this PR

Order/guest/legacy payment creation and authentic unknown-reference recovery, renewal before/after previews and capability/policy-constrained actions, legacy bcrypt reset/change hardening, and decisions on trusted hard caps, permission separation and step-up remain separate work. This PR does not close that broader backlog.
