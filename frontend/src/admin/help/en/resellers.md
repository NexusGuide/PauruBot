# Resellers
<!-- screens: /resellers /sub-resellers /billing -->

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

## Sub-resellers

A reseller with permission to **Manage sub-resellers** can add sub-resellers in [Sub-resellers](/sub-resellers). That person must have started the bot already. A sub-reseller gets at most the parent's own discount, and when they buy, the difference between their price and the parent's is added to the parent's wallet.

> If you set a channel for the **Resellers** topic in [Channels](/channels), reseller activity reports go there.
