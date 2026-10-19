# Password hash transition (audit S02)

New and explicitly changed staff passwords now use self-describing Argon2id hashes (v=19,
64 MiB memory, 3 iterations, parallelism 1, 16-byte salt, 32-byte hash). Every input byte is
hashed, including UTF-8 text past bcrypt's 72-byte boundary. The existing String(255) column
holds the encoded hash; no schema migration or API password-length change is needed.
`pyproject.toml` and `requirements.txt` both install `argon2-cffi`. Keep bcrypt to read old rows.

## Compatibility is deliberate, not a claim that old secrets were repaired

Existing `$2a$`, `$2b$` and `$2y$` bcrypt hashes remain readable with the previous 72-byte
semantics. Accounts are not locked out merely because their password exceeded that limit.
**A legacy hash has irreversibly lost its suffix: suffix collisions remain until an explicit
password change/reset.** This PR closes truncation for new hashes, not all existing accounts.
There is no automatic rehash at login: doing so could silently bless an arbitrary suffix of
an ambiguous old password and lock out its actual holder.

Use the existing authenticated password-change endpoint, authorised staff password reset,
or Telegram login followed by authorised reset. A password change still increments
`session_version` and revokes existing sessions. Announce a password-rotation campaign for
remaining bcrypt accounts; do not log passwords or expose encoded hashes to the browser.
Any forced deadline/reset policy is a separate product/operations decision.

## Rollout and rollback

- Test against a restored, isolated database first; verify login, password reset and session
  revocation for both hash formats. Deploy dependencies before application startup.
- All components reading staff passwords must understand Argon2id before new hashes are written.
  Do not run old-only readers alongside new writers. Coordinate the application rollout.
- Once any Argon2id hash exists, rolling back to the old bcrypt-only verifier breaks those
  accounts' password login. Keep this dual-format verifier in any rollback build; never
  convert hashes back or restore an old database just to undo a code rollout.
- Budget 64 MiB per concurrent Argon2 operation and benchmark on deployment hardware. KDFs
  remain synchronous as before; event-loop offloading and workload load tests are not part of
  this PR. Existing login/rate guards remain in place; no new MFA guarantee is claimed.

## Regression coverage

ASCII, Persian and emoji prefixes at exactly 72 UTF-8 bytes must not alias after the boundary;
encoded hashes must be salted/self-describing and fit the column. Bad/missing hashes fail
closed. Legacy login does not mutate the stored hash; an explicit API password change writes
Argon2id and normal staff authentication reads it. Existing CSRF and session-revocation tests
remain applicable. Local Python 3.13 tests are not a replacement for the supported CI matrix.
