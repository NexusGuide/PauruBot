"""F-11: a customer sees when their service was last online, from which app, and from where.

Addresses are read live and shown masked; the raw User-Agent and full addresses never leave
the server, and the live read does not touch the list the device-limit notice compares against.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
from sqlalchemy import select, update

from paurubot.api.app import create_app
from paurubot.config import get_settings
from paurubot.db.base import utcnow
from paurubot.db.models import OrderKind, PanelKind, Service
from paurubot.panels import GB, registry
from paurubot.provisioning import lifecycle, subscription
from tests import factories
from tests.fakes.panels import FakeXuiPanel, transport
from tests.unit import test_service_lifecycle, test_webapp_api
from tests.unit.test_service_lifecycle import _buy, _service
from tests.unit.test_webapp_api import h

app_env = test_webapp_api.app_env
env = test_service_lifecycle.env


def test_only_a_recognised_app_name_and_version_is_kept() -> None:
    assert subscription.client_app("v2rayNG/1.9.1") == "v2rayNG 1.9.1"
    assert subscription.client_app("HiddifyNext/2.5.7 (android) like ClashMeta") == "HiddifyNext 2.5.7"
    assert subscription.client_app("Happ/3.2.1/Android/12/Samsung SM-A515F/abcdef") == "Happ 3.2.1"
    assert subscription.client_app("ClashMetaForAndroid/2.10.1.Meta") == "ClashMetaForAndroid 2.10.1.Meta"
    assert subscription.client_app("Mozilla/5.0 (Windows NT 10.0) sing-box 1.9") == "sing-box"
    assert subscription.client_app("Streisand") == "Streisand"
    for nothing in (None, "", "Mozilla/5.0 (iPhone)", "curl/8.4.0", "python-httpx/0.27"):
        assert subscription.client_app(nothing) is None
    assert len(subscription.client_app("v2ray" + "x" * 200 + "/1") or "") <= 48


def test_a_fetch_is_recorded_when_newer_and_at_most_every_ten_minutes() -> None:
    service = Service()
    now = utcnow()
    assert subscription.client_seen(service, "v2rayNG 1.9.1", now)
    assert not subscription.client_seen(service, "v2rayNG 1.9.1", now + timedelta(minutes=5))
    assert subscription.client_seen(service, "Happ 3.2.1", now + timedelta(minutes=5))  # another app
    assert not subscription.client_seen(service, "v2rayNG 1.9.1", now)  # older than what we have
    assert not subscription.client_seen(service, None, now + timedelta(hours=1))
    assert subscription.client_seen(service, "Happ 3.2.1", now + timedelta(minutes=16))
    assert service.sub_client == "Happ 3.2.1" and service.sub_fetched_at == now + timedelta(minutes=16)


async def test_the_branded_link_and_the_panel_both_report_the_app(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "public_url", "https://bot.example.com")
    service = await _service(session, await _buy(session, env["cust"], OrderKind.PURCHASE, plan=env["plan"]))
    await session.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        browser = await client.get(
            f"/s/{service.sub_token}", headers={"user-agent": "Mozilla/5.0", "accept": "text/html"}
        )
        assert browser.status_code == 200
        await session.refresh(service)
        assert service.sub_client is None  # a browser looking at the page is not an import
        agent = "Happ/3.2.1/Android/12/Samsung SM-A515F/device-id-123"
        assert (await client.get(f"/s/{service.sub_token}", headers={"user-agent": agent})).status_code == 200
    await session.refresh(service)
    assert service.sub_client == "Happ 3.2.1" and service.sub_fetched_at is not None
    seen = service.sub_fetched_at

    # The panel's own record of a later fetch (through its direct link) wins when newer.
    later = seen + timedelta(hours=2)
    env["fake"].users[service.panel_username].update(
        sub_last_user_agent="v2rayNG/1.9.1", sub_updated_at=later.isoformat(), online_at=later.isoformat()
    )
    await lifecycle.refresh(session, service)
    assert service.sub_client == "v2rayNG 1.9.1" and service.sub_fetched_at == later
    assert service.online_at == later
    # An older panel record does not roll it back.
    env["fake"].users[service.panel_username].update(
        sub_last_user_agent="Streisand/1.0", sub_updated_at=seen.isoformat()
    )
    await lifecycle.refresh(session, service)
    assert service.sub_client == "v2rayNG 1.9.1"


async def test_the_mini_app_shows_access_and_masked_live_devices(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop, plan = app_env["client"], app_env["shop"], app_env["plan"]
    me = (await client.get("/api/webapp/me", headers=h(94001))).json()
    online = utcnow().replace(microsecond=0) - timedelta(minutes=3)
    marzban = Service(
        shop_id=shop.id,
        customer_id=me["id"],
        panel_id=app_env["panel"].id,
        plan_id=plan.id,
        panel_username="acc-m",
        status="active",
        data_limit=10 * GB,
        online_at=online,
        sub_client="v2rayNG 1.9.1",
        sub_fetched_at=online,
    )
    xui_panel = await factories.panel(session, shop, PanelKind.XUI, inbound_id=3)
    fake = FakeXuiPanel()
    registry.test_transports[xui_panel.id] = transport(fake)
    registry._cache.clear()
    xui_plan = await factories.plan(session, shop, xui_panel, price=10_000)
    xui_plan.ip_limit = 2
    xui = Service(
        shop_id=shop.id,
        customer_id=me["id"],
        panel_id=xui_panel.id,
        plan_id=xui_plan.id,
        panel_username="acc-x",
        status="active",
        data_limit=10 * GB,
        client_ips=["9.9.9.9"],  # the device-limit notice's last look
    )
    session.add_all([marzban, xui])
    await session.commit()

    view = (await client.get(f"/api/webapp/services/{marzban.id}", headers=h(94001))).json()["service"]
    assert view["access"] == {
        "online_at": online.isoformat(),
        "client": "v2rayNG 1.9.1",
        "client_at": online.isoformat(),
        "devices": False,  # Marzban cannot list live addresses
    }
    unsupported = await client.get(f"/api/webapp/services/{marzban.id}/devices", headers=h(94001))
    assert unsupported.status_code == 409 and unsupported.json()["error"] == "devices_unsupported"

    view = (await client.get(f"/api/webapp/services/{xui.id}", headers=h(94001))).json()["service"]
    assert view["access"]["devices"] is True and view["access"]["client"] is None
    fake.ips["acc-x"] = ["185.12.33.7", "2001:db8:1:2::9"]
    devices = await client.get(f"/api/webapp/services/{xui.id}/devices", headers=h(94001))
    assert devices.status_code == 200, devices.text
    assert devices.json() == {"count": 2, "limit": 2, "addresses": ["185.12.33.x", "2001:0db8:0001:…"]}
    assert "185.12.33.7" not in devices.text
    stored = await session.scalar(
        select(Service.client_ips).where(Service.id == xui.id).execution_options(populate_existing=True)
    )
    assert stored == ["9.9.9.9"]  # the live read leaves the notice's baseline alone

    # Someone else's service is not found; the read is rate limited; a panel error is a 503.
    other = await client.get(f"/api/webapp/services/{xui.id}/devices", headers=h(94002))
    assert other.status_code == 404
    codes = [
        (await client.get(f"/api/webapp/services/{xui.id}/devices", headers=h(94001))).status_code
        for _ in range(6)
    ]
    assert codes[-1] == 429 and set(codes[:-1]) == {200}
    registry.test_transports.clear()
    registry._cache.clear()


async def test_a_panel_error_on_the_device_read_is_reported(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop = app_env["client"], app_env["shop"]
    me = (await client.get("/api/webapp/me", headers=h(94011))).json()
    xui_panel = await factories.panel(session, shop, PanelKind.XUI, inbound_id=3)
    registry.test_transports[xui_panel.id] = httpx.MockTransport(lambda request: httpx.Response(500))
    registry._cache.clear()
    plan = await factories.plan(session, shop, xui_panel, price=10_000)
    service = Service(
        shop_id=shop.id,
        customer_id=me["id"],
        panel_id=xui_panel.id,
        plan_id=plan.id,
        panel_username="acc-e",
        status="active",
    )
    session.add(service)
    await session.commit()
    response = await client.get(f"/api/webapp/services/{service.id}/devices", headers=h(94011))
    assert response.status_code == 503 and response.json()["error"] == "panel_unavailable"
    await session.execute(update(Service).where(Service.id == service.id).values(status="deleted"))
    await session.commit()
    assert (
        await client.get(f"/api/webapp/services/{service.id}/devices", headers=h(94011))
    ).status_code == 404
    registry.test_transports.clear()
    registry._cache.clear()
