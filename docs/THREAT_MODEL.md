# Threat model

## Assets

| Asset | Why it matters |
|-------|----------------|
| Wallet balances and ledger | Direct money; forged credits are theft |
| Panel credentials | Full control of VPN infrastructure and every customer config |
| Payment merchant keys | Redirecting or forging payments |
| Bot token | Impersonating the shop to all customers |
| Customer data (Telegram IDs, phone numbers, subscription links) | Privacy; subscription links are bearer credentials |
| Admin sessions | Everything above |

## Actors

* **Customer** — untrusted. Controls every message, callback payload, Mini App request and uploaded receipt.
* **Reseller** — partially trusted staff, limited to its own customers.
* **Staff** (support/finance/admin) — trusted for their role only.
* **Owner** — fully trusted; still audited.
* **External systems** — payment gateways, panels, Telegram. Their responses are validated, never trusted blindly.
* **Network attacker** — can reach public endpoints (API, webhooks, Mini App).

## Threats and controls

| # | Threat | Control | Feature |
|---|--------|---------|---------|
| T1 | Forged Mini App requests | initData HMAC validation + `auth_date` max age | SEC-01 |
| T2 | Callback tampering (another user's service ID) | Every lookup scoped by `(shop_id, customer_id)`; callback data never trusted as authorization | SEC-15 |
| T3 | Double credit from replayed or concurrent payment callbacks | Unique `(provider, provider_tx_id)`, idempotency keys, lease-based fulfillment, compare-and-set status | PAY-02 |
| T4 | Amount tampering at the gateway | Verify amount, currency, merchant and status server-to-server | PAY-01 |
| T5 | Crypto payment to a wrong address or network | Destination + network check, integer base units, confirmations, unique tx hash | PAY-11 |
| T6 | Reused card receipts | SHA-256 and perceptual hash duplicate detection, admin review | PAY-04, PAY-06 |
| T7 | Balance race (two purchases at once) | Wallet row lock, journals that must sum to zero, reconciliation job | FIN-02 |
| T8 | SSRF through panel base URLs | Scheme allow-list, DNS resolution check on every request, metadata and link-local always blocked, private ranges only with explicit owner approval, no cross-host redirects | SEC-10 |
| T9 | MITM on panels with self-signed certificates | TLS always verified; self-signed panels use a pinned certificate approved by the owner | PNL-* |
| T10 | Secret leakage via logs, backups, errors | Redaction filter, encrypted secrets at rest, encrypted backups, generic API errors with correlation IDs | SEC-09, SEC-12 |
| T11 | Admin credential stuffing | Login guard keyed by account and trusted-hop client IP, bcrypt, session versioning | SEC-04, SEC-07 |
| T12 | CSRF / XSS on the admin panel | Double-submit CSRF + Origin check, strict CSP without inline scripts, React escaping | SEC-05, SEC-06 |
| T13 | Takeover of a fresh install | Setup wizard requires a one-time token printed to the operator console | SEC-08 |
| T14 | Webhook spoofing (panel, gateway) | Secret token compared in constant time, provider signature checks | SEC-11, PNL-03 |
| T15 | Reseller reading other shops' data | `shop_id` scoping in every query, reseller scope filters, tenant isolation tests | TEN-02, TEN-03 |
| T16 | Flooding the bot | Redis rate limits per user, ban and maintenance middlewares | SEC-14 |
| T17 | Malicious update package | Signed releases with checksums, atomic switch, no self-update from requests | OPS-01, OPS-07 |
| T18 | Losing secrets on restore | Master key escrowed in the backup, wrapped with a separate backup passphrase; restore drill | OPS-05 |
| T19 | Broadcast abuse / Telegram bans | Rate-limited broadcast queue respecting 429 `retry_after` | GRW-06 |
| T20 | Trial farming | Trial policy: one per Telegram account, optional phone requirement, global daily quota | CAT-04 |

## Residual risks

* A compromised owner account or server root is out of scope.
* Card-to-card payments rely on human review; duplicate detection reduces but cannot remove fraud.
* Panel APIs themselves may be vulnerable; PauruBot only limits how it talks to them.
