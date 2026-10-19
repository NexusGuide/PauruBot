# Mini App service read recovery (audit F-07)

## Behavior fixed

- A failed detail or extra-purchase service read stays on the current route, with the
  existing localized ReadFailure and a retry. It is not silently turned into navigation
  away from the user's task. Authorization remains on the server; a 404 is not bypassed.
- Config retrieval catches rejection, shows a persistent retryable failure, and clears
  that failure after recovery. Both config retrieval and link revocation are locally
  exclusive even before React commits a busy state.
- Subscription and WireGuard QR work is a caught resource read, with loading, empty,
  error/retry and appropriate image alt text. Copy/download fallbacks remain accessible
  if encoding fails (for example, QR capacity exceeded). Revocation invalidates previous
  config/QR state; WireGuard QR data is refetched after a successful revoke.
- A route-resource key remounts detail/extra state, so late reads/QR/config results do not
  populate another service. Leaving during an action suppresses stale UI updates,
  haptics and success/error toasts. It does not cancel or undo a mutation already sent
  to the server, and local exclusivity is not server-side idempotency.

## Verification

The existing `npm run test:ux` imports 12 new regressions (executable action-gate tests
plus source-wiring/localization contracts); no new required CI dependency or script is
introduced. Run all frontend test scripts, `npm run lint` and `npm run build`.

Optional real-browser QA is reproducible with:

```sh
npm run build
CHROMIUM_EXECUTABLE=$(command -v chromium) node tests/browser/service-read-qa.cjs
```

It requires a locally available `playwright` package and Chromium. `PLAYWRIGHT_MODULE`
can point to another installed compatible Playwright entry. It is not part of mandatory CI.
The script serves only built assets on an ephemeral localhost port, aborts external network
requests, and mocks every API response. It closes all created pages, browser and server.
Nine scenarios cover detail retry, config retry, pending controls, actual QR capacity
failure followed by revoke/recovery, WireGuard fallback/retry, extra read retry, late route
response, navigation during revoke, and Persian RTL retry. It fails on unhandled page errors.
Synthetic API success is not live Telegram/provider or server verification.

## Scope

No backend, endpoint, model, permission, payment or deployment behavior changed. Service
search/pagination (F-01), ticket attachments and rename/cancellation parity remain separate
backlog items. This PR does not claim the full audit or all service UX work is complete.
