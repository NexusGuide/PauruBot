# F-21 — reseller account hub (admin web)

## Scope and entry points

- Main-based, independent frontend slice; no backend, migration, feature flag or live-operation changes.
- A reseller with `dashboard.view` uses **Reseller hub** in navigation (`/billing`). Existing billing history remains below the new hub and keeps its original endpoint/component.
- Non-reseller staff with `resellers.manage` can open **Open account hub** on a reseller profile row. This opens a read-only sheet, not the profile/financial editor.
- Own data: `GET /api/admin/my/hub`. Staff data: `GET /api/admin/resellers/{positive profile ID}/hub`. Backend ownership and authorization remain authoritative.
- No financial writes, suspension/resume controls, Telegram messages, auto-refresh, probes, exports or new API routes.

## Meaning and privacy

- Wallet balance, billing debt and daily net spend are server values in toman. Signed wallet entries, balances and genuine zeroes are preserved; missing/invalid values are not converted to zero.
- Runway is a spending estimate, not profit, revenue, payment due date or a guarantee. The server's window is shown. Purchase/usage/membership charges less refunds count; top-ups, gifts and staff adjustments do not.
- Zero net spend shows **no estimate**, not infinite runway. Sub-day estimates use server hours; positive estimates below one hour are labeled explicitly. Server `low` controls warning status, not display rounding (including 1.96 versus 2 days).
- Timeline is at most 100 recent account events, newest first as returned by the server, **not complete billing history**. The filter operates only on those loaded events; there is no invented pagination or order detail link.
- Own audit projection retains only time, type and action even if a response erroneously contains staff-only fields. No staff identities or before/after payloads are retained in that projection.
- Staff audit projection allows only a narrow set of scalar account fields. Unknown/nested fields, arbitrary free text, credentials and contact fields are omitted. This is deliberately not a complete audit viewer.
- All text is rendered as React text; there is no HTML interpretation. Unknown future event types are generic and discard extra payloads.
- Snapshot time means **received in this browser**, not server as-of time. Refresh clears old values while pending; failure/malformed response is not an empty history or zero balance. No automatic retries. Unmounted requests cannot repopulate another sheet.

## Automated validation

- `cd frontend && npm run test:reseller-hub`: **64 passing Node tests**, using the actual TypeScript projection module. Covers schema failures, safe integers, signed values, null/no-spend estimates, low boundary, timeline limit, privacy projection, future event types and role/ID guards.
- Local partial-tree TypeScript check passes with the repository safety flags, including `noUnusedLocals`, `noUnusedParameters` and `noUncheckedIndexedAccess`.
- Production Vite harness build passes using the real new hub, SessionProvider, API client, shared Sheet, shared CSS, Tailwind and bundled Vazirmatn fonts.
- **25 passing Chromium scenarios**, all API requests mocked. These are new-module browser tests, **not a full application/server end-to-end test**. Scenarios cover own/staff paths, denial before reads, invalid profile ID, no reads before staff opens, GET-only operation, refresh, 404/503/malformed responses, empty/filter states, no-spend and 0/<1h/12h/1.96d/2d estimates, privacy, literal hostile text, delayed request after closing, keyboard trap/Escape/focus restoration, and responsive layouts.
- Browser widths: Persian 390 light, Persian 320 dark, English 1200 dark. Checked no horizontal overflow and feature controls at least 44px high. Full application lint/build and Python suites run in existing GitHub CI; local harness does not replace those checks.

## Visual review

Inspected screenshots individually: own Persian mobile metrics and warning; staff English mobile ledger/audit details; Persian 320px dark sheet with long account name; English desktop dark sheet; loading, error and empty snapshots. Long header names initially took excessive space: clamped to three lines, keeping the full name in the dialog's accessible label, then rerendered and checked again. CSS is feature-scoped; no Sheet backdrop styling or global theme overrides.

## Before release (not performed by this PR)

1. After review/merging, confirm actual navigation on reseller desktop and mobile More, and existing billing history below the hub.
2. On a staging system, verify own-account and same-shop staff data, cross-shop rejection, revoked permissions and signed financial values against the server ledger. Do not infer tenancy correctness from browser mocks.
3. Confirm with real sparse/no-spend/refund accounts that server estimates and event projections match expectations. Review omitted staff audit fields before expanding the allowlist.
4. No live server deployment or real financial operations were performed for this work.
