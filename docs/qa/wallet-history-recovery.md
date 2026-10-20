# Wallet history read recovery — audit F-27 (partial)

## Dependency and behavior

This change is stacked on PR #96 (`fix/mender-payment-intents`, base
`82226ea352b4184c9bd6899f896ae96ba01e6b54`). Keep that PR's top-up intent replay and
synchronous action gate. This is a read-side improvement, not a replacement for #96.

- History has localized visible loading, failure/retry and confirmed empty states.
  A failed HTTP request must not look like an empty ledger or remain unhandled.
- The existing `useResource` read lifecycle ignores responses after unmount or supersession.
  The history subtree remounts when the authenticated customer ID changes or a gift is
  confirmed; the previous owner's/previous revision's response cannot fill the new subtree.
- A gift success triggers only a fresh history read and the existing profile reload.
  Failure of that history read does not turn gift success into failure or repeat redemption.
- History retry calls only the existing GET. It never calls gift redemption, payment
  creation or a provider. The existing uncertain top-up key is preserved.
- Rendering of transaction IDs, signs, kinds, amounts and timestamps is retained.

## Scope deliberately unchanged

The existing endpoint returns the latest 20 transactions. This PR does **not** add history
pagination, a complete ledger export, persistent notifications or configurable low-wallet
thresholds. In particular F-27 is not fully closed. No financial policy, ledger, provider,
backend route, permission, schema or migration changes. Local screen isolation is not a
substitute for backend authorization, and the tests below do not prove server authorization.

## Reproducible tests

Run in a full frontend checkout with dependencies installed:

```sh
node --test tests/wallet-history.test.mjs
npm run test:ux
npm run lint
npm run build
```

The existing UX suite imports eight new tests. The small hook/JSX harness executes the real
history component and real `useResource` with deterministic dependency fixtures. Its
TypeScript compiler uses `--noCheck` **only for test transpilation**; it does not replace
`npm run lint` or production typechecking. Missing implementation has an explicit failing
test, rather than silently passing behavior tests.

Optional synthetic browser integration QA:

```sh
CHROMIUM_EXECUTABLE=$(command -v chromium) node tests/browser/wallet-history-qa.cjs
```

This optional script needs locally available `playwright` and `esbuild`; module locations
may be supplied with `PLAYWRIGHT_MODULE` and `ESBUILD_MODULE`. These are **not** new production
or required CI dependencies. `SOURCE_ROOT` can select an original frontend checkout for the
same scenarios. `QA_OUTPUT` writes JSON evidence; `QA_SCREENSHOT` optionally saves the RTL
screen. The script closes its contexts, browser and ephemeral localhost server.

The real screen, state provider, router, API/HTTP client, UI, localization, formatters and
read hook are bundled. Only Payment's unrelated memo helpers are substituted; no live
payment route is tested. Every API response and Telegram identity is synthetic; unexpected
or external requests are aborted and fail QA. Nine scenarios cover loading, GET recovery,
empty success, gift/read separation, owner switch, navigation during failure, preservation
of #96's uncertain key, Persian RTL and a denied read response.

## Verification for this change

- Node 22.23.3 / TypeScript 7.0.2: all **63 tests in the available UX suite passed**, including
  the eight new tests. This is not all ten frontend test scripts.
- Nine synthetic Chromium scenarios passed. On the original #96 source, eight failed and
  the existing successful-empty behavior passed. These are acceptance scenarios, not eight
  independent backend bugs; no live authorization/provider claim follows.
- Strict **scoped** typecheck of the changed-screen dependency closure passed, using the
  real API/state/UI/hook types and a typed stub for unrelated Payment memo helpers.
- This workspace contains a partial source extraction, not a complete application checkout.
  **No full frontend `npm run lint`/Vite build, full backend suite, live Telegram, gateway or
  staging QA was run for this PR.** The optional esbuild screen bundle is not a production
  Vite build. Earlier project-wide test counts must not be attributed to this change.
- GitHub CI is not declared green; review and valid required checks are still merge gates.
  No merge, deployment, billing change or check weakening is included.

The optional screen harness includes only app.css, not generated Tailwind/font/production
assets. RTL checks verify direction, copy and interactions, **not** complete visual/device
acceptance. Screenshots are diagnostic fixtures, not production UI renderings.
