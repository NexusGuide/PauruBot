# Mini App visual and navigation polish (F-10)

## Scope

This is a frontend-only change stacked on the F-09 help branch. It does not change API authorization, billing, payment/order semantics, delivery adapters, database schema, or deployment configuration. Draft PRs must be reviewed and integrated in dependency order; a green CI result is not a deployed release.

The Mini App imports its own `app.css` and sets `data-app="miniapp"` on its document root. All new CSS selectors are scoped to that marker. The shared stylesheet and admin entry point are unchanged. The admin panel's visual redesign and remaining feature UIs are separate work.

## Presentation

- Preserve the Pauru turquoise identity with an explicit light/dark palette. Text, secondary text, error text, primary button labels, selected states and input boundaries have tested contrast pairs. These token checks are not a blanket WCAG certification of every existing component.
- Use the existing local Vazirmatn font, 16px body/inputs and a 14px floor for small UI captions. SVG chart tick labels retain their existing 11px rendering and data geometry.
- A centered 32rem content column, 16–20px mobile gutters, consistent surfaces and rounded controls reduce crowding. The bottom navigation remains fixed, with content bottom padding and safe-area insets. Long pages are scrollable; keyboard-focused inputs and the last action must remain reachable above the bar.
- Small-width plan cards wrap the price below the description. The gift-code input retains useful width next to its action button rather than inheriting the shared full-width button rule.
- Selected filters have `aria-pressed`; nested routes select their parent navigation group. Error and success notices use live regions; loading indicators have localized names, while button spinners are decorative.
- A busy shared Button remains disabled even if the caller supplies `disabled={false}`. Its label stays visible. This is a UI duplicate-click guard, not a server-side idempotency mechanism.
- The accessible usage-history table sits inside a visually hidden wrapper rather than applying the hiding class to the intrinsically wide table itself. Its rows, raw counters, chart breaks, time range and scale calculations are unchanged.

## Navigation

`navigation.ts` contains pure tab-group and parent-route mappings. Visible header Back goes to the logical parent; the native Telegram Back button uses an in-app history entry when one exists and otherwise falls back to the parent. Going back does not cancel or roll back an order/payment.

On route-path changes, reset scroll and focus the main content. A deferred main rendered after a read completes also receives focus. Ordinary field edits and same-path rerenders do not steal focus. A keyboard skip link targets the main content.

## Read failure states

Home's service summary, the service list, and Buy's catalog use a read-only resource hook. Failed reads do not become empty successes or a zero service count, and have an explicit retry action. Responses from unmounted/obsolete reads are ignored. Requests are not forcibly aborted and mutations are never automatically retried by this hook.

This does not normalize every legacy error path: service-detail redirect-on-error, wallet history, staff/reseller reads, and existing payment polling retain their previous behavior. It does not introduce durable order/payment idempotency or financial-release approval.

## Validation and release checklist

Run from `frontend`:

```sh
npm ci
npm run test:usage
npm run test:help
npm run test:ux
npm run lint
npm run build
```

`test:ux` covers parent/tab mapping, scoped CSS rules, selected palette contrast and the actual shared Button's static React rendering (busy/disabled precedence and label/type preservation). It is included in the frontend CI job. Preserve the independent `test:confirm` script/job when integrating the separate F-22 typed-confirmation branch.

Local browser QA used the actual Mini App entry/screens/provider/API wrapper with compiled production Tailwind/Vazirmatn CSS, sandbox Chromium and mocked HTTP responses. It covered the 18 routes at 390px and 320px, selected 320px dark states, an English 768px form, read errors/retry/empty/loading, input focus, grouped navigation, native deep-link Back and Telegram theme-change callbacks. Screenshots were inspected individually. The early bare development harness screenshots were not evidence that production Tailwind was broken.

Before release, still validate in real Telegram iOS/Android/Desktop, actual device safe areas/soft keyboard and screen-reader announcements, real backend failures, live purchase/receipt/payment/renewal flows, and existing admin pages. The local browser exercise is not a live payment, staging, server, Telegram-client or full accessibility audit. No merge, deployment, migration or feature-flag activation is included here.
