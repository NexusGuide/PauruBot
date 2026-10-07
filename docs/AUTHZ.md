# Authorization matrix (SEC-15, TEN-03)

The matrix below is the contract. `paurubot/auth/rbac.py` implements it and
`tests/security/test_rbac_matrix.py` fails if the code and this table disagree.

The merchant role runs a reseller's own bot shop (TEN-04); every merchant permission applies to that shop only.

Legend: ✅ allowed · 🔸 allowed, scoped to the actor's own customers/services · — denied

| Permission           | owner | admin | finance | support | reseller | merchant |
|----------------------|:-----:|:-----:|:-------:|:-------:|:--------:|:--------:|
| `dashboard.view`     | ✅ | ✅ | ✅ | ✅ | 🔸 | ✅ |
| `settings.read`      | ✅ | ✅ | — | — | — | — |
| `settings.write`     | ✅ | ✅ | — | — | — | — |
| `panels.read`        | ✅ | ✅ | — | — | — | — |
| `panels.write`       | ✅ | ✅ | — | — | — | — |
| `panels.secrets`     | ✅ | — | — | — | — | — |
| `catalog.read`       | ✅ | ✅ | ✅ | ✅ | 🔸 | — |
| `catalog.write`      | ✅ | ✅ | — | — | — | — |
| `customers.read`     | ✅ | ✅ | ✅ | ✅ | 🔸 | ✅ |
| `customers.write`    | ✅ | ✅ | — | ✅ | 🔸 | ✅ |
| `customers.ban`      | ✅ | ✅ | — | ✅ | — | ✅ |
| `wallet.adjust`      | ✅ | ✅ | ✅ | — | — | ✅ |
| `services.read`      | ✅ | ✅ | ✅ | ✅ | 🔸 | ✅ |
| `services.write`     | ✅ | ✅ | — | ✅ | 🔸 | — |
| `services.create`    | ✅ | ✅ | — | — | 🔸 | — |
| `orders.read`        | ✅ | ✅ | ✅ | ✅ | 🔸 | ✅ |
| `payments.read`      | ✅ | ✅ | ✅ | ✅ | — | ✅ |
| `payments.review`    | ✅ | ✅ | ✅ | — | — | ✅ |
| `payments.refund`    | ✅ | — | ✅ | — | — | ✅ |
| `discounts.write`    | ✅ | ✅ | — | — | — | — |
| `broadcast.send`     | ✅ | ✅ | — | — | — | ✅ |
| `texts.write`        | ✅ | ✅ | — | — | — | ✅ |
| `tickets.read`       | ✅ | ✅ | — | ✅ | 🔸 | ✅ |
| `tickets.reply`      | ✅ | ✅ | — | ✅ | 🔸 | ✅ |
| `reports.read`       | ✅ | ✅ | ✅ | — | 🔸 | ✅ |
| `audit.read`         | ✅ | ✅ | — | — | — | — |
| `staff.manage`       | ✅ | — | — | — | — | — |
| `resellers.manage`   | ✅ | ✅ | — | — | — | — |
| `resellers.sub`      | ✅ | ✅ | — | — | 🔸 | — |
| `resellers.bot`      | ✅ | ✅ | — | — | 🔸 | — |
| `backups.manage`     | ✅ | — | — | — | — | — |

Rules:

1. Deny by default: a permission missing from a role is denied, and unknown permissions are denied.
2. Reseller permissions are the intersection of the reseller role above and the per-reseller
   allow-list stored on the reseller profile. Owners can only narrow a reseller, never widen past this table.
3. 🔸 scoping is enforced in the query layer (`owner_customer_id` / reseller scope filters), not in the UI.
4. `panels.secrets` (reading or replacing panel passwords, payment merchant keys) is owner-only and audited.
5. `resellers.sub` lets a level-1 reseller add and manage its own level-2 resellers. It is never part
   of the default allow-list given on approval; an owner grants it per reseller. Level-2 resellers
   can never hold it, and their discount can never exceed their parent's (TEN-01).
6. Customers (bot and Mini App users) have no staff permissions. Their actions are limited to their
   own customer row, services, orders, payments and tickets; every handler loads by `(shop_id, customer_id)`.
7. A Mini App user whose Telegram id belongs to an active staff account gets that account's role in the
   Mini App's management view (APP-03), through the same handlers and checks as the admin API. Shop-wide
   actions there (reviewing payments) are refused to resellers even if a role would allow them.
8. `resellers.bot` lets a reseller run its own Telegram bot that sells the shop's plans at its own prices
   (TEN-04). It is never part of the default allow-list. The bot's customers, wallets and payments live
   in a separate shop whose staff are merchants; every service it sells is first paid for from the
   reseller's own wallet at the reseller's price.
