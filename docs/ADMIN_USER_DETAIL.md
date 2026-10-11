# In-bot user list and individual management

This follows the existing admin user hub, without adding a database migration.

## Flow

1. Admin → User management → All users (or any filtered audience).
2. The inline keyboard renders Actions / Username / Telegram ID as three columns. Telegram clients can vary spacing and directionality; these are keyboard cells, not a web table.
3. **Manage user** opens that customer's existing profile, balance, service count, phone, ban and reseller state.
4. Customer controls are grouped into referrals, services/free service, balance/payment history, card policy, messaging/ban, hidden panels/trial reset and reseller controls. Staff see only controls allowed by their permissions.
5. **Back to list** preserves the filter and page through read-only child screens and balance/message text input. When there is no matching origin (for example, opening a customer directly from search), it falls back to the all-user list.
6. Closing replaces the message with a generic closed notice, removes its inline keyboard and clears pending input data. Previously sent messages aren't deleted.

The list contains 10 customers per page, clamps obsolete page numbers, uses an explicit no-username placeholder, and excludes other shops. Header/name/ID cells do not perform mutations.

## Payment and referral history

Payment history needs both `customers.read` and `payments.read`; it is scoped to the current shop and customer and paginated (10 rows). It shows ID, recorded amount/currency, provider, localized status and creation time explicitly marked UTC. It does not expose provider tokens, metadata, receipt content or gateway transaction references, and offers no payment mutation actions.

Referrals now have pagination and explicit shop-scoped counts as well as rows, rather than silently showing only the first 15.

## Scope and existing behavior

Existing service, free-service, balance, message, card-trust, ban, trial-reset, reseller-bot/group and hidden-panel handlers are reused; their existing permissions, auditing and behavior remain unchanged. In particular:

- Card trust is not KYC. A trusted customer may bypass receipt/card-review rules; this is the existing `normal → trusted → blocked` policy toggle, not identity verification.
- Ban/unban is the existing customer ban control, not a guarantee of panel-wide service disablement (that depends on shop configuration).
- “Free service” remains a free provisioning order, not a new paid-order form.
- Resetting trials uses the existing eligibility reset, not a configurable per-user trial quota.

No fake buttons were added for identity verification, full-account transfer, bulk removal of referrals, per-user custom base pricing, cron settings or reseller-expiry editing. Those require separate workflows and explicit business rules. This PR does not zero balances, delete data, run migrations or deploy production.

## Release check

Run automated tests first; then test in Telegram with a test customer: list → manage → child screen → back → same list, pagination, no-username display, permission-restricted staff, and payment history. Stacked on the user-hub PR; keep it unmerged until live validation and approval.
