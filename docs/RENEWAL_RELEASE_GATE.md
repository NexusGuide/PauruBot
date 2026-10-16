# Renewal/worker release gate

This batch extends PR #47. No new migration, production operation or feature flag is required.
Do not treat a passing CI run as proof that a live panel or an old ambiguous order is safe.

## Guarantees and boundaries

- New targets store `reset_protocol: 1` atomically before panel calls; each destructive reset is
  durably reserved before invocation. Missing legacy markers never mean "reset did not happen".
- An untracked legacy reset target cannot be automatically reset, reconciled as applied, or
  refunded. It remains uncertain for staff. **Never bulk-backfill `reset_protocol=1` on old targets.**
- Paid renewal/add-on/upgrade/move orders share a renewable per-service Redis lease, held across
  DB progress commits. A started unresolved order outranks a fresh order, even with a higher ID.
  Multiple pre-deploy started targets on one service are quarantined by the predecessor gate.
- Busy-service deferral does not consume the queue failure budget. Inspect blocked predecessors;
  repeated deferral must not be confused with a finished purchase or silently refunded.
- A queue heartbeat uses a separate DB session. Only a live RUNNING token may renew, complete,
  fail, defer or store progress. Lost claims cannot increment `done` or invoke dead-letter hooks.
- Periodic Redis leases renew and stop cooperative work if Redis ownership cannot be proved.
- Parked-order selection filters before LIMIT; each periodic pass traverses bounded keyset pages.
  Unresolved old rows no longer hide newer eligible orders.

These are cooperative cancellation and durable-state safeguards, **not remote API fencing**.
An HTTP request already accepted by a panel may outlive cancellation. DB rollback does not undo
remote writes or commits a handler already made. Recovery must read/reconcile durable intent.
Direct staff/bulk service mutations outside the paid-order path do not yet share this new gate;
coordinate them during staging and follow up separately before claiming global serialization.

## Pre-deploy runbook

1. Back up the DB and inventory PAID/PROVISIONING renewal/add-on/upgrade/move orders by service.
2. Pause new modifications and drain/stop the old worker processes before upgrading. Mixed old
   and new workers do not share these safeguards. Do not just replace one replica at a time.
3. Identify stored reset targets lacking reset_done/reset_attempted/reset_protocol. Record panel
   evidence and ledger references; leave uncertain cases parked for explicit staff resolution.
4. Identify multiple started targets on the same service. Decide their causal order and settlement
   from panel/ledger evidence; do not blindly retry or clear markers to bypass the gate.
5. Deploy to staging; test two independent workers against PostgreSQL and Redis, including a
   DB/Redis outage, expired/replaced token, lost reset response, process death, and late panel reply.
6. Check deferred jobs, parked orders, ledger drift and notification dedupe. Queue heartbeat adds
   short-lived DB sessions; verify pool capacity with the configured queue batch sizes.
7. Exercise supported panel types, especially date-only expiry and panels with unknown limits.
8. Approve merge and production deployment separately. Re-enable modifications only after old
   workers are gone and ambiguous predecessors have a documented resolution path.

## Test layers

`test_renewal_safety.py`: legacy-reset exclusion plus existing expiry/reset reservation regressions.
`test_worker_release_gate.py`: runner/lease cancellation boundaries, predecessor gating, real fixture
queries for parked scans and queue ownership, and deferral attempt restoration.

The repository fixture suite uses PostgreSQL and Redis. Local isolation with substitute dependencies
is useful regression evidence but is not a substitute for those integration tests or live staging.
