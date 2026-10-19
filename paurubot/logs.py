"""Structured JSON logging with tags and secret redaction (ARC-04, SEC-12)."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from paurubot.security.redact import RedactingFilter, redact

correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)
# Extra fields added to every log line of the current task: job, queue, shop, customer (ARC-04).
log_tags: contextvars.ContextVar[dict[str, object] | None] = contextvars.ContextVar("log_tags", default=None)

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def error_shop_id(value: object) -> int | None:
    """Only an explicit positive integer is tenant provenance; never coerce user text/bools."""
    return value if type(value) is int and value > 0 else None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        cid = correlation_id.get()
        if cid:
            payload["cid"] = cid
        for key, value in (log_tags.get() or {}).items():
            if value is not None:
                payload.setdefault(key, value)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if record.exc_text and record.exc_text not in text:
            text += "\n" + record.exc_text
        return text


class TelegramErrorHandler(logging.Handler):
    """Queue ERROR records in Redis; the worker posts them to the "errors" log channel (ARC-04).

    Logging is synchronous, so this uses a tiny blocking Redis client with short timeouts and
    gives up quietly: losing an error notification must never break the code that logged it.
    """

    KEY = "errors:pending"
    MAX_PENDING = 200

    def __init__(self, process: str) -> None:
        super().__init__(level=logging.ERROR)
        self.process = process
        self._client: Any = None
        self.addFilter(RedactingFilter())

    def _redis(self):  # type: ignore[no-untyped-def]
        if self._client is None:
            import redis

            from paurubot.config import get_settings

            self._client = redis.Redis.from_url(
                get_settings().redis_url, socket_timeout=0.5, socket_connect_timeout=0.5
            )
        return self._client

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(("paurubot.notifications", "redis")):
            return  # never loop on our own delivery failures
        try:
            entry = {
                "process": self.process,
                "logger": redact(record.name),
                "shop_id": error_shop_id((log_tags.get() or {}).get("shop")),
                "msg": redact(record.getMessage())[:500],
                "exc": record.exc_text or "",
                "cid": correlation_id.get(),
                "ts": record.created,
            }
            if record.exc_info and not entry["exc"]:
                entry["exc"] = logging.Formatter().formatException(record.exc_info)
            entry["exc"] = redact(str(entry["exc"]))[-1500:]
            if entry["cid"]:
                entry["cid"] = redact(str(entry["cid"]))
            client = self._redis()
            pipe = client.pipeline()
            pipe.rpush(self.KEY, json.dumps(entry, default=str))
            pipe.ltrim(self.KEY, -self.MAX_PENDING, -1)
            pipe.execute()
        except Exception:  # noqa: BLE001
            return


def setup_logging(level: str = "INFO", json_output: bool = True, process: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    if process:
        from paurubot.config import get_settings

        if get_settings().log_errors_to_telegram:
            root.addHandler(TelegramErrorHandler(process))
    root.setLevel(level.upper())
    for noisy in (
        "httpx",
        "httpcore",
        "aiogram.event",
        "apscheduler.executors.default",
        "uvicorn.access",
        "alembic.runtime.plugins",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def setup_error_monitoring(process: str) -> bool:
    """Send unhandled errors to Sentry when SENTRY_DSN is set (NEW-05). Events are redacted first."""
    from paurubot import __version__
    from paurubot.config import get_settings
    from paurubot.security.redact import redact

    settings = get_settings()
    dsn = settings.sentry_dsn.get_secret_value()
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logging.getLogger(__name__).warning("SENTRY_DSN is set but sentry-sdk is not installed")
        return False

    def scrub(event: dict, _hint: dict) -> dict:
        return json.loads(redact(json.dumps(event, default=str)))

    sentry_sdk.init(
        dsn=dsn,
        release=f"paurubot@{__version__}",
        environment=settings.environment,
        send_default_pii=False,
        traces_sample_rate=0.0,
        before_send=scrub,  # type: ignore[arg-type]
        server_name=process,
    )
    return True
