# Recoverable operational error reports

## Scope / evidence

The reported `errors.flush -> report -> send_text -> Retry(TelegramServerError)` traceback
has an application-level consequence beyond Telegram's transient 502: a popped batch is
lost and the periodic failure itself can become another Telegram error notification.
This PR changes **only the error-flush transport**, not payment/support business events.
It is stacked on #89 (`fix/mender-error-tenant-isolation`): merge that first, then retarget
this PR to main. Never merge duplicate predecessor #83 as well. No deployment is performed.

## Contract

- An atomic Lua step moves up to 50 pending Redis records to `errors:inflight`, with a stable
  batch id and no TTL. Existing inflight data is replayed before new pending records.
- One PostgreSQL `notify/error_report` job per group/destination is committed before the
  Redis batch is acknowledged. Enqueue dedupe handles commit-before-ACK interruptions;
  compare-and-delete ACK cannot delete a newer batch. Different recipients retry independently.
- Telegram delivery happens in the existing leased queue, not inside the periodic task.
  Temporary network/5xx/429 errors use the queue retry budget/backoff. A 429's structured
  `retry_after` is a **minimum**, never shortened by jitter or the backoff cap. Claim fencing,
  attempt budget and dead-letter state are unchanged.
- Scoped reports only use that shop's selected errors/all channel. Unknown, legacy or
  deleted-shop records go only to explicitly configured platform owners, never shop enumeration.
  A queued destination is reauthorized before delivery; revoked/moved channels or owners
  become dead jobs, not rerouted payloads.
- Blocked/deleted chats and Telegram bad requests become dead records with a warning, not
  endless retries. Exhausted `notify` jobs already do not create another dead-letter alert.
  Only the `notify/error_report` worker logging context is excluded from the Telegram sink;
  its traceback remains in normal process logs. Other worker errors are not suppressed.

## Local verification

Use disposable PostgreSQL + Redis and fake Telegram; never point the test suite at production.
Run `pytest tests/security/test_error_report_delivery.py tests/security/test_error_tenant_isolation.py
 tests/unit/test_worker_release_gate.py`. Regressions cover DB outage/rollback, ACK failure
following commit and even delivery, atomic competing takers, 50-record cap, tenant/privacy
boundaries, owner isolation, 429 scheduling, 5xx recovery, stale claim fencing, revoked
configuration, permanent rejection, exhaustion and unexpected alert-delivery exceptions.

## Operational limits / rollout

This does **not** make Telegram available, repair account Billing, or prove the live server
is running this commit. Deploy only after review and a staging smoke test. Verify bot channel
membership/post permission and thread id. Inspect normal worker/bot logs and existing dead-job
admin tooling during outages; do not send an alert about a failed error-alert through itself.
Coordinate the worker upgrade: stop the old worker pool, upgrade all notify workers and
restart on the same revision before producing the new `error_report` job kind. An old
worker would classify that unknown kind as permanent; mixed-revision consumers are unsafe.

After Telegram recovers, confirm pending jobs drain and original errors are delivered once
in the ordinary successful path. Never replay dead rows in bulk without checking destinations.

Delivery is at-least-once: Telegram accepting a send just before worker crash/claim loss can
still produce a duplicate. Dedupe protects Redis-to-DB handoff, **not Telegram exactly-once**.
Redis capture is best effort (pending cap remains 200, plus one inflight batch); Redis data
loss/eviction and initial logging failures can lose reports. Do not expire/delete inflight
manually as a repair shortcut. Retain job dedupe rows while an inflight batch can be replayed.
Backlog growth during a long Telegram outage needs operational monitoring/retention policy;
no unbounded new event source is introduced beyond the existing error logger.
