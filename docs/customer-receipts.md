# Customer receipt corrections (F-23)

The existing Telegram staff correction workflow remains the authority. Customers can reopen card payments, read the latest amount/card correction and send a replacement image from the web/Mini App. The receipts inbox creates no payments, orders, transfers or refunds. F-42 order/invoice history is a follow-up in this same customer-payments PR; it is not implemented by the receipts inbox.

## API contract

| Method | Path | Contract |
| --- | --- | --- |
| GET | `/api/webapp/receipts?before=<positive-id>` | Owned shop/customer card payments, descending IDs, at most 20 items, exclusive next_before cursor. |
| GET | `/api/webapp/payments/{id}/receipt-state` | Owned payment, real status, revision, deadline and latest customer-visible correction. |
| POST | `/api/webapp/payments/{id}/receipt-upload` | Multipart file + required expected_revision; row lock, state/deadline check, validated image, shop-bot Telegram storage, existing engine, distinct review job, explicit commit. |
| POST | `/api/webapp/payments/{id}/receipt` | Same guarded pipeline. Initial legacy uploads may omit expected_revision; operator corrections require a matching revision. Returns the actual payment status. |

The existing payment status endpoint also returns provider and order_id, allowing a card payment to reopen without session storage. Status and legacy receipt ownership checks additionally require the authenticated shop. New projections exclude staff IDs, old receipt file IDs, full correction history and arbitrary metadata.

Only pending card payments with no existing receipt and a valid deadline accept uploads. Missing deadlines retain the engine's no-deadline behavior. Malformed correction metadata fails closed. Paid, verifying, closed and non-card payments cannot be uploaded to. The revision covers state and correction changes, preventing a stale view from overwriting a newer decision.

Image bytes must be JPEG/PNG/WebP, at most 8 MiB and 20 million pixels, not animated. Server image inspection is independent of client MIME. If Telegram storage returns no photo ID, no unviewable review is created. The existing engine handles duplicate detection and financial decisions; corrected receipts cannot use trusted-customer auto-approval.

The route refreshes the engine's actual resulting state: paid/duplicate-blocked outcomes are not mislabeled verifying. Only verifying outcomes enqueue review. The dedupe key includes before/after revisions rather than a constant payment key or the correction count capped at ten.

Telegram sending is outside the DB transaction. A chat photo may exist without a recorded receipt if the deadline passes during upload, commit fails or the result is lost. This is not an exactly-once guarantee across Telegram and PostgreSQL.

## UI behavior

- More → Card receipts → existing payment. Keyset paging; an unavailable inbox is not shown as empty.
- Operator notes are literal text. Correction screens hide old checkout destinations and warn against paying again solely because correction was requested.
- Initial checkout destinations remain visible only for matching remembered amount and validated card fields. Missing details require support, not guessing.
- Preview and explicit acknowledgement before upload. Synchronous double-click guard; no automatic POST retries or optimistic verifying state.
- Unknown/malformed/lost upload replies block sending until a new manual server read and new file selection. Local expiry also blocks uploads. Refresh performs no transfer or upload.
- The new UI never falls back to the old upload endpoint. Persian copy for Persian locale; English fallback for other languages. Paid is not proof of service fulfillment.

## Validation and release gates

39 isolated API/core tests, 59 Node model tests and 15 local Chromium mock scenarios passed. Changed screens were checked with strict TypeScript using real UI/API files and mocked app state/unrelated screens. Ruff, formatting and compile checks passed; an isolated route type probe used payment-model contracts.

A real PostgreSQL ownership/security test is included for CI, not run locally. Local checks do not validate PostgreSQL lock races, actual Telegram storage, the full authentication shell, final CSS or live financial behavior. Require full CI Python/security/PostgreSQL tests and frontend build before merge. Staging must exercise concurrent staff correction/upload, expiry mid-upload, more than ten corrections, queue delivery, storage failures, rollback and reconciliation after lost replies.

No production upload, approval, transfer, merge or deployment was performed.

## Compatibility

The old /receipt endpoint remains for initial legacy clients, but now delegates to the same owned-shop/customer, row-locked, byte-validated, deadline-checked pipeline as /receipt-upload. It returns the actual engine status, uses the same revision-based review key and commits before reporting success. Legacy correction uploads without expected_revision fail with receipt_revision_required: an old client must not silently resubmit against an operator instruction it never read. New clients continue using /receipt-upload; they do not fall back silently.

Receipt storage uses telegram.send_photo with ctx.shop.id, matching the shop bot used by staff review delivery. A stopped/unreachable child bot, absent photo or storage exception fails closed; it never falls back to the main bot. Old receipts stored by the wrong bot are not migrated by this patch and need explicit staging/review handling. Telegram upload remains outside the DB transaction; a photo may exist without a committed receipt after an expiry/commit failure.

Regression coverage adds legacy initial/correction compatibility, schema and stale-version rejection, deadline before/during storage, byte validation, actual paid/failed output, duplicate clicks, storage/commit failure and matching storage/staff shop routing. These mocked cases do not replace real authenticated, PostgreSQL/Redis or Telegram staging tests.
