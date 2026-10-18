"""ASGI adapter boundaries; domain/network effects are controlled seams, not live providers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from paurubot.api.routes import tonpay_custom as api

pytestmark = pytest.mark.security


@pytest.fixture
async def adapter(monkeypatch):  # type: ignore[no-untyped-def]
    app = FastAPI()
    app.include_router(api.router)
    route = next(r for r in api.router.routes if r.path.endswith("/create"))
    dependency = next(d.call for d in route.dependant.dependencies if d.name == "ctx")
    ctx = SimpleNamespace(
        shop=SimpleNamespace(id=7),
        customer=SimpleNamespace(id=11),
        session=SimpleNamespace(commit=AsyncMock()),
    )

    async def principal():  # type: ignore[no-untyped-def]
        return ctx

    app.dependency_overrides[dependency] = principal
    monkeypatch.setattr(api.kv, "hit", AsyncMock(return_value=(True, 1)))
    start = AsyncMock(return_value={"payment_id": 13})
    monkeypatch.setattr(api.service, "start", start)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test.local"
    ) as client:
        yield client, start, ctx, app, dependency


async def test_authentication_runs_before_creation(adapter):  # type: ignore[no-untyped-def]
    client, start, _, app, dependency = adapter

    async def denied():  # type: ignore[no-untyped-def]
        raise HTTPException(401, "unauthorized")

    app.dependency_overrides[dependency] = denied
    assert (await client.post("/api/webapp/tonpay/create", content=b"{}")).status_code == 401
    start.assert_not_awaited()


async def test_owner_is_from_principal_not_body(adapter):  # type: ignore[no-untyped-def]
    client, start, ctx, *_ = adapter
    r = await client.post(
        "/api/webapp/tonpay/create", json={"amount": 100000, "intent_key": "synthetic-intent-001"}
    )
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    owner, amount, key = start.await_args.args
    assert (owner.shop_id, owner.customer_id, amount, key) == (7, 11, 100000, "synthetic-intent-001")
    ctx.session.commit.assert_awaited_once()


@pytest.mark.parametrize(
    "body",
    [
        b'{"amount":1,"amount":100000,"intent_key":"synthetic-intent-001"}',
        b'{"amount":true,"intent_key":"synthetic-intent-001"}',
        b'{"amount":100000,"intent_key":"synthetic-intent-001","shop_id":8}',
        b'{"amount":100000,"intent_key":"synthetic-intent-001","provider_ref":"forged"}',
        b'{"amount":"100000","intent_key":"synthetic-intent-001"}',
        b"not json",
    ],
)
async def test_invalid_or_ambiguous_input_never_creates(adapter, body):  # type: ignore[no-untyped-def]
    client, start, *_ = adapter
    r = await client.post(
        "/api/webapp/tonpay/create", content=body, headers={"content-type": "application/json"}
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "invalid_request"
    start.assert_not_awaited()


async def test_body_cap_counts_actual_stream_not_content_length(adapter):  # type: ignore[no-untyped-def]
    client, start, *_ = adapter

    async def chunks():  # type: ignore[no-untyped-def]
        yield b" " * 3000
        yield b" " * 3000

    r = await client.post(
        "/api/webapp/tonpay/create",
        content=chunks(),
        headers={"content-type": "application/json", "content-length": "2"},
    )
    assert r.status_code == 413
    start.assert_not_awaited()


async def test_rate_limit_prevents_service_call(adapter, monkeypatch):  # type: ignore[no-untyped-def]
    client, start, *_ = adapter
    monkeypatch.setattr(api.kv, "hit", AsyncMock(return_value=(False, 99)))
    assert (await client.post("/api/webapp/tonpay/create", json={})).status_code == 429
    start.assert_not_awaited()


async def test_callback_claims_are_never_payment_authority(adapter, monkeypatch):  # type: ignore[no-untyped-def]
    client, start, *_ = adapter
    refresh = AsyncMock()
    monkeypatch.setattr(api.service, "refresh", refresh)
    for method in ["GET", "POST"]:
        r = await client.request(
            method, "/api/pay/tonpay-custom?paid=true&order_id=13", content=b'{"paid":true}'
        )
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    start.assert_not_awaited()
    refresh.assert_not_awaited()


async def test_uncertain_receipt_denied_before_body_decode(adapter, monkeypatch):  # type: ignore[no-untyped-def]
    client, _, *_ = adapter
    monkeypatch.setattr(api.service, "client_for", AsyncMock(return_value=object()))
    monkeypatch.setattr(api.service, "view", AsyncMock(return_value={"uncertain": True}))
    submit = AsyncMock()
    monkeypatch.setattr(api.mutations, "submit_receipt", submit)
    r = await client.post(
        "/api/webapp/tonpay/13/receipt",
        content=b"not an image",
        headers={
            "content-type": "image/png",
            "x-tonpay-operation": '{"intent_key":"synthetic-intent-001","quote_revision":1}',
        },
    )
    assert r.status_code == 409
    submit.assert_not_awaited()


async def test_operator_route_does_not_allow_scoped_staff(adapter, monkeypatch):  # type: ignore[no-untyped-def]
    client, _, _, app, _ = adapter
    route = next(r for r in api.router.routes if r.path.endswith("/tonpay-reconcile"))
    dependency = next(d.call for d in route.dependant.dependencies if d.name == "ctx")

    async def staff():  # type: ignore[no-untyped-def]
        return SimpleNamespace(scoped=True)

    app.dependency_overrides[dependency] = staff
    refresh = AsyncMock()
    monkeypatch.setattr(api.service, "refresh", refresh)
    r = await client.post(
        "/api/admin/payments/13/tonpay-reconcile", json={"reason": "Synthetic review reason"}
    )
    assert r.status_code == 403
    refresh.assert_not_awaited()
