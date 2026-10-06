"""Structured JSON logging with tags and secret redaction (ARC-04, SEC-12)."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

from paurubot.security.redact import RedactingFilter

correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


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


def setup_logging(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
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
