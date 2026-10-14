# F-16: admin web broadcast management

## Scope

Independent, main-based frontend change. Growth's Discounts, GiftCodes and Lotteries remain intact; the existing Broadcasts route re-exports the new module. No backend, migrations, delivery worker, permissions, feature flags or deployed server are changed.

- `broadcast.send` gates reads and controls; the server remains authoritative.
- History is server-paged, 25 items at a time, with manual refresh. A failed read is never presented as an empty history. Stale rows have actions disabled.
- A general, unfiltered reachable count is labeled as such. It is not an exact count for the chosen audience, language, or actual delivery eligibility. Unknown is not rendered as zero.
- The audience selector is exclusive: all / buyers / resellers / active / inactive / never_bought. A language filter includes customers with no stored language, matching the current runner.
- Text is plain text (the backend escapes it); the browser never renders user text as HTML. This is not F-27's full Telegram message preview.
- Up to six labeled HTTPS/Telegram link buttons. Client validation rejects malformed URLs, credentials, whitespace, controls and backslashes. This does not establish destination safety or make any external request.
- Optional local date/time is reviewed with device zone and UTC ISO timestamp; future / 60-day bounds are checked again at the final click. Ambiguous fall-back DST times use the browser's Date interpretation; the displayed UTC timestamp is authoritative for review. Spring-forward nonexistent local times are rejected.
- Test send requires a separate review and fresh acknowledgement. It sends immediately **only to the signed-in staff member's Telegram chat**, regardless of audience or schedule. The test payload deliberately resets kind to all and clears these filters and schedule. Editing invalidates the local test result. A test is not proof of customer delivery.
- Creating a job requires review and fresh acknowledgement. Success means accepted into the queue, not delivered. No send or mutation occurs merely by opening the screen.
- Pause/resume/cancel require separate confirmation. Active batches may finish after pause/cancel. Already-sent messages cannot be recalled. Unknown statuses get no mutation controls.
- Counters are server snapshots. Blocked/deleted are subsets of failed; they are not added again. Queued/paused total=0 is labeled not counted yet. No synthetic remaining-recipient count or delivery percentage.
- Synchronous in-flight guards prevent same-view rapid duplicates. Busy controls and Escape/backdrop cannot dismiss pending work. Browser closure does not cancel server work. This is **not durable backend idempotency**.
- Uncertain create/test results do not auto-retry. Draft is retained in the open dialog. Recent history is read in place; an explicit acknowledgement is needed before another manual attempt. No matching history row is not proof of failure, especially for test sends (which do not create history rows). Request rejection and uncertain response are distinguished. Action-modal errors disable resubmission until the modal is closed and history checked.
- Drafts are memory-only; deliberate discard/navigation/reload can lose them. No content is stored in local storage.

## F-15 compatibility contract (follow-up in the same PR)

All customer campaign creates now use `POST /api/admin/broadcasts/targeted` with an explicit
`kind`. No fallback to the legacy create endpoint. The composer reads
`GET /api/admin/broadcasts/targeted/audience?kind=...&locale=...`; the locale parameter is omitted
when no language is selected. Review and final submission both require a successful count for
that exact current request path. Late responses for another selection cannot unlock submission.
An unavailable endpoint, 403/404/500, or malformed count is unknown, not zero or all customers.
This deliberately blocks customer queuing against the old main backend. History and own-staff
tests remain available; production needs the matching #49 backend (including migration/policy).
A count of zero is valid and does not guarantee eventual delivery membership.

Labels reflect the actual worker predicates: buyers have a non-trial service; active have an
ACTIVE non-trial service; inactive have a non-DELETED service and no service with online_at in
the past seven days (not "no active subscription", and may include trial holders). never_bought
has no non-trial service; resellers have an active reseller profile. All segments exclude opt-outs,
banned/blocked customers and Telegram-less guests on the compatible backend.

History reads explicit kind with legacy precedence; unsupported kinds or malformed legacy flags
are labeled unknown rather than all. `skipped` is shown separately: missing legacy values are
unknown, and late send-time exclusions are not failed deliveries or all historical opt-outs.

## Validation

- `cd frontend && npm ci && npm run test:broadcasts && npm run lint && npm run build`
- 134 Node/TypeScript-transpile tests pass after this follow-up and cover response projection/validation, page and counter shapes, status actions, exclusive audiences, URL restrictions, Unicode codepoint limits (matching Python), schedule limits and DST gaps.
- Original-commit validation: local sandbox Chromium exercised the actual Growth export, Broadcasts module, SessionProvider, CSRF-aware HTTP client and shared Sheet, with production Tailwind/Vazirmatn assets and **fully mocked HTTP**. It is not a deployed admin-shell or Telegram end-to-end test.
- Original-commit browser cases included permission denial, no POST on open, test recipient semantics and CSRF, review snapshot/consent reset, scheduled body, validation errors, rapid-click/busy handling, uncertain-result recovery, pause/resume/cancel, 409, GET failures, malformed data, unknown audience, pagination, dirty discard, keyboard focus, Persian/English, 320/390/1200px and dark/light layouts.

Follow-up local validation: 13 Chromium contract cases pass against the actual Broadcasts module
and extracted unchanged main Sheet, with mock session/HTTP adapters. Covers six create payloads,
selected language, missing/denied/failed targeted API with own-chat tests still available,
malformed/zero counts, rapid selection with differently-valued delayed responses, unknown history
kind and missing skipped. Isolated TypeScript strict flags pass over this local graph.
This does **not** rerun the full authenticated shell, production styling, CSRF or real Telegram.
Full repository lint/build, backend CI and staging integration remain release gates.

## Integration / release gates

1. Keep this Draft PR unmerged until reviewed. Reconcile frontend CI script additions with the other independent F-04/F-22 and Mini App feature branches when integrating; do not silently drop their test scripts.
2. This main-based branch does **not** include the F-15 marketing opt-out delivery changes from the Mini App stack. Integrate/review that backend eligibility policy before enabling production marketing campaigns. Do not claim opt-out enforcement from this UI alone.
3. No real Telegram messages (including staff test sends) were sent during development. A human-authorized staging test is still needed: current-staff chat linking, Telegram failure handling, scheduled enqueue, mixed delivery outcomes, permission/shop boundaries, pause/resume/cancel and worker batch behavior.
4. No backend idempotency key is available. After a timeout, verify history/server logs before another manual submission; never blindly replay requests.
5. Confirm deployment/worker readiness and rollback procedure independently. This frontend PR neither deploys nor enables any flag.
