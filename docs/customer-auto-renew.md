# F-02 — customer automatic renewal and add-ons

## Scope

This follow-up extends the existing customer-preferences PR #51, not a new PR. The authenticated
`/app/services/:id` customer screen shows the same component in the browser layout and Telegram
Mini App. Existing Telegram initData authentication is unchanged: this does not implement F-12
outside-Telegram login, staff-controlled wallet consent, spending caps or a new payment method.

GET `/api/webapp/services/{service_id}/auto-renew` returns a projected rule, off/on/stopped/
waiting_balance/unknown state, current wallet balance/currency and currently orderable choices.
Choices use the normal catalog and order-draft validators (including reseller, trial, panel and
extra restrictions). Base prices are estimates before discounts, not locked future invoices.
No order is created or paid by reading or saving preferences, and no panel call is made here.
An already-due eligible service may be paid by the existing worker soon after opt-in. The widget is keyed to the service and only mounted when the loaded service matches the route ID; navigating away does not cancel a submitted server request.

POST on the same path accepts a complete explicit off/renew/extra choice, strict integer amounts,
strict acknowledgement and the revision from the last read. Extra fields, body-supplied owner/shop,
operational markers and incompatible modes are forbidden. Both routes enforce service/customer/
shop ownership and hide deleted/foreign services as 404. Writes take a service row lock and compare
the revision, including worker operational changes; a stale setting returns 409 before mutation.
This hash is not an authorization token. Auth and ownership remain the security boundary.

Validation uses the existing F-02 `enable`/`disable` logic inside a savepoint. This API replaces the
whole extra selection instead of accidentally retaining a previously selected extra part. Failed
validation restores the previous rule. Changes and customer audit share the transaction; mutation success is returned only after an explicit commit succeeds. Identical
active/short settings are no-ops and preserve cooldown/last-order metadata. Changing an enabled
rule retains recent-order markers; an explicit restart of a stopped rule clears the failure marker
but retains its last_at cooldown. Full off removes the rule, as the existing bot does; this is not
a new global anti-toggle spending limit.

## Wallet and failure semantics

The customer reviews and explicitly acknowledges automatic wallet charges. Current price,
eligibility, discounts and wallet funds are checked again by the existing order/worker path at
execution. No future charge amount is guaranteed or capped by this UI. Data and day add-ons can
produce two separate automatic orders. Insufficient balance leaves the rule on and may charge
once funds arrive; other failures may stop it. Disabling does not cancel or refund previously
paid/queued/in-flight orders, undo resets or provide panel-side fencing.

The UI only confirms a validated response matching the requested service and complete choice.
No optimistic success, mount write or automatic POST retry. Duplicate-click guard and disabled
pending controls are local, not durable exactly-once delivery. A failed/malformed/unknown save
blocks another write until settings are read again. Old confirmed rules/balances remain identified
as snapshots; current status becomes unknown after an error. A manual refresh never replays POST.
Missing or forbidden API is a read error, never “off”. Unavailable previous choices are explicitly
shown, not silently replaced by the first available option. Off remains possible for an existing
rule on an ineligible service.

## Validation and release gates

- 52 Node model/copy tests pass locally and `test:auto-renew` is added alongside `test:usage` in CI.
- 29 API-unit cases pass locally against the complete new route module with injected auth/domain
  dependencies and savepoint fakes. No real wallet, queue, order or Telegram mutation.
- One added PostgreSQL-backed security case covers actual customer/shop/deleted-service boundaries
  and is pending CI; the local fake harness explicitly deselects it.
- 18 Chromium cases pass against the actual new component with mock HTTP/app-locale adapters and
  minimal layout CSS: review/focus/consent, renew/two-part-extra/off payloads, ineligible off,
  states, failed/missing API, conflict/rejection/unknown response, refresh recovery, duplicate click,
  unavailable option and 320px Persian/English layout. Not a full authenticated shell or production
  CSS test. No real purchase, wallet debit, renewal or Telegram send was performed.
- Strict TypeScript checks pass over the isolated component graph; Python compile, Ruff lint/format
  and limited new-module mypy pass. Generated API rows use the existing docgen renderer/source route
  names. Full imports, repository docgen/lint/build, PostgreSQL concurrency/rollback and CI still
  need to pass. Existing F-07 controls were not fully re-tested by this isolated F-02 harness.
- Keep Draft. Integrate the #49 → #50 → #51 stack and reconcile #52–#54 descendants in order;
  no rebases were performed here. Preserve all independent feature test scripts at integration.
- Resolve and integrate #47 renewal/worker safety before production automatic renewal. This branch
  does not contain that independent safety patch; passing frontend checks is not release approval.
- Human-authorized main/child-bot staging must verify actual catalog/prices, currency, shop isolation,
  paid/queued orders, opt-in near thresholds, short balance/refill, stopped restart, concurrent worker/
  preference updates and off behavior. Backups/rollback and real CSS/keyboard/mobile remain gates.
  This PR does not merge, deploy, migrate or activate server flags.


### Type-contract correction

The first F-02 head's frontend CI passed, but Python CI failed. A narrower type-contract probe
using the original SQLAlchemy Service annotations, original auto-renew protocol/signatures,
real enum definitions and typed API/domain interfaces independently reproduced a dict-key
invariance error when assigning inferred Literal-key cooldown markers to the service JSON field.
An explicit `dict[str, Any]` marker annotation fixes that error; the probe and all 29 injected API
cases pass after correction. This is not a full repository type/import check or a claim that no
other CI failure exists. Await the fresh full CI head; no checks were disabled or relaxed.
