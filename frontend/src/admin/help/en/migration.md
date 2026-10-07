# Moving from another bot
<!-- screens: /migration -->

If you were already selling with MirzaBot, Faoxima or WizWiz, bring customers, wallet balances, resellers and services over without anyone starting from scratch.

## What moves over

- **Customers** with Telegram ID, username, name and phone number. Customers banned in the old bot are banned here too.
- **Wallet balances** for each customer, as one credit in the wallet history.
- **Resellers** with the default reseller permissions. Set their group and discount later in [Resellers](/resellers).
- **Services**, as long as the user still exists on the panel. Data, usage and end date are read straight from the panel, so they are always right.

Nothing changes on the panels and customers' current links keep working.

## Getting the file from the old bot

- For MirzaBot and Faoxima (MySQL): use Export in phpMyAdmin, or run mysqldump on the old server. The result is a `.sql` file.
- For WizWiz: the same MySQL export, or the SQLite file if the bot ran on SQLite.

Files can be up to 300 MB.

## Steps

1. Add the old bot's panels in [Panels](/panels) first.
2. In [Move from another bot](/migration) tap **Choose file**. The bot detects the file type and shows how many customers, resellers, total balance and services it found.
3. Under **Old bot's panels**, pick the panel on the same server in this bot for each old panel. Set panels you don't want to **Don't move**.
4. Tap **Dry run (no changes)**. The result shows how many new customers would be created and services moved, without saving anything.
5. If it looks right, tap **Start the move**. It runs in the background and the final result appears on the same page.

## Reading the result

- **Not found on the panel**: a service that existed in the old bot but whose user is no longer on the panel, usually deleted or expired services.
- **Panel not chosen**: services whose panel you didn't pick in step 3.
- **Services already here**: a service the bot already knew, for example from an earlier run.

Running it again is safe; balances and services are never added twice. So if you missed a panel, add it and run the move again.

> Take a backup before the real move, and switch the old bot off afterwards so customers don't buy from both.
