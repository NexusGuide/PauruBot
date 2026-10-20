# Telegram polling resilience

## Why

The reported `GetUpdates` 429 and 502 errors are upstream/transient failures, not evidence
of their underlying cause (for example, this does not establish duplicate bot processes).
Aiogram retries failed polling, but the installed version's generic dispatcher backoff does
not honor `TelegramRetryAfter.retry_after`, and each failed attempt is logged at ERROR,
feeding operational alerts. Do not solve this by disabling all dispatcher error logging.

## Implementation

A request middleware installed by the shared `make_bot` factory for both main and reseller
bots intercepts **GetUpdates only**. The CLI reseller poller now passes its shop id to
the factory instead of constructing an unscoped reseller client. It retries network/TelegramServerError/429 with positive
jitter and exponential delay capped at a 30-second base. A Telegram 429's requested minimum
wait always wins, even if longer than the cap. The same method/offset is passed again;
aiogram still owns update yielding, offset advancement and handler dispatch.

Each retry is a normal warning containing only exception class, attempt and delay, not the
raw exception payload or bot token. A continuous outage emits ERROR after 300 seconds and
at most once every 900 seconds thereafter; successful recovery emits INFO. These ERRORs
remain actionable through the existing error pipeline. The log context explicitly carries
only the middleware's shop scope, avoiding inherited unrelated task scope. Install #89's
tenant-isolated error routing (or its successor) alongside this PR before production so
unscoped main-bot errors do not fan out to unrelated tenant channels.

401/409/bad-request and unexpected errors are **not swallowed**; cancellation immediately
interrupts requests/backoff. Non-polling methods, including side-effecting sends, retain
normal caller-specific retry policies. No private Dispatcher override or version-specific
copy of its polling generator is introduced.

## Validation / limits

`pytest tests/unit/test_polling_retry.py` uses synthetic exceptions and virtual sleeps to
verify retry_after, server/network recovery, escalation cadence, scope restoration, fatal
errors, cancellation, unchanged offset/method and both factory scopes. No live Telegram or
staging is touched. Run the full backend suite before release. This is resilience/noise
control, not a remedy for Telegram outages, duplicate pollers, bad bot tokens or permission
errors. On persistent errors, inspect connectivity/proxy/DNS and ensure one polling owner
per token; switch to webhook only as a separately reviewed deployment decision. Recovery
is INFO in process logs, not an additional channel message. Very long retry_after pauses
are respected; persistent escalation is evaluated on subsequent failed attempts, not by a
second timer while a request/backoff is in flight.
