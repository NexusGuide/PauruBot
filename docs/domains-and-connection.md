# Link domains, SSL and connection status

Two pages under **Panels** in the admin panel (ClockBot parity B4).

## Connection status (`/connection`, PNL-15)

Every panel of the shop with:

- its health
- the last response time and the day's average
- uptime over the last day and week
- the last 48 checks as bars: green is fast, amber is slow, red is failed
- each node with its state

The page refreshes itself every 30 seconds while it is open. **Check all now** probes every active panel at once, at most once per 30 seconds per shop.

Everything shown comes from the monitor's own checks (PNL-12, `panel_checks`). The page never shows a panel's address or login. It needs `panels.read`.

## Link domains and SSL (`/domains`, PNL-16)

A shop can serve its subscription links (`/s/<token>`) on a domain of its own, for example one behind a CDN. This keeps the links apart from the main domain, where the panel, the Mini App and the bot live.

### Setting up a domain

1. In the domain's DNS, add an A record to the server's IP, or a CNAME to the main domain.
2. Add the domain on the page. It is checked right away, and Caddy gets its certificate on that first visit.
3. Once it shows **Healthy**, press **Use for links**.

### How it works

- **Certificates (`deploy/Caddyfile`):**
  - The catch-all `https://` site uses on-demand TLS.
  - Before Caddy asks for a certificate, it calls `GET /internal/tls-ask?domain=…`. That route answers 200 only for a registered domain.
  - Caddy renews the certificates itself.
  - On these names Caddy serves `/s/*` only. Everything else answers 404.
  - The main site answers 404 for `/internal/*`.
- **The check** runs on "check now" and every 6 hours (`domains.check`). It looks at three things:
  - **DNS:** the name must resolve, and to public addresses only.
  - **Certificate:** it must be valid for the name. The check records its expiry and issuer.
  - **Route:** the name must reach this server. `/s/_/domain` answers an HMAC of the host keyed by `MASTER_KEY`, which only this install can produce.
- **Statuses:** `ok`, `pending`, `dns`, `tls`, `unreachable` and `not_ours`. Each one has its own fix hint on the page.
- **Links:**
  - Only a healthy domain carries links.
  - If the chosen domain fails a check, new links fall back to the main domain until it is healthy again.
  - Removing the domain does the same.
  - Links already sent keep the domain they were sent with, and may stop working once it is removed.
- **Who can do what:**
  - Anyone with `settings.read` sees the page.
  - Adding, removing, checking and choosing need `panels.secrets` (the owner).
  - A domain belongs to one shop only. A shop has at most 20.

Without the bundled Caddy, the proxy in front of the API must do the same: issue certificates for these names, ideally gated by `/internal/tls-ask`, and forward `/s/*` to the API.
