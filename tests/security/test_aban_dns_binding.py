"""Configurable payment origins must use the exact IPs approved by policy."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from paurubot.config import get_settings
from paurubot.payments.providers.gateways import AbanGatewayProvider
from paurubot.security import ssrf
from tests.security import test_panel_dns_binding as tls_fixtures

identity_server = tls_fixtures.identity_server

pytestmark = pytest.mark.security

CFG = {"abangateway_url": "https://aban.example.com:8443/api", "abangateway_api_key": "fake-aban-api-key"}


def payment():  # type: ignore[no-untyped-def]
    return SimpleNamespace(id=81, amount=100_000, provider_ref="abn_invoice81", provider_amount=1_000_000)


@pytest.mark.parametrize("operation", ["create", "verify"])
async def test_create_and_verify_connect_to_checked_ip_not_hostname(monkeypatch, operation) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "public_url", "https://bot.example.com")
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["8.8.8.8"]))
    seen = []

    async def network(self, request):  # type: ignore[no-untyped-def]
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "authority": "abn_invoice81",
                "payment_link": "https://aban.example.com/pay/81",
                "order_id": "81",
                "amount": 1_000_000,
            },
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    gateway = AbanGatewayProvider()
    if operation == "create":
        result = await gateway.create(payment(), CFG)
        assert result.provider_amount == 1_000_000
    else:
        result = await gateway.verify(payment(), CFG, {})
        assert result.paid and result.amount == 100_000
    assert len(seen) == 1 and seen[0].url.host == "8.8.8.8"
    assert seen[0].url.path == f"/api/{operation}"
    assert seen[0].headers["host"] == "aban.example.com:8443"
    assert seen[0].headers["authorization"] == "Bearer fake-aban-api-key"
    assert seen[0].extensions["sni_hostname"] == "aban.example.com"


@pytest.mark.parametrize("operation", ["create", "verify"])
@pytest.mark.parametrize(
    "addresses", [["127.0.0.1"], ["8.8.8.8", "10.0.0.2"], ["169.254.169.254"], ["::ffff:169.254.169.254"]]
)
async def test_gateway_never_inherits_private_panel_approval(monkeypatch, operation, addresses) -> None:  # type: ignore[no-untyped-def]
    from paurubot.payments.base import PaymentError

    monkeypatch.setattr(get_settings(), "allow_private_panel_hosts", True)
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=addresses))
    network = AsyncMock(return_value=httpx.Response(200))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    gateway = AbanGatewayProvider()
    with pytest.raises(PaymentError, match="gateway_error"):
        if operation == "create":
            await gateway.create(payment(), CFG)
        else:
            await gateway.verify(payment(), CFG, {})
    network.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "https://",
        "https://user:secret@aban.example.com",
        "https://aban.example.com:bad",
        "https://aban.example.com/api?token=secret",
        "https://aban.example.com/api#secret",
        "https://aban.example.com/a\nb",
        "http://aban.example.com",
        "https://127.0.0.1/api",
    ],
)
async def test_invalid_base_fails_before_key_can_be_sent(monkeypatch, url, caplog) -> None:  # type: ignore[no-untyped-def]
    from paurubot.payments.base import PaymentError

    network = AsyncMock(return_value=httpx.Response(200))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    gateway = AbanGatewayProvider()
    with pytest.raises(PaymentError, match="gateway_error"):
        await gateway.create(payment(), {**CFG, "abangateway_url": url})
    network.assert_not_awaited()
    assert "fake-aban-api-key" not in caplog.text and "secret" not in caplog.text


async def test_gateway_dns_failure_is_payment_error_without_retry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.payments.base import PaymentError

    resolve = AsyncMock(side_effect=ssrf.UnsafeUrl("cannot resolve configured gateway"))
    monkeypatch.setattr(ssrf, "resolve", resolve)
    network = AsyncMock()
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    with pytest.raises(PaymentError, match="gateway_error"):
        await AbanGatewayProvider().create(payment(), CFG)
    network.assert_not_awaited()
    resolve.assert_awaited_once()


@pytest.mark.parametrize("error", [httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError])
async def test_uncertain_post_is_not_replayed_on_second_address(monkeypatch, error, caplog) -> None:  # type: ignore[no-untyped-def]
    from paurubot.payments.base import PaymentError

    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["8.8.8.8", "1.1.1.1"]))
    network = AsyncMock(side_effect=error("network failed fake-aban-api-key"))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    with pytest.raises(PaymentError, match="gateway_error"):
        await AbanGatewayProvider().create(payment(), CFG)
    network.assert_awaited_once()
    assert "fake-aban-api-key" not in caplog.text


async def test_pre_http_failure_uses_next_approved_ip(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["8.8.8.8", "1.1.1.1"]))
    calls = []

    async def network(self, request):  # type: ignore[no-untyped-def]
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("unreachable")
        return httpx.Response(
            200,
            json={
                "success": True,
                "authority": "abn_invoice81",
                "payment_link": "https://pay.example.com/81",
            },
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    result = await AbanGatewayProvider().create(payment(), CFG)
    assert result.provider_ref == "abn_invoice81"
    assert [r.url.host for r in calls] == ["8.8.8.8", "1.1.1.1"]


async def test_create_verify_revalidate_and_do_not_follow_redirect(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["8.8.8.8"]))
    calls = []

    async def network(self, request):  # type: ignore[no-untyped-def]
        calls.append(request)
        return httpx.Response(
            302, headers={"Location": "https://169.254.169.254/metadata"}, json={"success": False}
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    result = await AbanGatewayProvider().verify(payment(), CFG, {})
    assert not result.paid
    assert len(calls) == 1 and calls[0].url.host == "8.8.8.8"


async def test_shared_provider_does_not_mix_shop_origins_or_keys(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    values = {"a.example.com": ("1.1.1.1", "key-a"), "b.example.com": ("8.8.8.8", "key-b")}

    async def resolve(host, port):  # type: ignore[no-untyped-def]
        return [values[host][0]]

    monkeypatch.setattr(ssrf, "resolve", resolve)
    barrier = asyncio.Barrier(2)
    calls = []

    async def network(self, request):  # type: ignore[no-untyped-def]
        calls.append(request)
        await barrier.wait()
        return httpx.Response(
            200,
            json={
                "success": True,
                "authority": "abn_invoice81",
                "payment_link": "https://pay.example.com/81",
            },
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    gateway = AbanGatewayProvider()
    await asyncio.wait_for(
        asyncio.gather(
            *(
                gateway.create(
                    payment(), {"abangateway_url": f"https://{host}/api", "abangateway_api_key": key}
                )
                for host, (_, key) in values.items()
            )
        ),
        timeout=3,
    )
    assert {(r.headers["host"], r.url.host, r.headers["authorization"]) for r in calls} == {
        (host, ip, f"Bearer {key}") for host, (ip, key) in values.items()
    }


async def test_explicit_mock_transport_remains_dns_free(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    resolver = AsyncMock(side_effect=AssertionError("test transport must not resolve"))
    monkeypatch.setattr(ssrf, "resolve", resolver)
    gateway = AbanGatewayProvider(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "success": True,
                    "authority": "abn_invoice81",
                    "payment_link": "https://pay.example.com/81",
                },
            )
        )
    )
    assert (await gateway.create(payment(), CFG)).provider_ref == "abn_invoice81"
    resolver.assert_not_awaited()


async def test_default_gateway_transport_keeps_ca_hostname_checks_and_no_private_override(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    import ssl

    from paurubot.payments.providers import gateways as gw

    original = gw.ValidatedTransport
    seen = []

    def transport(*args, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["context"].check_hostname
        assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["allow_private"] is False
        seen.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(gw, "ValidatedTransport", transport)
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["8.8.8.8"]))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    network = AsyncMock(return_value=httpx.Response(200, json={"success": False}))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    await AbanGatewayProvider().verify(payment(), CFG, {})
    assert seen == [CFG["abangateway_url"] + "/verify"] and network.await_count == 1


async def test_changed_dns_between_create_and_verify_is_blocked(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.payments.base import PaymentError

    resolver = AsyncMock(side_effect=[["8.8.8.8"], ["127.0.0.1"]])
    monkeypatch.setattr(ssrf, "resolve", resolver)
    network = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "authority": "abn_invoice81",
                "payment_link": "https://pay.example.com/81",
            },
        )
    )
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    gateway = AbanGatewayProvider()
    await gateway.create(payment(), CFG)
    with pytest.raises(PaymentError, match="gateway_error"):
        await gateway.verify(payment(), CFG, {})
    assert resolver.await_count == 2 and network.await_count == 1


@pytest.mark.parametrize("matches", [True, False])
async def test_gateway_client_real_tls_preserves_dns_certificate_identity(
    monkeypatch, identity_server, matches
) -> None:  # type: ignore[no-untyped-def]
    import ssl

    from paurubot.payments.providers import gateways as gw

    port, pem, names, requests = identity_server
    original_context = ssl.create_default_context
    monkeypatch.setattr(gw.ssl, "create_default_context", lambda: original_context(cadata=pem))

    async def checked(url, **kwargs):  # type: ignore[no-untyped-def]
        # Test-only route to the local TLS fixture; policy rejection is tested above.
        assert kwargs["allow_private"] is False
        return ["127.0.0.1"]

    monkeypatch.setattr(ssrf, "assert_safe_url", checked)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    host = "panel.identity.test" if matches else "wrong.identity.test"
    gateway = AbanGatewayProvider()
    async with gateway._request_client(f"https://{host}:{port}/api") as client:
        if matches:
            response = await client.get(
                f"https://{host}:{port}/api", headers={"Authorization": "Bearer fake-aban-api-key"}
            )
            assert response.text == "ok" and response.url.host == host
            assert names == [host] and len(requests) == 1
            assert f"Host: {host}:{port}".encode() in requests[0]
            assert b"Bearer fake-aban-api-key" in requests[0]
        else:
            with pytest.raises(httpx.ConnectError):
                await client.get(
                    f"https://{host}:{port}/api", headers={"Authorization": "Bearer fake-aban-api-key"}
                )
            assert requests == []
