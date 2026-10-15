# PR47 follow-up: lost-lease completion and bounded CI diagnostics

Based on PR47 head `94ddea310578b095670b96b3cbde4456f4acf3c8`.

## Independently reproduced lease defect

The Redis lease keeper cancels its caller when renewal returns zero or raises an
exception. Previously, if the lease body consumed `asyncio.CancelledError` and
returned normally, the context manager also returned normally. Lost ownership
could therefore be presented to its caller as successful completion.

The lease now checks the recorded loss after a normal body exit and raises
`LockLost`. Existing cancellation propagation and owner-checked release remain
unchanged. Two regression cases cover lost ownership and unavailable Redis with
a cancellation-consuming body.

This is a cooperative completion guard, not remote API fencing. It does not undo
an already accepted panel write, cancel a non-cooperative operation, or prove that
legacy ambiguous orders are safe.

## CI diagnostics

The last observed Python job ran about six hours and ended cancelled. Its complete
job logs were not available through the current GitHub tool connection, so the
root cause of that cancellation is **not established**.

Both existing security and full test suites remain mandatory and keep shell
`pipefail`. They now report each test name, dump stacks after 60 seconds, and fail
a test after 120 seconds using `pytest-timeout`'s thread method. The Python job has
a 25-minute outer limit. These are failure bounds, not skips or relaxed assertions.
Timeouts may stop pytest before JUnit XML is complete; console logs are also
uploaded with `always()` and seven-day retention when the runner can execute the
upload step.

The timeout plugin is installed only in CI. Runtime requirements and dependency
lockfiles are unchanged. No permissions beyond the existing `contents: read`
are requested.

## Validation and release boundaries

- An isolated harness executed the original lease AST, with injected Redis and
  lock dependencies. Before the change, cancellation-consuming bodies incorrectly
  succeeded for both loss scenarios; normal cancellation already raised LockLost.
- The same harness passed all four scenarios after the change.
- Original regression test bodies were executed against the extracted lease: the
  original source produced two expected failures and two passes; the patched source
  passed all four cases. These tests use injected lock/Redis I/O, not live servers.
- The actual workflow pytest/pipefail pipeline was smoke-tested with a temporary
  passing test, failing test and hanging test (shortened timeout of one second):
  exits were 0/1/1; the hang failed in about 1.2 seconds and retained its test name
  and stack output. Production CI retains the 120-second per-test bound.
- Ruff lint/format under the project's rules and Python syntax compilation passed
  for the modified production/test files. YAML gates, service configuration,
  permissions and the frontend job were compared with the original workflow.
- Local tests are not a full repository import, PostgreSQL/Redis integration,
  Python 3.12 CI, or live-panel run. GitHub's new head remains authoritative for
  complete compile/lint/types/security/full-suite results.
- Keep this PR Draft. The existing staging, old-worker drain, ambiguous-order,
  direct staff/bulk coordination, backup and rollout gates still apply.

No migration, payment-method change, merge, deployment, feature activation or live
Telegram/panel/financial operation is part of this follow-up.