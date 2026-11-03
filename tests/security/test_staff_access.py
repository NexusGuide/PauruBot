"""Bot-delivered login codes (SEC-02), personal API keys (SEC-03) and the metrics endpoint (JOB-05)."""

from __future__ import annotations

import re

import httpx
import pytest

from paurubot.api.app import create_app
from paurubot.auth.staff import create_staff
from paurubot.db.models import StaffRole
from paurubot.notifications import telegram as notify_telegram
from tests import factories
from tests.fakes.telegram import make_bot

pytestmark = pytest.mark.security
PASSWORD = "a-very-strong-pass"


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    owner = await create_staff(
        session, shop.id, role=StaffRole.OWNER, username="code_owner", password=PASSWORD
    )
    owner.telegram_id = 555_000_111
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    transport = httpx.ASGITransport(app=create_app(), client=("10.2.3.4", 4000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield {"client": client, "fake": fake, "owner": owner, "shop": shop}
    notify_telegram.set_bot(None)


async def test_login_with_a_code_from_the_bot(env) -> None:  # type: ignore[no-untyped-def]
    client, fake = env["client"], env["fake"]
    assert (await client.post("/api/auth/code", json={"username": "nobody"})).json() == {"ok": True}
    assert fake.texts(555_000_111) == []
    assert (await client.post("/api/auth/code", json={"username": "code_owner"})).status_code == 200
    message = fake.texts(555_000_111)[-1]
    code = re.search(r"(\d{6})", message).group(1)  # type: ignore[union-attr]
    wrong = "000000" if code != "000000" else "111111"
    bad = await client.post("/api/auth/code/verify", json={"username": "code_owner", "code": wrong})
    assert bad.status_code == 401 and bad.json()["error"] == "invalid_code"
    ok = await client.post("/api/auth/code/verify", json={"username": "code_owner", "code": code})
    assert ok.status_code == 200 and ok.json()["role"] == "owner"
    assert (await client.get("/api/auth/me")).status_code == 200
    reuse = await client.post("/api/auth/code/verify", json={"username": "code_owner", "code": code})
    assert reuse.status_code == 401  # codes work once


async def test_too_many_wrong_codes_burn_the_code(env) -> None:  # type: ignore[no-untyped-def]
    client, fake = env["client"], env["fake"]
    await client.post("/api/auth/code", json={"username": "code_owner"})
    code = re.search(r"(\d{6})", fake.texts(555_000_111)[-1]).group(1)  # type: ignore[union-attr]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        await client.post("/api/auth/code/verify", json={"username": "code_owner", "code": wrong})
    late = await client.post("/api/auth/code/verify", json={"username": "code_owner", "code": code})
    assert late.status_code in {401, 429}


async def test_api_keys(env) -> None:  # type: ignore[no-untyped-def]
    client = env["client"]
    login = await client.post("/api/auth/login", json={"username": "code_owner", "password": PASSWORD})
    csrf = login.json()["csrf_token"]
    made = await client.post(
        "/api/auth/api-keys", json={"name": "grafana", "read_only": True}, headers={"x-csrf-token": csrf}
    )
    assert made.status_code == 200
    key = made.json()["key"]
    assert key.startswith("pbk_")
    listed = (await client.get("/api/auth/api-keys")).json()["items"]
    assert listed[0]["name"] == "grafana" and "key" not in listed[0]

    async with httpx.AsyncClient(transport=client._transport, base_url="http://testserver") as bare:
        bearer = {"authorization": f"Bearer {key}"}
        assert (await bare.get("/api/admin/dashboard", headers=bearer)).status_code == 200
        write = await bare.post("/api/admin/locations", json={"title": "DE"}, headers=bearer)
        assert write.status_code == 403 and write.json()["error"] == "read_only_key"
        assert (await bare.get("/api/admin/staff", headers=bearer)).status_code == 403  # never staff.manage
        assert (await bare.get("/api/auth/api-keys", headers=bearer)).status_code == 403
        bad = {"authorization": "Bearer pbk_" + "0" * 12 + "_nope"}
        assert (await bare.get("/api/admin/dashboard", headers=bad)).status_code == 401

        revoked = await client.delete(f"/api/auth/api-keys/{listed[0]['id']}", headers={"x-csrf-token": csrf})
        assert revoked.json()["item"]["revoked_at"]
        assert (await bare.get("/api/admin/dashboard", headers=bearer)).status_code == 401


async def test_a_password_change_also_revokes_api_keys(env) -> None:  # type: ignore[no-untyped-def]
    """Changing a password to lock someone out must not leave their API key working."""
    client = env["client"]
    login = await client.post("/api/auth/login", json={"username": "code_owner", "password": PASSWORD})
    csrf = login.json()["csrf_token"]
    made = await client.post(
        "/api/auth/api-keys", json={"name": "left-behind", "read_only": True}, headers={"x-csrf-token": csrf}
    )
    bearer = {"authorization": f"Bearer {made.json()['key']}"}
    async with httpx.AsyncClient(transport=client._transport, base_url="http://testserver") as bare:
        assert (await bare.get("/api/admin/dashboard", headers=bearer)).status_code == 200
        changed = await client.post(
            "/api/auth/password",
            json={"current_password": PASSWORD, "new_password": "another-strong-pass"},
            headers={"x-csrf-token": csrf},
        )
        assert changed.status_code == 200
        assert (await bare.get("/api/admin/dashboard", headers=bearer)).status_code == 401
    # The changer stays signed in with the fresh session and can make a new key.
    fresh = changed.json()["csrf_token"]
    again = await client.post(
        "/api/auth/api-keys", json={"name": "new", "read_only": True}, headers={"x-csrf-token": fresh}
    )
    assert again.status_code == 200


async def test_metrics_endpoint_is_private(env) -> None:  # type: ignore[no-untyped-def]
    resp = await env["client"].get("/metrics")
    assert resp.status_code == 200
    assert "# TYPE paurubot_jobs gauge" in resp.text and "paurubot_orders_completed_24h" in resp.text
    public = httpx.ASGITransport(app=create_app(), client=("203.0.113.9", 4000))
    async with httpx.AsyncClient(transport=public, base_url="http://testserver") as outside:
        assert (await outside.get("/metrics")).status_code == 403
    # Behind Caddy with TRUST_PROXY_HOPS=0 the peer is the proxy's private address: still refused.
    relayed = await env["client"].get("/metrics", headers={"x-forwarded-for": "203.0.113.9"})
    assert relayed.status_code == 403
