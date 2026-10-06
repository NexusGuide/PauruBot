"""Self-signed panels work only with the exact certificate the owner pinned (NEW-02)."""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import ssl
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from paurubot.panels import PanelUnavailable
from paurubot.panels.http import SafeHttpClient, fetch_certificate

pytestmark = pytest.mark.security


def _self_signed(tmp: Path, name: str) -> tuple[Path, Path, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    cert_path, key_path = tmp / f"{name}.crt", tmp / f"{name}.key"
    cert_path.write_text(pem)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return cert_path, key_path, pem


async def _serve(cert: Path, key: Path) -> tuple[asyncio.Server, int]:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            body = b'{"ok":true}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    return server, port


@pytest.fixture
async def tls_server(tmp_path: Path) -> AsyncIterator[tuple[int, str, str]]:
    cert, key, pem = _self_signed(tmp_path, "panel")
    _, _, other_pem = _self_signed(tmp_path, "attacker")
    server, port = await _serve(cert, key)
    yield port, pem, other_pem
    server.close()
    await server.wait_closed()


async def test_unpinned_self_signed_is_rejected(tls_server) -> None:  # type: ignore[no-untyped-def]
    port, _, _ = tls_server
    client = SafeHttpClient(f"https://127.0.0.1:{port}", allow_private=True)
    with pytest.raises(PanelUnavailable):
        await client.request("GET", "/")
    await client.aclose()


async def test_pinned_certificate_is_accepted(tls_server) -> None:  # type: ignore[no-untyped-def]
    port, pem, _ = tls_server
    client = SafeHttpClient(f"https://127.0.0.1:{port}", pinned_pem=pem, allow_private=True)
    resp = await client.request("GET", "/")
    assert resp.json() == {"ok": True}
    await client.aclose()


async def test_different_pinned_certificate_is_rejected(tls_server) -> None:  # type: ignore[no-untyped-def]
    port, _, other_pem = tls_server
    client = SafeHttpClient(f"https://127.0.0.1:{port}", pinned_pem=other_pem, allow_private=True)
    with pytest.raises(PanelUnavailable):
        await client.request("GET", "/")
    await client.aclose()


async def test_fetch_certificate_returns_pem_and_fingerprint(tls_server) -> None:  # type: ignore[no-untyped-def]
    port, pem, _ = tls_server
    fetched, fingerprint = await fetch_certificate(f"https://127.0.0.1:{port}", allow_private=True)
    assert fetched.strip() == pem.strip()
    assert len(fingerprint.split(":")) == 32
