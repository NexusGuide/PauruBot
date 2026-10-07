# Backups and updates
<!-- screens: /backups /updates -->

A daily backup and safe updates are what keep the server worry-free. Both are available only to the owner.

## Automatic backups

An encrypted backup is written every day at a set hour, and the last few are kept. The hour and the number kept are shown on [Backups](/backups). Each file holds the whole database and the master encryption key (MASTER_KEY), locked with the backup passphrase, so on a fresh server panel and gateway passwords come back too. The `.env` file is not included.

- With Telegram delivery on, each backup is sent to the owner or to the channel you set for the **Backups** topic in [Channels](/channels).
- **Back up now** writes one right away. Download files from the list on the same page.
- No backup is written until the backup passphrase (BACKUP_PASSPHRASE) is set on the server. The installer shows it once; keep it somewhere off the server.

## Restoring a backup

Restoring happens on the server, because the whole database is replaced:

1. Put the `.pbak` file on the server.
2. Run this as root and answer the questions:

```
pauru restore /root/paurubot-backup.pbak
```

If the backup was made on another server with a different backup passphrase, the installer asks for it. If the restore fails, the bot starts again on the previous data.

## Copying the shop setup to another server

At the bottom of [Backups](/backups), **Download setup file** gives settings, plans, locations, discount codes, reseller groups, bot texts and channels in one JSON file. Secrets, customers, wallets and panel passwords are not in it; use a backup for a full move.

On the target server, first add the panels with the same names, then pick the file with **Choose a setup file** and tap **Check first** to see what would be created or changed. If it looks right, tap **Import**. Importing the same file again repeats nothing. A plan whose panel isn't on the target server is attached to its location instead.

## Updating from the panel

[Updates](/updates) shows the installed version, the latest release and the version you can roll back to. To update from here, run this once on the server:

```
pauru enable-agent
```

It starts a small service on the server that carries out the panel's requests. The panel itself has no direct access to the server.

1. Leave both fields empty to install the newest release of the current channel. You can also pick a channel (Stable or Beta) or an exact version such as v1.2.3.
2. Tap **Update** and confirm.
3. The server writes a backup, then downloads and starts the new version. The bot is offline for about a minute. Progress and the log appear under **Last action**.

If the new version doesn't come up properly, **Roll back** brings back the previous program.

> Rolling back does not undo database changes made by the newer version. If you need that, restore the backup taken before the update.

## The same from the command line

On the server, `pauru` with no arguments opens the management menu. Direct commands:

```
pauru update
pauru update --channel beta
pauru rollback
pauru backup
pauru status
```

To turn off updates from the panel: `pauru disable-agent`.
