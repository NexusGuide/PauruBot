# Staff, access and security
<!-- screens: /staff /audit /account -->

Everyone who works in the panel should have their own account, so their access stays limited and their actions can be traced.

## Roles

- **Owner**: everything, including panel and gateway passwords, staff and backups.
- **Admin**: everything except secrets, refunds, staff and backups.
- **Finance**: payments, receipts, wallets and reports.
- **Support**: customers, services and tickets.
- **Reseller**: only their own customers and services; you set their permissions in [Resellers](/resellers).

**Role permissions** in [Staff](/staff) shows exactly what each role can do.

## Adding a staff member

1. In [Staff](/staff) tap **New staff member**.
2. Choose a username and a role.
3. Either set a password of at least 10 characters, or just give their numeric Telegram ID so they sign in with a Telegram code.

With a Telegram ID they also get the admin menu inside the bot and the messages that match their role; for example, a finance member sees new receipts in Telegram and can approve them right there.

## Signing in with a Telegram code

On the sign-in page tap **Log in with a Telegram code** and enter the username. If the account has a Telegram ID on file, the bot sends a 6-digit code. Each code works once and only for five minutes, and it is cancelled after a few wrong tries. This is safer than a password that might leak.

## Password and My account

Change your password in [My account](/account); doing so signs you out everywhere else. Several wrong sign-ins in a row block sign-in for that account and address for a while.

## API keys

For automation such as Grafana or your own scripts, create a key under **API keys** in [My account](/account) with **Create key**:

- **Read only** is enough for reporting and cannot change anything.
- A key is shown only once; copy it right away.
- A key never has access to staff, panel and gateway passwords or backups, even when its owner is the shop owner.
- Use **Revoke** to retire a key you no longer need.

Send the key in the `Authorization: Bearer pbk_...` header.

## Audit log

Every important action (approving a receipt, changing a balance, changing settings, deleting a service, changing a password and so on) is recorded with who did it, when and from which address in the [Audit log](/audit), and cannot be deleted. Filter by action, for example payment, to quickly find who did what.

> Never share the owner's password; give each person their own account with the smallest role they need.

## Managing from the Mini App

Staff whose account has a **Telegram ID** see a **Manage** entry under More in the bot's Mini App. It shows the dashboard figures their role allows, and finance, admin and owner accounts can approve or reject waiting receipts there, with the receipt picture and the same checks and [Audit log](/audit) entries as this panel. Resellers see their own services, purchases and wallet. Removing the Telegram ID from a staff account, or deactivating it, takes this away immediately.
