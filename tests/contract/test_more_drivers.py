"""WGDashboard, WG-Mate, MikroTik and IBSng drivers against in-memory fakes (PNL-08, PNL-11)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from paurubot.panels.base import GB, PanelError, PanelRef, UserSpec
from paurubot.panels.drivers.ibsng import IbsngDriver, _bytes
from paurubot.panels.drivers.mikrotik import MikrotikDriver
from paurubot.panels.drivers.wgdashboard import WGDashboardDriver, keypair
from paurubot.panels.drivers.wgmate import WGMateDriver
from paurubot.panels.http import SafeHttpClient
from tests.fakes.more_panels import FakeIbsng, FakeMikrotik, FakeWGDashboard, FakeWGMate

BASE = "https://panel.example.com"


def _http(fake) -> SafeHttpClient:  # type: ignore[no-untyped-def]
    return SafeHttpClient(BASE, transport=httpx.MockTransport(fake.handler))


def test_wireguard_keys_are_a_real_pair() -> None:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private, public = keypair()
    raw = base64.b64decode(private)
    derived = (
        X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    assert len(raw) == 32 and base64.b64encode(derived).decode() == public


async def test_wgdashboard_lifecycle_and_config_file() -> None:
    fake = FakeWGDashboard()
    driver = WGDashboardDriver(_http(fake), "", "", {"api_token": "wg-key", "configuration": "wg0"})
    assert (await driver.health_check()).ok
    expire = datetime.now(UTC).replace(microsecond=0) + timedelta(days=30)
    user = await driver.create_user(UserSpec(username="wg_1", data_limit=10 * GB, expire_at=expire))
    assert user.status == "active" and user.data_limit == 10 * GB and user.expire_at == expire
    assert user.subscription_url and user.subscription_url.startswith(f"{BASE}/api/downloadPeer/wg0?id=")
    ref = PanelRef("wg_1", user.data)
    [conf] = await driver.fetch_links(user.subscription_url)
    assert conf.startswith("[Interface]") and "PrivateKey" in conf
    body = await driver.fetch_subscription(user.subscription_url)
    assert body is not None and "attachment" in body.headers["content-disposition"]
    assert await driver.fetch_links("https://elsewhere.example/sub") == []

    peer = fake.peers[user.data["id"]]
    peer["total_data"] = 11.0  # over the limit: the dashboard restricts it
    fake.restricted.add(peer["id"])
    assert (await driver.get_user(ref)).status == "limited"  # type: ignore[union-attr]

    later = expire + timedelta(days=30)
    renewed = await driver.update_user(ref, expire_at=later, data_limit=20 * GB, enabled=True)
    assert renewed.data_limit == 20 * GB and renewed.expire_at == later and renewed.status == "active"
    assert len(fake.peers[peer["id"]]["jobs"]) == 2  # the old jobs were replaced, not stacked
    unlimited = await driver.update_user(ref, data_limit=0, expire_at=None)
    assert unlimited.data_limit == 0 and unlimited.expire_at is None
    disabled = await driver.update_user(ref, enabled=False)
    assert disabled.status == "disabled"
    await driver.reset_usage(ref)
    assert fake.peers[peer["id"]]["total_data"] == 0.0

    revoked = await driver.revoke_subscription(ref)
    assert revoked.data["id"] != peer["id"] and revoked.username == "wg_1"
    assert peer["id"] not in fake.peers
    new_ref = PanelRef("wg_1", revoked.data)
    users, total = await driver.list_users()
    assert total == 1 and users[0].username == "wg_1"
    await driver.delete_user(new_ref)
    assert await driver.get_user(new_ref) is None
    await driver.delete_user(new_ref)


async def test_wgdashboard_errors() -> None:
    fake = FakeWGDashboard()
    bad = WGDashboardDriver(_http(fake), "", "nope", {})
    with pytest.raises(PanelError, match="API key rejected"):
        await bad.health_check()
    driver = WGDashboardDriver(_http(fake), "", "wg-key", {})
    fake.free.clear()
    with pytest.raises(PanelError, match="no free address"):
        await driver.create_user(UserSpec(username="wg_full"))


async def test_wgmate_lifecycle_with_template() -> None:
    fake = FakeWGMate()
    driver = WGMateDriver(_http(fake), "", "", {"api_token": "mate-key", "template_user": "template"})
    assert (await driver.health_check()).ok
    expire = datetime(2031, 1, 1, tzinfo=UTC)
    user = await driver.create_user(UserSpec(username="m_1", data_limit=5 * GB, expire_at=expire, ip_limit=2))
    assert fake.bodies[-1]["protocols"] == ["wireguard", "vless"] and fake.bodies[-1]["serverId"] == "srv-1"
    assert fake.bodies[-1]["expiresAt"] == "2031-01-01T00:00:00Z" and fake.bodies[-1]["connLimit"] == 2
    assert user.subscription_url and user.subscription_url.startswith(f"{BASE}/sub/")
    ref = PanelRef("m_1", user.data)
    fake.users["m_1"].update(status="disabled", disableReason="user_quota", usedBytes=5 * GB)
    assert (await driver.get_user(ref)).status == "limited"  # type: ignore[union-attr]
    renewed = await driver.update_user(ref, data_limit=10 * GB)
    assert renewed.status == "active" and renewed.data_limit == 10 * GB  # quota stop lifted
    assert (await driver.update_user(ref, enabled=False)).status == "disabled"
    await driver.reset_usage(ref)
    revoked = await driver.revoke_subscription(ref)
    assert revoked.subscription_url != user.subscription_url
    await driver.delete_user(ref)
    assert await driver.get_user(ref) is None
    await driver.delete_user(ref)
    with pytest.raises(PanelError, match="API key rejected"):
        await WGMateDriver(_http(fake), "", "wrong", {}).health_check()


async def test_mikrotik_sells_profiles_and_keeps_our_limits() -> None:
    fake = FakeMikrotik()
    driver = MikrotikDriver(_http(fake), "admin", "panel-password", {"profile": "monthly"})
    assert (await driver.health_check()).version == "7.15"
    assert [o.id for o in await driver.list_options()] == ["monthly", "weekly"]
    user = await driver.create_user(UserSpec(username="mk_1", data_limit=GB, options={"profile": "weekly"}))
    assert not user.limits_known and user.data["password"] and user.data["profile"] == "weekly"
    assert fake.assignments == [("mk_1", "weekly")]
    ref = PanelRef("mk_1", user.data)
    fake.usage["mk_1"] = 900
    assert (await driver.get_user(ref)).used_bytes == 1000  # type: ignore[union-attr]
    await driver.update_user(ref, expire_at=datetime.now(UTC) + timedelta(days=7), enabled=True)
    assert fake.assignments[-1] == ("mk_1", "weekly")  # renewal queues another period
    assert (await driver.update_user(ref, enabled=False)).status == "disabled"
    old = user.data["password"]
    assert (await driver.revoke_subscription(ref)).data["password"] != old
    await driver.reset_usage(ref)  # a no-op on User Manager
    await driver.delete_user(ref)
    assert await driver.get_user(ref) is None
    with pytest.raises(PanelError, match="no User Manager profile"):
        await MikrotikDriver(_http(fake), "admin", "panel-password", {}).create_user(
            UserSpec(username="mk_2")
        )
    with pytest.raises(PanelError, match="login failed"):
        await MikrotikDriver(_http(fake), "admin", "wrong", {}).health_check()


async def test_ibsng_create_read_delete() -> None:
    fake = FakeIbsng()
    driver = IbsngDriver(_http(fake), "admin", "panel-password", {"group": "1month"})
    assert (await driver.health_check()).ok
    user = await driver.create_user(UserSpec(username="ib_1"))
    assert user.data["id"] == "100" and user.data["group"] == "1month" and len(user.data["password"]) == 12
    assert user.used_bytes == 512 * 1024 * 1024 and not user.limits_known
    ref = PanelRef("ib_1", user.data)
    assert await driver.fetch_password(ref) == user.data["password"]
    assert (await driver.update_user(ref, expire_at=None)).username == "ib_1"
    # IBSng's group ends the account itself; turning it off from the bot changes nothing there.
    assert (await driver.update_user(ref, enabled=False)).username == "ib_1"
    with pytest.raises(PanelError, match="already taken"):
        await driver.create_user(UserSpec(username="ib_1"))
    fake.session = False  # an expired session signs in again by itself
    assert await driver.get_user(ref) is not None
    await driver.delete_user(ref)
    assert await driver.get_user(ref) is None
    await driver.delete_user(ref)
    with pytest.raises(PanelError, match="login failed"):
        await IbsngDriver(_http(fake), "admin", "wrong", {}).health_check()


def test_ibsng_amounts() -> None:
    assert _bytes("20 G") == 20 * GB and _bytes("512 M") == 512 * 1024 * 1024
    assert _bytes("1,024", default_unit=1024 * 1024) == 1024 * 1024 * 1024 and _bytes("") == 0
