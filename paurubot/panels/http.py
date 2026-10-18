"""Outbound HTTP for panel drivers: SSRF guard, verified or pinned TLS, strict timeouts,
no redirects, a small circuit breaker and redacted errors (SEC-10, NEW-02)."""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from paurubot.config import get_settings
from paurubot.panels.base import PanelError, PanelUnavailable
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


class ResolvedAddressTransport(httpx.AsyncHTTPTransport):
    """Keep HTTP identity at the original origin; connect only to the checked DNS answer.

    Each client has one origin, so pooled IP connections cannot be shared with another
    TLS/HTTP identity. Only connect failures may try another IP: never replay a request
    after a read/write error, when the remote side effect is uncertain.
    """

    def __init__(self, base_url: str, *, allow_private: bool, context: ssl.SSLContext) -> None:
        super().__init__(verify=context, trust_env=False)
        self._origin = self._origin_of(httpx.URL(base_url))
        self._allow_private = allow_private

    @staticmethod
    def _origin_of(url: httpx.URL) -> tuple[str, bytes, int]:
        return url.scheme, url.raw_host, url.port or (443 if url.scheme == "https" else 80)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._origin_of(request.url) != self._origin:
            raise PanelError("cross-origin panel request refused")
        connect_timeout = request.extensions.get("timeout", {}).get("connect")
        deadline = time.monotonic() + connect_timeout if connect_timeout is not None else None
        try:
            async with asyncio.timeout(connect_timeout):
                addresses = await ssrf.assert_safe_url(str(request.url), allow_private=self._allow_private)
        except ssrf.UnsafeUrl as exc:
            raise PanelError(f"blocked panel address: {exc}") from exc
        except TimeoutError as exc:
            raise httpx.ConnectTimeout("panel address resolution timed out", request=request) from exc

        for index, address in enumerate(addresses):
            extensions = dict(request.extensions)
            extensions["sni_hostname"] = request.url.raw_host.decode("ascii")
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise httpx.ConnectTimeout("panel connection timed out", request=request)
                extensions["timeout"] = {**extensions.get("timeout", {}), "connect": remaining}
            headers = request.headers.copy()
            headers["Host"] = request.url.netloc.decode("ascii")
            pinned = httpx.Request(
                request.method,
                request.url.copy_with(host=address),
                headers=headers,
                stream=request.stream,
                extensions=extensions,
            )
            try:
                return await super().handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if index == len(addresses) - 1:
                    raise
        raise PanelError("panel resolved to no addresses")


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
        context = ssl_context(pinned_pem)
        guarded_transport = (
            transport
            if transport is not None
            else ResolvedAddressTransport(self.base_url, allow_private=self.allow_private, context=context)
        )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(total, connect=settings.http_connect_timeout),
            verify=context,
            follow_redirects=False,
            transport=guarded_transport,
            trust_env=False,
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
        kwargs["follow_redirects"] = False
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
    import hashlib

    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise PanelError("certificate pinning needs an https URL")
    addresses = await ssrf.assert_safe_url(
        url, allow_private=allow_private or get_settings().allow_private_panel_hosts
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # inspection only; the result is never used to send data
    port = parts.port or 443
    try:
        async with asyncio.timeout(8):
            for index, address in enumerate(addresses):
                try:
                    _, writer = await asyncio.open_connection(
                        address, port, ssl=ctx, server_hostname=parts.hostname
                    )
                    break
                except OSError:
                    if index == len(addresses) - 1:
                        raise
    except (OSError, TimeoutError) as exc:
        raise PanelUnavailable(f"cannot connect to {parts.hostname}:{port}") from exc
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    finally:
        writer.close()
        try:
            async with asyncio.timeout(1):
                await writer.wait_closed()
        except (OSError, TimeoutError):
            pass
    if not der:
        raise PanelError("server presented no certificate")
    fingerprint = ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
    return ssl.DER_cert_to_PEM_cert(der), fingerprint
