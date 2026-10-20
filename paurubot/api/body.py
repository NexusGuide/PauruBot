"""Bounded raw-body reads for signed callbacks and authenticated panel webhooks."""

from __future__ import annotations

from starlette.requests import Request


class BodyTooLarge(Exception):
    pass


async def read_limited_body(request: Request, limit: int) -> bytes:
    """Reject at the first overflowing ASGI chunk, without draining the remaining stream.

    Content-Length is only an early rejection hint, never proof that a body is small.
    Preserve the exact received bytes for provider signature verification.
    """
    if limit < 0:
        raise ValueError("body limit must not be negative")
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdecimal():
        # Avoid int() on attacker-controlled arbitrarily long digit strings.
        normalized = declared.lstrip("0") or "0"
        ceiling = str(limit)
        if len(normalized) > len(ceiling) or (len(normalized) == len(ceiling) and normalized > ceiling):
            raise BodyTooLarge
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > limit - len(body):
            raise BodyTooLarge
        body.extend(chunk)
    return bytes(body)
