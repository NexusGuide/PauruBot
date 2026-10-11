# Telegram shop settings and product editor

This change is stacked on the finance/gateway admin branch. It does not merge `main`, deploy the
server, create a migration, change credentials, or import another bot's code.

## Entry points and layout

`Admin → Shop settings` opens paired products/categories, create/manage gift codes,
create/manage discounts, existing bulk-purchasing policy and an optional secure catalog web link.
Gift-code and discount creation/deletion reuse the existing permission-checked, audited handlers;
used codes retain their existing deletion protections. Growth rewards remain in Features and
mass wallet gifts remain in Users. No earlier admin tool loses its navigation entry.

Product lists are shop-local, ten items per page, with page indexes clamped to existing pages.
Opening a product, including old `section=plan` show links, opens the new editor. Old price/toggle
callbacks remain supported for backward compatibility; new editor actions use reviewed changes.

The product card displays name, ID, price, data, days, audience, category, destination, renewal policy,
visibility, order, connection limit, username prefix, description and completed **paid purchase**
count (not trials, free purchases, renewals or pending payments). Price/data/days occupy one row;
name/audience, note/renewal, category/destination and connection-limit/prefix occupy paired rows.
Boolean buttons show their current state and request an explicit desired state, not blind toggles.

Supported editable plan fields:

- title (1–96), price (0–100 billion toman), days (0–3650), data GB (0–100000), IP limit (0–100);
- description (up to 1000), username prefix (`[a-z0-9_]`, up to 16), sort (-1000000–1000000);
- audience (`all`, `customers`, `resellers`), active visibility, renewable flag;
- category, direct panel or location pool owned by this shop;
- renewal mode: inherit, extend/reset, extend/carry, restart/reset or restart/carry;
- first-connection timing only on driver families with genuine first-use expiry support.

Persian digits and ordinary separators work in numeric inputs. `-` clears description/prefix.
Stock-delivered plans expose only applicable fields; forged callbacks cannot change unsupported
service terms. Destination changes with existing driver overrides are rejected: configure inbounds,
groups and other driver-specific settings deliberately in the secure web catalog instead of silently
reusing or dropping old overrides. A location pool must have active compatible panels before
first-connection timing can be enabled.

## Safe writes

Edits display before/after values and require a second confirmation bound to the staff member's
FSM, shop, record, random nonce and a 15-minute lifetime. Current permissions are checked again.
A locked fresh record is compared with the review snapshot before applying; changed, replayed,
cancelled, expired and other-shop confirmations are rejected. Text entry also detects a record
changed since the input prompt. Numeric/text/enum and reference validation runs again on apply.
Successful writes produce audit entries. Opening pages does not mutate configuration.

Already issued services are not rewritten. Existing order amounts are not repriced. Material service
term/destination edits are blocked while this product has an unexpired pending or paid/provisioning
order; expired/cancelled/completed orders do not impose this restriction. New quotes use the new
price. This is a bot-editor guard, not a redesign of the web editor or provisioning concurrency model.

Categories can be created, renamed, sorted, disabled and restored. Creation serializes on the shop
row and rejects duplicate names. Disabling a category hides its products from the common customer
catalog query, including direct `get_plan` callbacks; it does not delete products or order history.

Bulk settings edit the existing enabled flag, reseller-only flag and maximum (2–200 in this editor).
They have their own confirmed/stale-checked, audited, row-locked writes. The existing bulk engine's
minimum of **two** remains fixed; there is no new quantity policy or migration.

## Deliberate differences from screenshot references

- Screenshots are visual references, not copied integrations or matching product semantics.
- `f/n/n2` are not mapped onto buyer audiences: their meaning is not defined by this project.
- Renewal consumption handling is not labelled as periodic data reset.
- No fake first-purchase-only, renewal-cashback or adjustable bulk-minimum button is added.
- Creating/deleting products and driver-specific inbound/group options stay in the existing web
  catalog. Per-user hidden panels remain in user management, not a new product property.
- Unsupported screenshot functionality is not presented as working.

## Verification and rollout

Automated tests run the real dispatcher with a recording Telegram transport and actual PostgreSQL /
Redis test stores. Cover navigation, layout, pagination, input bounds, confirmations, audit/replay,
permission revocation, tenant isolation, buyer visibility, category hide/restore, destination/stock
validation, on-hold compatibility, outstanding orders, new-price quotes and bulk policy effects.

Before merging, run CI and manually smoke-test these screens with a separate Telegram test bot and
isolated database/Redis. Do not run a second polling instance with the production token. Merge stacked
PRs in order only after checks pass; opening this PR is not a production deployment.
