# In-bot user list and individual management

This follows the existing admin user hub, without adding a database migration.

## Flow

1. Admin → User management → All users (or any filtered audience).
2. The inline keyboard renders Actions / Username / Telegram ID as three columns. Telegram clients can vary spacing and directionality; these are keyboard cells, not a web table.
3. **Manage user** opens that customer's profile, balance, service count, phone, ban and reseller state, plus identity mark, card-to-card state and cron-message state (and, for resellers, expiry and base prices).
4. Staff see only controls allowed by their permissions.
5. **Back to list** preserves the filter and page through read-only child screens and text input. When there is no matching origin (for example, opening a customer directly from search), it falls back to the all-user list.
6. Closing replaces the message with a generic closed notice, removes its inline keyboard and clears pending input data. Previously sent messages aren't deleted.

The list contains 10 customers per page, clamps obsolete page numbers, uses an explicit no-username placeholder, and excludes other shops. Header/name/ID cells do not perform mutations.

## User card buttons

Rows top to bottom (pairs are left | right as Telegram draws them). Every action is scoped to the current shop and audited.

| Button | Behaviour | Permission |
| --- | --- | --- |
| 👥 User's referrals | Paginated referral list | `customers.read` |
| 🔄 Remove from referrer / 🔄 Remove user's referrals | Clear this user's referrer (shown only if set) / after confirmation, release every referral (users are kept, `referrer_id` is cleared) | `customers.write` |
| 💳 Enable / disable card number | Card-trust `normal` / `blocked` (blocked = no card to card) | `payments.review` |
| Verify / unverify user | Staff-set `verified` tag on the customer; no documents are collected | `customers.write` |
| 🛒 Add order | Existing free-service flow | `services.create` |
| ➕ Test account limit | Existing "allow another trial" | `customers.write` |
| 💰 View user payments | Existing payment history | `payments.read` |
| Transfer account | Ask target Telegram ID/@username → confirm → move non-deleted services and positive wallet balance to the target. Not offered for resellers, self or other shops | `customers.write` + `services.write` + `wallet.adjust` |
| 💡 Turn account off / on | Disable (after confirmation) / re-enable all of the user's services on their panels; panel failures are counted, not fatal | `services.write` |
| 📑 Check channel membership | Fresh check of forced channels; lists missing ones | `customers.read` |
| 0️⃣ Zero balance | After confirmation, a ledger adjustment to bring the wallet to 0 | `wallet.adjust` |
| 🕐 Cron messages: on/off | Toggle the user's expiry / low-volume reminders (`alerts_muted`) | `customers.write` |
| 🤖 Activate sales bot / ❌ Remove sales bot | Existing reseller-bot screen / after confirmation stop the bot (data kept; only staff can allow it again) | `resellers.manage` |
| 🔋 Base volume price / ⏳ Base time price | Set the reseller's per-GB / per-hour price (`gb_price`, `hourly_price`) | `resellers.manage` |
| ❌ Hide a panel | Existing per-user hidden panels toggle | `customers.write` |
| 🗑 Show hidden panels | Only hidden panels; tap to show again | `customers.write` |
| ⌚ Reseller expiry | Days from today, `0` = no expiry | `resellers.manage` |
| ❌ Remove reseller | After confirmation, deactivate the reseller profile (wallet/history kept; running bot stopped) | `resellers.manage` |

Below them stay the existing services, balance adjustment, message, ban, card-trust level (normal → trusted → blocked), reseller group, back, admin menu and close buttons.

## Payment and referral history

Payment history needs both `customers.read` and `payments.read`; it is scoped to the current shop and customer and paginated (10 rows). It shows ID, recorded amount/currency, provider, localized status and creation time explicitly marked UTC. It does not expose provider tokens, metadata, receipt content or gateway transaction references, and offers no payment mutation actions.

## Release check

Run automated tests first; then test in Telegram with a test customer and a test reseller: every button above, confirmations, permission-restricted staff, and back navigation.
