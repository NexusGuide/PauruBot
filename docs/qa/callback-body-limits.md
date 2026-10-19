# Bounded callback reads (audit S06)

`read_limited_body` replaces unbounded `request.body()` at the panel webhook (512 KiB),
NowPayments IPN, Plisio callback and shared redirect-gateway callback (64 KiB each). Existing
limits, panel authentication order, JSON errors, payment plain-text 413 response and provider
signature inputs remain unchanged. Exact-boundary requests are allowed.

The helper checks a decimal ASCII Content-Length as an early rejection hint, without int()
on an unbounded digit string. Missing, malformed or understated headers never disable the
actual byte counter. ASGI chunks are counted before being added to the bounded body buffer;
the first overflowing chunk raises without reading/draining the rest. Raw bytes are returned
unchanged for signature verification; no JSON re-encoding or newline/Unicode normalisation.

## Boundaries, not overclaims

- The ASGI server/proxy already owns the current incoming chunk; the application cannot prevent
  that allocation. A bounded bytearray and its final bytes copy are not a strict RSS ceiling.
- These four callbacks are covered, not every API, Telegram update or multipart upload.
- Keep reverse-proxy body, header, read-timeout, connection and rate limits. A sender keeping
  a small request open indefinitely (slowloris) or many simultaneous requests needs those
  separate controls. No timeout/concurrency guarantee is introduced by this patch.
- Content-Length is a hint, not a trusted size proof. Oversized declarations are rejected even
  if the sender intended to send less, consistent with the existing gateway policy.
- Disconnects propagate rather than treating an incomplete signed request as complete. The
  helper is a one-shot read for these routes; consumers receive its bytes explicitly.

## Regression coverage

Direct ASGI receive counters prove oversized chunked/understated/malformed-header bodies stop
at the first crossing chunk, and declared overflow consumes zero body chunks. Panel-secret
failure still precedes reading. Exact limits preserve binary and signed JSON bytes, including
Persian text, whitespace and empty chunks; enormous numeric headers do not hit Python's int
conversion limit. Disconnects are not swallowed. Existing webhook/provider suites validate
normal routes, signatures and idempotent payment callbacks.

No DB migration, new configuration, secret or workflow change. Rollback restores the old
allocation exposure; retain proxy limits regardless. No real gateway/Telegram calls needed.
