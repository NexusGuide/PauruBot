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
| Four renewal modes | ✅ | — |
| Reserved renewal that starts on the first of: data used up, time ended, **a set date** | 🟡 queued renewal (period end / data out); no date trigger | F2 |
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
| Offline crypto: upload a receipt or a transaction hash | 🟡 on-chain payments are watched automatically | F2 |
