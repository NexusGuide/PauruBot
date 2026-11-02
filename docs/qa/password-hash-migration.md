# Password hashes (audit S02, AUD-10)

Staff passwords are stored as self-describing Argon2id hashes (v=19, 64 MiB memory,
3 iterations, parallelism 1, 16-byte salt, 32-byte hash). Every input byte is hashed,
including UTF-8 text past bcrypt's 72-byte boundary. The String(255) column holds the encoded
hash; `argon2-cffi` is the only password library installed.

## bcrypt hashes are no longer read

S02 kept a reader for older `$2a$`, `$2b$` and `$2y$` bcrypt hashes. bcrypt hashes only the
first 72 bytes, so any password sharing those bytes logged in. The November audit (AUD-10)
removed that reader, and `bcrypt` with it: nothing is installed yet and no release ever
wrote such a hash, so there is nothing to keep compatible with.

A row that still holds a bcrypt hash (for example from a hand-edited database) verifies
nothing; the password check runs the same Argon2 work either way, so timing does not reveal
it. Its holder gets a new password through an authorised staff reset or Telegram recovery.
A password change increments `session_version` and revokes existing sessions.

## Operations

- Budget 64 MiB per concurrent Argon2 operation and benchmark on deployment hardware.
  Password work runs off the event loop with at most two operations at once.
- Do not log passwords or expose encoded hashes to the browser.

## Regression coverage

ASCII, Persian and emoji prefixes at exactly 72 UTF-8 bytes must not alias after the boundary.
Encoded hashes are salted, self-describing and fit the column. Bad, missing and bcrypt hashes
fail closed, including real bcrypt hashes of the right password. A suffix of a password neither
signs in nor rotates it.
