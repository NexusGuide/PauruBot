# Plans and free trial
<!-- screens: /catalog -->

A plan is what customers buy in the bot: data, days and a price. In [Plans](/catalog) you create plans, group them and set their order.

## Locations and categories

- A **Location** is a group of panels, such as "Germany". Each location can have a flag emoji. Customers choose the location first, then the plan.
- A **Category** only groups plans in the buy menu.

## Creating a plan

1. Tap **New plan**.
2. Enter the name, data (GB), days and price in Toman. For data and days, 0 means unlimited.
3. Pick a location or a panel. Without a panel, the service goes to the least loaded panel in the location, which spreads the load better.
4. Set **Device limit**, **Who can buy** (everyone, customers only or resellers only) and **Can be renewed**.
5. Save. **On sale** and **Off sale** take a plan out of the menu without deleting it, and **Archive plan** puts it away completely; services already sold are not touched.

## More options

- **Start on first connection**: the service's days start when the customer first connects, not at purchase. Every panel except Remnawave supports this; on Remnawave the days count from purchase.
- **Look and naming**: the **Button emoji** and **Badge** (for example "Best seller") appear next to the plan name in the bot. **Prefix** replaces the panel's prefix in usernames of this plan's services.
- **Extra data and time**: with **Sell extra data and time for this plan's services**, customers can buy more GB or days without renewing. Leave the price per GB or per day empty to use the default price from the **Services** group in [Settings](/settings).
- **Panel settings for this plan**: an inbound, group or flow just for this plan. Left empty, the panel's own settings apply.

## Selling ready-made configs from stock

If you have ready-made configs (for example from a server or panel the bot isn't connected to), set **Delivery** in the plan form to **Ready-made config from stock** and pick a location. After saving, paste configs into the **Stock** section: one config per line, or a blank line between configs that span several lines. Duplicates are skipped.

- Each purchase hands over exactly one available config, and no config is ever given to two people.
- The config is sent to the customer in the bot right away and stays available under **Bought configs**.
- When the stock runs out, the plan leaves the buy menu on its own and comes back when you add more. If stock ran out after a payment, the amount goes back to the wallet.
- Configs are stored encrypted, and every **Show** of a full config is recorded in the audit log.

## Custom data and days

In [Settings](/settings), **Custom purchase** group, turn on **Sell custom data and days**. Customers pick the data and days themselves in the bot, and the price is the **Base price** plus the price per GB and per day. The minimum and maximum data and days, and the target panel or location, are set there too.

## Buying on the web

With **Guests can buy on the web** on (the **Buying on the web** group of [Settings](/settings)), your server address followed by /buy is a public page where anyone can buy a plan without Telegram. It offers the online gateways you have turned on (Zarinpal and the gateways in **More gateways**, plus NowPayments and Plisio); card transfer and Stars need the bot.

After paying, the buyer lands on a private page that shows the subscription link and QR; its address is the only key to the purchase, so they should bookmark it. The **Add it to my Telegram** button there opens the bot and moves the service into their account, where it renews and sends alerts like any other.

In [Customers](/customers) these buyers carry the tag guest until they move to Telegram. **Plans on the web page** limits the page to the plans you tick; ready-made stock configs are never offered there.

## Renewals

- **Renewal resets usage** in the **Services** group decides whether leftover data carries over to the next period. Each panel can override this for itself.
- With **Reserve renewals for active services**, renewing a service that still has time and data applies when the current period ends, so nothing is wasted.

## Free trial

Trial settings live in [Settings](/settings), **Free trial** group: days, data, panel and **Daily trial limit**. Each Telegram account gets one trial; raising **Trial round** lets everyone take a trial again. You can require a phone number for trials. Customers marked **High risk** don't get a trial.
