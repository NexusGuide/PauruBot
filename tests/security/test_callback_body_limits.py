"""Reject oversized callbacks while receiving, not after an unbounded request.body()."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from paurubot.api.routes import pay, webhooks
from paurubot.payments.providers.gateways import GATEWAYS
from paurubot.security.secretbox import encrypt

pytestmark = pytest.mark.security


class IncomingBody:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.calls = 0

    async def receive(self) -> dict:
        index = self.calls
        self.calls += 1
        if index >= len(self.chunks):
            raise AssertionError("body was read again after its terminal chunk")
        return {"type": "http.request", "body": self.chunks[index], "more_body": index < len(self.chunks) - 1}


def request_for(body: IncomingBody, declared: str | None = None, secret: str = "test-secret") -> Request:
    headers = [(b"content-type", b"application/json"), (b"x-webhook-secret", secret.encode())]
    if declared is not None:
        headers.append((b"content-length", declared.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/callback",
            "scheme": "http",
            "server": ("testserver", 80),
            "query_string": b"",
            "headers": headers,
        },
        body.receive,
    )


async def invoke(route: str, request: Request):  # type: ignore[no-untyped-def]
    db = AsyncMock()
    if route == "panel":
        db.get.return_value = SimpleNamespace(webhook_secret_enc=encrypt("webhook", "test-secret"))
        return await webhooks.panel_webhook(1, request, db)
    if route == "nowpayments":
        return await pay.nowpayments_ipn(request, db)
    if route == "plisio":
        return await pay.plisio_callback(request, db)
    return await pay.gateway_callback(next(iter(GATEWAYS)), request, db)


@pytest.mark.parametrize("route", ["panel", "nowpayments", "plisio", "gateway"])
@pytest.mark.parametrize("header", [None, "1", "invalid"])
async def test_stream_overflow_stops_before_reading_the_remainder(route: str, header: str | None) -> None:
    limit = webhooks.MAX_BODY if route == "panel" else pay.MAX_CALLBACK
    body = IncomingBody([b"a" * (limit // 2), b"b" * (limit // 2 + 1), b"unread-remainder"])
    try:
        response = await invoke(route, request_for(body, header))
        status = response.status_code
    except HTTPException as exc:
        status = exc.status_code
    assert status == 413
    assert body.calls == 2  # do not drain an attacker-controlled continuation


@pytest.mark.parametrize("route", ["panel", "nowpayments", "plisio", "gateway"])
async def test_declared_overflow_is_rejected_without_consuming_body(route: str) -> None:
    limit = webhooks.MAX_BODY if route == "panel" else pay.MAX_CALLBACK
    body = IncomingBody([b"not-even-read"])
    try:
        response = await invoke(route, request_for(body, str(limit + 1)))
        status = response.status_code
    except HTTPException as exc:
        status = exc.status_code
    assert status == 413
    assert body.calls == 0


async def test_panel_auth_failure_still_precedes_body_read() -> None:
    body = IncomingBody([b"unread"])
    with pytest.raises(HTTPException) as exc:
        await invoke("panel", request_for(body, secret="wrong"))
    assert exc.value.status_code == 403 and body.calls == 0


@pytest.mark.parametrize("declared", [None, "1", "invalid", "0008", "+8", "-1"])
async def test_limited_reader_keeps_exact_bytes_at_the_limit(declared: str | None) -> None:
    from paurubot.api.body import read_limited_body

    body = IncomingBody([b"\x00\xffa", b"\r\n", b"xyz"])
    assert await read_limited_body(request_for(body, declared), 8) == b"\x00\xffa\r\nxyz"
    assert body.calls == 3


async def test_huge_declared_number_is_safe_and_rejects_before_reading() -> None:
    from paurubot.api.body import BodyTooLarge, read_limited_body

    body = IncomingBody([b"unread"])
    with pytest.raises(BodyTooLarge):
        await read_limited_body(request_for(body, "9" * 10_000), 8)
    assert body.calls == 0


async def test_zero_limit_allows_empty_body_but_not_a_byte() -> None:
    from paurubot.api.body import BodyTooLarge, read_limited_body

    assert await read_limited_body(request_for(IncomingBody([b""])), 0) == b""
    with pytest.raises(BodyTooLarge):
        await read_limited_body(request_for(IncomingBody([b"a"])), 0)


@pytest.mark.parametrize("route,field", [("nowpayments", "order_id"), ("plisio", "order_number")])
async def test_exact_boundary_preserves_signed_raw_body(route, field, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from starlette.responses import Response

    raw = (' \n{"' + field + '": 123, "label":"ش"}').encode()
    raw += b" " * (pay.MAX_CALLBACK - len(raw))
    incoming = IncomingBody([raw[:200], raw[200:]])
    callback = AsyncMock(return_value=Response("ok"))
    monkeypatch.setattr(pay, "_invoice_callback", callback)
    response = await invoke(route, request_for(incoming, str(len(raw))))
    assert response.status_code == 200
    assert callback.await_args.args[2:4] == (123, raw)


async def test_disconnect_is_not_silently_treated_as_a_complete_body() -> None:
    from starlette.requests import ClientDisconnect

    from paurubot.api.body import read_limited_body

    async def disconnected() -> dict:
        return {"type": "http.disconnect"}

    request = Request({"type": "http", "headers": []}, disconnected)
    with pytest.raises(ClientDisconnect):
        await read_limited_body(request, 8)
