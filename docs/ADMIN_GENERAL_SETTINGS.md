# General settings and main options (bot admin)

The **⚙️ تنظیمات عمومی** button opens one hub; **🛠 قابلیت‌های ربات** opens the feature status list.
Only settings PauruBot actually enforces are shown.

## General settings hub
| Row | Buttons | Setting / target |
| --- | --- | --- |
| 1 | Bot status (live) | `bot_enabled`; turning off needs confirmation, staff keep access |
| 2 | Main options · Feature status | `feat` main table · feature categories |
| 3 | Support · Languages | `support_username` (validated, `-` clears) · `languages` (default locale and last language cannot be removed) |
| 4 | Menu mode · Look/emoji/sticker | `menu_mode` (Mini App only with an HTTPS public URL) · existing `look` screen |
| 5 | Rules · Accept rules again | `terms_required` · bump `terms_version` (confirmed, stale version rejected) |
| 6 | Crons and timing · Values and limits | existing `cfg` crons group · existing `settings` editor |
| 7 | System status · Backups | existing doctor and backup screens |
| 8 | Web link | forced channels, log channels and the full settings page stay in the secure web panel |

## Main options table
Two columns (status, title); both cells carry the wanted value, so a repeated or stale tap
cannot undo itself. Rows: bot status, rules, phone verification, phone for trials, card-to-card
verification, Mini App, premium emoji, Iranian numbers only (`phone_country_codes` = `["98"]`;
custom codes set on the web are never overwritten), menu mode and start sticker/media.

## Other feature pages
Each page has its own description. A third cell opens that feature's existing settings page,
only when the staff member has its permission.
- **Users and support**: reseller requests (→ applications), reseller web panel, reseller bot,
  panel accounts, IP notice, ban disables services, customer self-delete (→ refund settings),
  plus a support tickets row.
- **Sales and services**: bulk buy (→ bulk settings), custom buy (→ settings), custom username,
  extra data/days (→ prices), upgrade, location move (→ prices), renew resets usage, renew queue,
  service info card (→ colour theme, cycles the real `info_card_theme` values), card QR, branded links.
- **Wheel, lottery and referrals**: trial (→ trial settings), referrals (→ settings), commission on
  every purchase, start gift for referred users only, wheel (→ prizes), buyers-only wheel,
  lottery (→ manage), loyalty club, plus the growth page.

## Feature categories
Main, users and resellers, sales and services, wheel/lottery/referrals, payments (opens the
finance gateway hub with its readiness checks), crons, premium emoji, system status, extra
values and growth rewards.

Not included because PauruBot has no such feature: anti-spam settings (the flood guard is
fixed), shared-host optimizer, ID/KYC or link verification, Mini App tickets and a separate
Redis screen (Redis is part of system status). Every change is audited as `settings.update`.
