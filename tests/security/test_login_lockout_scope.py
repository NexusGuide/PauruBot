"""Failed sign-ins lock the address that made them, not the account (audit SEC-01).

A lock keyed only on the username let anyone who knew it keep the owner out of password login, the
Telegram code and the password reset by failing five times every fifteen minutes. The attacker's
address is locked now; the owner signs in from their own. Many addresses guessing one username still
lock it, at a much higher count.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from paurubot.api.app import create_app
from paurubot.config import get_settings
from paurubot.security import login_guard
from tests.security.test_api_auth import BASE, _setup

pytestmark = pytest.mark.security

GOOD = "a-very-strong-pass"


def _client(ip: str) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(), client=(ip, 5555))
    return httpx.AsyncClient(transport=transport, base_url=BASE)


@pytest.fixture
async def attacker(session) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    async with _client("203.0.113.66") as http:
        yield http


@pytest.fixture
async def owner(session) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    async with _client("198.51.100.7") as http:
        yield http


async def _fail(client: httpx.AsyncClient, times: int) -> httpx.Response:
    resp = httpx.Response(200)
    for _ in range(times):
        resp = await client.post("/api/auth/login", json={"username": "owner", "password": "wrong-password"})
    return resp


async def test_an_attacker_locks_only_their_own_address(
    attacker: httpx.AsyncClient, owner: httpx.AsyncClient
) -> None:
    await _setup(owner)
    await _fail(attacker, 5)
    locked = await attacker.post("/api/auth/login", json={"username": "owner", "password": GOOD})
    assert locked.status_code == 429, "the guessing address stays locked"

    resp = await owner.post("/api/auth/login", json={"username": "owner", "password": GOOD})
    assert resp.status_code == 200, resp.text


async def test_the_owner_can_still_ask_for_a_code_and_a_reset(
    attacker: httpx.AsyncClient, owner: httpx.AsyncClient
) -> None:
    await _setup(owner)
    await _fail(attacker, 5)
    for _ in range(3):  # the attacker also uses up their own code and reset requests
        await attacker.post("/api/auth/code", json={"username": "owner"})
        await attacker.post("/api/auth/password-reset/request", json={"username": "owner"})

    code = await owner.post("/api/auth/code", json={"username": "owner"})
    assert code.status_code == 200, code.text
    reset = await owner.post("/api/auth/password-reset/request", json={"username": "owner"})
    assert reset.status_code == 200, reset.text


async def test_guessing_one_username_from_many_addresses_still_locks_it(session) -> None:  # type: ignore[no-untyped-def]
    limit = get_settings().login_max_attempts
    async with _client("192.0.2.10") as first:
        await _setup(first)
    addresses = -(-limit * login_guard.ACCOUNT_FACTOR // (limit - 1))  # enough to reach the account's budget
    for n in range(addresses):  # each address stops before its own lock
        async with _client(f"192.0.2.{20 + n}") as guesser:
            await _fail(guesser, limit - 1)
    async with _client("192.0.2.99") as fresh:
        resp = await fresh.post("/api/auth/login", json={"username": "owner", "password": GOOD})
    assert resp.status_code == 429, "a distributed guess at one account is still stopped"
