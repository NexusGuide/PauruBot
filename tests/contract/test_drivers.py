"""Contract tests: every driver honours the same lifecycle against a fake of its panel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from paurubot.panels import GB, PanelError, PanelRef, PanelUnavailable, UserSpec
from paurubot.panels.base import PanelDriver, decode_subscription, parse_expire
from paurubot.panels.drivers.guard import GuardDriver
from paurubot.panels.drivers.marzban import MarzbanDriver
from paurubot.panels.drivers.pasarguard import PasarGuardDriver
from paurubot.panels.drivers.rebecca import RebeccaDriver
from paurubot.panels.drivers.remnawave import RemnawaveDriver
from paurubot.panels.drivers.xui import XuiDriver
from paurubot.panels.http import SafeHttpClient
from tests.fakes.panels import (
    FakeGuardPanel,
    FakeRebeccaPanel,
    FakeRemnawavePanel,
    FakeTokenPanel,
    FakeXuiPanel,
    transport,
)

BASE = "https://panel.example.com"


def make(kind: str, **options):  # type: ignore[no-untyped-def]
    if kind in {"guard", "guard_v2"}:
        fake = FakeGuardPanel(version="v2" if kind == "guard_v2" else "v1")  # type: ignore[assignment]
        options.setdefault("guard_version", "v2" if kind == "guard_v2" else "v1")
        http = SafeHttpClient(BASE, transport=transport(fake))
        return GuardDriver(http, "", "guard-key", options), fake
    if kind == "rebecca":
        fake = FakeRebeccaPanel()  # type: ignore[assignment]
        http = SafeHttpClient(BASE, transport=transport(fake))
        return RebeccaDriver(http, "admin", "panel-password", options), fake
    if kind == "remnawave":
        fake = FakeRemnawavePanel()  # type: ignore[assignment]
        http = SafeHttpClient(BASE, transport=transport(fake))
        return RemnawaveDriver(http, "admin", "panel-password", options), fake
    if kind == "xui":
        fake = FakeXuiPanel()
        options.setdefault("inbound_id", 3)
        options.setdefault("sub_base_url", "https://sub.example.com/sub/")
        cls: type[PanelDriver] = XuiDriver
    else:
        fake = FakeTokenPanel(kind)  # type: ignore[assignment]
        cls = PasarGuardDriver if kind == "pasarguard" else MarzbanDriver
    http = SafeHttpClient(BASE, transport=transport(fake))
    return cls(http, "admin", "panel-password", options), fake


KINDS = ["marzban", "pasarguard", "xui", "remnawave", "rebecca", "guard", "guard_v2"]


@pytest.mark.parametrize("kind", KINDS)
async def test_full_lifecycle(kind: str) -> None:
    driver, _ = make(kind)
    health = await driver.health_check()
    assert health.ok
    assert await driver.list_options()
    expire = datetime.now(UTC).replace(microsecond=0) + timedelta(days=30)
    created = await driver.create_user(UserSpec(username="pb_1001", data_limit=10 * GB, expire_at=expire))
    assert created.username == "pb_1001"
    assert created.subscription_url and created.subscription_url.startswith("https://")
    ref = PanelRef(created.username, created.data)
    fetched = await driver.get_user(ref)
    assert fetched is not None
    assert fetched.data_limit == 10 * GB
    assert fetched.expire_at == expire
    later = expire + timedelta(days=30)
    updated = await driver.update_user(ref, expire_at=later, data_limit=20 * GB)
    ref = PanelRef(updated.username, {**ref.data, **updated.data})
    assert updated.data_limit == 20 * GB and updated.expire_at == later
    disabled = await driver.update_user(ref, enabled=False)
    ref = PanelRef(disabled.username, {**ref.data, **disabled.data})
    assert disabled.status == "disabled"
    await driver.reset_usage(ref)
    revoked = await driver.revoke_subscription(ref)
    assert revoked.subscription_url != created.subscription_url
    ref = PanelRef(revoked.username, {**ref.data, **revoked.data})
    await driver.delete_user(ref)
    assert await driver.get_user(ref) is None
    await driver.delete_user(ref)  # idempotent


@pytest.mark.parametrize("kind", KINDS)
async def test_unlimited_and_on_hold(kind: str) -> None:
    driver, _ = make(kind)
    user = await driver.create_user(UserSpec(username="pb_free"))
    assert user.data_limit == 0 and user.expire_at is None
    held = await driver.create_user(UserSpec(username="pb_hold", data_limit=GB, on_hold_days=30))
    if kind == "remnawave":
        # No first-connection start on Remnawave: the days count from purchase.
        assert held.status == "active" and held.expire_at is not None
    else:
        assert held.status == "on_hold"


@pytest.mark.parametrize("kind", ["marzban", "pasarguard", "remnawave", "rebecca"])
async def test_token_refresh_after_expiry(kind: str) -> None:
    driver, fake = make(kind)
    await driver.health_check()
    fake.token = "rotated"
    await driver.health_check()
    assert fake.logins == 2


@pytest.mark.parametrize("kind", KINDS)
async def test_server_errors_raise_and_bad_credentials(kind: str) -> None:
    driver, fake = make(kind)
    await driver.health_check()
    fake.fail_next = 500
    with pytest.raises(PanelError):
        await driver.create_user(UserSpec(username="x_1"))
    bad, _ = make(kind)
    bad.password = "wrong"  # Guard sends the password field as its API key
    with pytest.raises(PanelError, match="login failed|API key rejected"):
        await bad.health_check()


async def test_marzban_payload_uses_selected_inbounds_and_flow() -> None:
    driver, fake = make("marzban", inbounds=["vless:VLESS TCP"], flow="xtls-rprx-vision")
    await driver.create_user(UserSpec(username="pb_in", data_limit=GB))
    sent = fake.users["pb_in"]["request"]
    assert sent["inbounds"] == {"vless": ["VLESS TCP"]}
    assert sent["proxies"] == {"vless": {"flow": "xtls-rprx-vision"}}


async def test_pasarguard_sends_groups_and_iso_expiry() -> None:
    driver, fake = make("pasarguard", group_ids=["1", 2])
    await driver.create_user(UserSpec(username="pg_1", expire_at=datetime(2031, 1, 1, tzinfo=UTC)))
    sent = fake.users["pg_1"]["request"]
    assert sent["group_ids"] == [1, 2] and sent["expire"] == "2031-01-01T00:00:00Z"


async def test_pasarguard_falls_back_to_legacy_paths() -> None:
    driver, fake = make("pasarguard")
    fake.kind = "marzban"  # fake now only answers legacy /api/user/{name}
    await driver.create_user(UserSpec(username="legacy_1"))
    assert await driver.get_user(PanelRef("legacy_1")) is not None
    assert ("GET", "/api/user/by-username/legacy_1") in fake.calls
    assert ("GET", "/api/user/legacy_1") in fake.calls


async def test_circuit_breaker_opens_after_repeated_failures() -> None:
    driver, fake = make("marzban")
    await driver.health_check()
    for _ in range(5):
        fake.fail_next = 502
        with pytest.raises(PanelError):
            await driver.health_check()
    with pytest.raises(PanelUnavailable, match="circuit open"):
        await driver.health_check()


async def test_ssrf_guard_applies_to_real_clients() -> None:
    http = SafeHttpClient("http://169.254.169.254")
    with pytest.raises(PanelError, match="blocked"):
        await http.request("GET", "/")
    await http.aclose()


def test_helpers() -> None:
    import base64

    assert parse_expire("2030-01-01T00:00:00Z") == datetime(2030, 1, 1, tzinfo=UTC)
    assert parse_expire(1_900_000_000_000) == datetime.fromtimestamp(1_900_000_000, tz=UTC)
    raw = base64.b64encode(b"vless://a@b:1#x\ntrojan://c@d:2#y").decode()
    assert decode_subscription(raw) == ["vless://a@b:1#x", "trojan://c@d:2#y"]


async def test_remnawave_api_token_squads_and_plain_http() -> None:
    fake = FakeRemnawavePanel(api_token="fixed")
    driver = RemnawaveDriver(
        SafeHttpClient("http://10.0.0.5:3000", allow_private=True, transport=transport(fake)),
        "",
        "",
        {"api_token": "fixed", "squad_ids": ["sq-1"]},
    )
    assert [o.id for o in await driver.list_options()] == ["sq-1"]
    user = await driver.create_user(UserSpec(username="pb_sq", data_limit=GB, note="tg:42", ip_limit=2))
    stored = next(iter(fake.users.values()))
    assert stored["activeInternalSquads"] == [{"uuid": "sq-1", "name": "sq-1"}] and stored["telegramId"] == 42
    assert user.data["uuid"] == stored["uuid"] and fake.logins == 0
    assert set(fake.forwarded) == {"https"}  # plain HTTP is announced as coming through TLS
    users, total = await driver.list_users()
    assert total == 1 and users[0].username == "pb_sq"
    stats = await driver.stats()
    assert stats["users_total"] == 1 and stats["nodes"][0]["status"] == "connected"
    fake.api_token = "rotated"
    with pytest.raises(PanelError):
        await driver.health_check()  # a fixed token is never swapped for a login


async def test_rebecca_uses_services_and_an_api_key() -> None:
    fake = FakeRebeccaPanel()
    driver = RebeccaDriver(
        SafeHttpClient(BASE, transport=transport(fake)), "", "", {"api_token": fake.token, "service_ids": [7]}
    )
    assert [o.id for o in await driver.list_options()] == ["7"]
    await driver.create_user(UserSpec(username="rb_1", data_limit=GB, ip_limit=3))
    assert fake.v2_bodies[0]["service_id"] == 7 and fake.v2_bodies[0]["ip_limit"] == 3
    assert fake.logins == 0


async def test_guard_on_hold_and_telegram_id() -> None:
    driver, fake = make("guard", service_ids=[2])
    held = await driver.create_user(UserSpec(username="g_hold", data_limit=GB, on_hold_days=10, note="tg:77"))
    sub = fake.subs["g_hold"]
    assert held.status == "on_hold" and sub["limit_expire"] == -10 * 86400
    assert sub["services"] == [2] and sub["telegram_id"] == "77"
    users, total = await driver.list_users()
    assert total == 1 and users[0].username == "g_hold"
