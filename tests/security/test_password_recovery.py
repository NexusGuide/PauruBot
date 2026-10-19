"""Independent recovery, not proof by an ambiguous legacy password suffix."""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock

import bcrypt
import httpx
import pytest

from paurubot import kv
from paurubot.api.app import create_app
from paurubot.auth import password_reset, sessions
from paurubot.auth import staff as staff_service
from paurubot.db.models import StaffRole
from paurubot.notifications import telegram
from paurubot.security.tokens import verify_password
from tests import factories

pytestmark = pytest.mark.security


@pytest.fixture
async def recovery_env(session, monkeypatch):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    staff = await staff_service.create_staff(
        session,
        shop.id,
        role=StaffRole.OWNER,
        username="recover-owner",
        password=None,
        telegram_id=88201,
    )
    staff.password_hash = bcrypt.hashpw(b"a" * 72, bcrypt.gensalt(rounds=4)).decode()
    await session.commit()
    delivery = AsyncMock()
    monkeypatch.setattr(telegram, "send_text", delivery)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        yield {"staff": staff, "shop": shop, "client": client, "delivery": delivery}


async def issued(env):  # type: ignore[no-untyped-def]
    response = await env["client"].post(
        "/api/auth/password-reset/request", json={"username": "recover-owner"}
    )
    assert response.status_code == 200 and response.json() == {"ok": True}
    destination, message = env["delivery"].call_args.args
    assert destination == 88201
    assert "Password reset" in message and "بازنشانی" in message
    match = re.search(r"<code>([A-Za-z0-9_-]{43})</code>", message)
    assert match is not None
    return match.group(1)


async def submit(env, token, password="ش" * 40 + "new-secret"):  # type: ignore[no-untyped-def]
    return await env["client"].post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": password},
    )


async def test_reset_replaces_all_bytes_revokes_sessions_and_api_keys(session, recovery_env):  # type: ignore[no-untyped-def]
    env, staff = recovery_env, recovery_env["staff"]
    sid, _ = await sessions.create(staff.id, staff.shop_id, staff.session_version, "127.0.0.1")
    _, api_key = await staff_service.create_api_key(session, staff, "old", read_only=False, expires_at=None)
    await session.commit()
    token = await issued(env)
    raw = await kv.get_redis().get(password_reset._key(token))
    assert raw and token not in (raw.decode() if isinstance(raw, bytes) else raw)
    response = await submit(env, token)
    assert response.status_code == 200, response.text
    assert "set-cookie" not in response.headers  # recovery is not automatic login
    await session.refresh(staff)
    assert staff.password_hash.startswith("$argon2id$") and staff.session_version == 2
    assert verify_password("ش" * 40 + "new-secret", staff.password_hash)
    assert not verify_password("ش" * 40 + "wrong-tail", staff.password_hash)
    assert await sessions.load(sid) is None
    assert await staff_service.resolve_api_key(session, api_key) is None
    assert (await submit(env, token)).status_code == 400
    assert (
        await env["client"].post(
            "/api/auth/login",
            json={"username": staff.username, "password": "ش" * 40 + "new-secret"},
        )
    ).status_code == 200


@pytest.mark.parametrize("condition", ["disabled", "telegram", "shop", "version", "password", "expired"])
async def test_state_changes_or_expiry_invalidate_token(session, recovery_env, condition):  # type: ignore[no-untyped-def]
    env, staff = recovery_env, recovery_env["staff"]
    token = await issued(env)
    original = staff.password_hash
    if condition == "disabled":
        staff.is_active = False
    elif condition == "telegram":
        staff.telegram_id = 88202
    elif condition == "shop":
        staff.shop_id = (await factories.shop(session)).id
    elif condition == "version":
        staff.session_version += 1
    elif condition == "password":
        staff.password_hash = bcrypt.hashpw(b"replacement", bcrypt.gensalt(rounds=4)).decode()
    else:
        await kv.get_redis().delete(password_reset._key(token))
    await session.commit()
    response = await submit(env, token)
    assert response.status_code == 400 and response.json()["error"] == "invalid_reset"
    await session.refresh(staff)
    assert staff.password_hash == original or condition == "password"


async def test_unknown_disabled_and_unlinked_return_same_response_without_delivery(session, recovery_env):  # type: ignore[no-untyped-def]
    env = recovery_env
    for name, state in (
        ("unknown-name", None),
        ("recover-owner", "disabled"),
        ("recover-owner", "unlinked"),
    ):
        if state:
            env["staff"].is_active = state != "disabled"
            env["staff"].telegram_id = None if state == "unlinked" else 88201
            await session.commit()
        response = await env["client"].post("/api/auth/password-reset/request", json={"username": name})
        assert response.status_code == 200 and response.json() == {"ok": True}
    env["delivery"].assert_not_awaited()


async def test_wrong_token_and_short_password_do_not_consume_valid_token(recovery_env):  # type: ignore[no-untyped-def]
    token = await issued(recovery_env)
    assert (await submit(recovery_env, "x" * 43)).status_code == 400
    assert (await submit(recovery_env, token, "short")).status_code == 422
    assert (await submit(recovery_env, token)).status_code == 200


async def test_rate_limits_request_and_verify(recovery_env):  # type: ignore[no-untyped-def]
    env = recovery_env
    for _ in range(3):
        await issued(env)
    limited = await env["client"].post("/api/auth/password-reset/request", json={"username": "recover-owner"})
    assert limited.status_code == 429 and limited.headers["Retry-After"]
    for _ in range(10):
        assert (await submit(env, "x" * 43)).status_code == 400
    assert (await submit(env, "x" * 43)).status_code == 429


async def test_redis_atomic_consumption_allows_exactly_one_winner():
    redis = kv.get_redis()
    key = password_reset._key("concurrent-test-token")
    await redis.set(key, "binding", ex=password_reset.TTL)
    results = await asyncio.gather(
        *[redis.eval(password_reset.CONSUME, 1, key, "binding") for _ in range(20)]
    )
    assert sum(int(result) for result in results) == 1


async def test_failed_telegram_delivery_removes_challenge(recovery_env):  # type: ignore[no-untyped-def]
    env = recovery_env
    env["delivery"].side_effect = telegram.Undeliverable("not reachable")
    result = await env["client"].post("/api/auth/password-reset/request", json={"username": "recover-owner"})
    assert result.status_code == 200
    keys = await kv.get_redis().keys(password_reset.PREFIX + "*")
    assert not [key for key in keys if b":send:" not in (key.encode() if isinstance(key, str) else key)]


async def test_legacy_cookie_cannot_rotate_secret_with_a_suffix_collision(session, recovery_env):  # type: ignore[no-untyped-def]
    env = recovery_env
    response = await env["client"].post(
        "/api/auth/login",
        json={"username": "recover-owner", "password": "a" * 72 + "attacker-tail"},
    )
    assert response.status_code == 200
    result = await env["client"].post(
        "/api/auth/password",
        headers={"x-csrf-token": response.json()["csrf_token"]},
        json={
            "current_password": "a" * 72 + "another-tail",
            "new_password": "attacker-replacement",
        },
    )
    assert result.status_code == 409 and result.json()["error"] == "legacy_password_reset_required"
    await session.refresh(env["staff"])
    assert env["staff"].password_hash.startswith("$2b$")


async def test_requester_cannot_supply_destination_or_target_account(recovery_env):  # type: ignore[no-untyped-def]
    client = recovery_env["client"]
    response = await client.post(
        "/api/auth/password-reset/request",
        json={"username": "recover-owner", "telegram_id": 99999},
    )
    assert response.status_code == 422
    token = await issued(recovery_env)
    response = await client.post(
        "/api/auth/password-reset/confirm",
        json={
            "token": token,
            "new_password": "replacement-password",
            "staff_id": 99999,
        },
    )
    assert response.status_code == 422
    assert (await submit(recovery_env, token)).status_code == 200


async def test_silent_delivery_skip_also_removes_token(recovery_env):  # type: ignore[no-untyped-def]
    recovery_env["delivery"].return_value = None
    result = await recovery_env["client"].post(
        "/api/auth/password-reset/request", json={"username": "recover-owner"}
    )
    assert result.status_code == 200
    keys = await kv.get_redis().keys(password_reset.PREFIX + "*")
    assert all(b":send:" in (key.encode() if isinstance(key, str) else key) for key in keys)


@pytest.mark.parametrize("raw", ["not-json", "[]", '{"staff_id":true}', '{"staff_id":null}'])
async def test_corrupt_stored_token_fails_closed(recovery_env, raw):  # type: ignore[no-untyped-def]
    token = "z" * 43
    await kv.get_redis().set(password_reset._key(token), raw, ex=password_reset.TTL)
    assert (await submit(recovery_env, token)).status_code == 400
