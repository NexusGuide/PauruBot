# F-16: admin web broadcast management

## Scope

Independent, main-based frontend change. Growth's Discounts, GiftCodes and Lotteries remain intact; the existing Broadcasts route re-exports the new module. No backend, migrations, delivery worker, permissions, feature flags or deployed server are changed.

- `broadcast.send` gates reads and controls; the server remains authoritative.
- History is server-paged, 25 items at a time, with manual refresh. A failed read is never presented as an empty history. Stale rows have actions disabled.
- A general, unfiltered reachable count is labeled as such. It is not an exact count for the chosen audience, language, or actual delivery eligibility. Unknown is not rendered as zero.
- The audience selector is exclusive: all / buyers / resellers. A language filter includes customers with no stored language, matching the current runner.
- Text is plain text (the backend escapes it); the browser never renders user text as HTML. This is not F-27's full Telegram message preview.
- Up to six labeled HTTPS/Telegram link buttons. Client validation rejects malformed URLs, credentials, whitespace, controls and backslashes. This does not establish destination safety or make any external request.
- Optional local date/time is reviewed with device zone and UTC ISO timestamp; future / 60-day bounds are checked again at the final click. Ambiguous fall-back DST times use the browser's Date interpretation; the displayed UTC timestamp is authoritative for review. Spring-forward nonexistent local times are rejected.
- Test send requires a separate review and fresh acknowledgement. It sends immediately **only to the signed-in staff member's Telegram chat**, regardless of audience or schedule. The test payload deliberately clears these filters and schedule. Editing invalidates the local test result. A test is not proof of customer delivery.
- Creating a job requires review and fresh acknowledgement. Success means accepted into the queue, not delivered. No send or mutation occurs merely by opening the screen.
- Pause/resume/cancel require separate confirmation. Active batches may finish after pause/cancel. Already-sent messages cannot be recalled. Unknown statuses get no mutation controls.
- Counters are server snapshots. Blocked/deleted are subsets of failed; they are not added again. Queued/paused total=0 is labeled not counted yet. No synthetic remaining-recipient count or delivery percentage.
- Synchronous in-flight guards prevent same-view rapid duplicates. Busy controls and Escape/backdrop cannot dismiss pending work. Browser closure does not cancel server work. This is **not durable backend idempotency**.
- Uncertain create/test results do not auto-retry. Draft is retained in the open dialog. Recent history is read in place; an explicit acknowledgement is needed before another manual attempt. No matching history row is not proof of failure, especially for test sends (which do not create history rows). Request rejection and uncertain response are distinguished. Action-modal errors disable resubmission until the modal is closed and history checked.
- Drafts are memory-only; deliberate discard/navigation/reload can lose them. No content is stored in local storage.

## Validation

- `cd frontend && npm ci && npm run test:broadcasts && npm run lint && npm run build`
- Pure Node/TypeScript-transpile tests cover response projection/validation, page and counter shapes, status actions, exclusive audiences, URL restrictions, Unicode codepoint limits (matching Python), schedule limits and DST gaps.
- Local sandbox Chromium exercises the actual Growth export, Broadcasts module, SessionProvider, CSRF-aware HTTP client and shared Sheet, with production Tailwind/Vazirmatn assets and **fully mocked HTTP**. It is not a deployed admin-shell or Telegram end-to-end test.
- Browser cases include permission denial, no POST on open, test recipient semantics and CSRF, review snapshot/consent reset, scheduled body, validation errors, rapid-click/busy handling, uncertain-result recovery, pause/resume/cancel, 409, GET failures, malformed data, unknown audience, pagination, dirty discard, keyboard focus, Persian/English, 320/390/1200px and dark/light layouts.

## Integration / release gates

1. Keep this Draft PR unmerged until reviewed. Reconcile frontend CI script additions with the other independent F-04/F-22 and Mini App feature branches when integrating; do not silently drop their test scripts.
2. This main-based branch does **not** include the F-15 marketing opt-out delivery changes from the Mini App stack. Integrate/review that backend eligibility policy before enabling production marketing campaigns. Do not claim opt-out enforcement from this UI alone.
3. No real Telegram messages (including staff test sends) were sent during development. A human-authorized staging test is still needed: current-staff chat linking, Telegram failure handling, scheduled enqueue, mixed delivery outcomes, permission/shop boundaries, pause/resume/cancel and worker batch behavior.
4. No backend idempotency key is available. After a timeout, verify history/server logs before another manual submission; never blindly replay requests.
5. Confirm deployment/worker readiness and rollback procedure independently. This frontend PR neither deploys nor enables any flag.
