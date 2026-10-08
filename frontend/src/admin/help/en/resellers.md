# Resellers
<!-- screens: /resellers /sub-resellers /billing /my-bot -->

A reseller is a customer who buys at a discount and sells services to their own customers. Resellers can work from the bot and from the web panel, and always see only their own customers and services.

## Getting resellers

With **Accept reseller requests** on in the **Resellers** settings group, customers apply from the **Resellers** button in the bot. Requests appear in [Resellers](/resellers) under **Reseller requests** and are closed with **Approve reseller** or a rejection. After approving, tap the reseller to see their settings.

## Groups and discounts

Under **Reseller groups**, create groups such as "Bronze" and "Gold" with a discount percentage and a **Minimum top-up**, and put each reseller in a group. If one reseller needs a different discount, fill in **Own discount (%)** for them. With **Who can buy** on a plan you can make plans just for resellers.

## Per-reseller settings

- **Reseller until**: after this date the discount and access are removed; empty means no end date.
- **What they can do in the web panel**: for example viewing customers, creating services or replying to tickets. Resellers sign in to the web panel with the **Reseller** role.
- **Limits**: **Daily purchase limit**, **Active service limit**, **Total data cap (GB)** and **Sub-reseller limit**.
- **Parent reseller**: choosing one makes this reseller a sub-reseller of another.
- **Low-balance warning**: below this balance the reseller gets one warning a day.

## Billing mode

- **Prepaid**: each purchase is charged to the wallet right away. The usual mode.
- **By usage**: services are created free, and every hour the data used (**Price per GB used**) is charged to the wallet.
- **Hourly**: services are created free, and every hour each active service (**Price per service per hour**) is charged to the wallet.

In the last two modes the balance can go negative. When the debt passes the **Debt limit**, the reseller is suspended automatically and their services are turned off. Once they top up and clear the debt, everything comes back on within a few minutes. Each period is recorded in **Billing history**, and resellers see it themselves on their [billing page](/billing).

You can also suspend a reseller by hand with **Suspend reseller**. A manual suspension is not lifted by a top-up; tap **Lift suspension**.

## Fixed prices and hidden panels

When editing a group, set a price per plan under **Fixed reseller prices**; empty means the group's percentage discount applies. The same table is on each reseller, and a reseller's own price beats its group's. Fixed prices count for purchases and renewals, for the wholesale price in a reseller's own bot and for sub-reseller commission; a sub-reseller never pays less than the reseller above it.

With **Hidden panels** on a group, the plans of those panels are neither shown to nor sold to that group's resellers (in their own bot too).

## The reseller panel in the bot

An active reseller who presses “Reseller” in the bot sees its own panel: balance, discount, active services, services ending soon and 30-day sales. From there:

- “Quick sale”: buy with the customer's name as the label.
- “Find a service”: by label or panel username.
- “Ending soon”: services that end within 3, 7 or 30 days or have used 90% of their data, with “Renew all”, which renews each service with its own plan from the wallet and stops when the money runs out.
- “Sales report”: today, 7 and 30 days, data sold, best-selling plans and sub-reseller commission.
- “Sub-resellers”: add by Telegram ID, change the discount and pause (with the “manage sub-resellers” right).

## Selling PasarGuard admin accounts

Besides services, you can sell a whole admin account on a PasarGuard panel: the buyer signs in to the panel and creates its own users, within a traffic quota and a user cap.

1. The bot's admin on the PasarGuard panel needs permission to manage admins.
2. In [Admin accounts](/admin-accounts) add a plan: the panel, the admin role it gets, the traffic quota, the user cap, the period and the price. "Price per extra GB" and "Price per extra user" let buyers add traffic or user slots later; 0 means not sold.
3. Turn on "Sell PasarGuard admin accounts" under the Resellers section of [Settings](/settings). With "Admin accounts for resellers only" on, only resellers see them.

In the bot the buyer opens "Admin accounts" (in the menu, or in the reseller panel), picks a plan and pays like any order: from the wallet or any payment method. Once paid, the bot creates the admin and sends the panel address, username and password. From the account card the buyer can see its usage and users, renew (the period is added to what's left and usage resets), buy traffic or user slots, get a new password, and pause the account with all its users.

Three days before the end the buyer is warned; at the end the admin and its users are switched off until a renewal. At 90% of the traffic quota the buyer is warned once. Reseller discounts and fixed prices don't apply to admin accounts, and they can't be sold in a reseller's own bot.

In [Admin accounts](/admin-accounts) you also see every account sold, with its usage, and can pause, resume or delete one.

## Web panel and Mini App for resellers

When you approve a reseller, it also gets its own sign-in to this panel: the username is "r" plus its Telegram ID, with no password; it signs in with "Log in with a Telegram code" and the bot sends the code. The approval message in the bot tells it the address and username, and the "Web panel" button in its bot panel shows them again. Turn this off with "Web panel for approved resellers" under the Resellers section of [Settings](/settings).

In the web panel the reseller sees only its own customers and services. On the [Sell](/desk) page it sells a plan under a customer name, paid from its wallet at its own price, and sees services running out within 3, 7 or 30 days, searches them and renews the ones it ticks in one go.

In the Mini App a reseller can name each purchase after its customer at checkout, and "Open the reseller desk" under More shows its figures and the services running out, with "Renew all".

## Paid reseller membership

Set "Reseller membership price", "Group for paid members" (the group's ID) and "Membership period" under the Resellers section of [Settings](/settings). The Reseller button in the bot then offers membership: the customer pays from the wallet and becomes a reseller in that group at once, with the web panel sign-in. Before it ends, "Renew membership" in the reseller panel adds another period. A reseller you switched off can't reopen itself by paying. With applications also on, customers can still apply without paying.

## Bulk buying

With **Bulk buying** on in the **Resellers** settings group, a **Buy several** button appears at checkout in the bot. The reseller sends how many (up to **Most services per bulk purchase**) and all the services are paid in one wallet payment: either all of them are paid or none is. When they're all ready, the reseller gets one file in the bot with every service's name and link. Turn off **Bulk buying for resellers only** to let regular customers use it too.

## Sub-resellers

A reseller with permission to **Manage sub-resellers** can add sub-resellers in [Sub-resellers](/sub-resellers). That person must have started the bot already. A sub-reseller gets at most the parent's own discount, and when they buy, the difference between their price and the parent's is added to the parent's wallet.

> If you set a channel for the **Resellers** topic in [Channels](/channels), reseller activity reports go there.

## A reseller's own bot

Tick **Run their own bot** in a reseller's permissions and they get [My bot](/my-bot) in their panel. There they paste a token made with @BotFather; that bot then sells your plans under their shop name and at their prices: a markup on their own price, or a fixed price per plan, never below what they pay.

- Buyers in that bot are the reseller's customers: they pay the reseller (to the cards the reseller sets) and the reseller approves receipts in that bot, where they are its merchant.
- Each sale first takes the reseller's own price from their wallet in your shop. Without enough balance the sale is refunded to the buyer and the reseller is told to top up, so nothing is handed out unpaid.
- Only purchases and renewals are sold there; trials, extras, moves and the Mini App stay in your own bot. The reseller needs prepaid billing.
- In the reseller's details you see how their bot is doing and can stop it at any time; its customers and services stay.
