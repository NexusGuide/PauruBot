# Operational error tenant isolation (audit S01 / F-04)

- `TelegramErrorHandler` records the positive integer shop from task-local `log_tags`, never a message/record-extra guess. The worker runner already sets and resets this context.
- Redis error groups include `shop_id`; equal messages from different shops remain separate. A valid shop goes only to that shop's existing `errors`/`all` LogChannel.
- Missing, malformed, legacy and deleted-shop scopes go only to explicitly configured `OWNER_TELEGRAM_IDS`, through the main bot. No owners means no Telegram delivery, **not** fallback to all shops. Process stdout/Sentry sinks are unchanged.
- Unknown-scope dead-letter alerts use the same platform-only route. Error text and exception text are scrubbed before Redis and before rendering old queued entries.
- The current API/Bot paths without task-local shop provenance remain platform-only; this change intentionally does not guess their tenant.

## Compatibility and validation

No database migration or new environment variable. The existing bounded `errors:pending` list remains readable during rollout; pre-upgrade entries have no shop scope and are platform-only. Restart all processes so producers include scope. Alert delivery stays best-effort (this is not a new durable delivery/outbox guarantee).

Run `pytest tests/security/test_error_tenant_isolation.py tests/unit/test_observability.py`, the complete suite, Ruff and mypy against disposable PostgreSQL/Redis. Tests cover two shops with identical errors, legacy/malformed records, redaction, missing owners and dead letters. Telegram is stubbed; these are not live destination-permission tests.

Before rollback to older code, disable `LOG_ERRORS_TO_TELEGRAM`: the older consumer broadcasts errors across shops. Do not restore that behavior as a fallback.
