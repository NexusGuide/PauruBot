# F-09: Mini App help, connection instructions and app sources

Stacked after PR52 -> PR51 -> PR50 -> PR49. No migration, new setting, staff permission,
deployment or feature flag activation. More -> Help and apps (/app/help).

## Sources and API

Authenticated GET /api/webapp/help uses only the signed-in shop's existing `help_items`,
`download_apps` and bot username. Arbitrary shop IDs do not select content. Response is an explicit
projection: title/plain text/safe URL, app name/platform/store URL/constructed repository releases
page, bot URL. No full settings dump, cache-chat IDs, Telegram file IDs, asset patterns, staff data,
payment credentials or private service configuration. No GitHub API, Redis release-cache lookup,
asset fetch, file proxy or Telegram API call is made by the endpoint itself. Existing authentication
still uses its normal rate limiting/settings dependencies.

Configured help items retain their source order. At most the first 30 entries are considered;
malformed/empty unusable entries are skipped, titles capped at 128 Unicode code points and text at
8000. At most the first 20 download app entries are considered; names capped at 32 code points.
Known platforms follow the existing bot's android/ios/windows/mac/linux schema. Bad entries do not
prevent good entries from showing. General connection guidance is packaged fa/en text; shop content
is displayed as configured, not automatically translated. No invented app URLs or assumed protocol
compatibility. The current support/service UI was inspected when writing the quick steps.

## UI

Read-only shop tutorials in native keyboard-operable details/summary, plain-text rendering (no raw
HTML/Markdown execution), Persian/Arabic-normalized AND-term search, explicit operating-system
filter (All by default; no device fingerprinting), loading/error/retry/empty/no-match states, manual
refresh with abort/generation guards, service/ticket/back navigation. Generic steps distinguish
subscription links, WireGuard file/QR and account credentials; client button names/compatibility
are not promised. No customer configuration or credentials are requested on this page.

Store links and repository `/releases` pages open externally with noopener/noreferrer and no-referrer.
Destination hostname is visible. Sources are configured by the shop, not independently verified,
guaranteed compatible, or guaranteed latest. No current version badge/latest-asset claim. Users must
pick the correct release/architecture themselves. Telegram cached-file delivery remains in the bot;
its link only opens that shop's chat, with explicit /apps or /help instructions, not a fabricated
start payload or silent command execution.

## Navigation policy and limits

Server and frontend both reject non-HTTPS, credentials, controls/encoded controls, backslashes,
ambiguous encoded hosts, non-ASCII URLs (percent-encoded paths are allowed), IP literals,
local/internal host suffixes, single-label hosts and non-443 ports. Hostnames are syntactically
bounded DNS labels. This may hide legacy HTTP/local/Unicode links; encode Unicode paths and update
settings to a usable HTTPS source. Inline text still appears when its URL is rejected. Shared URL
fixtures run in Python and Node; response validation fails closed on malformed structure.

This is navigation filtering, not URL reputation, domain-ownership, DNS/redirect validation,
antivirus or a safe-download guarantee. No configured link is fetched by this feature. Existing
bot/admin settings and their validation are unchanged. Cross-shop help inheritance is not added.

## Release gates

Full CI including the added test:help Node step, actual main/child-bot settings isolation,
mobile/keyboard/full production CSS, external-link behavior on Telegram iOS/Android/Desktop and
real bot /apps cached-file handoff remain gates. Keep PR48 test:confirm, PR50 test:usage and this
PR's test:help when integrating the stack. No merge, deployment or server migrations here.
