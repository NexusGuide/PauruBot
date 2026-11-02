"""Remove secrets from log records and error strings (SEC-12)."""

from __future__ import annotations

import logging
import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "<bot-token>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1<redacted>"),
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|merchant(?:_id)?)"
            r"(\"?\s*[:=]\s*\"?)([^\s\"'&,}]+)"
        ),
        r"\1\2<redacted>",
    ),
    # user:password@ and the password-only :password@ that a Redis URL uses (AUD-11).
    (re.compile(r"(?i)(://[^/\s:@]*:)[^@\s/]+@"), r"\1<redacted>@"),
    (re.compile(r"\b(?:\d[ -]?){16}\b"), "<card>"),
]

_extra: set[str] = set()


def register_secret(value: str | None) -> None:
    """Redact an exact secret value (e.g. a decrypted panel password) wherever it appears."""
    if value and len(value) >= 6:
        _extra.add(value)


def redact(text: str) -> str:
    for value in _extra:
        if value in text:
            text = text.replace(value, "<redacted>")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format args; keep the raw message
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True
