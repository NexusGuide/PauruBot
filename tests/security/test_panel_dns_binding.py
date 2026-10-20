"""The address checked by policy must be the address given to the socket transport."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from paurubot.panels.http import SafeHttpClient, fetch_certificate
from paurubot.security import ssrf

pytestmark = pytest.mark.security


async def test_checked_address_is_used_with_original_host_and_sni(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["8.8.8.8"]))
    seen = []

    async def network(self, request):  # type: ignore[no-untyped-def]
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    client = SafeHttpClient("https://panel.example.com:8443/admin")
    try:
        response = await client.request("GET", "/users", params={"page": 2})
    finally:
        await client.aclose()
    assert seen[0].url.host == "8.8.8.8"
    assert seen[0].headers["host"] == "panel.example.com:8443"
    assert seen[0].extensions["sni_hostname"] == "panel.example.com"
    assert seen[0].url.path == "/admin/users" and seen[0].url.query == b"page=2"
    assert response.request.url.host == "panel.example.com"


async def test_certificate_inspection_connects_to_validated_ip(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import Mock

    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["8.8.8.8"]))
    writer = Mock()
    writer.get_extra_info.return_value = SimpleNamespace(getpeercert=lambda **_: b"fake-der")
    writer.wait_closed = AsyncMock()
    connect = AsyncMock(return_value=(None, writer))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    await fetch_certificate("https://panel.example.com:8443")
    assert connect.call_args.args == ("8.8.8.8", 8443)
    assert connect.call_args.kwargs["server_hostname"] == "panel.example.com"


@pytest.mark.parametrize(
    "addresses,private",
    [
        (["8.8.8.8", "127.0.0.1"], False),
        (["169.254.169.254"], True),
        (["::ffff:169.254.169.254"], True),
        (["fd00:ec2::254"], True),
    ],
)
async def test_entire_dns_answer_is_checked_before_any_dial(monkeypatch, addresses, private) -> None:  # type: ignore[no-untyped-def]
    from paurubot.panels.base import PanelError

    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=addresses))
    network = AsyncMock(return_value=httpx.Response(200))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    client = SafeHttpClient("https://panel.example.com", allow_private=private)
    try:
        with pytest.raises(PanelError, match="blocked"):
            await client.request("GET", "/")
        network.assert_not_awaited()
    finally:
        await client.aclose()


async def test_next_request_revalidates_changed_dns_without_sixty_second_cache(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.panels.base import PanelError

    resolver = AsyncMock(side_effect=[["8.8.8.8"], ["127.0.0.1"]])
    monkeypatch.setattr(ssrf, "resolve", resolver)
    network = AsyncMock(return_value=httpx.Response(200))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    client = SafeHttpClient("https://panel.example.com")
    try:
        await client.request("GET", "/")
        with pytest.raises(PanelError, match="blocked"):
            await client.request("GET", "/")
        assert network.await_count == 1 and resolver.await_count == 2
    finally:
        await client.aclose()


async def test_connect_failure_tries_next_validated_ip_and_keeps_body(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["8.8.8.8", "1.1.1.1"]))
    seen = []

    async def network(self, request):  # type: ignore[no-untyped-def]
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ConnectError("unreachable", request=request)
        assert await request.aread() == b"body-once"
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    client = SafeHttpClient("https://panel.example.com")
    try:
        await client.request("POST", "/users", content=b"body-once")
        assert [r.url.host for r in seen] == ["8.8.8.8", "1.1.1.1"]
        assert all(r.headers["host"] == "panel.example.com" for r in seen)
    finally:
        await client.aclose()


@pytest.mark.parametrize("error", [httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError])
async def test_no_post_replay_after_http_may_have_started(monkeypatch, error) -> None:  # type: ignore[no-untyped-def]
    from paurubot.panels.base import PanelUnavailable

    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["8.8.8.8", "1.1.1.1"]))
    network = AsyncMock(side_effect=error("uncertain"))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    client = SafeHttpClient("https://panel.example.com")
    try:
        with pytest.raises(PanelUnavailable):
            await client.request("POST", "/users", content=b"body")
        assert network.await_count == 1
    finally:
        await client.aclose()


async def test_http_500_does_not_try_another_address(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["8.8.8.8", "1.1.1.1"]))
    network = AsyncMock(return_value=httpx.Response(500))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    client = SafeHttpClient("https://panel.example.com")
    try:
        assert (await client.request("POST", "/users")).status_code == 500
        assert network.await_count == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "target",
    [
        "https://other.example.com/users",
        "http://panel.example.com/users",
        "https://panel.example.com:444/users",
        "https://user:pass@panel.example.com/users",
    ],
)
async def test_absolute_origin_changes_are_refused_before_auth_can_leave(monkeypatch, target) -> None:  # type: ignore[no-untyped-def]
    from paurubot.panels.base import PanelError

    network = AsyncMock(return_value=httpx.Response(200))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    client = SafeHttpClient("https://panel.example.com")
    try:
        with pytest.raises(PanelError, match="cross-origin"):
            await client.request("GET", target, headers={"Authorization": "test-credential"})
        network.assert_not_awaited()
    finally:
        await client.aclose()


async def test_host_and_sni_cannot_be_overridden(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["8.8.8.8"]))

    async def network(self, request):  # type: ignore[no-untyped-def]
        assert request.headers["host"] == "panel.example.com"
        assert request.extensions["sni_hostname"] == "panel.example.com"
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    client = SafeHttpClient("https://panel.example.com")
    try:
        await client.request(
            "GET", "/", headers={"Host": "evil.example.com"}, extensions={"sni_hostname": "evil.example.com"}
        )
    finally:
        await client.aclose()


async def test_dns_timeout_is_bounded_and_translated(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    from paurubot.config import get_settings
    from paurubot.panels.base import PanelUnavailable

    async def slow(*args, **kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(10)

    monkeypatch.setattr(ssrf, "resolve", slow)
    monkeypatch.setattr(get_settings(), "http_connect_timeout", 0.01)
    client = SafeHttpClient("https://panel.example.com")
    try:
        with pytest.raises(PanelUnavailable, match="ConnectTimeout"):
            await asyncio.wait_for(client.request("GET", "/"), timeout=1)
    finally:
        await client.aclose()


async def test_certificate_fallback_only_uses_validated_addresses(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import Mock

    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["8.8.8.8", "1.1.1.1"]))
    writer = Mock()
    writer.get_extra_info.return_value = SimpleNamespace(getpeercert=lambda **_: b"fake-der")
    writer.wait_closed = AsyncMock()
    connect = AsyncMock(side_effect=[OSError("unreachable"), (None, writer)])
    monkeypatch.setattr(asyncio, "open_connection", connect)
    await fetch_certificate("https://panel.example.com")
    assert [c.args[0] for c in connect.call_args_list] == ["8.8.8.8", "1.1.1.1"]
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


@pytest.mark.parametrize(
    "base,expected",
    [("http://8.8.8.8:8080", "8.8.8.8"), ("https://[2606:4700:4700::1111]:8443", "2606:4700:4700::1111")],
)
async def test_numeric_origins_need_no_hostname_resolution(monkeypatch, base, expected) -> None:  # type: ignore[no-untyped-def]
    resolver = AsyncMock(side_effect=AssertionError("literal must not resolve"))
    monkeypatch.setattr(ssrf, "resolve", resolver)

    async def network(self, request):  # type: ignore[no-untyped-def]
        assert request.url.host == expected
        assert request.headers["host"] == httpx.URL(base).netloc.decode()
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    client = SafeHttpClient(base)
    try:
        await client.request("GET", "/")
        resolver.assert_not_awaited()
    finally:
        await client.aclose()


@pytest.fixture
async def identity_server(tmp_path):  # type: ignore[no-untyped-def]
    """A CA-trusted DNS certificate, not check_hostname=False pinning."""
    import asyncio
    import datetime as dt
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "panel.identity.test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("panel.identity.test")]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    cp, kp = tmp_path / "cert.pem", tmp_path / "key.pem"
    cp.write_text(pem)
    kp.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cp, kp)
    names, requests = [], []
    context.set_servername_callback(lambda sock, name, ctx: names.append(name))

    async def handle(reader, writer):  # type: ignore[no-untyped-def]
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\nSet-Cookie: login=kept; Path=/\r\n\r\nok"
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    yield server.sockets[0].getsockname()[1], pem, names, requests
    server.close()
    await server.wait_closed()


async def test_real_tls_keeps_dns_identity_host_cookies_and_ignores_proxy(
    monkeypatch, identity_server
) -> None:  # type: ignore[no-untyped-def]
    import ssl

    from paurubot.panels import http

    port, pem, names, requests = identity_server
    ctx = ssl.create_default_context(cadata=pem)
    assert ctx.check_hostname
    monkeypatch.setattr(http, "ssl_context", lambda _: ctx)
    resolver = AsyncMock(return_value=["127.0.0.1"])
    monkeypatch.setattr(ssrf, "resolve", resolver)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    client = SafeHttpClient(f"https://panel.identity.test:{port}", allow_private=True)
    try:
        for _ in range(2):
            response = await client.request("GET", "/users")
            assert response.text == "ok" and response.url.host == "panel.identity.test"
        assert resolver.await_count == 2
        assert names == ["panel.identity.test", "panel.identity.test"]
        assert all(f"Host: panel.identity.test:{port}".encode() in req for req in requests)
        assert b"Cookie: login=kept" in requests[1]
    finally:
        await client.aclose()


async def test_real_tls_wrong_dns_name_is_rejected_even_at_approved_ip(monkeypatch, identity_server) -> None:  # type: ignore[no-untyped-def]
    import ssl

    from paurubot.panels import http
    from paurubot.panels.base import PanelUnavailable

    port, pem, _, requests = identity_server
    monkeypatch.setattr(http, "ssl_context", lambda _: ssl.create_default_context(cadata=pem))
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["127.0.0.1"]))
    client = SafeHttpClient(f"https://wrong.identity.test:{port}", allow_private=True)
    try:
        with pytest.raises(PanelUnavailable):
            await client.request("POST", "/login", content=b"test-password")
        assert requests == []
    finally:
        await client.aclose()
