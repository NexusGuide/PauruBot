# Shop warning thresholds in the Mini App — F-27 (partial)

## Dependency and contract

Stacked on #105 (`fix/mender-wallet-history-recovery`, exact base
`998af3b55c48696837bca5dd682dd695771f6dc5`), retaining #96's top-up intent and #105's wallet
read recovery. No financial, ledger, provider, staff permission, DB migration or API path change.

The existing authenticated `/api/webapp/me` response adds `alert_policy`, an allowlisted
object containing **only** `warn_remaining_percent` and `warn_days` from `ctx.cfg`.
The unchanged WebApp auth source loads that config for the shop selected by signed initData,
not a client-supplied shop. No new input or role is accepted. No staff settings view or
secret/set-unset map is exposed. This PR does not change or newly prove authentication.

Both the worker and the projection use small shared integer readers in `core/alert_policy.py`.
Defaults remain the existing shop defaults (10 percent / 2 days), not new hard-coded frontend
policy. None/zero, negative and above-100 values retain the existing worker semantics; there
is **no new validation, clamp, admin control or change to shop policy**. Worker reads remain
inside their original guards: queued-renewal release/suppression and terminal alerts run
before any unneeded setting is parsed. Its AST matches the base after replacing just these
shared calls with the original integer expressions and removing the new import.

## Frontend behavior

- Home's notification badge and progress-warning color, and Notifications' derived feed,
  consume the same profile policy. Predictive volume warnings use the worker's inclusive
  remaining-bytes comparison. Expiry warnings use positive seconds within the configured
  days, only for active services, not rounded calendar days.
- The expiry label retains the worker's existing `floor(seconds/86400)+1` convention,
  including the exact-day boundary. This PR does not silently correct that old convention.
- An old/malformed policy does not revive the old 15 percent / 3 day literals. Predictive
  warnings are suppressed; terminal status and wallet records remain, and Notifications
  shows a localized limitation message. The client requires two safe integers, not strings,
  booleans, fractions, infinity or unsafe integers.
- Each loaded Home/Notifications read subtree is keyed by confirmed `me.id`. Changing the
  customer remounts it; `useResource` ignores the detached reads. A same-customer `/me`
  refresh updates the policy without repeating a financial action.
- Operational mute and marketing preferences are not written, merged or reinterpreted.
  This is still a client-derived informational feed, not Telegram delivery or a durable inbox.

## Rollout and open boundaries

The API addition is backward compatible with old clients. A new client on an old API shows
policy-unavailable rather than inventing thresholds. For aligned behavior, deploy the new
API/worker artifact and frontend together; no database migration is needed.

The existing settings cache can be up to 15 seconds old. UI policy is a `/me` snapshot and
updates when that profile is reloaded; no push, background refresh or immediate admin-change
propagation is promised. Provider observations can also lag. Terminal inference, queued
renewal state, deduplication and delivery mute are not a complete server notification feed.
Other service charts/status decorations are not changed. Device-local read IDs remain as
before; F-18 persistent announcements/inbox and full cross-channel parity remain open.
The Home trial mutation lifecycle and all other actions are not hardened by read remounting.

## Verification

In a complete checkout with its normal dependencies/test services:

```sh
python -m pytest -q tests/unit/test_service_alert_policy.py tests/contract/test_webapp_alert_policy.py
cd frontend
node --test tests/alert-policy.test.mjs
npm run test:ux
npm run lint
npm run build
CHROMIUM_EXECUTABLE=$(command -v chromium) node tests/browser/alert-policy-qa.cjs
```

Browser QA is optional evidence tooling requiring locally available `esbuild` and `playwright`
(`ESBUILD_MODULE` / `PLAYWRIGHT_MODULE` may point to them). They are **not** new production or
required CI dependencies. SOURCE_ROOT selects a baseline frontend; QA_OUTPUT writes JSON.
QA_SCREENSHOT optionally writes a diagnostic RTL image. Contexts, browser and local server
are closed. All identities/API responses are synthetic and unexpected/external requests are
aborted. Real React, router, AppProvider, API/HTTP, localization, Home and Notifications run;
no child-widget stub is needed. Only app.css is included, **not generated Tailwind/fonts**,
so the Home badge test asserts DOM presence, not full visual/device acceptance. The esbuild
bundle is not a Vite production build.

Recorded local checks:

- Python 3.12.15: **33 isolated backend tests passed**. The 25 worker/guard cases also pass
  against the original worker; the other eight cover projection and the `/me` handler.
- Node 22.23.3: **99 tests in the available UX entrypoint passed**, including 36 new cases.
  This is not all ten frontend test scripts. Three targeted behavior assertions fail against
  the original frontend; they concern policy mismatch/missing-policy behavior, not three
  independent security findings.
- **12/12 synthetic Chromium scenarios passed**; original frontend: 3 passed / 9 failed.
  These are acceptance scenarios, not nine independent bugs or a server authorization proof.
- Strict scoped TypeScript check of the real changed-screen dependency closure passed
  without unrelated-widget type stubs. Selected Python lint/formatter, compilation, AST
  preservation, JSON and patch-whitespace checks passed.
- The workspace is a **partial extraction**, not a full application checkout. Backend
  evidence executes the real new pure module plus selected AST bodies of settings defaults,
  `sync.evaluate_alerts` and `webapp.me`, with unavailable business dependencies stubbed.
  The committed tests use normal production imports in a complete checkout. These local
  results are **not** a full app boot, HTTP auth/tenant test, full backend suite/mypy,
  all frontend lint/build/scripts or live DB/Redis/provider/Telegram/staging validation.
- Counts overlap across suites/layers; do not add them to prior project-wide counts.
  CI is not declared green. Full-checkout validation and valid required checks remain gates.
  No merge, deployment, billing change or workflow/check weakening is included.
