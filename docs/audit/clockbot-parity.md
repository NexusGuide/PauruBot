# ClockBot parity tracker

Every feature of the reference bot (ClockBot, read-only clone) compared with PauruBot, down to
single settings. Rule: ClockBot's features are added beside ours; nothing PauruBot already does is
changed or removed. Status: ✅ we have it · 🟡 partly · ❌ missing. Each ❌/🟡 row is closed by a
batch below; the batch number is in the last column.

## Panel pages

| Area | ClockBot | PauruBot | Batch |
| --- | --- | --- | --- |
| Dashboard | metrics, sales trend, host gauges (CPU/RAM/disk), PG health, action center | 🟡 metrics + inbox; no host gauges, no trend chart on home | B1 |
| Inbox | dismiss / reset dismissals | ✅ (reset added in B1) | — |
| Broadcast | history, cancel, delete one, clear history, bulk actions | ✅ (delete one, clear history added in B1) | — |
| Plans | edit, toggle, delete, **clone**, trial plan, custom plan, **wholesale tiers**, **add-on packages**, categories, bulk actions | ✅ (clone, bulk-buy quantity tiers, ready-made data/time packages added in B2) | — |
| Gift codes | list, edit, toggle, bulk | ✅ (toggle via edit) | — |
| Catalog discounts | time-limited discount on the whole catalog, stop | ✅ (B2: percent, end time, optional categories; resellers keep their prices) | — |
| Campaigns | marketing campaigns per segment | ❌ | B3 |
| Customers | card, edit, message, color tag, staff note, wallet credit, services (extend/renew/delete/link), block, **create**, **delete**, **bulk message**, **photo**, **test customers (demo)**, quick renew | ✅ (create, delete, bulk message in B1; photo, test customers, quick renew in B2) | — |
| Trial users | list of trial users, who bought, message, block | ✅ (B1) | — |
| Service cancellations | customer asks, staff approve/reject with refund | ❌ (cancel is instant) | B3 |
| Delivery failures | stuck orders: retry, mark delivered, refund, close, dismiss | ✅ (stuck orders page with retry and mark delivered added in B2; refund as before) | — |
| Finance | orders, payments, cancellations, reports, automation, charts | ✅ orders, payments, reports; ❌ automation review | B3 |
| Loyalty | points, earn rules, rewards catalog, points adjust, tiers, wheel | 🟡 tiers + wheel + lottery; ❌ points, rules, rewards | B3 |
| Tickets | bot tickets + **panel tickets** (staff/resellers ↔ owner) | 🟡 bot tickets only | B3 |
| Security | password, username, credentials, login guard | ✅ | — |
| Backups | create, delete, download, upload, restore, Telegram test, **verify last**, schedule, **own backup bot** | ✅ (verify and separate backup bot added in B2) | — |
| Update | channel (stable/beta), changelog, start, status | ✅ (update channel setting) | — |
| Domains / SSL | domains, certificate progress | ❌ | B4 |
| Connection status | customer-facing host status + probe monitor | ❌ | B4 |
| PasarGuard | users, admins, nodes, groups, hosts, templates, inbounds, bulk | 🟡 users (as services), admins, nodes; ❌ groups/hosts/templates edit | B4 |
| Resellers | applications, plans, billing top-up, subscription adjust/sync, color tag, delete, child | ✅ mostly; ❌ color tag on resellers | B1 |
| Settings export / import | whole shop settings to a file and back | ✅ (B2; secrets never leave the shop) | — |
| Message variables | ✅ | ✅ | — |
| Mini App brand / PWA brand | ✅ | ✅ | — |

## Settings

| Setting | PauruBot | Batch |
| --- | --- | --- |
| Welcome, shop title, guide, FAQ, empty texts, success texts | ✅ (texts) | — |
| Message / button / screen-title texts, reset to defaults | ✅ (reset a section added in B1) | — |
| Button colours per menu | ✅ | — |
| Menu layout classic / compact | ✅ (custom grid) | — |
| QR: auto send, link in caption, caption, background image | 🟡 auto send and link added in B2; caption and background image left | B3 |
| Service naming prefix / suffix / pattern | ✅ (suffix added in B2) | — |
| Limits: daily cap, active cap | ✅ | — |
| Forced referrer, phone before purchase, trial phone (Iran only) | ✅ | — |
| Receipt anti-fraud (warn / block) | ✅ | — |
| Terms: entry, customer purchase, reseller purchase | ✅ | — |
| Add a previous service (+ allowed panels / admins) | ✅ (per-panel allow list added in B2) | — |
| Connection guides with media per platform | ✅ (B2: per-device guides with picture or video, edited on the bot menu page) | — |
| Notifications: low traffic %, low **time %**, first-use nudge | 🟡 time % missing | B1 |
| Daily report: template, metrics, **reseller daily report** | ✅ (reseller daily report added in B2) | — |
| Capacity warning %, funnel tracking, one-tap renew | 🟡 capacity warning added in B2; funnel tracking left | B3 |
| PAYG billing (low balance, on empty, tick) | 🟡 | B3 |
| Maintenance mode + text | ✅ | — |
| Pending order cleanup (TTL, awaiting approval TTL) | ✅ | — |
| Payment methods, card list random, gateways, PSP API, crypto rate API (URL, path, divisor), stars title/description | ✅ (own rate API added in B2) | — |
| Wallet top-up presets | ✅ (B1) | — |
| Backup schedule, keep count, include env, Telegram bot | ✅ (B2) | — |

## Bot (customer side)

| Feature | PauruBot | Batch |
| --- | --- | --- |
| My orders (history) in the bot | ✅ (B1) | — |
| Subscription lookup (paste any link, see status) | ✅ (B1) | — |
| Connection guides after delivery, per platform with media | ✅ (B2) | — |
| Loyalty club (points, rewards) | ❌ | B3 |
| Cancellation request | 🟡 instant | B3 |
| Connection status | ❌ | B4 |
| Admin: preview the bot as a customer | ❌ | B1 |
| Admin: screen-title editor in the bot | 🟡 panel Texts page edits screen titles; in-bot editor left | B3 |

## UI to match the approved design

Every page is rebuilt to its artboard in the design canvas: dashboard (work queue, health strip,
charts), customer 360, payments review, services, plans, panels, marketing, resellers, reports,
tickets, system, settings groups. Batch U runs beside B1–B4, page by page.
