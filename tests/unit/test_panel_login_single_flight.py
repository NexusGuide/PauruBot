"""F-05: concurrent calls share one panel login, and a 401 storm re-logs in once."""

from __future__ import annotations

import asyncio

import httpx

from paurubot.panels.drivers.marzban import MarzbanDriver
from paurubot.panels.http import SafeHttpClient


def _driver(transport: httpx.MockTransport) -> MarzbanDriver:
    http = SafeHttpClient("https://panel.example.com", transport=transport)
    return MarzbanDriver(http, "admin", "secret-password")


async def test_concurrent_calls_log_in_once() -> None:
    logins = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal logins
        if request.url.path == "/api/admin/token":
            logins += 1
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"access_token": "t1"})
        return httpx.Response(200, json={"version": "1", "total_user": 3})

    driver = _driver(httpx.MockTransport(handle))
    await asyncio.gather(*(driver.call("GET", "/api/system") for _ in range(8)))
    assert logins == 1
    await driver.close()


async def test_expired_token_is_refreshed_once_for_many_callers() -> None:
    logins = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal logins
        if request.url.path == "/api/admin/token":
            logins += 1
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"access_token": f"t{logins + 1}"})  # t1 has expired
        if request.headers.get("authorization") == "Bearer t1":
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, json={"version": "1"})

    driver = _driver(httpx.MockTransport(handle))
    driver._token = "t1"
    await asyncio.gather(*(driver.call("GET", "/api/system") for _ in range(6)))
    assert logins == 1
    assert driver._token == "t2"
    await driver.close()
