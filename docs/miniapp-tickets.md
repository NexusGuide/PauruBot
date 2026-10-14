# F-08: customer support tickets in Mini App

Stacked after PR51 -> PR50 -> PR49. No new schema, migration, permission, deployment or flag.
The existing signed Telegram initData API and core ticket/notification workflow remain authoritative.

## Customer workflow

More -> Support tickets (/app/tickets), New ticket (/app/tickets/new), conversation
(/app/tickets/:id). Lists show status and update time, explicit loading/empty/error/retry and manual
refresh; there is no live push/polling claim. Tickets are ordered by descending ticket ID, not
latest reply time. Stable `before` ID cursors expose older tickets (20/page) and older public
messages (100/page, returned chronologically). Refresh returns to the latest conversation page.
Internal staff notes are filtered before pagination, and staff IDs/file tokens are never serialized.

New tickets accept subject (128), text (4000) and a currently active department in the customer's
shop, or General. Whitespace-only subject/text and extra fields are rejected. Replies are text-only,
use the existing core state transition and queue the existing staff notification. Closed tickets
cannot receive replies. Closing requires an inline confirmation; cancel sends nothing. The existing
three-open-ticket limit remains; Mini App creation takes a customer row lock before checking it.
Reply/close take an owned ticket row lock and explicit shop + customer predicates. These locks
serialize these Mini App routes, not every legacy bot/staff writer; broader cross-channel concurrency
and anti-spam/rate-limit hardening are separate work.

## UI safety and deliberate limits

Both fa/en and RTL, labelled native form fields, chronological speaker-labelled messages, escaped
untrusted text, closed-ticket read-only view, fallback navigation and per-thread draft isolation.
Reads abort/ignore stale responses after cursor/route changes. Mutations guard synchronous duplicate
submits and disable controls in flight. Only confirmed success clears a reply or navigates away.
Known validation errors retain text; uncertain/network/server errors retain text and tell customers
to check the list/conversation before resubmitting. No automatic mutation retry. There is no durable
idempotency key or exactly-once claim: a lost response may have committed. Unsent drafts are in-memory
only and are discarded on navigation/reload, as stated in the UI. Closing is not an account deletion.

Attachments are not uploaded/downloaded through Mini App in this iteration: messages show a marker
and direct customers to their bot conversation. No Telegram file tokens or public download URLs are
exposed. No browser notifications, reopening, staff desk/F-14 UI, internal notes UI or SLA promise.

## Verification / release gates

Added authenticated API coverage for ownership (including inconsistent historical shop rows), strict
bodies, active own-shop departments, open-ticket limit, closed replies, cursor boundaries, internal
note and file-token exclusion. Supplemental sandbox browser checks use real React components with
mock HTTP and minimal layout CSS, not live Telegram, real backend or production styles. Full CI and
real main/child-bot staging, actual staff/customer notification delivery and attachment handoff,
mobile/keyboard/full stylesheet review remain release gates. Preserve the PR48 and PR50 test scripts
when eventually integrating the stacked branches. This draft does not merge or run migrations.
