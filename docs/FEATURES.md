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
| TEN-14 | Selling PasarGuard admin accounts: plans with role, quota, user cap and period; renewal, extra traffic and user slots, password, pause, usage sync, alerts and expiry | v1.1* | done |
| TEN-15 | Reseller web panel and Mini App: sign-in by Telegram code made on approval, selling desk with customer names, expiring list with bulk renewal | v1.1* | done |
| TEN-16 | Paid reseller membership with renewal | v1.1* | done |
| TEN-17 | Reseller sales bot built and run from Telegram: token, markup, plan prices, cards, support, on/off; staff build, stop or allow it from the reseller card | v1.1* | done |

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
| CAT-10 | Panels hidden from one customer (and from a reseller's own bot) | v1.1* | done |
| CAT-11 | Another trial for one customer and a new trial round for everyone from the bot | v1.1* | done |
| CAT-12 | Customers choose their service username at purchase | v1.1* | done |

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
| SVC-13 | Renewal modes per plan or shop (extend or restart; reset or carry data) | v1.1* | done |
| SVC-14 | Customers delete their own service with a refund of the unused value | v1.1* | done |
| SVC-15 | Customers turn their own reminders off | v1.1* | done |
| SVC-16 | Staff notes on services | v1.1* | done |
| SVC-17 | Every link in one message and every config in one file | v1.1* | done |

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
| PAY-17 | Card trust per customer (trusted, blocked), card shown by rotation or at random, random amount tail, late auto-approval | v1.1* | done |
| PAY-18 | Customers register their bank card before paying card to card, approved by staff | v1.1* | done |

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
| FIN-08 | Wallet transfer between customers with a minimum and a daily cap | v1.1* | done |

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
| GRW-09 | Welcome gift and referral limits (minimum order, daily cap per referrer, banned referrers) | v1.1* | done |
| GRW-10 | Private discount codes and My discounts; referral reward on every purchase with a monthly cap | v1.1* | done |

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
| BOT-10 | Premium emoji in every message, set from the bot | v1.1* | done |
| BOT-11 | Start sticker, GIF or photo and a buy banner; one-tap copy buttons | v1.1* | done |
| BOT-12 | Menu as a keyboard, glass buttons or Mini App only | v1.1* | done |

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
| ADM-09 | Admin tools in the bot: customer services, free service, message, referrals, reseller group, plans and bulk prices, panels, money figures with Excel, settings, gift to many, broadcast audience/button/pin | v1.1* | done |

## Support & Communication

| ID | Feature | Phase | Status |
|----|---------|-------|--------|
| SUP-01 | Tickets user↔admin with attachments | MVP | done |
| SUP-02 | Internal panel tickets reseller↔owner | v1.1 | done |
| SUP-03 | Report channel split by topics | MVP | done |
| SUP-04 | Support contacts | MVP | done |
| SUP-05 | "Report a problem" on a service: a ticket with its state, and a known-outage answer | v1.1* | done |

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
| OPS-09 | Ops in the bot: system status, backups on demand and download, log groups set with /setlog (forum topics included) | v1.1* | done |

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
| NEW-06 | Admin quick commands /config + /extend in the bot (service infocard, extend flow) | v1.1* | done |
| NEW-07 | Admin /lookup by panel username (local search; live-panel query is NEW-16) | v1.1* | done |
| NEW-08 | Service-card transfer prompt, disorder ticket and changelink rotation in bot + Mini App | v1.1* | done |
| NEW-09 | Callback double-tap lock (opt-in, off by default) + reply-keyboard auto-remove option | v1.1* | done |
| NEW-10 | Jalali month/weekday names + jstrftime_fa in bot and Mini App | v1.1* | done |
| NEW-11 | Admin /stats hour slices (1h, today, yesterday, month) | v1.1* | done |
| NEW-12 | One-tap renew on every alert + Mini App renew CTA on expiring/low-volume rows | v1.1* | done |
| NEW-13 | Subscription link-button toggle + forced-miniapp mode in the Mini App | v1.1* | done |
| NEW-14 | Per-guide app buttons in Mini App help (payload when present, static fallback) | v1.1* | done |
| NEW-15 | Referral banner + start-gift texts (Mini App banner wired; bot photo variant is NEW-19) | v1.1* | done |
| NEW-16 | Live-panel foreign-username query via provisioning/importer | v1.1* | todo |
| NEW-17 | Service transfer core helper backing the bot transfer button | v1.1* | todo |
| NEW-18 | Backend settings for subscription_link_button / forced_miniapp_mode / guide apps | v1.1* | todo |
| NEW-19 | Referral banner photo variant sent from the bot | v1.1* | todo |
| MGMT-01 | Staff command deck hub in the bot (pulse entry + panel hall entry) | v1.1* | done |
| MGMT-02 | Pulse slices: last hour / today / yesterday / this month / last month / custom day | v1.1* | done |
| MGMT-03 | Panel hall in the bot: health, stats, distribution, money, rename/check/toggle/delete, extras, username rule, showcase flags | v1.1* | done |
| MGMT-10 | Faoxima-style mirror hub in the bot (nine sections, stub cards for wave 2) | v1.1* | done |
| MGMT-11 | Mirror panels menu: add wizard with probe, quick time/volume prices, extended leaves (FX/test desks, limits, hidden list, toggles) | v1.1* | done |
| MGMT-12 | Mirror full-stats card (all-time slice, custom date range, averages, renewal share) | v1.1* | done |
| MGMT-20 | Mirror finance hub: gateway table with toggles, per-gateway desks, card desk with accounts | v1.1* | done |
| MGMT-21 | Mirror shop hub: categories, plans, gift and discount codes, bulk display | v1.1* | done |
| MGMT-22 | Mirror users hub: team list, staff add/remove, lookup and receipts links | v1.1* | done |
| MGMT-23 | Mirror support hub: support username, departments, contacts, open tickets | v1.1* | done |
| MGMT-24 | Mirror help hub: bot text overrides list, edit and reset | v1.1* | done |
| MGMT-25 | Mirror channels hub: forced channels CRUD, reports chat | v1.1* | done |
| MGMT-26 | Mirror technical hub: real capability toggles plus redis/version info | v1.1* | done |
