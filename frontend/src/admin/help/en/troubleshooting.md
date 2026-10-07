# Troubleshooting
<!-- screens: /health /jobs -->

When something isn't working, open [System health](/health) first. Most problems show up there along with their cause.

## System health

This page checks the main parts and marks each one **OK**, **Warning** or **Problem**: configuration, master encryption key, public URL, database and its version, server clock, Redis, background worker, failed jobs, queue delay, panels, backups, disk space, the Telegram bot and its webhook. Each item has a short explanation underneath. **Check now** runs all the checks again.

## Job queue

Creating services on panels, renewals, broadcasts and bulk actions run in the background through the [Job queue](/jobs). When a panel doesn't answer, the job is retried a few times with pauses in between. A job whose tries all fail is marked failed and admins get an alert.

- Tap a job to see its last error.
- After fixing the cause (for example once the panel is back), tap **Try again**, or put them all back in line with **Retry all failed**.

## Common problems

### The bot doesn't answer

1. In System health look at **Telegram bot** and **Telegram webhook**. If the bot token changed, it must be changed on the server too.
2. Make sure **Bot is on** hasn't been switched off in the settings; while it's off only staff get answers.
3. If you use **Required channels**, the bot must be an admin in all of them.

### Services aren't created

- In [Panels](/panels) check that the panel is **Healthy** and **Sell on this panel** is on.
- Make sure the panel's **Capacity** or the bot admin's quota on the panel isn't full.
- See the exact error in the job queue.

### A payment isn't confirmed

- **Card transfer**: the receipt is waiting for review in [Payments](/payments), unless auto-approve applied to it.
- **Zarinpal**: **Public URL** in System health must be OK.
- **Crypto**: the customer must have sent exactly the amount shown. Transfers are checked every 30 seconds.

### Server clock

If **Server clock** shows a warning, service end dates and code sign-in may not work properly. Sync the server clock with NTP.

## Server logs

On the server, as root:

```
pauru status
pauru logs bot
pauru logs worker
pauru logs api
pauru restart
```

`pauru status` shows the state of every part plus the same health checks. If you set a channel for the **Errors** topic in [Channels](/channels), application errors are sent there automatically too.

> When you send an error somewhere to get help, include its reference ID, but never send passwords, the bot token or the `.env` file.
