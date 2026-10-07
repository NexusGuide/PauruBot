from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from paurubot.api.app import create_app
from paurubot.core.setup import issue_token

pytestmark = pytest.mark.security

BASE = "http://testserver"


@pytest.fixture
async def client(session) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    transport = httpx.ASGITransport(app=create_app(), client=("10.1.2.3", 5555))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
        yield http


async def _setup(client: httpx.AsyncClient, password: str = "a-very-strong-pass") -> None:
    token = await issue_token()
    resp = await client.post(
        "/api/setup/complete",
        json={
            "token": token,
            "shop_title": "Shop",
            "username": "owner",
            "password": password,
        },
    )
    assert resp.status_code == 200, resp.text


async def _login(client: httpx.AsyncClient, password: str = "a-very-strong-pass") -> str:
    resp = await client.post("/api/auth/login", json={"username": "owner", "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["csrf_token"]


async def test_setup_requires_valid_token_and_runs_once(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/setup/status")).json() == {"setup_required": True}
    bad = await client.post(
        "/api/setup/complete",
        json={"token": "x" * 20, "shop_title": "S", "username": "owner", "password": "a-very-strong-pass"},
    )
    assert bad.status_code == 403
    await _setup(client)
    assert (await client.get("/api/setup/status")).json() == {"setup_required": False}
    token = await issue_token()
    again = await client.post(
        "/api/setup/complete",
        json={"token": token, "shop_title": "S2", "username": "intruder", "password": "a-very-strong-pass"},
    )
    assert again.status_code == 409


async def test_login_sets_strict_httponly_session(client: httpx.AsyncClient) -> None:
    await _setup(client)
    resp = await client.post("/api/auth/login", json={"username": "owner", "password": "a-very-strong-pass"})
    cookie = resp.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookie if c.startswith("pb_session="))
    assert "HttpOnly" in session_cookie and "SameSite=strict" in session_cookie
    me = await client.get("/api/auth/me")
    assert me.json()["role"] == "owner"
    assert "panels.secrets" in me.json()["permissions"]


async def test_unauthenticated_and_csrf(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/auth/me")).status_code == 401
    await _setup(client)
    csrf = await _login(client)
    missing = await client.post(
        "/api/auth/password", json={"current_password": "x", "new_password": "y" * 12}
    )
    assert missing.status_code == 403 and missing.json()["error"] == "csrf_failed"
    wrong = await client.post(
        "/api/auth/password",
        headers={"x-csrf-token": "nope"},
        json={"current_password": "x", "new_password": "y" * 12},
    )
    assert wrong.status_code == 403
    ok = await client.post(
        "/api/auth/password",
        headers={"x-csrf-token": csrf},
        json={"current_password": "x", "new_password": "y" * 12},
    )
    assert ok.status_code == 400  # wrong current password, but CSRF passed


async def test_cross_origin_post_rejected(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/auth/login",
        headers={"origin": "https://evil.example"},
        json={"username": "owner", "password": "whatever-pass"},
    )
    assert resp.status_code == 403 and resp.json()["error"] == "bad_origin"


async def test_password_change_revokes_other_sessions(client: httpx.AsyncClient, session) -> None:  # type: ignore[no-untyped-def]
    await _setup(client)
    transport = client._transport  # same app
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as other:
        await _login(other)
        assert (await other.get("/api/auth/me")).status_code == 200
        csrf = await _login(client)
        resp = await client.post(
            "/api/auth/password",
            headers={"x-csrf-token": csrf},
            json={"current_password": "a-very-strong-pass", "new_password": "another-strong-pass"},
        )
        assert resp.status_code == 200
        assert (await other.get("/api/auth/me")).status_code == 401
        assert (await client.get("/api/auth/me")).status_code == 200


async def test_login_lockout(client: httpx.AsyncClient) -> None:
    await _setup(client)
    for _ in range(5):
        resp = await client.post("/api/auth/login", json={"username": "owner", "password": "wrong-password"})
        assert resp.status_code == 401
    locked = await client.post(
        "/api/auth/login", json={"username": "owner", "password": "a-very-strong-pass"}
    )
    assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0


async def test_security_headers(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    csp = resp.headers["content-security-policy"]
    assert "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "x-request-id" in resp.headers
    app_csp = (await client.get("/app/")).headers["content-security-policy"]
    assert "web.telegram.org" in app_csp


def test_client_ip_uses_trusted_hops_not_leftmost() -> None:
    from paurubot.security.login_guard import client_ip

    xff = "6.6.6.6, 203.0.113.9"
    assert client_ip("172.18.0.5", xff, trusted_hops=1) == "203.0.113.9"
    assert client_ip("172.18.0.5", xff, trusted_hops=0) == "172.18.0.5"
    assert client_ip("172.18.0.5", "garbage", trusted_hops=1) == "172.18.0.5"


async def test_frontend_redirects_stay_on_this_site(client: httpx.AsyncClient) -> None:
    for path in ("/app", "/admin"):
        for query in ("", "?name=/evil.example", "?name=//evil.example"):
            resp = await client.get(f"{path}{query}", follow_redirects=False)
            assert resp.status_code in (302, 307)
            assert resp.headers["location"] == f"{path}/"
