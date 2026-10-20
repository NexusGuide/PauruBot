# F-08 — real customer ticket-history filters

## Scope and stack

This PR is based on PR #101 (`fix/mender-service-read-recovery`). It is independent of
#102's service-search feature, but shares the same small optional `Screen.backTo`
extension. That identical extension is intentionally present in both branches; do not
copy an entire api.ts/webapp.py from one branch over the other when combining them.
No merge, deployment, billing, workflow, financial, role or permission change is included.

## What was wrong

The customer Mini App previously fetched an unfiltered 20-ticket page and filtered
status/department only in the browser. A matching closed or retired-department ticket
outside that page could be reported as empty. Changing filters did not reset the ID
cursor, and going back from a thread lost filter/page context.

## Contract

`GET /api/webapp/tickets` retains the fixed 20-row descending-ID page and `next_before`.
Existing unparameterized calls still work. Optional query parameters:

- `status`: open, answered, closed. Omitted means all. Unsupported/empty values are 422.
- `department_id`: omitted means all; **0 means no department / General support**;
  a positive value filters that shop's department, including a retired one.
- `before`: a positive signed-64-bit ID. The cursor is not an authorization token.

Every query applies authenticated customer AND shop before filter/order/limit. Both
filters combine with AND. Positive foreign and nonexistent department IDs yield the
same empty owned page; a malformed historical foreign-department link cannot authorize
that filter. IDs/SQL values are bound through the ORM, never interpolated into SQL.

- `departments` remains **active-only** for the new-ticket form, preserving its policy.
- Additive `filter_departments` contains active desks plus retired desks actually
  referenced in this customer's own shop-scoped history, independently of the current
  page/filter. Unused inactive names and foreign desks are not disclosed. Both lists
  expose only id/title, not staff chat/thread routing fields.
- Retired desks remain invalid for creating a new ticket. No change to message access,
  internal-note exclusion, attachment placeholder, close/reply policy or ticket limits.

## UX and recovery

Status/department/cursor live in a canonical local URL. Filter changes clear the cursor;
Older/Newest retain filters; thread in-app Back and browser Back restore filter/page.
Back refetches that bounded page; no scroll, draft or accumulated-page cache is promised.
There is no loaded-subset filter. Server response validation rejects malformed IDs,
nonmonotonic/overlapping pages, bad cursor metadata and malformed department choices.

Abort plus request-generation guards reject late page/filter responses and clear
key-mismatched errors. Initial/error/retry and filtered-empty states are distinct;
a failed refresh can retain its same-key page, whereas a failed move to another page
shows retry and Newest rather than mislabelling a failed read as empty. Department
choices remain usable after a successful read, including while switching filters.
General support is available even if no departments are configured. Clear filters and
English/Persian labels are included. External thread-return URLs are rejected.

## Rollout and limitations

Deploy the backend supporting filters before the frontend. An old backend silently
ignores unknown query parameters; filtered reads now require the additive filter metadata
and verify every row against the requested filters. A legacy unfiltered response therefore
fails visibly with retry instead of showing incorrect results; only unfiltered legacy
metadata remains accepted. There is no new migration/extension. Existing customer/shop indexes
remain in use; no large-history query-plan/latency guarantee is claimed. Evaluate query
plans before adding indexes for real deployment distributions rather than guessing.

Pagination is **live keyset, not MVCC snapshot**: replies do not reorder IDs, insertions
above the cursor do not shift later pages, and a deleted anchor need not exist to resume.
Status/department edits can change membership during traversal; refresh/Newest rereads.
No total count is promised. Attachment upload/download remains separate F-02 work;
this PR must not be represented as solving it or completing the entire audit.

## Local QA reproduction

- `pytest tests/security/test_ticket_filters.py`: signed Telegram test authentication,
  real local PostgreSQL/Redis; global status and department combinations, inactive/general,
  cursor walks, insert/delete anchor, ownership and malformed historical associations,
  private metadata projection, invalid query values and unchanged creation restrictions.
- All existing frontend `test:*` scripts; helper contract/localization/source-wiring
  regressions join existing `test:ux` via `tests/ticket-pages.test.mjs`.
- TypeScript/build, Ruff check/format, mypy, compile and whitespace checks.
- Optional `frontend/tests/browser/ticket-filters-qa.cjs` after build requires separate
  Playwright/Chromium; supports PLAYWRIGHT_MODULE and CHROMIUM_EXECUTABLE. It launches
  only a local preview with synthetic APIs and blocks external requests. Includes mobile
  viewport/Persian RTL, old-page matches, combined filters, retry, stale responses,
  department creation choices, malformed data, page and thread Back restoration.
- Also run the earlier service-read browser regressions; on the cumulative worktree,
  run the service-search browser regressions added by #102 as well.

Local test results are not live Telegram-device/provider/staging verification or a
GitHub Actions attestation. No production account or secret is used by browser QA.

## Recorded validation for this revision

- Independent stack (#101 + this): **1065 backend tests, 701 frontend tests** passed.
  The 17 ticket-browser scenarios passed in two successive runs; the earlier 9
  service-read scenarios also passed. TypeScript/build, Ruff check/format (350 files),
  mypy (222 source files), Python compilation and whitespace checks passed.
- Cumulative #89–102 + this: **1319 backend tests, 759 frontend tests** passed.
  All 17 ticket-browser scenarios passed twice and all 22 service-read/search scenarios
  passed. TypeScript/build, Ruff check/format (370 files), mypy (229 source files),
  compilation and whitespace checks passed.
- Browser coverage includes a legacy backend missing the filter contract and a response
  inconsistent with the requested filter: both show retryable error rather than wrong
  filtered results. All browser APIs are synthetic; no live staging/device claim.
- Three-way content merging against #101 and #102 was checked for the six files both
  branches touch; all merge cleanly after placing the ticket helper import away from
  the service-search import. Cumulative testing preserves earlier payment/security work.
- Existing frontend chunk/dynamic-import warnings remain. No required CI check was
  removed/bypassed, and local green is not evidence that GitHub Actions executed green.
