# F-14: staff ticket desk in the admin web panel

## Scope and permissions

Independent main-based frontend-only Draft PR. No backend, schema, worker, migrations, feature flags or deployment changes. No real customer messages, staff Telegram messages or panel probes were performed.

The existing People screen's reseller ticket list/detail and new-ticket flow remain unchanged. Exported route wrappers send only non-reseller staff with `tickets.read` to the new desk; ticket detail is keyed by route ID to avoid draft leakage between tickets. Backend ownership/permission checks remain authoritative.

- `tickets.read`: ticket list/detail, department lookup, public and internal staff-visible history as returned by the existing API.
- `tickets.reply`: public reply, close, internal note, priority/department/assignee changes, and saved-reply lookup. A read-only user gets no mutation or canned-reply controls/requests.
- `settings.write`: department and saved-reply management. Saved-reply lookup additionally requires `tickets.reply`, matching the API.
- No request to `/api/admin/staff` or assumption of `staff.manage`. Assignment supports own staff ID, clearing assignment and a manual positive staff ID. The backend validates that an assignee is active, non-reseller and in the same shop. No staff directory is invented.

## Desk behavior

- Every filter is sent to the server: status, priority, department (`0` means no department), assignee (me/unassigned) and awaiting-staff flag. Changing a filter resets URL-backed page selection. Server pagination uses 25 items; no client-only whole-database filtering.
- Awaiting state comes from the server; waiting timestamp is displayed without inventing an SLA or exact wait metric.
- Failed reads are not shown as empty results. Stale detail controls are disabled. Unknown ticket statuses are displayed without enabling mutation controls. Invalid numeric routes make no ticket-detail request.
- Detail shows up to the latest 200 messages, oldest first; earlier history is explicitly unavailable in this UI. Attachments remain in the Telegram bot; no Telegram file IDs or invented download links are rendered.
- All subjects, names, messages, notes, saved replies and errors are plain React text; no HTML execution or HTML message preview.
- Internal notes are unmistakably marked staff-only, in both history and review. They do not notify customers, change ticket status or count as answers. Closed tickets can still receive internal notes; public replies are disabled.
- Public replies are reviewed separately and require fresh acknowledgement. Success means saved plus notification queued, not confirmed Telegram delivery.
- Saved replies append to the in-memory draft, never auto-send. `{name}` and `{ticket}` substitution follows backend sequential substitution and Python-compatible 4000-codepoint truncation; unknown tokens stay literal. Combined drafts over the limit cannot be sent. UTF-16 length is not used for limits.
- Metadata updates are sparse PATCH bodies. Unchanged fields are omitted; department/assignee clearing sends explicit `null`. Editing only priority cannot accidentally remove assignment or routing.
- Closing requires a separate confirmation and warns that public replies will be blocked. No reopen endpoint is invented.
- Department CRUD preserves sort/activity/log routing. Negative Telegram chat IDs are supported, thread IDs must be positive integers. Deletion explicitly warns that tickets lose their department and saved replies become shared; deactivation is offered as the safer alternative in the warning. Saving settings does not test a log chat or send a Telegram message.
- Saved-reply CRUD is management only and does not call a customer reply endpoint. Department deletion and saved-reply deletion are reviewed before DELETE requests.

## Mutation and draft safety

Every new desk mutation goes through a review dialog, exact request snapshot and fresh acknowledgement, with synchronous in-flight locking, disabled controls, trapped keyboard focus and busy dismissal guards. Browser closure cannot cancel server work already started.

No automatic mutation retries. Network/5xx/malformed-success results are uncertain rather than claimed successes. Rejected requests are distinguished. The failed review cannot resubmit: after dismissal, write controls stay locked until the relevant data was successfully refreshed and the operator explicitly acknowledges checking it. Draft text remains in memory. A missing history message is not proof that an uncertain request failed. There is no backend idempotency key, optimistic concurrency token or exactly-once guarantee.

Drafts are not persisted or written to local storage. Leaving a ticket/settings editor, reloading, or deliberate cancellation can discard them; this is stated on the form. Navigation is not a durable draft recovery mechanism. The legacy reseller workflow is preserved, not retrofitted with every staff-desk guard.

## Validation and remaining release gates

- `cd frontend && npm ci && npm run test:tickets && npm run lint && npm run build`
- Node tests cover response validation/projection, internal-note identity, list/detail bounds, Unicode codepoint limits, numeric Telegram IDs, canned rendering, sparse PATCH/null semantics and filter request construction.
- Local exact-repository TypeScript safety flags apply to the new staff desk and its available dependencies. A production Tailwind/Vazirmatn harness uses real new desk modules, SessionProvider, CSRF-aware HTTP client and shared Sheet with fully mocked HTTP. Full repository integration/type/build validation runs in PR CI.
- Browser coverage is for new staff modules, not the full admin shell or legacy reseller flow. Staging still needs human-authorized checks for shop/permission isolation, notification delivery and Telegram attachments, stale concurrent operator edits, assignment eligibility, department deletion effects, worker availability and rollback.
- There is no automatic draft persistence or server concurrency protection; operators must review fresh data when another staff member may have changed the same ticket/settings.
- Reconcile independent branches' frontend package/CI test-script additions when integrating F-04/F-16/F-22 and the Mini App stack. Keep Draft/unmerged until reviewed. No deployment readiness is claimed by this UI work.
