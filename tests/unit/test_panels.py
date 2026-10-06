from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from paurubot.panels import GB, PanelRef, UserSpec
from paurubot.panels.base import decode_subscription, parse_expire
from paurubot.panels.drivers.marzban import MarzbanClient, PasarGuardClient
from paurubot.panels.drivers.xui import XuiClient


def _attach(client, handler) -> None:  # type: ignore[no-untyped-def]
    client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(handler))


def test_parse_expire_variants() -> None:
    ts = 1_900_000_000
    assert parse_expire(ts) == datetime.fromtimestamp(ts, tz=UTC)
    assert parse_expire(ts * 1000) == datetime.fromtimestamp(ts, tz=UTC)
    assert parse_expire("2030-01-01T00:00:00Z") == datetime(2030, 1, 1, tzinfo=UTC)
    assert parse_expire(0) is None
    assert parse_expire(None) is None


def test_decode_subscription_base64() -> None:
    import base64

    raw = base64.b64encode(b"vless://a@b:1#x\ntrojan://c@d:2#y").decode()
    assert decode_subscription(raw) == ["vless://a@b:1#x", "trojan://c@d:2#y"]
    assert decode_subscription("vmess://abc") == ["vmess://abc"]


async def test_marzban_create_user_payload() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.headers["Authorization"] == "Bearer tok"
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "username": seen["username"],
                "status": "active",
                "used_traffic": 0,
                "data_limit": seen["data_limit"],
                "expire": seen["expire"],
                "subscription_url": "/sub/abc",
                "links": ["vless://x"],
            },
        )

    client = MarzbanClient("https://panel.example.com/", "u", "p", {"inbounds": ["vless:VLESS TCP"]})
    _attach(client, handler)
    expire = datetime.now(UTC).replace(microsecond=0) + timedelta(days=30)
    user = await client.create_user(UserSpec(username="ab_1", data_limit=10 * GB, expire_at=expire))
    assert seen["inbounds"] == {"vless": ["VLESS TCP"]}
    assert seen["proxies"] == {"vless": {}}
    assert seen["expire"] == int(expire.timestamp())
    assert user.subscription_url == "https://panel.example.com/sub/abc"
    assert user.expire_at == expire
    assert user.data_limit == 10 * GB


async def test_pasarguard_falls_back_to_legacy_path() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "tok"})
        calls.append(request.url.path)
        if "by-username" in request.url.path:
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(
            200,
            json={
                "username": "u1",
                "status": "active",
                "used_traffic": 5,
                "data_limit": 0,
                "expire": "2031-05-01T00:00:00Z",
                "subscription_url": "https://s/x",
            },
        )

    client = PasarGuardClient("https://pg.example.com", "u", "p", {"group_ids": [1]})
    _attach(client, handler)
    user = await client.get_user(PanelRef("u1"))
    assert user is not None and user.used_bytes == 5
    assert calls == ["/api/user/by-username/u1", "/api/user/u1"]


async def test_pasarguard_create_sends_iso_expire_and_groups() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "tok"})
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"username": "u2", "status": "active", "id": 7})

    client = PasarGuardClient("https://pg.example.com", "u", "p", {"group_ids": ["1", 2]})
    _attach(client, handler)
    user = await client.create_user(
        UserSpec(username="u2", data_limit=GB, expire_at=datetime(2031, 1, 1, tzinfo=UTC))
    )
    assert seen["group_ids"] == [1, 2]
    assert seen["expire"] == "2031-01-01T00:00:00Z"
    assert user.data == {"id": 7}


async def test_xui_create_and_get() -> None:
    added: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/login":
            return httpx.Response(200, json={"success": True}, headers={"set-cookie": "3x-ui=abc"})
        if path == "/panel/api/inbounds/get/3":
            return httpx.Response(200, json={"success": True, "obj": {"id": 3, "protocol": "vless"}})
        if path == "/panel/api/inbounds/addClient":
            form = dict(httpx.QueryParams(request.content.decode()))
            added.update(json.loads(form["settings"])["clients"][0])
            return httpx.Response(200, json={"success": True, "obj": None})
        if path.startswith("/panel/api/inbounds/getClientTraffics/"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "obj": {"up": GB, "down": GB, "total": 5 * GB, "expiryTime": 0, "enable": True},
                },
            )
        return httpx.Response(404)

    client = XuiClient(
        "https://x.example.com", "u", "p", {"inbound_id": 3, "sub_base_url": "https://sub.example.com/sub/"}
    )
    _attach(client, handler)
    user = await client.create_user(UserSpec(username="mail1", data_limit=5 * GB))
    assert added["email"] == "mail1" and added["totalGB"] == 5 * GB and "id" in added
    assert user.subscription_url == f"https://sub.example.com/sub/{added['subId']}"
    fetched = await client.get_user(PanelRef("mail1", user.data))
    assert fetched is not None and fetched.used_bytes == 2 * GB and fetched.status == "active"


async def test_marzban_get_missing_user_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(404, json={"detail": "User not found"})

    client = MarzbanClient("https://m.example.com", "u", "p")
    _attach(client, handler)
    assert await client.get_user(PanelRef("nobody")) is None


@pytest.mark.parametrize("status", [500, 422])
async def test_marzban_errors_raise(status: int) -> None:
    from paurubot.panels import PanelError

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(status, json={"detail": "bad"})

    client = MarzbanClient("https://m.example.com", "u", "p")
    _attach(client, handler)
    with pytest.raises(PanelError):
        await client.create_user(UserSpec(username="x"))
