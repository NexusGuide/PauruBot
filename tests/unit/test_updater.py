"""Updates and rollbacks requested from the admin panel (OPS-07)."""

from __future__ import annotations

import json
import re
import time

import httpx
import pytest

from paurubot import kv
from paurubot.api.app import create_app
from paurubot.auth.staff import create_staff
from paurubot.db.models import StaffRole
from paurubot.ops import updater
from tests import factories

PASSWORD = "a-very-strong-pass"


async def _agent(previous: str | None = "v1.0.0", state: str | None = "done") -> None:
    redis = kv.get_redis()
    await redis.set("updater:heartbeat", str(int(time.time())))
    await redis.set("updater:version", "v1.1.0")
    await redis.set("updater:channel", "stable")
    await redis.set("updater:latest", "v1.2.0")
    if previous:
        await redis.set("updater:previous", previous)
    if state:
        await redis.set("updater:state", state)


def _shell_field(name: str, payload: str) -> str:
    """Same pattern the installer's agent uses to read a request."""
    match = re.search(rf'"{name}": *"([^"]*)"', payload)
    return match.group(1) if match else ""


async def test_status_without_an_agent() -> None:
    current = await updater.status()
    assert current.agent_online is False and current.update_available is False
    with pytest.raises(updater.UpdateError, match="agent_offline"):
        await updater.request("update")


async def test_request_is_readable_by_the_agent_and_only_one_waits() -> None:
    await _agent()
    current = await updater.status()
    assert current.agent_online and current.update_available and current.installed == "v1.1.0"
    await updater.request("update", "beta", "v1.2.0-rc.1")
    raw = await kv.get_redis().get(updater.REQUEST_KEY)
    assert raw is not None
    assert [_shell_field(k, raw) for k in ("action", "channel", "version")] == [
        "update",
        "beta",
        "v1.2.0-rc.1",
    ]
    assert json.loads(raw)["action"] == "update"
    with pytest.raises(updater.UpdateError, match="update_pending"):
        await updater.request("rollback")
    assert (await updater.status()).pending == {
        "action": "update",
        "channel": "beta",
        "version": "v1.2.0-rc.1",
    }
    assert await updater.cancel() is True


@pytest.mark.parametrize(
    ("action", "channel", "version", "error"),
    [
        ("reboot", None, None, "bad_action"),
        ("update", "nightly", None, "bad_channel"),
        ("update", None, "main; rm -rf /", "bad_version"),
        ("update", None, '1.0.0" "x', "bad_version"),
    ],
)
def test_bad_requests_never_reach_the_agent(action: str, channel: str, version: str, error: str) -> None:
    with pytest.raises(updater.UpdateError, match=error):
        updater.build_request(action, channel, version)


async def test_rollback_needs_a_previous_version_and_waits_while_running() -> None:
    await _agent(previous=None)
    with pytest.raises(updater.UpdateError, match="nothing_to_roll_back"):
        await updater.request("rollback")
    await _agent(state="running")
    with pytest.raises(updater.UpdateError, match="update_running"):
        await updater.request("update")


def test_version_comparison() -> None:
    assert updater._newer("v1.10.0", "v1.9.3")
    assert not updater._newer("v1.2.0", "v1.2.0")
    assert not updater._newer("main", "v1.0.0")
    assert not updater._newer(None, "v1.0.0")


async def test_admin_endpoints_are_owner_only(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await create_staff(session, shop.id, role=StaffRole.OWNER, username="up_owner", password=PASSWORD)
    await create_staff(session, shop.id, role=StaffRole.ADMIN, username="up_admin", password=PASSWORD)
    await session.commit()
    await _agent()
    transport = httpx.ASGITransport(app=create_app(), client=("10.2.3.4", 4000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post("/api/auth/login", json={"username": "up_admin", "password": PASSWORD})
        csrf = {"x-csrf-token": login.json()["csrf_token"]}
        assert (await client.get("/api/admin/updates")).status_code == 403
        assert (
            await client.post("/api/admin/updates", json={"action": "update"}, headers=csrf)
        ).status_code == 403

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post("/api/auth/login", json={"username": "up_owner", "password": PASSWORD})
        csrf = {"x-csrf-token": login.json()["csrf_token"]}
        got = (await client.get("/api/admin/updates")).json()
        assert got["agent_online"] and got["latest"] == "v1.2.0"
        bad = await client.post("/api/admin/updates", json={"action": "update", "channel": "x"}, headers=csrf)
        assert bad.status_code == 422
        sent = await client.post("/api/admin/updates", json={"action": "rollback"}, headers=csrf)
        assert sent.status_code == 200 and sent.json()["requested"] == {"action": "rollback"}
        again = await client.post("/api/admin/updates", json={"action": "update"}, headers=csrf)
        assert again.status_code == 409 and again.json()["error"] == "update_pending"
        assert (await client.delete("/api/admin/updates", headers=csrf)).json() == {"canceled": True}
