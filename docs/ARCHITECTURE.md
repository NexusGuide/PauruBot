# PauruBot architecture

PauruBot is a Telegram VPN subscription sales platform built as a **modular monolith**: one Python
package, one database, several processes. Feature IDs (`PAY-02`, `SVC-10`, …) refer to the NexusGuard
feature matrix tracked in [FEATURES.md](FEATURES.md).

## Processes

| Process  | Entry point              | Responsibility |
|----------|--------------------------|----------------|
| `api`    | `paurubot api`           | FastAPI: admin/reseller API, Mini App API, payment and panel webhooks, Telegram webhook (optional), static frontends |
| `bot`    | `paurubot bot`           | aiogram 3 long polling (skipped when the bot runs in webhook mode inside `api`) |
| `worker` | `paurubot worker`        | Scheduler and durable queues: payment fulfillment, provisioning, deliveries, broadcasts, usage sync, reports, backups |
| `cli`    | `paurubot <command>`     | Ops: `status`, `doctor`, `migrate`, `backup`, `restore`, `setup-token`, `reconcile` |

All processes share PostgreSQL (source of truth) and Redis (FSM state, locks, rate limits, caches,
OTP). Nothing important lives only in process memory, so any process can restart at any time.

## Layers

```
paurubot/
├── api/            HTTP adapters (routers, schemas, dependencies)
├── bot/            Telegram adapters (routers, keyboards, middlewares)
├── worker/         scheduler, queue runners, job registry
├── cli/            operator commands
├── core/           domain services: shops, customers, catalog, orders, services, discounts, texts, settings
├── ledger/         double-entry wallet ledger (append-only)
├── payments/       provider protocol, fulfillment engine, providers/*
├── panels/         driver protocol, safe HTTP client, registry, drivers/*
├── provisioning/   provision queue, lifecycle operations, usage sync
├── notifications/  delivery queue, broadcast queue, alert rules
├── security/       secret box, SSRF guard, redaction, CSRF, CSP, rate limits, initData, passwords
├── auth/           staff, sessions, RBAC permission matrix
├── audit/          audit log with before/after diffs
├── migration/      importers from other bots
├── i18n/           locale catalogs (fa, en) and message variables
└── db/             SQLAlchemy models, sessions, Alembic glue
```

Rules:

* Adapters (`api`, `bot`, `worker`) call domain services; they never write models directly.
* Domain services take an `AsyncSession` and never commit unless they own the unit of work
  (documented on the function). Long external calls (panels, gateways) happen **outside**
  database transactions; state transitions are guarded by leases or compare-and-set updates.
* Every tenant-owned row carries `shop_id`, and every query on such a table filters on it.

## Tenancy (TEN-*)

* `shops` — the root shop is created by the setup wizard. Reseller sub-shops (`parent_id`,
  own plans, own wallet scope, optional own bot token) arrive in v1.1/v2 on the same schema.
* `customers` — a Telegram identity inside one shop (`unique(shop_id, telegram_id)`). Wallets are
  per customer, so wallets are isolated per shop by construction (TEN-02).
* `staff` — people who operate a shop (owner, admin, finance, support, reseller). Permissions are
  resolved from the role matrix in `auth/rbac.py`; resellers are deny-by-default and get an explicit
  allow-list (TEN-03).

## Money (FIN-01, FIN-02)

* Amounts are integers in the shop currency's base unit (Toman for IRR shops). Floats never touch money.
* `ledger_accounts` per shop: one wallet per customer plus system accounts (`revenue`, `gateway:<provider>`,
  `promotions`, `adjustments`, `refunds`).
* `ledger_journals` group `ledger_entries`; every journal sums to zero. Entries are immutable (a PostgreSQL
  trigger rejects UPDATE and DELETE). Corrections are new reversing journals.
* `wallets.balance` is a cached projection updated in the same transaction as the journal, under
  `SELECT … FOR UPDATE`. A reconciliation job compares it with `SUM(entries)` and alerts on drift.
* Every journal carries a unique `idempotency_key`, so retried operations cannot double-post.

## Payments (PAY-01, PAY-02)

```
created → pending → verifying → paid → (fulfilled)
                 ↘ failed / expired          ↘ refunded / disputed
```

* Providers implement `PaymentProvider` (`create`, `verify`, `refund`, optional `parse_webhook`).
* Uniqueness: `(provider, provider_tx_id)` and `idempotency_key`. Amount, currency, merchant and
  destination are checked on verify.
* Fulfillment is lease-based: a worker claims paid, unfulfilled payments with
  `FOR UPDATE SKIP LOCKED`, writes a `claim_token` and `lease_until`, then in **one transaction**
  credits the ledger, settles the order and enqueues provisioning (transactional outbox).
* Card-to-card receipts are hashed (exact SHA-256 and perceptual hash) to flag duplicates (PAY-06).

## Provisioning (SVC-*)

* `provision_jobs` is a durable queue with attempts, exponential backoff and a dead state. A failed
  purchase after the final attempt is refunded through a reversing journal and shown in the admin inbox.
* Telegram deliveries go through `deliveries` (SVC-10) so a blocked or flooded send is retried, not lost.
* Usage sync polls drivers that have no webhooks; PasarGuard webhooks (PNL-03) update services in real time.

## Panels (ARC-03, PNL-*)

* `PanelDriver` protocol: `health_check`, `create_user`, `get_user`, `update_user`, `delete_user`,
  `reset_usage`, `revoke_subscription`, `list_options`.
* Drivers share `SafeHttpClient`: strict timeouts, no cross-host redirects, SSRF guard on every
  resolved address, redacted logs, and TLS that is **always verified** — either against the public CA
  store or against a pinned certificate the owner approved for that panel (self-signed panels).
* Credentials are stored encrypted (SEC-09) and decrypted only inside the driver factory.

## Frontends (APP-01, ADM-*)

`frontend/` is one Vite + React + TypeScript project with two entries: `/admin` (owner, staff and
reseller panel) and `/app` (Telegram Mini App). The build output is served by the `api` process with a
nonce-free strict CSP (no inline scripts at all).

## Deployment (OPS-*)

Docker Compose: `api`, `bot`, `worker`, `postgres`, `redis`, `caddy` (automatic TLS). Images are pinned by
digest in releases. Backups are encrypted archives with a manifest of SHA-256 hashes; the master key is
escrowed inside the archive, wrapped with a separate backup passphrase, so a restore on a fresh server
recovers panel credentials too.
