# Mini App ticket attachments and linked services

## Scope

Customers can create a ticket from a service detail screen or the ticket list, then send an attachment as its own message in the existing conversation. Staff can view these messages and download attachments in the web ticket desk. Existing Telegram image/text attachments are proxied on demand after authorization; Telegram tokens, file IDs and download URLs are never sent to the browser. This does not add a new staff upload composer or promise arbitrary Telegram document compatibility.

## Proposed conservative defaults

- PNG/JPEG: 4 MiB input/output, at most 12 million decoded pixels. Decode/verify/re-encode strips metadata and untrusted trailing content. First frame only.
- Plain UTF-8 text: 512 KiB; disallow control bytes and active markup. PDF, SVG, HTML, archives and executables are not supported.
- At most 16 live attachments / 16 MiB per ticket and 32 MiB per customer in that shop. Customer and ticket locks serialize quota and replay decisions. These are proposed support limits, not changes to financial policy.
- Payloads live in deferred database binary columns, in the message transaction. No temporary files, public object keys or orphan files on rollback. Account for this data in database capacity, encrypted storage and backup policy before rollout.
- Hourly cleanup processes at most 20 old closed tickets per run, deleting binary payloads after 30 days since last ticket activity. It retains expiration markers and request hashes. This is eventual bounded cleanup, not a deletion-time SLA. Active-ticket files remain subject to quotas; legacy Telegram storage and backup retention are not deleted by this job.

## Access and retries

Every customer download/upload checks both customer and shop; message download also matches the ticket and excludes internal notes. Admin downloads require `tickets.read` plus existing shop/reseller scope, and internal notes are restricted to staff. No file bytes are read from Telegram before these checks.

Uploads stream raw request bytes under a hard bound (no multipart parser or client file-ID reference). Responses are private/no-store, attachment-only, nosniff and sandboxed. The frontend fetches with existing authentication and uses short-lived, revoked blob URLs rather than placing credentials in image URLs.

A mandatory upload request key is bound to the raw bytes and MIME. Ticket creation and text replies accept optional keys for old client compatibility; the new UI always supplies them. Repeating a key with identical content returns the saved result even if the ticket was subsequently closed or the attachment expired; changing the content returns 409. Replay does not add another message or notification job. Cancellation only aborts waiting; it cannot undo an already committed message. The upload UI keeps the original File and key in memory for retry until discarded/unmounted. Reload/reselection starts a new request, so inspect the refreshed conversation first. No file data or Telegram auth is written to browser storage.

Service-linked creation accepts a service ID, not a client snapshot. Server checks owner, shop and non-deleted status before saving minimal ID/status/usage/expiry context. It never snapshots subscription URLs, panel credentials, staff notes or arbitrary panel data. Replay returns the original creation context.

## Release gate

This PR is based on `fix/mender-shop-alert-thresholds`, independently of wallet-transfer #117 and inbox #118; it is not intended to be merged straight into an older `main` without the base stack.

`0026_ticket_media` follows `0025`. Before combining sibling feature PRs (including inbox migration), reconcile Alembic heads in the final integration branch. Run an upgrade from an existing database, a fresh upgrade, rollback on a disposable copy, and combined backend/frontend tests. Do not solve sibling heads by silently renumbering an already deployed migration.

Staging acceptance remains required: real Telegram WebView upload/download on mobile, slow network/cancel/lost response retry, cross-customer/shop and staff roles, old Telegram media including provider failure, quota/retention and worker health. This PR must not be interpreted as a merge/deployment approval. Tonpay and all financial workflow changes are out of scope.
