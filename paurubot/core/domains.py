"""Subscription-link domains (ClockBot parity B4).

A shop registers a domain; Caddy (on-demand TLS) asks `/internal/tls-ask` before it requests a
certificate, so only registered hosts ever get one. A check, at "check now" and every few hours,
looks at DNS (a public address), the certificate (valid, its expiry and issuer) and that the host
really reaches this server (a mark only this install can answer). Only a healthy domain the shop
chose carries its subscription links; anything else falls back to the main address.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import re
import socket
import ssl
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.config import get_settings
from paurubot.core.shops import lock_shop
from paurubot.db.base import utcnow
from paurubot.db.models import ShopDomain
from paurubot.security import ssrf

PENDING, OK, DNS, TLS, UNREACHABLE, NOT_OURS = "pending", "ok", "dns", "tls", "unreachable", "not_ours"
MAX_PER_SHOP = 20
MARK_PATH = "/s/_/domain"
DNS_TIMEOUT, CONNECT_TIMEOUT = 5.0, 30.0  # the first handshake waits while Caddy gets the certificate
PARALLEL_CHECKS = 8
MAX_MARK_BYTES = 32 * 1024
CERT_WARN_DAYS = 14
ALERT_COOLDOWN = 86_400
_FAILED_STATUSES = frozenset({DNS, TLS, UNREACHABLE, NOT_OURS})

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TLD = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")
_RESERVED_TLDS = {"localhost", "local", "internal", "lan", "home", "test", "invalid", "onion", "arpa"}


class DomainError(Exception):
    """A refused request; the message is the error code the API returns."""


class ProbeError(Exception):
    def __init__(self, message: str, *, unreachable: bool = False) -> None:
        super().__init__(message)
        self.unreachable = unreachable


@dataclass(frozen=True, slots=True)
class CertInfo:
    expires_at: datetime | None
    issuer: str | None


@dataclass(frozen=True, slots=True)
class CheckResult:
    status: str
    error: str | None = None
    expires_at: datetime | None = None
    issuer: str | None = None


class Probe(Protocol):
    async def resolve(self, host: str) -> list[str]: ...
    async def certificate(self, host: str) -> CertInfo: ...
    async def fetch_mark(self, host: str) -> str: ...


# ---------------------------------------------------------------- the address


def normalize_host(raw: str | None) -> str:
    """A bare lower-case host name (punycode for international names), or DomainError."""
    text = (raw or "").strip()
    if not text or len(text) > 300 or any(ch.isspace() for ch in text):
        raise DomainError("domain_invalid")
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme.lower() not in {"http", "https"}:
            raise DomainError("domain_invalid")
        netloc = parts.netloc
    else:
        netloc = re.split(r"[/?#]", text, maxsplit=1)[0]
    if "@" in netloc or netloc.startswith("["):
        raise DomainError("domain_invalid")
    host, _, port = netloc.partition(":")
    if port not in {"", "443", "80"}:
        raise DomainError("domain_invalid")
    host = host.rstrip(".").lower()
    if "*" in host:
        raise DomainError("domain_invalid")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise DomainError("domain_invalid") from exc
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise DomainError("domain_invalid")
    labels = host.split(".")
    if (
        len(host) > 253
        or len(labels) < 2
        or not all(_LABEL.match(label) for label in labels)
        or not _TLD.match(labels[-1])
        or labels[-1] in _RESERVED_TLDS
    ):
        raise DomainError("domain_invalid")
    return host


def main_host() -> str | None:
    url = get_settings().public_url
    return (urlsplit(url).hostname or "").lower().rstrip(".") or None if url else None


def marker(host: str) -> str:
    """What this install answers at MARK_PATH on `host`: proves the domain reaches this server."""
    key = get_settings().master_key.get_secret_value().encode() or b"paurubot"
    return hmac.new(key, f"domain:{host.lower()}".encode(), hashlib.sha256).hexdigest()[:32]


# ---------------------------------------------------------------- the shop's domains


async def add(session: AsyncSession, shop_id: int, raw: str) -> ShopDomain:
    host = normalize_host(raw)
    if host == main_host():
        raise DomainError("domain_is_main")
    await lock_shop(session, shop_id)  # two adds at once can't both take the last slot
    count = await session.scalar(select(func.count(ShopDomain.id)).where(ShopDomain.shop_id == shop_id))
    if (count or 0) >= MAX_PER_SHOP:
        raise DomainError("domain_limit")
    if await session.scalar(select(ShopDomain.id).where(ShopDomain.host == host)) is not None:
        raise DomainError("domain_taken")
    row = ShopDomain(shop_id=shop_id, host=host, status=PENDING)
    session.add(row)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        raise DomainError("domain_taken") from exc
    return row


async def use_for_links(session: AsyncSession, shop_id: int, domain_id: int | None) -> None:
    """The domain that carries the shop's links from now on; None goes back to the main address."""
    row = None
    if domain_id is not None:
        row = await session.get(ShopDomain, domain_id)
        if row is None or row.shop_id != shop_id:
            raise DomainError("domain_not_found")
        if row.status != OK:
            raise DomainError("domain_not_ready")
    await session.execute(update(ShopDomain).where(ShopDomain.shop_id == shop_id).values(for_links=False))
    if row is not None:
        row.for_links = True
    await session.flush()


async def link_domain(session: AsyncSession, shop_id: int) -> str | None:
    """The healthy domain the shop chose for its links, if any."""
    return await session.scalar(
        select(ShopDomain.host).where(
            ShopDomain.shop_id == shop_id, ShopDomain.for_links.is_(True), ShopDomain.status == OK
        )
    )


async def tls_allowed(session: AsyncSession, raw: str | None) -> bool:
    """Caddy's question before it asks for a certificate: is this a registered domain?"""
    try:
        host = normalize_host(raw)
    except DomainError:
        return False
    return await session.scalar(select(ShopDomain.id).where(ShopDomain.host == host)) is not None


# ---------------------------------------------------------------- the check


class NetProbe:
    """The real probe: resolves once and talks only to the address it resolved."""

    def __init__(self) -> None:
        self._address: dict[str, str] = {}

    async def resolve(self, host: str) -> list[str]:
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM), timeout=DNS_TIMEOUT
        )
        addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
        if addresses:
            self._address[host] = addresses[0]
        return addresses

    async def _open(self, host: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        address = self._address.get(host)
        if address is None:
            raise ProbeError("not resolved", unreachable=True)
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(address, 443, ssl=ssl.create_default_context(), server_hostname=host),
                timeout=CONNECT_TIMEOUT,
            )
        except ssl.SSLError as exc:
            raise ProbeError(str(exc.reason or exc)[:200]) from exc
        except (OSError, TimeoutError) as exc:
            raise ProbeError(str(exc)[:200] or "timeout", unreachable=True) from exc

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ssl.SSLError):
            pass

    @staticmethod
    def _decode_chunked(body: bytes) -> bytes:
        out = bytearray()
        pos = 0
        while pos < len(body):
            line_end = body.find(b"\r\n", pos)
            if line_end == -1:
                break
            size_part = body[pos:line_end].split(b";", 1)[0].strip()
            try:
                size = int(size_part, 16)
            except ValueError:
                return body
            pos = line_end + 2
            if size <= 0:
                break
            out.extend(body[pos : pos + size])
            pos += size + 2
        return bytes(out)

    async def certificate(self, host: str) -> CertInfo:
        _, writer = await self._open(host)
        try:
            cert = writer.get_extra_info("peercert") or {}
        finally:
            await self._close(writer)
        expires = None
        if cert.get("notAfter"):
            expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), tz=UTC)
        issuer = None
        for part in cert.get("issuer", ()):
            for key, value in part:
                if key == "organizationName" or (key == "commonName" and issuer is None):
                    issuer = str(value)[:128]
        return CertInfo(expires, issuer)

    async def fetch_mark(self, host: str) -> str:
        reader, writer = await self._open(host)
        try:
            writer.write(
                f"GET {MARK_PATH} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: PauruBot-domain-check\r\n"
                "Connection: close\r\n\r\n".encode()
            )
            await writer.drain()
            chunks = bytearray()
            async with asyncio.timeout(CONNECT_TIMEOUT):
                while len(chunks) < MAX_MARK_BYTES:
                    chunk = await reader.read(min(4096, MAX_MARK_BYTES - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
            raw = bytes(chunks)
        except (OSError, TimeoutError) as exc:
            raise ProbeError(str(exc)[:200] or "timeout", unreachable=True) from exc
        finally:
            await self._close(writer)
        head, sep, body = raw.partition(b"\r\n\r\n")
        if not sep or (not head.startswith(b"HTTP/1.1 200") and not head.startswith(b"HTTP/1.0 200")):
            raise ProbeError(head.split(b"\r\n", 1)[0].decode("latin-1")[:120] or "no answer")
        if b"transfer-encoding: chunked" in head.lower():
            body = self._decode_chunked(body)
        return body.decode("ascii", "replace").strip()


def default_probe() -> Probe:
    return NetProbe()


async def check(host: str, *, probe: Probe | None = None) -> CheckResult:
    probe = probe or default_probe()
    try:
        addresses = await probe.resolve(host)
    except (OSError, TimeoutError):
        return CheckResult(DNS, "no DNS record for this name")
    if not addresses:
        return CheckResult(DNS, "no DNS record for this name")
    for address in addresses:
        try:
            ssrf.classify(address, allow_private=False)
        except (ssrf.UnsafeUrl, ValueError):
            return CheckResult(DNS, f"points to {address}, a private or local address")
    try:
        cert = await probe.certificate(host)
    except ProbeError as exc:
        return CheckResult(UNREACHABLE if exc.unreachable else TLS, str(exc) or "no answer")
    try:
        mark = await probe.fetch_mark(host)
    except ProbeError as exc:
        return CheckResult(NOT_OURS, f"the name does not reach this server ({exc})")
    if not hmac.compare_digest(mark, marker(host)):
        return CheckResult(NOT_OURS, "the name reaches another server")
    return CheckResult(OK, None, cert.expires_at, cert.issuer)


def apply(row: ShopDomain, result: CheckResult) -> None:
    row.status = result.status
    row.error = result.error[:255] if result.error else None
    row.checked_at = utcnow()
    if result.status == OK:
        row.cert_expires_at = result.expires_at
        row.cert_issuer = result.issuer


async def check_row(session: AsyncSession, row: ShopDomain, *, probe: Probe | None = None) -> ShopDomain:
    apply(row, await check(row.host, probe=probe))
    await session.flush()
    return row


async def _notify_changes(
    session: AsyncSession, row: ShopDomain, previous_status: str, result: CheckResult
) -> None:
    """Alert staff when a domain carrying shop links breaks, recovers, or nears cert expiry."""
    if not row.for_links:
        return
    from paurubot import kv
    from paurubot.worker import queue

    if previous_status == OK and result.status in _FAILED_STATUSES:
        if await kv.set_once(f"domain:{row.id}:down", ALERT_COOLDOWN):
            await queue.enqueue(
                session,
                "notify",
                "domain_alert",
                {
                    "domain_id": row.id,
                    "host": row.host,
                    "event": "down",
                    "error": (result.error or result.status)[:200],
                },
                shop_id=row.shop_id,
            )
    elif previous_status in _FAILED_STATUSES and result.status == OK:
        await kv.forget_once(f"domain:{row.id}:down")
        await queue.enqueue(
            session,
            "notify",
            "domain_alert",
            {"domain_id": row.id, "host": row.host, "event": "up"},
            shop_id=row.shop_id,
        )
    if result.status == OK and row.cert_expires_at is not None:
        days_left = (row.cert_expires_at - utcnow()).days
        if days_left <= CERT_WARN_DAYS:
            if await kv.set_once(f"domain:{row.id}:cert_expiring", ALERT_COOLDOWN):
                await queue.enqueue(
                    session,
                    "notify",
                    "domain_alert",
                    {
                        "domain_id": row.id,
                        "host": row.host,
                        "event": "cert_expiring",
                        "days": max(0, days_left),
                    },
                    shop_id=row.shop_id,
                )
        else:
            await kv.forget_once(f"domain:{row.id}:cert_expiring")


async def run_checks(session: AsyncSession, *, probe: Probe | None = None) -> int:
    """Check every registered domain; commits. Returns how many were checked."""
    rows: Sequence[ShopDomain] = list(await session.scalars(select(ShopDomain).order_by(ShopDomain.id)))
    gate = asyncio.Semaphore(PARALLEL_CHECKS)

    async def one(host: str) -> CheckResult:
        async with gate:
            return await check(host, probe=probe or default_probe())

    results = await asyncio.gather(*(one(row.host) for row in rows))
    for row, result in zip(rows, results, strict=True):
        previous = row.status
        apply(row, result)
        await _notify_changes(session, row, previous, result)
    await session.commit()
    if rows:
        from paurubot.core import settings as shop_settings

        shop_settings.invalidate()
    return len(rows)
