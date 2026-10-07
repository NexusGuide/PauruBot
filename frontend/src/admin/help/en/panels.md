# Panels
<!-- screens: /panels -->

Every service you sell is created on one of your panels. The bot works with PasarGuard, Marzban, Marzneshin and 3x-ui, and you can run several panels of different types at once.

## Adding a panel

1. In [Panels](/panels) tap **New panel**.
2. Pick the **Panel type** and enter the **Panel address** exactly as you open it in a browser to sign in.
3. Enter the **Panel username** and password. On 3x-ui, an **API token** is used instead of the password when you have one.
4. Pick the panel's location so that plans in that location can create services on it.
5. Tap **Test connection** and save.
6. Choose the inbounds or groups new users should be created in, and save.

It's best to create a separate admin for the bot on the panel. If that admin has a user or data quota, its usage shows on the panel card and you get a warning when it's nearly full.

## Self-signed certificates and private addresses

The connection to a panel is always encrypted and verified. If the panel uses a self-signed certificate, tap **Read certificate**, compare the SHA-256 fingerprint with the certificate on your server, then tap **Trust this certificate**.

Private network addresses (such as 127.0.0.1 or 10.x) are blocked by default. Turn on **Allow private address** only when the panel runs on this server or your own private network.

## Capacity and pausing sales

- **Capacity** is the most services allowed on the panel. When it's full, plans sold by location create the service on the least loaded healthy panel left in that location, while a plan tied directly to this panel stops selling until there's room. Panels that are down, or whose bot admin quota is full, are skipped too.
- Turning off **Sell on this panel** stops new services from being created there; existing services keep working and can be renewed.
- **Data on renewal** overrides the shop setting for this panel's services. **Sell extra data** and **Sell extra time** can also be switched off per panel.

## Monitoring and alerts

The bot checks panels every two minutes. Each panel card shows CPU and memory use, users and online users, connected nodes and **Uptime** for today and this week. If a panel fails two checks in a row, an outage alert goes to the **Panels** report channel and to admins, followed by a recovery message with the outage length once it's back. Nodes going down and coming back are reported too.

**Check now** runs an immediate check. **Restart core** disconnects users for a few seconds, so use it only when needed.

## Importing existing panel users

If the panel has users the bot doesn't know, tap **Import users**. Nothing changes on the panel; the bot just starts managing them.

- When a note or username holds a numeric Telegram ID, **Find owners by Telegram ID** gives each service to that customer.
- The rest go to the customer or reseller you pick as **Owner**, or are left out with **Skip users without an ID**.
- The result is recorded in the [Audit log](/audit).

## Moving users between panel admins

On PasarGuard, Marzban and Marzneshin every user belongs to a panel admin. **Move between admins** on the panel card lists the admins with how many users each owns, and gives all users of one admin to another; for example when a reseller gets their own panel admin, or when you retire an admin. Nothing else about the users changes and customers stay connected. The bot's admin on the panel must be allowed to do this.

> To bring customers and balances over from your previous bot, see [the migration guide](help:migration).
