# F-01 — owned service search, status filters and keyset pages

## Stack and scope

This change is based on `fix/mender-service-read-recovery` (PR #101), not directly on
main: it preserves the read/config/QR retry and stale-response protection work.
Merge #101 first, then retarget this PR to main. Do not cherry-pick both full stacks.
No deployment, merge, workflow, billing or production configuration change is included.

## API and ownership

`GET /api/webapp/services?limit=20&q=...&status=active&before=123` uses the existing
Telegram-authenticated WebApp principal. Every query requires both the resolved shop
and customer; deleted services are never exposed. Filters are SQL predicates **before**
the page limit, not filters over the currently loaded client subset.

- Any explicit query parameter selects bounded mode (default 20; permitted limit 1–50).
- `q` is at most 120 characters, trimmed, case-insensitive literal substring search
  over label and panel username. `%`, `_`, backslash and SQL metacharacters are not
  user-supplied LIKE patterns or SQL. Persian text is supported; Unicode normalization
  or transliteration is not introduced.
- Status is one of pending/active/on_hold/limited/expired/disabled. Deleted and invalid
  enum values are rejected with 422, as are invalid limits/cursors/oversize queries.
- Rows sort by descending unique service ID, using `id < before`; limit+1 detects more.
  `next_before` is the last returned ID only when another row exists, otherwise null.
- A cursor is not an authorization token. Foreign-shop/customer rows remain excluded.
- This is **live keyset pagination, not an MVCC snapshot**. New higher IDs do not shift
  later pages, and deleting the cursor row does not break traversal. Status/label edits
  can change membership while traversing; refresh starts again. No total count is promised.

### Deliberate compatibility debt

Unparameterized `/services` keeps the old `{items}` response and all owned nondeleted
rows. Home and derived notification callers still use that contract; silently truncating
it would lose older alerts. **Only the Services screen is migrated to bounded reads.**
Moving those aggregate consumers to independent summaries/alerts is follow-up work.

## Mini App behavior

- Accessible labelled search and status controls, English/Persian labels and mobile RTL.
- Nonempty queries debounce 250 ms; URL query/status are replaced, not pushed for every
  keystroke. A filter change resets rows/cursor and aborts obsolete requests.
- First-page loading, real no-service versus filtered-no-match states, clear filters,
  retryable initial errors, and a load-more error that preserves already loaded rows.
- Local request gates prevent rapid duplicate load-more. Late responses cannot replace
  the next filter's data. Malformed/nonmonotonic page metadata fails visibly, with retry.
- Each detail navigation carries a canonical local list return target. In-app Back and
  browser Back preserve search/status. **Accumulated pages/scroll position are not cached**;
  returning fetches the first bounded page again. External return targets are rejected.
- Existing detail/config/QR/revoke/extra retry and action protections remain in place.

## Database and rollout

Migration 0026 (after 0025) adds `ix_services_owner_page(shop_id, customer_id, id)`;
model metadata has the same index. No schema data rewrite or new extension is required.
The index supports owner-scoped keyset order; contains search is **not** a trigram/full-text
index and no large-dataset latency guarantee is claimed.

Run migration/backend before the new frontend. The legacy response is retained for old
clients; the new frontend requires `next_before` and fails safely against old servers.
This migration uses ordinary transactional CREATE INDEX: it can block writes and needs
an appropriately planned rollout/maintenance window on a large table. It is not a
concurrent or zero-downtime migration. Downgrade drops only this index.

## Reproducible local QA

- `pytest tests/security/test_service_pages.py`: PostgreSQL/Redis-backed tests for scope,
  global search, every allowed status, literal wildcard escaping, cursor traversal,
  invalid inputs, legacy compatibility, unauthenticated access, index and transactional
  migration downgrade/upgrade. Full-suite run is required in both standalone and the
  cumulative open-PR worktree; do not run DB suites concurrently against one test DB.
- `npm run test:ux`: includes `tests/service-pages.test.mjs`, testing query encoding,
  response/cursor validation, canonical return routes, localization and source wiring.
- Run all frontend `test:*` scripts, TypeScript, build, Ruff and mypy.
- After build, optional `tests/browser/service-read-qa.cjs` runs actual local Chromium
  against synthetic APIs: the previous 9 recovery cases plus 13 list/filter/page cases.
  It requires Playwright and Chromium (optional PLAYWRIGHT_MODULE/CHROMIUM_EXECUTABLE).
  External network requests are blocked. No live Telegram, payment/panel provider,
  production accounts, physical mobile device or deployed staging validation is claimed.

Local green tests do not establish green GitHub Actions, production performance, or
completion of the overall audit. Production rollout and merge remain separate decisions.

## Recorded validation for this revision

- Independent stack (#101 + this change): 1073 backend tests; 686 frontend tests;
  TypeScript, production build, Ruff check/format, mypy (223 source files), compile and
  whitespace checks passed.
- Combined #89–101 + this change: 1294 backend tests; 695 frontend tests; TypeScript,
  build, Ruff check/format, mypy (229 source files), compile and whitespace checks passed.
- All 22 Chromium scenarios passed on the combined build and on three successive
  independent-build runs. The rapid-typing case originally reproduced a dropped
  character in URL-controlled input. The fix keeps immediate input state synchronous
  while ignoring only this screen's own deferred REPLACE updates; external navigation
  remains able to restore the URL query.
- These results are local, synthetic and revision-specific, not a GitHub CI attestation.
  Ruff covered the CI paths (paurubot/tests) plus new migration 0026. Pre-existing import
  ordering violations in older migrations from a broader exploratory check were not
  altered or presented as fixed. Existing frontend chunk warnings remain.
