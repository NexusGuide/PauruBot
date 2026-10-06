# Authorization matrix (SEC-15, TEN-03)

The matrix below is the contract. `paurubot/auth/rbac.py` implements it and
`tests/security/test_rbac_matrix.py` fails if the code and this table disagree.

Legend: ✅ allowed · 🔸 allowed, scoped to the actor's own customers/services · — denied

| Permission           | owner | admin | finance | support | reseller |
|----------------------|:-----:|:-----:|:-------:|:-------:|:--------:|
| `dashboard.view`     | ✅ | ✅ | ✅ | ✅ | 🔸 |
| `settings.read`      | ✅ | ✅ | — | — | — |
| `settings.write`     | ✅ | ✅ | — | — | — |
| `panels.read`        | ✅ | ✅ | — | — | — |
| `panels.write`       | ✅ | ✅ | — | — | — |
| `panels.secrets`     | ✅ | — | — | — | — |
| `catalog.read`       | ✅ | ✅ | ✅ | ✅ | 🔸 |
| `catalog.write`      | ✅ | ✅ | — | — | — |
| `customers.read`     | ✅ | ✅ | ✅ | ✅ | 🔸 |
| `customers.write`    | ✅ | ✅ | — | ✅ | 🔸 |
| `customers.ban`      | ✅ | ✅ | — | ✅ | — |
| `wallet.adjust`      | ✅ | ✅ | ✅ | — | — |
| `services.read`      | ✅ | ✅ | ✅ | ✅ | 🔸 |
| `services.write`     | ✅ | ✅ | — | ✅ | 🔸 |
| `services.create`    | ✅ | ✅ | — | — | 🔸 |
| `orders.read`        | ✅ | ✅ | ✅ | ✅ | 🔸 |
| `payments.read`      | ✅ | ✅ | ✅ | ✅ | — |
| `payments.review`    | ✅ | ✅ | ✅ | — | — |
| `payments.refund`    | ✅ | — | ✅ | — | — |
| `discounts.write`    | ✅ | ✅ | — | — | — |
| `broadcast.send`     | ✅ | ✅ | — | — | — |
| `texts.write`        | ✅ | ✅ | — | — | — |
| `tickets.read`       | ✅ | ✅ | — | ✅ | 🔸 |
| `tickets.reply`      | ✅ | ✅ | — | ✅ | 🔸 |
| `reports.read`       | ✅ | ✅ | ✅ | — | 🔸 |
| `audit.read`         | ✅ | ✅ | — | — | — |
| `staff.manage`       | ✅ | — | — | — | — |
| `resellers.manage`   | ✅ | ✅ | — | — | — |
| `resellers.sub`      | ✅ | ✅ | — | — | 🔸 |
| `backups.manage`     | ✅ | — | — | — | — |

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
