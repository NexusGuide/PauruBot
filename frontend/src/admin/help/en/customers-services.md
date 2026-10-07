# Customers, services and tickets
<!-- screens: /customers /services /tickets -->

Anyone who starts the bot is a customer, and each subscription they have on a panel is a service. This page covers the everyday work on both.

## Finding a customer

In [Customers](/customers) search by name, username, Telegram ID or phone number. Filters separate banned customers, marker colours and risk levels. Tap a customer to open their full page: wallet, services, orders, referrals and notes.

## Wallet

With **Adjust balance**, **Add to wallet** or **Take from wallet**. The memo you write is saved in the wallet history. Every balance change is written to the ledger and never deleted, so fix a mistake with an opposite change.

## Marker colour, tags and risk

- **Marker colour** and **Tag** (for example vip) are visible only to staff and help you find people quickly. The **Internal note** works the same way.
- **Risk level**: with medium or high risk, this customer's card receipts are never auto-approved. With high risk they also get no free trial.

## Banning

**Ban** blocks the customer from the bot. If **Banning disables services** is on in the settings, their services are turned off too, and they come back on when you unban.

## Working with a service

In [Services](/services) or on the customer page, tap a service:

- **Read from panel** fetches fresh usage and status from the panel. Usage is also synced automatically every few minutes.
- **Disable** and **Enable** take a reason; with **Tell the customer** on, the reason is sent to the customer.
- **Extra GB** and **Extra days** add to the service free of charge.
- **Reset usage** sets the service's usage on the panel back to zero.
- **Replace link** cancels the old subscription link and makes a new one; use it when a link has leaked.
- **Delete service** also removes the user from the panel and cannot be undone.
- **Move to another panel** takes the service to the target panel with its remaining data, end date and the same branded link, and removes the old account. With **Customers can change location** on in the settings, customers can do the same from the **Change location** button in the bot, paying the **Location change price** from their wallet.

**Give a free service** on the customer page creates a service on any plan without charging the wallet.

### Bulk actions

In the services list, select several services or take every result of a filter and act on all of them; for example, after an outage use **Gift time and data** to give extra days to every service on one panel. To empty a panel, filter the list by that panel and use **Move all listed**. The work runs in the background and you can follow it in the [Job queue](/jobs).

## Subscription link

With **Subscription links on your domain** on, customers get a /s/... link on the bot's domain and the panel address stays hidden. In a browser the link shows a page with usage, end date and buttons that add it to an app; in VPN apps it returns the configs directly. Change the app buttons under **App buttons on the subscription page**.

## Service card

With **Service info card** on (the **Services** group of [Settings](/settings)), every service in the bot has a **Card** button that sends one picture with remaining data, days left, end date, location, plan and the QR. Customers can keep it or forward it to whoever uses the service. After a purchase the card is sent in place of the bare QR. **Card colours** picks the colour set, and turning off **QR on the card** leaves the link out when customers post cards publicly.

## Tickets

Customers' support messages collect in [Tickets](/tickets). Your reply goes to the customer in the bot. Once it's solved, tap **Close ticket**. Resellers can also send you tickets from their own panel with **Message the shop**.
