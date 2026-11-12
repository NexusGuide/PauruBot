# Full audit of 12 November 2026: fixes

The read-only audit of commit `28b8748` found no critical or high issues. This change fixes what it
did find. Each fix has a test that failed before the change and passes after it.

| ID | What was wrong | Fix | Test |
|----|----------------|-----|------|
| SEC-01 | Five wrong passwords for a username locked that account for everyone, including the owner's code and password-reset requests. Anyone who knew the username could keep the owner out. | Failures count per address+account first, so a guesser locks only itself. The account as a whole locks only after ten times as many failures, from many addresses. Code and reset sends are limited per requesting address, with a larger cap per account. | `tests/security/test_login_lockout_scope.py` |
| SEC-02 | Bad Mini App initData was checked against every reseller bot's token before any rate limit. | An address that sends 200 bad sign-ins in 10 minutes gets 429 before the scan. The limit is generous because mobile carriers share addresses. | `tests/security/test_webapp_bad_init_data.py` |
| SEC-03 | Dumps uploaded to move from another bot (customers, phones, balances) stayed on disk forever. | The real import deletes its upload. Uploads older than three days are removed every hour. | `tests/unit/test_migration_uploads_cleanup.py` |
| SEC-04 | postgres, redis and caddy ran from moving tags, and the GHCR copies used when Docker Hub is blocked were unsigned. | The release signs each copy with cosign. The installer verifies a copy before using it and pins both sources by digest in `.env`. | `tests/unit/test_base_image_trust.py` |
| SEC-06 | `%` and `_` typed into staff search boxes acted as SQL wildcards. | One helper, `paurubot/db/search.py`, escapes them for every contains-search. | `tests/unit/test_search_literal.py` |
| SEC-07 | A backup header's scrypt `r` and `p` were read without bounds. | Only the values PauruBot writes are accepted, and the check runs before any key is derived. | `tests/unit/test_backup_header_limits.py` |
| SEC-08 | IPv6 addresses carrying an IPv4 address (NAT64, 6to4) were not judged by that address. The Caddyfile was never validated. | The outbound policy unwraps them, as it already did for IPv4-mapped addresses. CI validates the Caddyfile with the real Caddy. | `tests/security/test_ssrf_embedded_ipv4.py`, `test_deployment_hardening.py` |
| SEC-F11 follow-up | Terms or phone asked "before purchase" were not asked for a reseller's bulk renewal, which spends the wallet. | `POST /api/webapp/reseller/renew` counts as a purchase. | `tests/unit/test_webapp_gate.py` |
| BE-01 | Two adds at once could both take a shop's last forced-channel or link-domain slot. | The shop row is locked for the count and the insert. | `tests/unit/test_shop_limits_race.py` |
| BE-02 | Add-on prices were worked out for any service id before ownership was checked. | Ownership is checked first. | existing bot tests |
| OPS-01 | The image built the frontend on Node 26 while CI tested Node 22. On Node 26 two frontend tests also assigned the read-only Web Storage globals. | CI uses Node 26, and the tests replace the globals with `defineProperty`. | `tests/unit/test_dependency_lock.py`, frontend suite |
| OPS-02 | The app containers kept Docker's default capabilities. | api, bot, worker and migrate drop all capabilities and set `no-new-privileges`. | `test_deployment_hardening.py` |
| OPS-03 | The release workflow granted write permissions to every job. | It is read-only by default. Only `image` and `mirror` get write and OIDC access. | `tests/unit/test_base_image_trust.py` |

## Not changed, and why

- **SEC-05** (domain check reaching internal addresses) was a false positive. `core/domains.check`
  classifies every resolved address with the SSRF policy before connecting, and connects only to
  that address.
- **The installer never upgrades an existing cosign.** Replacing a binary the server owner installed
  is their decision. Verification still uses the identity and issuer flags, which every supported
  cosign version honours.
- **Reseller webhook secret derived from the bot token.** This is by design: only the token holder
  can derive it, and they already control that bot.
- **PERF-01** (log redaction scans every registered secret). The set holds one entry per distinct
  secret. It is not a defect at current sizes.
