"""Bind validated addresses to HTTP sockets, retaining the logical HTTP/TLS origin.

Only the inner transport sees a numeric URL. HTTPX's outer client retains the
original request for cookies, response URLs and subsequent authentication calls.
"""

from __future__ import annotations

import asyncio
import ssl
import time

import httpx

from paurubot.security import ssrf


class UnsafeRequest(httpx.RequestError):
    """A configured outbound destination violates address/origin policy."""


class ValidatedTransport(httpx.AsyncBaseTransport):
    def __init__(self, base_url: str, *, context: ssl.SSLContext, allow_private: bool) -> None:
        self.origin = httpx.URL(base_url)
        self.allow_private = allow_private
        # No environmental proxy may re-resolve the logical hostname elsewhere.
        self.inner = httpx.AsyncHTTPTransport(verify=context, trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (request.url.scheme, request.url.host, request.url.port) != (
            self.origin.scheme,
            self.origin.host,
            self.origin.port,
        ) or request.url.userinfo:
            raise UnsafeRequest("cross-origin outbound request refused", request=request)
        timeouts = dict(request.extensions.get("timeout", {}))
        budget = timeouts.get("connect")
        deadline = time.monotonic() + budget if budget is not None else None
        try:
            async with asyncio.timeout(budget):
                addresses = await ssrf.assert_safe_url(str(request.url), allow_private=self.allow_private)
        except ssrf.UnsafeUrl as exc:
            raise UnsafeRequest(f"blocked outbound address: {exc}", request=request) from exc
        except TimeoutError as exc:
            raise httpx.ConnectTimeout("outbound address resolution timed out", request=request) from exc
        # Policy validates the ENTIRE answer before the first connection attempt.
        for index, address in enumerate(addresses):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise httpx.ConnectTimeout("outbound connection budget exhausted", request=request)
            extensions = {
                **request.extensions,
                "sni_hostname": request.url.raw_host.decode("ascii"),
                "timeout": {**timeouts, "connect": remaining},
            }
            headers = httpx.Headers(request.headers)
            headers["Host"] = request.url.netloc.decode("ascii")
            connected = httpx.Request(
                method=request.method,
                url=request.url.copy_with(host=address),
                headers=headers,
                stream=request.stream,
                extensions=extensions,
            )
            try:
                return await self.inner.handle_async_request(connected)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # Only pre-HTTP connection failures are safe to try on another IP.
                # Never replay a body after a read/write/protocol/HTTP failure.
                if index == len(addresses) - 1:
                    raise
        raise httpx.ConnectError("outbound host resolved to no addresses", request=request)

    async def aclose(self) -> None:
        await self.inner.aclose()
