# Feature tracker

Every row of the NexusGuard feature matrix with its phase in PauruBot and its status.
Phase changes from the original matrix are marked with `*`.

Status: `todo` · `wip` · `done` (done means implemented **and** covered by tests).


## Platform & Architecture

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| ARC-01 | Modular Python core: FastAPI + aiogram + SQLAlchemy 2 + Alembic + PostgreSQL | MVP | done |
| ARC-02 | Redis-backed FSM state, locks and callback payloads | MVP | done |
| ARC-03 | Panel driver registry (one interface, many panel types) | MVP | done |
| ARC-04 | Structured logging with tags and Telegram log channels | v1.1 | done |
| ARC-05 | Ops CLI: status, logs, health, backup, restore, migrate, doctor | MVP | done |
| ARC-06 | Modular monolith with domain/service/adapter layers | MVP | done |

## Identity, Auth & Security

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| SEC-01 | Telegram WebApp initData HMAC validation | MVP | done |
| SEC-02 | Phone OTP login delivered by the bot | v1.1 | done |
| SEC-03 | Profile API key login (hashed, shown once) | v1.1 | done |
| SEC-04 | Session version revoke on credential change | MVP | done |
| SEC-05 | CSRF double-submit + Origin/Referer guard | MVP | done |
| SEC-06 | CSP nonce + security headers | MVP | done |
| SEC-07 | Login brute-force guard with trusted-hop X-Forwarded-For | MVP | done |
| SEC-08 | First-run setup wizard protected by one-time gate token | MVP | done |
| SEC-09 | Secrets encrypted at rest (Fernet + HKDF domain separation) | MVP | done |
| SEC-10 | SSRF guard for panel base URLs | MVP | done |
| SEC-11 | Webhook secret-token authentication (constant-time compare) | MVP | done |
| SEC-12 | Log redaction of tokens and passwords | MVP | done |
| SEC-13 | Terms-of-service acceptance gate | v1.1 | done |
| SEC-14 | Ban, anti-flood and maintenance middlewares | MVP | done |
| SEC-15 | Fine-grained RBAC with authorization matrix tests | MVP | done |

## Multi-tenant & Resellers

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| TEN-01 | Org hierarchy Owner → Level-1 → Level-2 with visible scope | v1.1 | done |
| TEN-02 | Per-shop wallet isolation | MVP | done |
| TEN-03 | Reseller feature permissions (deny by default) | MVP | done |
| TEN-04 | Dedicated reseller bot with its own token | v2 | done |
| TEN-05 | Reseller billing: PAYG, hourly and usage-based with snapshots | v1.1 | done |
| TEN-06 | Reseller low-balance warning and auto-suspend | v1.1 | done |
| TEN-07 | Reseller usage cap and capacity limits | v1.1 | done |
| TEN-08 | Agent expiry and max-buy limits | v1.1 | done |
| TEN-09 | Reseller application / approval workflow | v1.1 | done |
| TEN-10 | Import existing panel users into a reseller | v1.1 | done |
| TEN-11 | Move users between panel admins | v2 | done |
| TEN-12 | Fixed reseller prices per plan (group or reseller) and panels hidden from a group | v1.1* | done |
| TEN-13 | Reseller panel in the bot: figures, search, quick sale, expiring list with bulk renewal, sales report, sub-resellers | v1.1* | done |

## VPN Panel Integrations

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| PNL-01 | PasarGuard deep admin: users, admins, groups, hosts, nodes, templates, inbounds | MVP | done |
| PNL-02 | PasarGuard live role/ACL and quota read from the panel | v1.1 | done |
| PNL-03 | PasarGuard webhook events (created, expired, limited, usage %, days left) | MVP | done |
| PNL-04 | Marzban driver | MVP | done |
| PNL-05 | Marzneshin driver | v1.1 | done |
| PNL-06 | X-UI (Sanaei / Alireza) and S-UI drivers | MVP* | done |
| PNL-07 | Hiddify driver | v2 | done |
| PNL-08 | WireGuard: WGDashboard and WGMate | v2 | done |
| PNL-09 | Remnawave driver + usage sync | v2 | done |
| PNL-10 | Guard and Rebecca drivers | v2 | done |
| PNL-11 | MikroTik and IBSng drivers | v2 | done |
| PNL-12 | Panel and node uptime monitoring with alerts | v1.1 | done |
| PNL-13 | Node traffic and host resource gauges | v1.1 | done |
| PNL-14 | Per-panel inbound / protocol settings | v1.1 | done |

## Catalog & Sales

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| CAT-01 | Plans, categories and products with per-location availability | MVP | done |
| CAT-02 | Plan audience, add-ons, per-plan naming and button styles | v1.1 | done |
| CAT-03 | Custom buy: user chooses volume and days | v1.1 | done |
| CAT-04 | Trial accounts with quota generations and reservations | MVP | done |
| CAT-05 | Manual config stock / inventory sale | v2 | done |
| CAT-06 | Manual service creation by admin | v1.1 | done |
| CAT-07 | Bulk purchase | v2 | done |
| CAT-08 | Guest purchase (no prior registration) | v2 | done |
| CAT-09 | Configurable username generation methods | MVP | done |

## Service Lifecycle

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| SVC-01 | Renew, extra volume, extra time, upgrade | MVP | done |
| SVC-02 | Queued renewal processor | v1.1 | done |
| SVC-03 | On-hold services (start on first connection) | v1.1 | done |
| SVC-04 | Location / panel change for an existing service | v2 | done |
| SVC-05 | Expiry and low-volume notifications | MVP | done |
| SVC-06 | Auto disable / re-activate configs | v1.1 | done |
| SVC-07 | Bulk volume/time increase for many users | v1.1 | done |
| SVC-08 | Branded subscription link page | v1.1 | done |
| SVC-09 | QR code delivery | MVP | done |
| SVC-10 | Delivery failure queue with retry | MVP | done |
| SVC-11 | Usage chart per service | v1.1 | done |
| SVC-12 | Service quota cache | MVP* | done |

## Payments

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| PAY-01 | Payment provider interface | MVP | done |
| PAY-02 | Fulfillment state machine with leases / claim tokens | MVP | done |
| PAY-03 | Payment settlements | v1.1 | done |
| PAY-04 | Card-to-card with receipt upload and admin approval | MVP | done |
| PAY-05 | Card auto-approve rules | v1.1 | done |
| PAY-06 | Duplicate receipt detection (image hash) | MVP* | done |
| PAY-07 | Zarinpal | MVP | done |
| PAY-08 | Aqayepardakht, IranPay/Tetra98, Variza | v2 | done |
| PAY-09 | AbanGateway, AtlasPay, Blupal, Tronado, CubePay | v2 | done |
| PAY-10 | NowPayments and Plisio | v1.1 | done |
| PAY-11 | Direct on-chain USDT-TRC20 / TRX / TON | v1.1 | done |
| PAY-12 | Telegram Stars | MVP* | done |
| PAY-13 | FX / crypto price sync | v1.1 | done |
| PAY-14 | Payment bonus / cashback per method | v1.1 | done |
| PAY-15 | Direct pay (invoice without wallet top-up) | MVP | done |
| PAY-16 | Payment expiry job | MVP | done |

## Wallet, Billing & Finance

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| FIN-01 | Wallet top-up and balance | MVP | done |
| FIN-02 | Append-only ledger with reconciliation | MVP | done |
| FIN-03 | Charge / gift codes | v1.1 | done |
| FIN-04 | Finance reports and daily report | MVP | done |
| FIN-05 | Excel export | v1.1 | done |
| FIN-06 | Top customers report | v1.1 | done |
| FIN-07 | Shop bundle export / import | v2 | done |

## Growth & Marketing

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| GRW-01 | Referral system with rewards | v1.1 | done |
| GRW-02 | Loyalty tiers, rules and rewards | v2 | done |
| GRW-03 | Lucky wheel | v2 | done |
| GRW-04 | Lottery | v2 | done |
| GRW-05 | Discount codes incl. sticky discount | MVP | done |
| GRW-06 | Broadcast with rate-limited queue and job tracking | MVP | done |
| GRW-07 | Forced channel join | MVP | done |
| GRW-08 | Trust channel | v2 | done |

## Telegram Bot UX

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| BOT-01 | Editable bot texts with message variables | MVP | done |
| BOT-02 | Drag-and-drop keyboard layout editor | v1.1 | done |
| BOT-03 | Bot appearance, colored buttons, premium emoji | v1.1 | done |
| BOT-04 | Multi-language (fa, en, ru, zh) | MVP | done |
| BOT-05 | Help buttons and app downloads from GitHub releases | v1.1 | done |
| BOT-06 | Generated user info card image | v2 | done |
| BOT-07 | Telegram Business account automation | v2 | done |
| BOT-08 | User color/risk tags | v1.1 | done |
| BOT-09 | Delete-with-reason and user notification | v1.1 | done |

## Mini App & Web

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| APP-01 | React + TypeScript Mini App: home, buy wizard, services, renew, wallet, transactions | MVP | done |
| APP-02 | PWA install and icons | v1.1 | done |
| APP-03 | Role-aware Mini App for resellers / admins | v2 | done |

## Admin Web Panel

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| ADM-01 | Dashboard with live metrics | MVP | done |
| ADM-02 | Inbox of pending orders/payments with dismissals | MVP | done |
| ADM-03 | Audit log of admin actions | MVP | done |
| ADM-04 | Table bulk actions, search, sort, paging | MVP | done |
| ADM-05 | In-panel help docs with search | v1.1 | done |
| ADM-06 | Sales funnel analytics | v2 | done |
| ADM-07 | Panel settings modal (PasarGuard server settings) | v1.1 | done |
| ADM-08 | Environment analyzer / doctor | v1.1 | done |

## Support & Communication

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| SUP-01 | Tickets user↔admin with attachments | MVP | done |
| SUP-02 | Internal panel tickets reseller↔owner | v1.1 | done |
| SUP-03 | Report channel split by topics | MVP | done |
| SUP-04 | Support contacts | MVP | done |

## Jobs & Monitoring

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| JOB-01 | Job registry and scheduler | MVP | done |
| JOB-02 | Daily status report | MVP | done |
| JOB-03 | IP block notification | v2 | done |
| JOB-04 | Log cleanup | MVP | done |
| JOB-05 | Metrics, tracing and dead-letter queues | v1.1 | done |

## Install, Backup & Update

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| OPS-01 | One-line installer with menu | MVP | done |
| OPS-02 | Release channels and version pinning (beta/release, --version) | MVP | done |
| OPS-03 | Docker Compose stack | MVP | done |
| OPS-04 | Automatic SSL (ACME) | MVP | done |
| OPS-05 | Backup with manifest hashes and validated restore | MVP | done |
| OPS-06 | Scheduled backup sent to Telegram | MVP | done |
| OPS-07 | Panel-based update with rollback UI | v1.1 | done |
| OPS-08 | SQLite → PostgreSQL migration tool | v1.1 | done |

## Migration & Import

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| MIG-01 | Importers from DDBot, Faoxima, WizWiz and SQL dumps | v1.1 | done |

## Quality & CI

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| QA-01 | Curated security regression suite in CI | MVP | done |
| QA-02 | Ruff lint/format + compile in CI, Dependabot | MVP | done |
| QA-03 | Docs generated from source | v1.1 | done |

## Added during review

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| NEW-01 | Master key escrow inside encrypted backups (restores recover panel credentials) | MVP | done |
| NEW-02 | TLS certificate pinning for self-signed panels instead of disabling verification | MVP | done |
| NEW-03 | Periodic usage sync for panels without webhooks | MVP | done |
| NEW-04 | Refund and service cancellation policy | MVP | done |
| NEW-05 | Error monitoring hook (Sentry DSN, optional) | MVP | done |
