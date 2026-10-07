# PauruBot

A Telegram bot for selling VPN subscriptions, with a web admin panel, a Telegram Mini App and support for many panels at once.

[فارسی](README.md)

## What it does

- **Automatic sales:** customers pick a plan in the bot or the Mini App and pay. The service is created on a panel, and they get the link, a QR code and a service card picture.
- **A double-entry wallet:** every top-up, purchase, refund and reward is a posted journal. Repeating a request never moves money twice.
- **A Persian and English admin panel:** customers, services, orders, payments, tickets, plans, discounts, sales reports, sales funnel, backups and updates, with built-in help for every screen.
- **Resellers:** groups and discounts, and prepaid, usage-based or hourly billing. There are purchase limits, second-level resellers and bulk buying, and resellers can run a bot of their own with their own token.
- **Growth:** referrals, loyalty tiers, a lucky wheel, lotteries, discount and gift codes, required channels and free trials. Visitors can buy on the web without signing up, and Telegram Business automation answers messages.
- **Operations:** durable job queues, panel monitoring and alerts, usage sync, data and time warnings and device cut-off notices. Encrypted backups run daily, and updates and rollbacks start from the admin panel.

The full feature list with its status is in [docs/FEATURES.md](docs/FEATURES.md).

## Panels

| Kind | Panels |
|---|---|
| Subscription (V2Ray/Xray) | PasarGuard, Marzban, Marzneshin, Remnawave, Rebecca, Guard (v1 and v2), Hiddify, 3x-ui (Sanaei) |
| WireGuard | WGDashboard (.conf file and QR), WG-Mate |
| Username and password | MikroTik User Manager (RouterOS 7), IBSng (basic: create, read usage, delete) |

## Payments

- Card transfer with receipt review, duplicate detection and optional auto-approval
- Zarinpal, Aqayepardakht, Tetra98, Variza, AbanGateway, BluPal, CubePay and AtlasPay
- TRX through Tronado, NowPayments and Plisio
- Direct crypto (USDT-TRC20, TRX, TON) with automatic deposit detection
- Telegram Stars

No payment is credited from what a callback says alone. It is either checked with the gateway or accepted only with a valid HMAC signature. If less money arrives, the payment waits for staff review.

## Install

You need an Ubuntu or Debian server with root access and at least 1 GB of RAM, a domain pointing at it, and a bot token from @BotFather.

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/NexusGuide/PauruBot/main/install.sh)
```

The installer sets up Docker, writes `.env` and generates the keys. It then starts the services and prints a one-time link for creating the owner account. It can also run without questions:

```bash
install.sh install --domain bot.example.com --token 123:ABC --owner 123456789
```

Afterwards, `pauru` with no arguments opens the server menu: status, logs, restart, backup, restore, update and rollback.

### Key `.env` settings

| Key | Meaning |
|---|---|
| `DOMAIN`, `PUBLIC_URL` | The server's domain. Caddy obtains the TLS certificate. |
| `BOT_TOKEN` | The main bot's token |
| `OWNER_TELEGRAM_IDS` | Owners' numeric Telegram ids, comma separated |
| `MASTER_KEY` | Encrypts panel passwords and gateway keys. Losing it makes them unreadable. |
| `BACKUP_PASSPHRASE` | Encrypts backups. Keep it somewhere off the server. |
| `BOT_MODE` | `polling` or `webhook` |

Everything else (payments, plans, texts, the bot menu) is set in the admin panel. All settings are listed in [docs/reference/settings.md](docs/reference/settings.md).

## After installing

1. Create the owner account from the setup link.
2. Add your VPN panel under Panels and test the connection.
3. Create a location and a plan under Plans.
4. Turn on a payment method in Settings.
5. Make a test purchase yourself.

Every part has a full guide inside the admin panel under Help.

## Layout

One Python application runs as three processes on PostgreSQL and Redis:

- `api`: the admin panel, the Mini App, payment callbacks and webhooks
- `bot`: the main bot and resellers' own bots
- `worker`: queues and scheduled jobs

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/AUTHZ.md](docs/AUTHZ.md) for roles and [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

| Part | Stack |
|---|---|
| Server | Python 3.12, aiogram 3, FastAPI, SQLAlchemy 2, Alembic |
| Data | PostgreSQL 16, Redis |
| Interfaces | React 19, TypeScript, Vite, Tailwind 4 |
| Runtime | Docker Compose, Caddy |

## Development

```bash
pip install -e .[dev]
bash scripts/check.sh          # ruff, mypy and tests (needs local PostgreSQL and Redis)
cd frontend && npm ci && npm run build
```

Reference docs (settings, API, roles and commands) are generated from the code:

```bash
python -m paurubot.ops.docgen
```
