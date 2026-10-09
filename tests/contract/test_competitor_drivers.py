"""Contract tests for the competitor-parity drivers (MirzaAgent, Nexora) and the new option
knobs on Remnawave (squad/HWID), Guard (service allow-list, api_version) and X-UI (api_mode)."""

from __future__ import annotations

import json

import httpx
import pytest

from paurubot.panels import PanelError, PanelRef, UserSpec, registry
from paurubot.panels.base import GB, PanelDriver
from paurubot.panels.drivers.guard import GuardDriver
from paurubot.panels.drivers.mirza_agent import MirzaAgentDriver
from paurubot.panels.drivers.nexora import NexoraDriver
from paurubot.panels.drivers.remnawave import RemnawaveDriver
from paurubot.panels.drivers.xui import XuiDriver
from paurubot.panels.http import SafeHttpClient
from tests.contract.test_drivers import make
from tests.fakes.panels import FakeGuardPanel, FakeRemnawavePanel, FakeXuiPanel, transport

BASE = "https://panel.example.com"


def _refusing_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP expected without creds: {request.method} {request.url.path}")

    return httpx.MockTransport(handler)


def _capturing(fake: FakeRemnawavePanel) -> tuple[httpx.MockTransport, list]:
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.content:
            try:
                bodies.append(json.loads(request.content))
            except ValueError:
                pass
        return fake.handler(request)

    return httpx.MockTransport(handler), bodies


def test_competitor_kinds_resolve_in_registry() -> None:
    assert registry.driver_class_for("mirza_agent") is MirzaAgentDriver
    assert registry.driver_class_for("nexora") is NexoraDriver
    assert registry.driver_class_for("nope") is None
    assert registry.delivery_of("mirza_agent") == "subscription"
    assert "renew" in registry.changes_of("nexora")
    assert issubclass(MirzaAgentDriver, PanelDriver) and issubclass(NexoraDriver, PanelDriver)


@pytest.mark.parametrize("cls", [MirzaAgentDriver, NexoraDriver])
async def test_competitor_drivers_raise_cleanly_without_creds(cls: type[PanelDriver]) -> None:
    driver = cls(SafeHttpClient(BASE, transport=_refusing_transport()), "", "", {})
    try:
        with pytest.raises(PanelError, match="not_configured"):
            await driver.health_check()
        with pytest.raises(PanelError, match="not_configured"):
            await driver.create_user(UserSpec(username="u1"))
        with pytest.raises(PanelError, match="not_configured"):
            await driver.get_user(PanelRef("u1"))
        with pytest.raises(PanelError, match="not_configured"):
            await driver.list_options()
    finally:
        await driver.close()


async def test_remnawave_squad_and_hwid_options_reach_payload() -> None:
    fake = FakeRemnawavePanel(api_token="fixed")
    tr, bodies = _capturing(fake)
    driver = RemnawaveDriver(
        SafeHttpClient(BASE, transport=tr),
        "",
        "",
        {"api_token": "fixed", "squad": "sq-9", "hwid_limit": 3},
    )
    await driver.create_user(UserSpec(username="pb_sq9"))
    assert bodies[-1]["activeInternalSquads"] == ["sq-9"]
    assert bodies[-1]["hwidDeviceLimit"] == 3


async def test_remnawave_defaults_unchanged_without_options() -> None:
    fake = FakeRemnawavePanel(api_token="fixed")
    tr, bodies = _capturing(fake)
    driver = RemnawaveDriver(SafeHttpClient(BASE, transport=tr), "", "", {"api_token": "fixed"})
    await driver.create_user(UserSpec(username="pb_plain"))
    assert bodies[-1]["activeInternalSquads"] == []
    assert "hwidDeviceLimit" not in bodies[-1]
    await driver.create_user(UserSpec(username="pb_ip", ip_limit=2))
    assert bodies[-1]["hwidDeviceLimit"] == 2


async def test_remnawave_update_carries_squads_and_hwid() -> None:
    fake = FakeRemnawavePanel(api_token="fixed")
    tr, bodies = _capturing(fake)
    driver = RemnawaveDriver(
        SafeHttpClient(BASE, transport=tr),
        "",
        "",
        {"api_token": "fixed", "squad_ids": ["sq-1"], "hwid_limit": 5},
    )
    created = await driver.create_user(UserSpec(username="pb_up"))
    updated = await driver.update_user(PanelRef(created.username, created.data), data_limit=2 * GB)
    assert updated.data_limit == 2 * GB
    patches = [b for b in bodies if b.get("uuid")]
    assert patches and patches[-1]["activeInternalSquads"] == ["sq-1"]
    assert patches[-1]["hwidDeviceLimit"] == 5


async def test_remnawave_flush_hwid_hits_reset_endpoint() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"response": {"username": "u", "status": "ACTIVE"}})

    driver = RemnawaveDriver(
        SafeHttpClient(BASE, transport=httpx.MockTransport(handler)), "", "", {"api_token": "x"}
    )
    await driver.flush_hwid(PanelRef("u", {"uuid": "uuid-1"}))
    assert ("POST", "/api/users/uuid-1/actions/reset-hwid") in calls
    await driver.close()


async def test_guard_api_version_alias_and_service_allow_list() -> None:
    fake = FakeGuardPanel(version="v2")
    driver = GuardDriver(
        SafeHttpClient(BASE, transport=transport(fake)),
        "",
        "guard-key",
        {"api_version": "v2", "service_ids": [2]},
    )
    assert driver.v2 and driver.api_version == "v2"
    assert [o.id for o in await driver.list_options()] == ["2"]
    await driver.create_user(UserSpec(username="g1", data_limit=GB))
    assert fake.subs["g1"]["services"] == [2]

    legacy_fake = FakeGuardPanel(version="v1")
    plain = GuardDriver(SafeHttpClient(BASE, transport=transport(legacy_fake)), "", "guard-key", {})
    assert not plain.v2 and plain.api_version == "v1"
    assert len(await plain.list_options()) == 2


async def test_xui_server_status_none_when_unsupported() -> None:
    driver, _fake = make("xui")
    assert await driver.server_status() is None


async def test_xui_token_mode_needs_token() -> None:
    _driver, fake = make("xui")
    http = SafeHttpClient(BASE, transport=transport(fake))
    driver = XuiDriver(http, "admin", "panel-password", {"api_mode": "token", "inbound_id": 3})
    with pytest.raises(PanelError, match="api_token"):
        await driver.health_check()


async def test_xui_legacy_forces_session_login_despite_token() -> None:
    fake = FakeXuiPanel()
    http = SafeHttpClient(BASE, transport=transport(fake))
    driver = XuiDriver(
        http,
        "admin",
        "panel-password",
        {
            "api_mode": "legacy",
            "api_token": "tok",
            "inbound_id": 3,
            "sub_base_url": "https://sub.example.com/sub/",
        },
    )
    assert driver.api_mode == "legacy"
    assert (await driver.health_check()).ok and fake.logged_in
