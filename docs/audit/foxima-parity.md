# Foxima parity tracker

Features of the Foxima reference bot (read-only clone, read with grep/sed only, never run)
compared with PauruBot. As with ClockBot (`clockbot-parity.md`), Foxima's features are added
beside ours; nothing PauruBot already does is changed or removed. Foxima and ClockBot share an
ancestor, so most of Foxima is already covered by the ClockBot batches; this file lists what
Foxima has on top. Status: ✅ we have it · 🟡 partly · ❌ missing. The batch closes the row.

## Messaging and broadcasts (batch F0, done)

| Foxima | PauruBot | Batch |
| --- | --- | --- |
| Broadcast text or media to all / buyers / non-buyers | ✅ (ours also: active, inactive, resellers, language, opt-out) | — |
| Forward broadcast, pin with an unpin time, unpin all | ✅ (web form now pins too) | F0 |
| Buttons that open the bot (buy, trial, help, referral, top up, start) | ✅ section buttons beside link buttons | F0 |
| Buyers of one panel; people idle in the bot for N days | ✅ | F0 |
| Live status, cancel; final report to the sender | ✅ (ours also pause / resume / schedule / test send) | F0 |
| Message one user with text or a picture, "user may reply" | ✅ any media, reply button opens tickets | F0 |

## Referrals

| Foxima | PauruBot | Batch |
| --- | --- | --- |
| Percent, commission after purchase, first purchase only, start gift | ✅ | — |
| Daily / monthly cap of counted invites | ✅ | — |
| Anti-abuse: invite counts only with a verified phone | ✅ (F1; off by default) | — |
| Anti-abuse: minimum account age before an invite counts | ✅ (F1; off by default) | — |
| Referral reports | ✅ (referrals page, report group) | — |

## Payments

| Foxima | PauruBot | Batch |
| --- | --- | --- |
| Card to card, trusted users, receipt review, bank SMS (`forapp`) | ✅ | — |
| Gateways: Zarinpal, AqayePardakht, Tetra98, Variza, AbanGateway, Blupal, Tronado, CubePay, AtlasPay, TonPay, NowPayments, Plisio, Stars | ✅ | — |
| Per-method minimum and maximum amount | ✅ (F1, within the shop-wide range) | — |
| Per-method cashback percent | ✅ (top-up bonus rules already target one method) | — |
| Per-method tutorial text shown before paying | ✅ (F1; bot message, Mini App note) | — |
| Per-method display name | ✅ | — |

## Customers

| Foxima | PauruBot | Batch |
| --- | --- | --- |
| Add / remove balance, block with reason, orders, payments, manual phone confirm | ✅ | — |
| Delete an order, delete with refund, add / remove reseller | ✅ | — |
| Disable all of a customer's services at once, and enable them again | ✅ (bot: customer card; web: bulk by customer) | — |
| Exports: users, orders, payments (Excel) | ✅ | — |

## Shop and services

| Foxima | PauruBot | Batch |
| --- | --- | --- |
| Plans, categories, locations, bulk price, manual sale, stock | ✅ | — |
| Five renewal modes | 🟡 four of five (`extend_reset`, `extend_carry`, `restart_reset`, `restart_carry` in `paurubot/provisioning/renewal_policy.py`); the fifth — zero the usage counter, keep the remaining volume, add time — is missing | F4 |
| Reserved renewal that starts on the first of: data used up, time ended, **a set date** | ✅ `core/orders.py` (`start_at`, `queued_start_at`), `api/routes/webapp.py` (SVC-02) | — |
| Buy flow grouped by duration (1 month, 3 months…) | ✅ (F2, bot; off by default) | — |
| Dice game with a wallet prize | ✅ (F2: the wheel can be Telegram's dice 🎲 or slot 🎰; our wheel stays the default) | — |
| Banner on the buy screen (set, edit, view, delete, restore) | ✅ | — |
| Per-panel account limit with reset and "unlimited" | ✅ (panel capacity) | — |
| Daily status report, uptime of panels and nodes, IP block notice, on-hold reminder | ✅ | — |
| x-ui inbound sync, Remnawave usage | ✅ | — |
| Remnawave loyalty | ✅ (F2: GOLD / SILVER / BRONZE tag by the buyer's active services; off by default) | — |

## Mini App

| Foxima | PauruBot | Batch |
| --- | --- | --- |
| Buy, renew (options, preview, confirm), extras, services, configs, QR | ✅ | — |
| Wallet, transfer, history, gift code, discount check, tickets, phone verify | ✅ | — |
| Offline crypto: upload a receipt or a transaction hash | 🟡 auto-watch only (`payments/chains.py`); no tx-hash / receipt submission, no re-check or cancel by staff | F5 |

## Open gaps from the full inventory (2026-11)

Found by walking Foxima's whole surface (`src/`, `panel/`, `payment/`, `cronbot/`, `api/`, `app/`)
and matching each entry against PauruBot code; a row is here only when the PauruBot file named
below has no such path. Sizes: S under a day, M a few days, L a batch.

### Access and safety

| Foxima | PauruBot today | Size |
| --- | --- | --- |
| Anti-spam: per-minute message cap, temporary mute, staff actions (`Handlers/AntiSpamSettings.php`, `AntiSpamActions.php`) | `bot/context.py` gates terms/phone only | M |
| Web-panel managers with an IP allowlist (`admin.iplogin`, `Handlers/WebAccessAndReceiptReview.php`, `RenewalOptionsAndWebManagers.php`) | `db/models/tenancy.py` staff rows carry no IP list | M |

### Customer bot and Mini App

| Foxima | PauruBot today | Size |
| --- | --- | --- |
| Service note the customer can read and search | `db/models/catalog.py:193` is staff-only | S |
| Move one service to another customer | only whole-customer move (`api/routes/admin/customers.py`) and gifts (`core/service_gifts.py`) | M |
| Sixth renewal mode: zero usage, keep remaining volume, add time (`RenewalOptionsAndWebManagers.php`) | `provisioning/renewal_policy.py` has four modes | M |
| Account-page layout and per-service button editor (`panel/account_layout.php`, `panel/service_keyboard.php`) | `frontend/src/admin/screens/BotMenu.tsx` edits the main menu only | M |
| Media per section (buy, services, wallet, tickets) (`panel/appearance.php`) | only `start_media` and `buy_banner_file_id` (`core/settings.py:334`) | S |
| Help categories with media and shipped defaults (`default_help.json`, `Handlers/TextAndHelpSettings.php`) | flat `help_items` (`core/customer_help.py:60`) | M |
| In-bot ad modal and ad API (`panel/ad_api.php`, `panel/ads.js`) | none | M |
| Discount code on wallet top-up | `bot/routers/wallet.py` has no code step | S |
| Purchase cashback with target and scope (`Handlers/CashbackSettings.php`, `src/Cashback`) | only `topup_bonus_rules` and referral (`core/settings.py:151`) | L |
| Wheel: agents only, first purchase only | buyer/all audiences only | S |
| Send API docs to a customer from the bot | `ops/docgen.py` writes files, never sends | S |
| Mini App push text and hiding the bot menu (`text.json:miniapp_suggest_1`) | none | S |

### Web panel

| Foxima | PauruBot today | Size |
| --- | --- | --- |
| Log viewer: tail, search, download (`panel/logviewer.php`, `logviewer_api.php`) | no `/logs` route in `api/routes/admin` | M |
| Forapp unmatched-deposit review page (`panel/forapp.php`) | only the unmatched-SMS notice (`payments/bank_sms.py:238`) | M |
| Config file-name and caption editor (`panel/filenaming.php`, `filenaming_api.php`) | fixed names (`api/routes/webapp_stock.py:79`) | S |
| Admin bulk renewal (`Handlers/BulkRenewalAndProviderCapacity.php`) | reseller bulk renew only (`i18n/fa.json:626`); admin bulk is delete/disable/move | M |
| Provider capacity view (same Foxima file) | per-panel capacity number (`api/routes/admin/panels.py`) | S |
| Host optimizer (`optimization_config.php`, `Handlers/OptimizerAndCronReports.php`) | none | M |
| Sales-readiness checklist | gateway readiness line only (`i18n/fa.finance.json:25`) | S |
| Worker concurrency from the panel (`Handlers/WorkerSettingsAndCatalogControls.php`) | env `BOT_UPDATE_CONCURRENCY` (`config.py:28`) | S |
| Test-service cleaner (`src/Cron/Services/TestServiceCleaner.php`, `src/Services/TestAccounts/`) | none | S |
| Public log drain (`cronbot/public_log_drain.php`) | none | S |
| Mini App UI / client action log (`api/uiactionlog.php`, `api/clientlog.php`) | staff audit trail only | M |
| Global header search | settings search only (`frontend/src/admin/locales/en.json:2321`) | S |
| Admin notification bell / inbox | none | M |
| Dashboard 24h / week / 3-month / custom ranges and a category donut | 7 and 30 days only (`frontend/src/admin/screens/Home.tsx`) | M |
| FX sync from an API and USD/TRX tickers (`cronbot/fx_rate_sync.php`, `Handlers/FxPricing.php`) | manual `usdt_rate` / `trx_rate` (`core/settings.py:254`) | M |
| Free accent colour picker (`panel/appearance.php`) | presets only (`core/brand.py:3`) | S (cosmetic) |

### Payments

| Foxima | PauruBot today | Size |
| --- | --- | --- |
| Rialit gateway (`payment/rialit.php`, `src/Payments/Gateways/Rialit`) | absent from `payments/providers/gateways.py` | M |
| Tetraminator gateway (`payment/tetraminator.php`) | absent; `tetra98` is a different provider | M |
| Manual crypto: paste a hash or a receipt, re-check, cancel (`panel/crypto-offline.js`, `api/payment.php`) | auto-watch only (`payments/chains.py`) | M |

### Panels and provisioning

| Foxima | PauruBot today | Size |
| --- | --- | --- |
| JovadPanel driver (`src/Admin/Keyboard/ProviderPanels.php`) | 12 drivers under `panels/drivers/`, no jovadpanel | M |
| Guardian limits and warnings on the panel card | `panels/drivers/guard.py` sets limits per user, no editor | S |
| Usage coefficient (multiplier on counted traffic) (`Handlers/CoefficientsAndWalletDefaults.php`) | none in `core/settings.py` | M |

