# Independent staff password recovery (stacked on #91)

This slice adds a recovery path independent of a legacy bcrypt password. It does not recover
an irreversibly truncated password suffix, force all legacy users to rotate, or introduce a
general financial step-up policy.

## Contract

- Login and Account expose the same Persian/English recovery form. Passwords/tokens stay in
  component state, never URL parameters or browser persistence. Confirmation clears them.
- POST `/api/auth/password-reset/request` accepts only `username`. A generic `{"ok":true}`
  is not evidence of existence or delivery. Unknown, inactive and unlinked accounts are not sent
  messages. Throttles: 3/name and 10/IP in ten minutes, in addition to the existing login guard.
- Only the Telegram ID already on the active Staff row can receive the token, through its shop's
  bot. There is no user-supplied Telegram destination, email, redirect or Host-derived reset URL.
  Missing/stopped bot or undeliverable chat leaves no usable token. Delivery errors do not log tokens.
- A CSPRNG 32-byte token (43 URL-safe characters) lasts at most five minutes. Redis uses a
  SHA-256 token digest, bound to staff ID, shop, Telegram ID, session version and hash fingerprint.
  This namespace is unrelated to six-digit login codes or personal API keys.
- POST `/api/auth/password-reset/confirm` accepts only `token` and `new_password` (10–256
  characters). It rate-limits attempts, locks/reloads the Staff row, verifies its binding and
  atomically consumes the token with Redis compare-and-delete. Request another token after expiry,
  account changes or an uncertain result. A crash/DB rollback does not resurrect consumed tokens.
- The existing Argon2id writer replaces the entire password, increments session_version and revokes
  sessions. Recovery also revokes every personal API key. No automatic login or new cookie is issued.
- `/api/auth/password` rejects legacy bcrypt with `409 legacy_password_reset_required`; neither
  the remembered password nor a matching 72-byte prefix is sufficient proof for this endpoint.
  Existing modern password changes retain cookie/CSRF checks and revalidate the locked session version.

## Important scope boundary: legacy privileges are not repaired

Legacy password login is deliberately still compatible with #91. Existing authorized administrative
staff-reset / Telegram-relink powers are unchanged. Therefore this PR **does not claim** that a
compromised legacy password cannot be used through another privileged route. A general forced
rotation / privileged reauthentication restriction (including staff management, destination relink,
API-key creation, backups and financial writes) is a separate rollout/policy decision. Review this
before production; do not treat this endpoint as a complete legacy-account takeover defense.
An authorized administrator should rotate/review sensitive legacy accounts through an independently
verified operational process. Do not self-link a new Telegram account as proof of existing identity.

Accounts with no previously linked Telegram need an independently verified authorized administrator;
this form deliberately offers no weaker fallback. No arbitrary lockout deadline is imposed.

## Evidence and release checks

Local helper exercise: actual new helper, actual changed password function and Argon2id verifier,
minimal SQLite models, fakeredis Lua, controlled delivery/audit/session seams. This is NOT evidence
of PostgreSQL locking, full FastAPI, production Redis, bot delivery or real browser integration.
Frontend tests execute actual form handlers with controlled hooks/API seams; isolated strict
component typechecking is not a full frontend build. CI must run the committed PostgreSQL/Redis
security/API tests and normal frontend build before review/merge.

Staging checklist (not executed):
- Restore isolated data; preserve dual-format readers; use a staging shop bot and disposable staff.
- Verify linked destination, non-delivery, 5-minute expiry, resend throttles and token single use.
- Race two confirmations / password changes using independent PostgreSQL connections: at most one
  operation with the original version succeeds; verify the final hash/session version.
- Confirm all old cookie sessions and API keys fail; another staff/shop remains unaffected.
- Confirm reset never grants a new session. Test failed response / retry without assuming success.
- Test Persian/English on real Telegram/mobile and admin browser, keyboard focus and password managers.
- Review Redis ACL/availability, deployment capacity for Argon2id, secret redaction and rollback.
  A rollback must keep Argon2id support; never restore old credentials to undo this release.
