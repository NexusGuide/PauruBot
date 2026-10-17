# Unsequenced panel events are refresh hints

## Contract and behavior

Authenticated Marzban/PasarGuard callbacks no longer apply event status, traffic,
limits, expiry, or deletion directly. These event formats have no trusted ordering
contract here; an old ACTIVE event must not reactivate a newer DISABLED service,
and an arrival timestamp is not a provider observation timestamp.

The endpoint validates the existing secret and provider support, accepts bounded
batches, extracts string usernames (maximum 128 characters), deduplicates usernames
within the processed batch, and enqueues tenant-scoped `provision/refresh_service`
jobs. Unknown, already-deleted and inactive-panel resources are ignored. No event
state, subscription link or credentials are persisted in the job.

Successful response changes from immediate `applied` accounting to
`{"received": N, "applied": 0, "queued": Q}`. `queued` means durable work in the
request transaction, not completed synchronization. Only the first MAX_EVENTS
(currently 500) are processed, preserving the previous batch bound. Delivery retries
can enqueue another refresh; there is deliberately no forever-unique per-service
job key that would suppress future legitimate events.

The worker validates shop, service and panel binding, locks the current service row,
GETs the current provider user through the existing driver, then runs existing usage
history and alert/queued-renewal evaluation. Missing provider users follow the
existing deletion policy. Current provider usage resets to zero are allowed; using
`max(old, event)` would invent consumption. Provider failures use existing job
retry/backoff/dead-letter behavior, rather than trusting the event on failure.

## Rollout

No database migration or new queue process is needed. Deploy the new worker handler
before, or together with, the new API; old workers cannot consume the new job kind.
Existing periodic polling remains a recovery path. Refreshes are eventually
consistent and depend on a working provision worker and provider GET API.

This is not a claim of globally linearizable provider state: other polling/admin
operations retain their existing concurrency behavior. Repeated authenticated
callbacks can increase queue load; cross-request coalescing, a webhook-specific
rate policy and provider sandbox validation remain separate work. Apply the streamed
body-limit PR as well; this change does not replace that protection.

## Validation

The original direct-write implementation reproduced stale-event mutation and did
not enqueue a refresh. Regression tests cover no direct state/history/alert writes,
current-provider reads (including real zero reset), tenant mismatch, inactive/deleted/
moved/missing resources, failure without mutation, later events not suppressed,
secret rejection and within-batch deduplication. All tests use fake providers and
local PostgreSQL/Redis, not live Telegram or a provider sandbox.
