# Panel capability report in the admin web UI (F-04)

## Scope and permissions

This frontend-only change uses the existing `POST /api/admin/panels/{panel_id}/capabilities` endpoint. It changes no driver, schema, panel credentials, permissions or deployment setting. The report button appears in each saved panel's card only for staff with `panels.write`, matching the endpoint's server permission. A reader without that permission does not gain a probe action, even for read-only checks.

The report dialog is per panel and per visit. Opening it does not send a probe, and the previous panel's report is not carried across to another panel. The existing admin API client adds CSRF protection to POST requests; backend shop ownership/permission checks remain authoritative.

## Running probes

- **Read-only checks** explicitly send `{ "write": false }`. They run connection/listing probes and show driver declarations for client IPs, webhooks, delivery and supported changes. A successful declared-capability row is not proof that every operation was live-tested.
- **Write probe** is collapsed and disabled by default. Staff must open its warning, check a fresh authorization checkbox, and click the separate write button. It explicitly sends `{ "write": true }`. It creates a temporary `pbprobe_` user, reads/modifies it, revokes its link when supported, and attempts deletion.
- Every run clears authorization and the old result. A synchronous ref blocks repeated submission while the request is pending. There are no automatic retries, write-on-open, polling probes or implicit write opt-ins. This is a UI guard, not server-side durable idempotency or a cross-browser concurrency lock.
- The dialog keeps focus inside it and restores the trigger on close. Running/result/error states are brought into view and focused. While running, close/Escape/backdrop dismissal is ignored; closing the browser/tab still does not cancel server work.

## Interpreting results safely

Rows use text statuses: passed, warning/limitation, failed, skipped/unsupported. The overall result is validated against the row statuses. `write_tested=false` is explicitly distinguished from a completed write probe. Unknown future check keys remain visible as bounded plain text. Raw details are plain escaped text, not HTML, links or executable markup; unsolicited response fields are not copied into the view model.

Temporary-user deletion failure gets a prominent manual-cleanup warning, including the deletion row's username/detail. A write run without successful deletion confirmation also warns: creation failure does not prove that no temporary user could remain. A failed or unreadable write response warns that the outcome/cleanup is unknown and tells staff to inspect `pbprobe_` users and panel logs before repeating. Write retry always needs renewed authorization.

The time displayed is when this browser received the result, not a server-generated test timestamp. Reports are not saved/cached/exported by the UI and are not permanent certificates. Address, credentials, driver settings, permissions or panel version changes may invalidate a prior observation. No general secret-redaction guarantee is added to upstream error strings by this UI.

## Layout and validation

Feature styles are scoped to the capability dialog, with light/system-dark palette, 16px body, 14px captions, visible keyboard focus and 44–48px controls. Persian instructions isolate the operational `pbprobe_` prefix as LTR. Long panel names wrap in both the report header and panel cards; check details and unknown identifiers wrap rather than overflowing. No shared admin stylesheet, Mini App or global modal component is changed.

Run in `frontend`:

```sh
npm ci
npm run test:capabilities
npm run lint
npm run build
```

49 Node tests cover response validation/projection, malformed and future data, summary/write-marker consistency, cleanup interpretation, translation coverage, stylesheet scope and selected text/control contrast pairs. `test:capabilities` is included in frontend CI. Preserve independent `test:confirm`, `test:usage`, `test:help` and `test:ux` scripts/jobs when combining other feature branches; this PR is based directly on main, not on the Mini App stack or F-22 branch.

Local validation compiled the actual Panels screen, admin session/API/UI and capability dialog with production Tailwind/Vazirmatn, plus a sandbox-only harness. 19 Chromium scenarios with mocked HTTP cover no probe on open, exact read/write bodies and CSRF, permission visibility, renewed consent, busy/dismissal behavior, keyboard focus, unknown/untrusted text, connection/create/cleanup failures, uncertain writes, response failure/retry, switching panels, long names, Persian390/320 dark and English1200 dark. Relevant screenshots were inspected individually. Full dual-entry/admin-shell builds are delegated to CI; the local harness does not include the entire admin layout/help-article bundle.

No live panel was probed during development. Before release, review/merge separately, validate real staff roles, test read-only connectivity on authorized panels, and run write probes only on an explicitly authorized non-production panel with cleanup verified. Also validate real mobile keyboards/safe areas and screen readers. Token contrast tests and sandbox screenshots are not a complete accessibility or live-panel certification. No deployment, migration or flag activation is included.
