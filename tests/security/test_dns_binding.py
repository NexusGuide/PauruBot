"""S05: the destination validated by the SSRF policy must be the socket destination."""

from __future__ import annotations

import asyncio
import ssl
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from paurubot.panels.base import PanelError, PanelUnavailable
from paurubot.panels.http import SafeHttpClient, fetch_certificate
from paurubot.security import ssrf

pytestmark = pytest.mark.security


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    calls: list[httpx.Request] = []

    async def send(self, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    return calls


@pytest.mark.parametrize("address", ["1.1.1.1", "2606:4700:4700::1111"])
async def test_connects_to_validated_ip_keeps_host_sni_and_original_response_url(monkeypatch, wire, address):
    resolve = AsyncMock(return_value=[address])
    monkeypatch.setattr(ssrf, "resolve", resolve)
    client = SafeHttpClient("https://panel.example:8443/base")
    try:
        response = await client.request("POST", "/api?x=1", content=b"payload")
        assert wire[0].url.host == address
        assert wire[0].headers["host"] == "panel.example:8443"
        assert wire[0].extensions["sni_hostname"] == "panel.example"
        assert wire[0].url.port == 8443
        assert wire[0].url.raw_path == b"/base/api?x=1"
        assert await wire[0].aread() == b"payload"
        assert response.request.url.host == "panel.example"
        resolve.assert_awaited_once_with("panel.example", 8443)
    finally:
        await client.aclose()


async def test_rebinding_between_requests_is_rejected_before_second_send(monkeypatch, wire):
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(side_effect=[["1.1.1.1"], ["169.254.169.254"]]))
    client = SafeHttpClient("https://panel.example")
    try:
        await client.request("GET", "/")
        with pytest.raises(PanelError, match="blocked outbound address"):
            await client.request("POST", "/", content=b"secret")
        assert len(wire) == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize("allow_private", [False, True])
async def test_mixed_dns_answer_rejects_entire_set(monkeypatch, wire, allow_private):
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["1.1.1.1", "169.254.169.254"]))
    client = SafeHttpClient("https://panel.example", allow_private=allow_private)
    try:
        with pytest.raises(PanelError):
            await client.request("GET", "/")
        assert wire == []
    finally:
        await client.aclose()


@pytest.mark.parametrize("allowed", [False, True])
async def test_private_dns_needs_explicit_policy(monkeypatch, wire, allowed):
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["10.2.3.4"]))
    client = SafeHttpClient("http://panel.example", allow_private=allowed)
    try:
        if allowed:
            await client.request("GET", "/")
            assert wire[0].url.host == "10.2.3.4"
        else:
            with pytest.raises(PanelError):
                await client.request("GET", "/")
            assert not wire
    finally:
        await client.aclose()


async def test_retries_other_validated_ip_only_on_connect_failure(monkeypatch):
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["1.1.1.1", "8.8.8.8"]))
    calls = []

    async def send(self, request):
        calls.append(request.url.host)
        if len(calls) == 1:
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    client = SafeHttpClient("https://panel.example")
    try:
        assert (await client.request("POST", "/", content=b"one effect")).status_code == 200
        assert calls == ["1.1.1.1", "8.8.8.8"]
    finally:
        await client.aclose()


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError])
async def test_uncertain_delivery_does_not_retry_another_ip(monkeypatch, error):
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["1.1.1.1", "8.8.8.8"]))
    send = AsyncMock(side_effect=error("uncertain delivery"))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    client = SafeHttpClient("https://panel.example")
    try:
        with pytest.raises(PanelUnavailable):
            await client.request("POST", "/", content=b"one effect")
        assert send.await_count == 1
    finally:
        await client.aclose()


async def test_request_sni_extension_cannot_override_verified_hostname(monkeypatch, wire):
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["1.1.1.1"]))
    client = SafeHttpClient("https://panel.example")
    try:
        await client.request("GET", "/", extensions={"sni_hostname": "attacker.example"})
        assert wire[0].extensions["sni_hostname"] == "panel.example"
    finally:
        await client.aclose()


async def test_environment_proxy_cannot_bypass_binding(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:8080")
    client = SafeHttpClient("https://panel.example")
    try:
        # httpx environment proxy mounts replace the guarded transport if trust_env is left on.
        assert not any(value is not None for value in client._client._mounts.values())
    finally:
        await client.aclose()


async def test_absolute_cross_origin_request_rejected(monkeypatch, wire):
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["1.1.1.1"]))
    client = SafeHttpClient("https://panel.example")
    try:
        with pytest.raises(PanelError, match="origin"):
            await client.request("GET", "https://other.example/api", headers={"Authorization": "secret"})
        assert not wire
    finally:
        await client.aclose()


async def test_redirects_stay_disabled_even_if_requested(monkeypatch):
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["1.1.1.1"]))
    calls = []

    async def send(self, request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "/again"}, content=b"")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    client = SafeHttpClient("https://panel.example")
    try:
        response = await client.request("GET", "/", follow_redirects=True)
        assert response.status_code == 302
        assert len(calls) == 1
    finally:
        await client.aclose()


async def test_custom_mock_transport_keeps_existing_offline_contract():
    client = SafeHttpClient(
        "https://panel.invalid", transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    try:
        assert (await client.request("GET", "/")).status_code == 200
    finally:
        await client.aclose()


async def test_certificate_probe_uses_validated_ip_with_original_sni(monkeypatch):
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["1.1.1.1"]))
    seen = []

    class TLS:
        def getpeercert(self, binary_form=False):
            return b"test-certificate-bytes"

    class Writer:
        def get_extra_info(self, key):
            return TLS()

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(host, port, **kwargs):
        seen.append((host, port, kwargs["server_hostname"]))
        return None, Writer()

    monkeypatch.setattr(asyncio, "open_connection", connect)
    await fetch_certificate("https://panel.example:9443")
    assert seen == [("1.1.1.1", 9443, "panel.example")]


async def test_real_tls_host_and_sni_survive_ip_binding(monkeypatch, tmp_path: Path):
    """No external connection: DNS policy approves loopback explicitly; TLS checks the DNS name."""
    import datetime as dt
    import socket

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "panel.invalid")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("panel.invalid")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    certpath, keypath = tmp_path / "cert.pem", tmp_path / "key.pem"
    certpath.write_text(pem)
    keypath.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(certpath, keypath)
    sni, messages = [], []
    server_ctx.set_servername_callback(lambda obj, name, ctx: sni.append(name))

    async def handle(reader, writer):
        try:
            messages.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["127.0.0.1"]))
    loop = asyncio.get_running_loop()
    original_getaddrinfo = loop.getaddrinfo

    async def numeric_loopback_only(host, *args, **kwargs):
        if host not in ("127.0.0.1", b"127.0.0.1"):
            raise socket.gaierror("test blocks non-loopback socket destinations")
        return await original_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", numeric_loopback_only)
    # Test normal CA/hostname verification, not the hostname-disabled TOFU mode.
    from paurubot.panels import http as panel_http

    monkeypatch.setattr(panel_http, "ssl_context", lambda pinned: ssl.create_default_context(cadata=pem))
    client = SafeHttpClient(f"https://panel.invalid:{port}", allow_private=True)
    try:
        response = await client.request("GET", "/")
        assert response.text == "ok"
        assert sni == ["panel.invalid"]
        assert f"Host: panel.invalid:{port}".encode() in messages[0]
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()


async def test_resolution_timeout_is_bounded(monkeypatch, wire):
    async def slow_resolve(host, port):
        await asyncio.sleep(1)
        return ["1.1.1.1"]

    monkeypatch.setattr(ssrf, "resolve", slow_resolve)
    client = SafeHttpClient("https://panel.example")
    try:
        with pytest.raises(PanelUnavailable, match="ConnectTimeout"):
            await client.request("GET", "/", timeout=0.001)
        assert not wire
    finally:
        await client.aclose()


@pytest.mark.parametrize("status", [400, 429, 500])
async def test_http_response_is_never_replayed_on_another_ip(monkeypatch, status):
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["1.1.1.1", "8.8.8.8"]))
    send = AsyncMock(return_value=httpx.Response(status, content=b""))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    client = SafeHttpClient("https://panel.example")
    try:
        assert (await client.request("POST", "/")).status_code == status
        assert send.await_count == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "url", ["http://169.254.169.254", "http://[::ffff:169.254.169.254]", "http://[fe80::1]"]
)
async def test_metadata_and_link_local_never_allowed_even_with_private_opt_in(wire, url):
    client = SafeHttpClient(url, allow_private=True)
    try:
        with pytest.raises(PanelError):
            await client.request("GET", "/")
        assert not wire
    finally:
        await client.aclose()


async def test_cookies_stay_scoped_to_original_hostname(monkeypatch):
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["1.1.1.1"]))
    calls = []

    async def send(self, request):
        calls.append(request)
        return httpx.Response(
            200, headers={"Set-Cookie": "session=opaque; Domain=panel.example; Path=/; Secure"}, content=b""
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    client = SafeHttpClient("https://panel.example")
    try:
        await client.request("GET", "/login")
        await client.request("GET", "/account")
        assert calls[1].headers["Cookie"] == "session=opaque"
        assert list(client.cookies.jar)[0].domain == ".panel.example"
    finally:
        await client.aclose()


async def test_connect_budget_is_shared_across_addresses(monkeypatch):
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["1.1.1.1", "8.8.8.8"]))
    calls = []

    async def send(self, request):
        calls.append(request)
        await asyncio.sleep(0.02)
        raise httpx.ConnectTimeout("unreachable")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    client = SafeHttpClient("https://panel.example")
    try:
        with pytest.raises(PanelUnavailable):
            await client.request("GET", "/", timeout=0.01)
        assert len(calls) == 1
    finally:
        await client.aclose()


async def test_certificate_probe_retries_only_checked_addresses(monkeypatch):
    monkeypatch.setattr(ssrf, "resolve", AsyncMock(return_value=["1.1.1.1", "8.8.8.8"]))
    connect = AsyncMock(side_effect=OSError("offline"))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    with pytest.raises(PanelUnavailable):
        await fetch_certificate("https://panel.example")
    assert [call.args[0] for call in connect.await_args_list] == ["1.1.1.1", "8.8.8.8"]
