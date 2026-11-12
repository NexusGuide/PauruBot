"""Forged Mini App sign-ins are throttled before the reseller bots' tokens are tried (audit SEC-02).

initData that the main bot did not sign is tried against every reseller bot's token, one HMAC and one
decryption each. Without a limit before that, junk requests cost work that grows with the number of
bots. An address that keeps sending bad initData now gets 429 and the scan is skipped.
"""

from __future__ import annotations

import httpx
import pytest

from paurubot.api import webapp_auth
from paurubot.api.app import create_app

pytestmark = pytest.mark.security


async def test_an_address_sending_bad_init_data_is_stopped_before_the_scan(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    scans = 0
    real = webapp_auth.match_init_data

    def counting(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal scans
        scans += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(webapp_auth, "match_init_data", counting)
    junk = {webapp_auth.HEADER: "user=%7B%22id%22%3A1%7D&auth_date=1&hash=" + "0" * 64}
    transport = httpx.ASGITransport(app=create_app(), client=("203.0.113.9", 4444))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        codes = [
            (await client.get("/api/webapp/me", headers=junk)).status_code
            for _ in range(webapp_auth.BAD_INIT_LIMIT + 5)
        ]
    assert codes[: webapp_auth.BAD_INIT_LIMIT] == [401] * webapp_auth.BAD_INIT_LIMIT
    assert set(codes[webapp_auth.BAD_INIT_LIMIT :]) == {429}
    assert scans == webapp_auth.BAD_INIT_LIMIT, "no reseller-token scan once the address is throttled"

    other = httpx.ASGITransport(app=create_app(), client=("198.51.100.20", 4444))
    async with httpx.AsyncClient(transport=other, base_url="http://testserver") as client:
        assert (await client.get("/api/webapp/me", headers=junk)).status_code == 401, (
            "other addresses unaffected"
        )
