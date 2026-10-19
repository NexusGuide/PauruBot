"""Outbound HTTP for panel drivers: SSRF guard, verified or pinned TLS, strict timeouts,
no redirects, a small circuit breaker and redacted errors (SEC-10, NEW-02)."""

from __future__ import annotations

import logging
import ssl
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from paurubot.config import get_settings
from paurubot.panels.base import PanelError, PanelUnavailable
from paurubot.panels.transport import ValidatedTransport
from paurubot.security import ssrf
from paurubot.security.redact import redact

log = logging.getLogger(__name__)

FAILURE_THRESHOLD = 5
OPEN_SECONDS = 30.0


def ssl_context(pinned_pem: str | None) -> ssl.SSLContext:
    """Public CA verification, or trust in exactly one certificate the owner approved."""
    if not pinned_pem:
        return ssl.create_default_context()
    ctx = ssl.create_default_context(cadata=pinned_pem)
    # A pinned self-signed certificate is matched by its exact bytes, not by host name
    # (panels are often reached by IP); trust in it is narrower than any public CA.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class SafeHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        pinned_pem: str | None = None,
        allow_private: bool = False,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        ssrf.check_url_shape(base_url)
        self.base_url = base_url.rstrip("/")
        self._host = urlsplit(self.base_url).hostname
        self.allow_private = allow_private or settings.allow_private_panel_hosts
        total = timeout or settings.http_total_timeout
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(total, connect=settings.http_connect_timeout),
            follow_redirects=False,
            trust_env=False,
            transport=transport
            if transport is not None
            else ValidatedTransport(
                self.base_url, context=ssl_context(pinned_pem), allow_private=self.allow_private
            ),
            headers={"User-Agent": "PauruBot"},
        )
        self._failures = 0
        self._open_until = 0.0

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if time.monotonic() < self._open_until:
            raise PanelUnavailable("panel temporarily unavailable (circuit open)")
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            self._record_failure()
            raise PanelUnavailable(f"connection failed: {exc.__class__.__name__}") from exc
        if response.status_code >= 500:
            self._record_failure()
        else:
            self._failures = 0
        if response.is_redirect:
            location = response.headers.get("location", "")
            target = urlsplit(location).hostname
            if target and target != self._host:
                raise PanelError("cross-host redirect refused", response.status_code)
        return response

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= FAILURE_THRESHOLD:
            self._open_until = time.monotonic() + OPEN_SECONDS
            self._failures = 0
            log.warning("circuit opened for %s", redact(self.base_url))

    async def aclose(self) -> None:
        await self._client.aclose()


async def fetch_certificate(url: str, *, allow_private: bool = False) -> tuple[str, str]:
    """Read a server's leaf certificate without trusting it, for the owner to approve (TOFU).

    Returns (PEM, SHA-256 fingerprint). Nothing is trusted until the owner confirms the fingerprint.
    """
    import asyncio
    import hashlib

    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise PanelError("certificate pinning needs an https URL")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # inspection only; the result is never used to send data
    port = parts.port or 443
    try:
        # One bounded operation: resolve/validate once, then dial only numeric results.
        async with asyncio.timeout(8):
            addresses = await ssrf.assert_safe_url(
                url, allow_private=allow_private or get_settings().allow_private_panel_hosts
            )
            writer = None
            for index, address in enumerate(addresses):
                try:
                    _, writer = await asyncio.open_connection(
                        address, port, ssl=ctx, server_hostname=parts.hostname
                    )
                    break
                except OSError:
                    if index == len(addresses) - 1:
                        raise
            if writer is None:
                raise PanelUnavailable("panel resolved to no addresses")
            try:
                ssl_object = writer.get_extra_info("ssl_object")
                der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, ssl.SSLError):
                    pass
    except (OSError, TimeoutError) as exc:
        raise PanelUnavailable(f"cannot connect to {parts.hostname}:{port}") from exc
    if not der:
        raise PanelError("server presented no certificate")
    fingerprint = ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
    return ssl.DER_cert_to_PEM_cert(der), fingerprint
