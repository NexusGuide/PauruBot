"""Tonpay standard contract with synthetic HTTP; no live invoices or credentials."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from paurubot.db.models import Payment
from paurubot.payments.base import PaymentError
from paurubot.payments.providers.gateways import GATEWAYS, TonpayProvider

pytestmark = pytest.mark.security
CFG = {"tonpay_enabled": True, "tonpay_api_key": "synthetic-key"}


@pytest.fixture
def payment():  # type: ignore[no-untyped-def]
    return cast(
        Payment,
        SimpleNamespace(
            id=42,
            amount=100_000,
            provider_ref="invoice_42",
            provider_amount=100_000,
            provider_currency="IRT",
        ),
    )


@pytest.fixture(autouse=True)
def public_url(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "paurubot.payments.providers.gateways.get_settings",
        lambda: SimpleNamespace(public_url="https://shop.invalid"),
    )


def confirmed(**changes):  # type: ignore[no-untyped-def]
    return {
        "order_id": "42",
        "invoice_id": "invoice_42",
        "paid": True,
        "status": "completed",
        "request_amount": 100_000,
        "final_amount": 100_000,
        **changes,
    }


def gateway(data, status=200):  # type: ignore[no-untyped-def]
    calls = []

    def handle(request):  # type: ignore[no-untyped-def]
        calls.append(request)
        return httpx.Response(status, json=data)

    return TonpayProvider(httpx.MockTransport(handle)), calls


async def test_standard_create_contract_and_no_pii(payment):  # type: ignore[no-untyped-def]
    provider, calls = gateway(
        confirmed(
            invoice_url="https://t.me/synthetic?start=42",
            final_amount=100_037,
            status="pending",
            paid=False,
        )
    )
    checkout = await provider.create(payment, CFG)
    assert checkout.provider_ref == "invoice_42"
    assert checkout.provider_amount == 100_037
    assert checkout.provider_currency == "IRT"
    assert checkout.redirect_url == "https://t.me/synthetic?start=42"
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "POST"
    assert str(request.url) == "https://tonpays.online/api/v1/invoices/create"
    assert request.headers["X-API-Key"] == "synthetic-key"
    assert json.loads(request.content) == {
        "amount": 100_000,
        "order_id": "42",
        "callback_url": "https://shop.invalid/api/pay/gw/tonpay?pid=42",
    }


async def test_default_disabled_key_and_public_url_required(monkeypatch):  # type: ignore[no-untyped-def]
    provider = GATEWAYS["tonpay"]
    assert provider.polls and provider.late_ok
    assert not await provider.enabled({})
    assert not await provider.enabled({"tonpay_enabled": True})
    assert await provider.enabled(CFG)
    monkeypatch.setattr(provider, "public_url", lambda: "")
    assert not await provider.enabled(CFG)


@pytest.mark.parametrize(
    "source", [{"source": "poll"}, {"json": {"paid": False}}, {"query": {"order_id": "42"}}]
)
async def test_only_authenticated_get_can_confirm(payment, source):  # type: ignore[no-untyped-def]
    provider, calls = gateway(confirmed())
    result = await provider.verify(payment, CFG, source)
    assert result.paid and result.amount == payment.amount
    assert result.provider_tx_id == "tonpay:invoice_42"
    assert result.currency == "IRT"
    assert len(calls) == 1 and calls[0].method == "GET"
    assert str(calls[0].url) == "https://tonpays.online/api/v1/invoices/check/invoice_42"
    assert calls[0].headers["X-API-Key"] == "synthetic-key"


@pytest.mark.parametrize(
    "changes",
    [
        {"paid": False},
        {"paid": "false"},
        {"paid": "true"},
        {"paid": 1},
        {"status": "pending"},
        {"status": "need_action"},
        {"status": "expired"},
        {"order_id": "43"},
        {"order_id": True},
        {"invoice_id": "other"},
        {"request_amount": 99_999},
        {"request_amount": True},
        {"final_amount": None},
        {"final_amount": -1},
        {"final_amount": 0},
        {"final_amount": True},
        {"final_amount": "100000.1"},
        {"final_amount": 100000.0},
        {"final_amount": "NaN"},
        {"final_amount": "1e5"},
        {"final_amount": "9" * 19},
        {"currency": "IRR"},
        {"success": False},
    ],
)
async def test_unconfirmed_malformed_or_wrong_identity_never_pays(payment, changes):  # type: ignore[no-untyped-def]
    provider, calls = gateway(confirmed(**changes))
    try:
        result = await provider.verify(payment, CFG, {"json": confirmed()})
    except PaymentError:
        pass
    else:
        assert not result.paid
    assert len(calls) == 1


@pytest.mark.parametrize("actual", [99_999, 100_001])
async def test_changed_settlement_cannot_authorize_wallet_credit(payment, actual):  # type: ignore[no-untyped-def]
    provider, _ = gateway(confirmed(final_amount=actual))
    result = await provider.verify(payment, CFG, {})
    assert not result.paid and result.amount is None
    assert "frozen quote" in result.detail


async def test_optional_remote_invoice_id_and_integer_strings(payment):  # type: ignore[no-untyped-def]
    data = confirmed(request_amount="100000", final_amount="100000")
    del data["invoice_id"]
    provider, _ = gateway(data)
    assert (await provider.verify(payment, CFG, {})).paid


@pytest.mark.parametrize(
    "params",
    [
        {"json": {"order_id": "43"}},
        {"json": {"invoice_id": "other"}},
        {"headers": {"x-api-key": "wrong"}},
    ],
)
async def test_mismatched_callback_cannot_trigger_provider_read(payment, params):  # type: ignore[no-untyped-def]
    provider, calls = gateway(confirmed())
    assert not (await provider.verify(payment, CFG, params)).paid
    assert not calls


async def test_matching_callback_key_is_not_proof_of_payment(payment):  # type: ignore[no-untyped-def]
    provider, calls = gateway(confirmed(paid=False))
    assert not (await provider.verify(payment, CFG, {"headers": {"x-api-key": "synthetic-key"}})).paid
    assert len(calls) == 1


@pytest.mark.parametrize("reference", [None, "", "..", "../create", "x?key=y", "x%2Fy", "a" * 129, True])
async def test_invalid_stored_reference_never_sends_http(payment, reference):  # type: ignore[no-untyped-def]
    payment.provider_ref = reference
    provider, calls = gateway(confirmed())
    assert not (await provider.verify(payment, CFG, {})).paid
    assert not calls


@pytest.mark.parametrize(
    "url",
    [
        "http://tonpays.online/pay",
        "https://evil.invalid/pay",
        "https://tonpays.online.evil.invalid/pay",
        "https://user:secret@tonpays.online/pay",
        "https://tonpays.online:444/pay",
        "javascript:alert(1)",
        "https://127.0.0.1/pay",
        "https://tonpays.online/\nsecret",
        "https://t.me\\@evil.invalid/pay",
    ],
)
async def test_unsafe_checkout_url_is_not_exposed(payment, url):  # type: ignore[no-untyped-def]
    provider, _ = gateway(confirmed(invoice_url=url))
    with pytest.raises(PaymentError, match="gateway_error"):
        await provider.create(payment, CFG)


@pytest.mark.parametrize("status", [301, 302, 401, 429, 500])
async def test_http_failures_never_follow_redirect_or_recreate(payment, status):  # type: ignore[no-untyped-def]
    calls = []

    def handle(request):  # type: ignore[no-untyped-def]
        calls.append(request)
        return httpx.Response(status, json=confirmed(), headers={"Location": "https://evil.invalid/"})

    provider = TonpayProvider(httpx.MockTransport(handle))
    with pytest.raises(PaymentError, match="gateway_error"):
        await provider.verify(payment, CFG, {})
    assert len(calls) == 1 and calls[0].method == "GET"


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"null", b"{" + b" " * 65536 + b"}"])
async def test_malformed_or_oversized_response_rejected(payment, body):  # type: ignore[no-untyped-def]
    provider = TonpayProvider(httpx.MockTransport(lambda _: httpx.Response(200, content=body)))
    with pytest.raises(PaymentError, match="gateway_error"):
        await provider.verify(payment, CFG, {})


async def test_limit_checked_while_streaming(payment):  # type: ignore[no-untyped-def]
    consumed = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            for index in range(10):
                consumed.append(index)
                yield b" " * 32768

    provider = TonpayProvider(httpx.MockTransport(lambda _: httpx.Response(200, stream=Chunks())))
    with pytest.raises(PaymentError, match="gateway_error"):
        await provider.verify(payment, CFG, {})
    assert consumed == [0, 1, 2]


async def test_timeout_is_not_retried_and_secret_not_logged(payment, caplog):  # type: ignore[no-untyped-def]
    calls = []

    def handle(request):  # type: ignore[no-untyped-def]
        calls.append(request)
        raise httpx.ReadTimeout("synthetic-key should not be logged", request=request)

    provider = TonpayProvider(httpx.MockTransport(handle))
    with pytest.raises(PaymentError, match="gateway_error"):
        await provider.create(payment, CFG)
    assert len(calls) == 1
    assert "synthetic-key" not in caplog.text


async def test_missing_key_and_malformed_create_are_closed(payment):  # type: ignore[no-untyped-def]
    provider, calls = gateway({"invoice_url": "https://tonpays.online/pay"})
    with pytest.raises(PaymentError, match="gateway_error"):
        await provider.create(payment, {})
    assert not calls
    with pytest.raises(PaymentError, match="gateway_error"):
        await provider.create(payment, CFG)
    assert len(calls) == 1


def test_callback_locator_only_provides_identity():
    assert TonpayProvider.locate({"json": {"order_id": "42", "paid": True}}) == ("id", "42")
    assert TonpayProvider.locate({"json": {"invoice_id": "invoice_42"}}) == ("ref", "invoice_42")
    assert TonpayProvider.locate({"json": {"paid": True}}) is None


async def test_adjusted_quote_credits_nominal_not_provider_tail(payment):  # type: ignore[no-untyped-def]
    payment.provider_amount = 100_037
    provider, calls = gateway(confirmed(final_amount=100_037))
    result = await provider.verify(payment, CFG, {"json": {"final_amount": 999_999}})
    assert result.paid and result.amount == 100_000
    assert result.raw == {"request_amount": 100_000, "final_amount": 100_037, "currency": "IRT"}
    assert len(calls) == 1 and calls[0].method == "GET"


@pytest.mark.parametrize("actual", [99_999, 100_000, 100_036, 100_038])
async def test_nominal_amount_does_not_bypass_adjusted_quote(payment, actual):  # type: ignore[no-untyped-def]
    payment.provider_amount = 100_037
    provider, _ = gateway(confirmed(final_amount=actual))
    assert not (await provider.verify(payment, CFG, {})).paid


@pytest.mark.parametrize("field", ["order_id", "request_amount", "final_amount"])
async def test_incomplete_creation_quote_fails_closed(payment, field):  # type: ignore[no-untyped-def]
    data = confirmed(invoice_url="https://t.me/synthetic?start=42")
    del data[field]
    provider, calls = gateway(data)
    with pytest.raises(PaymentError, match="gateway_error"):
        await provider.create(payment, CFG)
    assert len(calls) == 1
