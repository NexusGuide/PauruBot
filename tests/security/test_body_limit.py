"""Every request body is capped before any route, sign-in or parser reads it.

FastAPI reads a JSON body into memory and spools a multipart upload to disk before the route's own
size checks or its authentication run, so without a cap one anonymous request could fill either.
"""

from __future__ import annotations

import httpx
import pytest

from paurubot.api import middleware
from paurubot.api.app import create_app

pytestmark = pytest.mark.security


@pytest.fixture
async def client(session):  # type: ignore[no-untyped-def]
    transport = httpx.ASGITransport(app=create_app(), client=("10.9.8.7", 4000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_a_declared_oversized_body_is_refused_before_anything_reads_it(client) -> None:  # type: ignore[no-untyped-def]
    body = b"{" + b" " * (middleware.MAX_BODY + 10) + b"}"
    resp = await client.post("/api/auth/login", content=body, headers={"content-type": "application/json"})
    assert resp.status_code == 413 and resp.json()["error"] == "too_large"


async def test_a_streamed_oversized_upload_is_cut_off_before_the_sign_in_check(client) -> None:  # type: ignore[no-untyped-def]
    async def chunks():  # no Content-Length: the size only shows while it streams
        boundary = b'--x\r\nContent-Disposition: form-data; name="file"; filename="r.jpg"\r\n\r\n'
        yield boundary
        for _ in range(12):
            yield b"\0" * (1024 * 1024)

    resp = await client.post(
        "/api/webapp/payments/1/receipt-upload",
        content=chunks(),
        headers={"content-type": "multipart/form-data; boundary=x"},
    )
    assert resp.status_code == 413  # not 401 after the whole file was spooled to disk


async def test_ordinary_requests_and_the_owner_import_are_unaffected(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post("/api/auth/login", json={"username": "nobody", "password": "wrong-password"})
    assert resp.status_code == 401
    assert middleware.body_limit("/api/admin/migration/upload") > 300 * 1024 * 1024
    assert middleware.body_limit("/api/webapp/orders") == middleware.MAX_BODY
