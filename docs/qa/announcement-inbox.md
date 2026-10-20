# Persistent Mini App announcement inbox

## Scope

Shop-wide, immutable plain-text announcements with PostgreSQL first-seen/read state per customer. Separate from the existing derived service/wallet alerts, local alert read markers and Telegram broadcast jobs. No Tonpay, financial operation, live provider or production setting is touched.

Admin navigation: **Announcement inbox**, permission `broadcast.send`, shop-wide only. The existing cookie-CSRF and read-only API-key controls remain authoritative; scoped reseller access is denied even if that permission were supplied. Admin can page publications, preview the exact title/text/kind/window, publish, retry an uncertain publication or revoke it. There is no edit/restore endpoint. Revocation and first publication are audited without copying the body into the audit record.

Customer navigation: the existing Notifications screen contains an independent Announcement inbox section, followed by the unchanged computed alerts section. Empty/loading/error/retry, all/unread filters and bounded keyset pagination are distinct. Opening acknowledges `seen`; reading requires an explicit `Mark read` action. GET has no receipt side effect. The UI shows a mutation as successful only after the server acknowledgement. Plain text is rendered as text nodes, never HTML. Customer changes remount the whole feed; local marketing preference changes also remount the inbox.

## Contract

- `GET /api/webapp/announcements?before=ID&limit=20&unread=true|false`: authenticated customer's shop only; max 50, descending immutable ID cursor, visibility/unread predicates applied before pagination.
- `POST /api/webapp/announcements/{id}/state` with `{action: "seen" | "read"}`: no client-selected shop/customer, monotonic first-seen/read timestamps. Read implies seen. Unknown, expired, future, revoked and cross-shop IDs return the same 404.
- `GET /api/admin/announcements`: same bounded keyset paging, including scheduled/expired/revoked records for that staff shop.
- `POST /api/admin/announcements`: UUID request key, title 1–128, plain text 1–4096, explicit `operational`/`marketing`, optional timezone-aware start/expiry. NULs, unknown fields and invalid windows rejected. Absent start means publication time. A new request cannot have an already-expired window.
- `POST /api/admin/announcements/{id}/revoke`: idempotent, own shop only.
- All successful responses are `Cache-Control: no-store`. No customer-facing DTO exposes the staff author, request key or fingerprint.

The publication key is shop-scoped and fingerprinted over the canonical payload (UTC dates; absent start remains absent in the fingerprint). Same key + same payload returns the original publication, including after expiry/revocation. Changed payload returns conflict, not false success. Unique insertion arbitrates concurrent independent transactions. The UI persists the exact pending payload in owner-scoped sessionStorage before POST; an unknown result cannot create a fresh publication. Explicit pre-write validation/window rejection permits editing. Storage failure prevents sending. Separately intentional keys, different devices or cleared browser storage are not globally deduplicated.

Seen/read upserts preserve their first timestamps. A publication row lock serializes state mutations with revocation. Current customer shop/ban and marketing opt-out are reread for each inbox fetch/state action. Marketing is excluded for opted-out customers; operational notices are independent. Previously delivered text cannot be recalled from another tab or device without a subsequent fetch. No cross-request preference-change serialization or realtime revocation push is claimed.

## Retention and rollout

Migration `0026_announcements`, parent `0025`, creates only `announcements` and `announcement_reads`; no money or existing broadcast data is rewritten. Upgrade database before deploying this API/UI. Roll back application first; downgrade removes the new inbox/history and is destructive, so export/backup if it is needed. Re-enabling the feature after downgrade does not recover read state.

Rows are retained until their shop/customer is deleted, or an explicitly approved later retention migration is applied. Expiry/revocation hides content; it does not purge it. No arbitrary retention duration, cleanup job or automatic Telegram broadcast was introduced. Operators select an expiry explicitly when needed.

This independent branch uses #108 as its base, not #117 and not Tonpay branches. Other unmerged branches add their own migration head after `0025` (e.g. hosted attempts). The revision ID here is distinct, but integrating both requires an explicitly reviewed Alembic merge revision/order and combined migration test; `alembic upgrade head` with multiple heads must not be ignored or weakened.

## Validation and remaining gates

- 24 backend tests passed on isolated PostgreSQL16 / Python3.13 with the entire unchanged existing eight-module ORM package plus these models; real core, audit writer and permission/CSRF/read-only-key checker. Local config, auth/session/lookup and logging infrastructure have controlled seams, so this is not signed-initData or full-application validation.
- The local fixture creates the old schema from real metadata and runs this actual migration. A separate private-schema test performs upgrade → reflected column/nullability checks → downgrade → upgrade. The committed test suite uses the repository's real Alembic `engine` fixture in CI.
- Independent committed PostgreSQL transactions verify once-only concurrent publication and monotonic concurrent read/seen state. Tenant isolation, independent customer state, banned customers, opt-out, schedule/expiry, revocation, unread-before-limit, immutable conflict and audit-once are covered.
- New UI handler tests cover server acknowledgement, escaped text, pending/empty/error, same-tick gates, read-only retry, keyset pagination, owner-scoped reload/replay, storage failure, pre-write rejection, revoke and StrictMode effect replay. The StrictMode regression failed before the fix (23 passed / 1 failed), without a fixture error.
- Exact updated UX command on Node22.23.3: **132 tests passed**, zero failure/error/skip; includes 24 new announcement cases plus existing top-up/history/alert/UI tests. These are overlapping suite counts, not additive unique counts.
- Targeted TypeScript check includes repository strict/noUncheckedIndexedAccess/noUnused/isolatedModules settings and real Mini App dependencies; unrelated Admin session/UI have explicit type-only seams. Not a full clean frontend build.
- Ruff lint/formatter, compile and targeted mypy pass locally. The partial source tree requires explicit `known-first-party=["paurubot"]` for Ruff import classification; no repository rule/config is changed.
- API reference: five rows rendered by the unchanged canonical docgen helpers from actual routes/dependencies, all other bytes preserved. Full `docgen --check`, whole-tree mypy/tests and clean frontend build remain exact-head Actions gates.

No merge/deploy. Before rollout, review the dependency integration/migration heads and perform signed-initData, real staff role/session, accessibility/mobile and multilingual staging QA. Do not describe the entire remaining project backlog as complete.
