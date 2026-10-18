"""Method-specific schema and HTTP dispatch for the shared gateway callback.

Provider and DB effects are deliberately faked. These are not settlement or live-provider tests.
"""

from __future__ import annotations

import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute
from paurubot.payments.providers import gateways

from paurubot.api.routes import pay

PATH = "/api/pay/gw/{name}"


def _app(db=None):
    app = FastAPI()
    app.include_router(pay.router)
    if db is not None:
        # Override the route's real DB dependency, not a query parameter or the endpoint.
        route = next(r for r in pay.router.routes if isinstance(r, APIRoute) and r.path == PATH)
        dependency = route.dependant.dependencies[0].call

        async def fake_db():
            return db

        app.dependency_overrides[dependency] = fake_db
    return app


def test_payment_openapi_has_no_duplicate_ids_or_warnings():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = _app().openapi()
    assert not [w for w in caught if "Duplicate Operation ID" in str(w.message)]
    operations = [op for path in schema["paths"].values() for op in path.values()]
    ids = [op["operationId"] for op in operations]
    assert len(ids) == len(set(ids))


def test_gateway_operation_ids_are_explicit_method_specific_and_stable():
    schemas = [_app().openapi() for _ in range(2)]
    for schema in schemas:
        path = schema["paths"][PATH]
        assert set(path) == {"get", "post"}
        assert path["get"]["operationId"] == "gateway_callback_get"
        assert path["post"]["operationId"] == "gateway_callback_post"
    assert schemas[0] == schemas[1]


def test_both_gateway_routes_reuse_the_exact_same_handler_and_dependencies():
    routes = [r for r in pay.router.routes if isinstance(r, APIRoute) and r.path == PATH]
    assert len(routes) == 3
    # Decorators register bottom-up: the legacy dispatch route stays first.
    assert routes[0].methods == {"GET", "POST"}
    assert not routes[0].include_in_schema
    documented = [r for r in routes if r.include_in_schema]
    assert {frozenset(r.methods) for r in documented} == {frozenset({"GET"}), frozenset({"POST"})}
    assert all(r.endpoint is pay.gateway_callback for r in routes)
    assert all(r.dependant.dependencies[0].call is routes[0].dependant.dependencies[0].call for r in routes)
    assert all(r.tags == ["payments"] for r in routes)


def test_existing_single_method_callback_ids_and_query_contract_are_unchanged():
    schema = _app().openapi()
    expected = {
        ("/api/pay/zarinpal/callback", "get"): "zarinpal_callback_api_pay_zarinpal_callback_get",
        ("/api/pay/nowpayments/ipn", "post"): "nowpayments_ipn_api_pay_nowpayments_ipn_post",
        ("/api/pay/plisio/callback", "post"): "plisio_callback_api_pay_plisio_callback_post",
    }
    for (path, method), operation_id in expected.items():
        assert schema["paths"][path][method]["operationId"] == operation_id
    parameters = schema["paths"]["/api/pay/zarinpal/callback"]["get"]["parameters"]
    assert {p["name"] for p in parameters} == {"pid", "Authority", "Status"}
    for method in ("get", "post"):
        assert schema["paths"][PATH][method]["parameters"] == [
            {"name": "name", "in": "path", "required": True, "schema": {"type": "string", "title": "Name"}}
        ]


@pytest.fixture
def configured(monkeypatch):
    payment = SimpleNamespace(id=7, provider="synthetic", status=pay.PaymentStatus.PAID)
    db = SimpleNamespace(get=AsyncMock(return_value=payment), commit=AsyncMock(), refresh=AsyncMock())
    gateway = SimpleNamespace(locate=lambda _: None, throttled=lambda _: False)
    monkeypatch.setattr(gateways, "GATEWAYS", {"synthetic": gateway})
    monkeypatch.setattr(pay.engine, "PROVIDERS", {"synthetic": gateway})
    monkeypatch.setattr(pay.engine, "OPEN_STATUSES", set())
    monkeypatch.setattr(pay.engine, "verify", AsyncMock(return_value=True))
    monkeypatch.setattr(pay.engine, "fulfill_one", AsyncMock())
    monkeypatch.setattr(gateways, "parse_body", lambda raw, content_type: ({"fixture": True}, {}))
    monkeypatch.setattr(pay, "_return_page", AsyncMock(return_value=RedirectResponse("/synthetic", 303)))
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_unknown_gateway_remains_404_without_verification(configured, method):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(configured)), base_url="http://test"
    ) as client:
        response = await client.request(method, "/api/pay/gw/missing")
    assert response.status_code == 404
    assert response.text == "unknown gateway"
    pay.engine.verify.assert_not_awaited()
    configured.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["HEAD", "PUT", "PATCH", "DELETE"])
async def test_other_methods_remain_disallowed(configured, method):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(configured)), base_url="http://test"
    ) as client:
        response = await client.request(method, "/api/pay/gw/synthetic?pid=7")
    assert response.status_code == 405
    assert {m.strip() for m in response.headers["allow"].split(",")} == {"GET", "POST"}
    pay.engine.verify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "content_type,raw",
    [
        ("application/json", b'{"signature":"unaltered", "x":1}'),
        ("application/x-www-form-urlencoded", b"x=%2B+%20&x=again"),
    ],
)
async def test_both_methods_preserve_raw_bytes_query_and_provider_verification(
    configured, method, content_type, raw
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(configured)), base_url="http://test"
    ) as client:
        response = await client.request(
            method,
            "/api/pay/gw/synthetic?pid=7&ref=fixture",
            content=raw,
            headers={"content-type": content_type, "X-Signature": "fixture"},
        )
    assert response.status_code == 200
    assert response.json() == {"status": "paid"}
    pay.engine.verify.assert_awaited_once()
    args = pay.engine.verify.await_args.args
    assert args[:2] == (configured, 7)
    assert args[2]["raw"] == raw
    assert args[2]["query"] == {"pid": "7", "ref": "fixture"}
    assert args[2]["headers"]["x-signature"] == "fixture"
    assert args[2]["source"] == "callback"
    configured.commit.assert_awaited_once()
    configured.refresh.assert_awaited_once()
    pay.engine.fulfill_one.assert_awaited_once_with(configured, 7)
    pay._return_page.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,expected", [("GET", 303), ("POST", 200)])
async def test_only_html_get_uses_the_existing_return_page(configured, method, expected):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(configured)), base_url="http://test"
    ) as client:
        response = await client.request(
            method, "/api/pay/gw/synthetic?pid=7", headers={"accept": "text/html"}
        )
    assert response.status_code == expected
    if method == "GET":
        assert response.headers["location"] == "/synthetic"
        pay._return_page.assert_awaited_once()
    else:
        assert response.json() == {"status": "paid"}
        pay._return_page.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("declared", [None, "1", str(pay.MAX_CALLBACK + 1)])
async def test_streaming_body_limit_survives_the_route_split(configured, method, declared):
    consumed = []

    async def chunks():
        for part in [b"a" * pay.MAX_CALLBACK, b"!", b"must-not-be-drained"]:
            consumed.append(len(part))
            yield part

    headers = {"content-length": declared} if declared is not None else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(configured)), base_url="http://test"
    ) as client:
        response = await client.request(
            method, "/api/pay/gw/synthetic?pid=7", content=chunks(), headers=headers
        )
    assert response.status_code == 413
    assert response.text == "too large"
    assert consumed == ([] if declared == str(pay.MAX_CALLBACK + 1) else [pay.MAX_CALLBACK, 1])
    pay.engine.verify.assert_not_awaited()
    configured.get.assert_not_awaited()
    configured.commit.assert_not_awaited()
