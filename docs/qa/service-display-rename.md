# Mini App service display rename — audit F-03 (partial)

## Dependency and behavior

This PR is stacked on service search/paging PR #102 (`fix/mender-service-search`, base
`7fa7074d6b43d7bbc97b1ba31268efb6fea1373f`), which itself depends on #101. An initial
parallel-base integration check found adjacent import conflicts in Services and the UX
entrypoint. They were resolved before publishing by keeping **both** features and placing
rename on #102; search, keyset pagination, safe return navigation and #101 recovery remain.
Do not merge a copied Services.tsx from the old parallel base over #102.

- An inline, explicitly labelled form edits the existing display label. Cancel does not
  send a request; draft and error stay visible after rejection for a deliberate retry.
- The existing authenticated `POST /api/webapp/services/{id}/rename` and `api.rename` are
  reused. Only `{label}` is sent. No new backend endpoint or client-side financial authority.
- Trim and a 1–48 Unicode-code-point validator guard even programmatic submits; blank,
  unchanged and overlong values do not send. Native `maxLength=48` is intentionally more
  conservative for UTF-16 surrogate pairs. This validation improves UI behavior; the
  backend continues to be authoritative and is **not** hardened by this frontend PR.
- The name is not optimistically persisted: the screen displays the server's returned
  service after success. The panel username, credentials, connection link and plan are not
  changed by this UI action. HTML-looking labels are escaped by React.
- Rename shares the existing synchronous service-action gate with links/revoke. Double
  submits before React commits cannot start a second action. Input and action controls are
  disabled while pending; response/error/feedback after leaving is suppressed by #101's
  keyed screen and gate cleanup. An already-sent server mutation is **not** canceled.
- Persian/English copy distinguishes display name from panel username. Browser-native
  back navigation stays available while a request is pending.

## Not covered

F-03 also includes cancellation/move parity and associated server capability/financial
policy. Those are **not** added or declared complete here. No refund, transfer, panel
migration, DB migration, permission or financial state changes. There is no claim of
server-side mutation idempotency, conflict detection between tabs, new RBAC tests or
verification of a live provider. A mocked denied API response tests UI failure behavior,
not actual server ownership enforcement. Trial/account/WireGuard delivery policy remains
whatever the existing backend endpoint permits.

## Tests and reproducibility

```sh
node --test tests/service-rename.test.mjs tests/service-read.test.mjs
npm run test:ux
npm run lint
npm run build
```

Thirteen new tests execute validation and actual ServiceDetailBody event handlers with the
real action gate plus deterministic hook/JSX fixtures; they are imported by the existing
UX suite. Test-only TypeScript `--noCheck` is transpilation, not a replacement for lint or
production typechecking. The old source assertion that counted exactly two disabled controls
was replaced by targeted assertions for the two original link/revoke controls, since the
new form legitimately adds more disabled controls. No runtime safety check is removed.

Optional synthetic integration QA:

```sh
CHROMIUM_EXECUTABLE=$(command -v chromium) node tests/browser/service-rename-qa.cjs
```

Requires external `playwright` and `esbuild`, optionally located with `PLAYWRIGHT_MODULE`
and `ESBUILD_MODULE`. They are not new required CI/runtime dependencies. `SOURCE_ROOT`
selects an alternative original frontend; `QA_OUTPUT` writes JSON, and `QA_SCREENSHOT`
optionally saves the RTL screen. The script closes every browser context and the local
browser/server. Real React/router/state/API/HTTP/UI/localization are used; unrelated Home
ratio helpers, AutoRenew and UsageChart are explicit widget fixtures. APIs and Telegram
identity/feedback are synthetic, and unexpected/external requests fail QA.

## Verification for this exact stacked source

- Node 22.23.3 / TypeScript 7.0.2: **129 tests in the available UX suite passed**, including
  13 new rename tests and the existing #101/#102 tests. These are not all frontend tests.
- Ten Chromium scenarios passed: cancel, normalized/server-confirmed result, validation,
  pending/double submit, rejection/retry, late success/failure, Persian RTL, escaping and
  denied response. All ten are absent-feature acceptance failures on original #102; they
  are not ten independent security findings.
- Strict **scoped** typecheck passed against real API/state/UI types, with typed stubs for
  the three unrelated widget modules. esbuild bundled the synthetic screen successfully.
- Source was extracted selectively. **No complete frontend lint/Vite production build,
  complete backend suite, physical Telegram/device, real panel or staging test was run.**
  Earlier full-project counts are not evidence for this new PR. All-script/full-checkout
  verification and valid Actions checks remain merge gates.
- No merge/deploy, billing changes, branch-protection or workflow weakening is included.

The optional screen harness includes only app.css, not generated Tailwind/font/production
assets. RTL checks verify direction, copy and interactions, **not** complete visual/device
acceptance. Screenshots are diagnostic fixtures, not production UI renderings.
