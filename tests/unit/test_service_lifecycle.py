from __future__ import annotations

import base64
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from paurubot.api.app import create_app
from paurubot.config import get_settings
from paurubot.core import customers, orders
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Job, JobStatus, OrderKind, OrderStatus, PanelKind, Service, ServiceStatus
from paurubot.ledger import wallet
from paurubot.panels import GB, registry
from paurubot.provisioning import lifecycle, subscription, sync
from paurubot.worker import jobs as worker_jobs  # noqa: F401  (registers queues)
from paurubot.worker import queue
from paurubot.worker.runner import QUEUES, run_job
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport


async def drain(session, name: str) -> int:  # type: ignore[no-untyped-def]
    total = 0
    while True:
        claimed = await queue.claim(session, name, limit=1)
        if not claimed:
            return total
        await run_job(name, QUEUES[name], claimed[0])
        total += 1


@pytest.fixture(autouse=True)
async def isolate_queue(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    yield


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN, inbounds=["vless:VLESS TCP"])
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    plan = await factories.plan(session, shop, panel, price=100_000, days=30, data_gb=50)
    cust = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, cust.id, 2_000_000, provider="card", payment_id=cust.id)
    await session.commit()
    yield {"shop": shop, "panel": panel, "plan": plan, "cust": cust, "fake": fake}
    registry.test_transports.clear()
    registry._cache.clear()


async def _buy(session, cust, kind, **kw):  # type: ignore[no-untyped-def]
    order = await orders.create(session, cust, await orders.draft(session, cust, kind, **kw))
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    return order


async def _service(session, order) -> Service:  # type: ignore[no-untyped-def]
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED, order.error
    service = await session.get(Service, order.service_id, populate_existing=True)
    assert service is not None
    return service


async def test_on_hold_plan_starts_on_first_connection(session, env) -> None:  # type: ignore[no-untyped-def]
    env["plan"].on_hold = True
    await session.commit()
    order = await _buy(session, env["cust"], OrderKind.PURCHASE, plan=env["plan"])
    service = await _service(session, order)
    request = env["fake"].users[service.panel_username]["request"]
    assert request["status"] == "on_hold"
    assert request["on_hold_expire_duration"] == 30 * 86400
    assert service.status == ServiceStatus.ON_HOLD
    assert service.expire_at is None


async def test_plan_panel_options_and_prefix_reach_the_panel(session, env) -> None:  # type: ignore[no-untyped-def]
    env["plan"].panel_options = {"inbounds": ["vless:Reality"], "flow": "xtls-rprx-vision", "group_ids": []}
    env["plan"].username_prefix = "vip"
    await session.commit()
    order = await _buy(session, env["cust"], OrderKind.PURCHASE, plan=env["plan"])
    service = await _service(session, order)
    assert service.panel_username.startswith("vip")
    request = env["fake"].users[service.panel_username]["request"]
    assert request["inbounds"] == {"vless": ["Reality"]}
    assert request["proxies"]["vless"] == {"flow": "xtls-rprx-vision"}
    assert service.sub_token and subscription.TOKEN_RE.match(service.sub_token)


async def test_custom_buy_prices_and_provisions(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, cust = env["shop"], env["cust"]
    with pytest.raises(orders.OrderError) as off:
        await orders.draft(session, cust, OrderKind.PURCHASE, custom_gb=20, custom_days=30)
    assert off.value.key == "custom_unavailable"
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "custom_buy_enabled": True,
            "custom_panel_id": env["panel"].id,
            "custom_gb_price": 2_000,
            "custom_day_price": 1_000,
            "custom_base_price": 5_000,
        },
    )
    with pytest.raises(orders.OrderError) as out:
        await orders.draft(session, cust, OrderKind.PURCHASE, custom_gb=5_000, custom_days=30)
    assert out.value.key == "custom_out_of_range" and out.value.params["max_gb"] == 500
    draft = await orders.draft(session, cust, OrderKind.PURCHASE, custom_gb=20, custom_days=30)
    assert draft.base_price == 5_000 + 20 * 2_000 + 30 * 1_000
    order = await _buy(session, cust, OrderKind.PURCHASE, custom_gb=20, custom_days=30)
    service = await _service(session, order)
    assert service.data_limit == 20 * GB
    assert service.plan_id is None
    assert service.expire_at is not None
    assert timedelta(days=29) < service.expire_at - utcnow() <= timedelta(days=30)


async def test_plan_addons_override_extra_prices(session, env) -> None:  # type: ignore[no-untyped-def]
    cfg = await shop_settings.load(session, env["shop"].id)
    plan = env["plan"]
    assert orders.extra_prices(cfg, plan) == (cfg["extra_gb_price"], cfg["extra_day_price"])
    plan.addons = {"extra_gb_price": 1_234}
    assert orders.extra_prices(cfg, plan) == (1_234, cfg["extra_day_price"])
    plan.addons = {"extras": False}
    assert orders.extra_prices(cfg, plan) is None
    order = await _buy(session, env["cust"], OrderKind.PURCHASE, plan=plan)
    service = await _service(session, order)
    with pytest.raises(orders.OrderError):
        await orders.draft(session, env["cust"], OrderKind.EXTRA_VOLUME, service=service, extra_gb=5)


async def test_renewal_waits_for_the_current_period(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, cust, plan = env["shop"], env["cust"], env["plan"]
    service = await _service(session, await _buy(session, cust, OrderKind.PURCHASE, plan=plan))
    await shop_settings.set_many(session, shop.id, {"renew_queue_when_active": True})
    await session.commit()
    renewal = await _buy(session, cust, OrderKind.RENEW, plan=plan, service=service)
    assert renewal.params["queued"] is True
    job = await session.scalar(select(Job).where(Job.dedupe_key == f"order:{renewal.id}:provision"))
    assert job is not None and job.run_at > utcnow() + timedelta(days=29)
    assert await drain(session, "provision") == 0  # not due yet
    assert await orders.has_queued(session, service.id)

    # The data runs out early: sync sees LIMITED and the renewal runs now, with a fresh volume.
    fake_user = env["fake"].users[service.panel_username]
    fake_user["used_traffic"] = fake_user["data_limit"]
    await sync.sync_panel(session, env["panel"])
    await session.refresh(job)
    assert job.run_at <= utcnow()
    renewed = await _service(session, renewal)
    assert renewed.status == ServiceStatus.ACTIVE
    assert renewed.used_bytes == 0
    assert renewed.data_limit == 50 * GB


async def test_ban_disables_and_unban_restores_only_ban_disabled(session, env) -> None:  # type: ignore[no-untyped-def]
    cust, plan = env["cust"], env["plan"]
    first = await _service(session, await _buy(session, cust, OrderKind.PURCHASE, plan=plan))
    second = await _service(session, await _buy(session, cust, OrderKind.PURCHASE, plan=plan))
    await lifecycle.set_enabled(session, second, False, "staff")
    await session.commit()
    from paurubot.audit.log import Actor

    await customers.set_ban(session, cust, True, "spam", Actor.system())
    await session.commit()
    await drain(session, "bulk")
    await session.refresh(first)
    assert first.status == ServiceStatus.DISABLED and first.disabled_reason == "ban"
    assert env["fake"].users[first.panel_username]["status"] == "disabled"

    await customers.set_ban(session, cust, False, None, Actor.system())
    await session.commit()
    await drain(session, "bulk")
    await session.refresh(first)
    await session.refresh(second)
    assert first.status == ServiceStatus.ACTIVE and first.disabled_reason is None
    assert second.status == ServiceStatus.DISABLED and second.disabled_reason == "staff"


async def test_branded_subscription_link(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "public_url", "https://bot.example.com")
    service = await _service(session, await _buy(session, env["cust"], OrderKind.PURCHASE, plan=env["plan"]))
    cfg = await shop_settings.load(session, env["shop"].id)
    link = subscription.customer_link(service, cfg)
    assert link == f"https://bot.example.com/s/{service.sub_token}"
    await shop_settings.set_many(session, env["shop"].id, {"branded_sub_links": False})
    cfg = await shop_settings.load(session, env["shop"].id, fresh=True)
    assert subscription.customer_link(service, cfg) == service.subscription_url
    await session.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        assert (await client.get("/s/short")).status_code == 404
        assert (await client.get("/s/" + "A" * 32)).status_code == 404

        app_resp = await client.get(f"/s/{service.sub_token}", headers={"user-agent": "v2rayNG/1.9.1"})
        assert app_resp.status_code == 200
        assert base64.b64decode(app_resp.content).startswith(b"vless://")
        assert "subscription-userinfo" in app_resp.headers
        assert "x-panel-secret" not in app_resp.headers
        title = app_resp.headers["profile-title"]
        assert base64.b64decode(title.removeprefix("base64:")).decode() == env["shop"].title
        assert env["fake"].sub_agents[-1] == "v2rayNG/1.9.1"

        page = await client.get(
            f"/s/{service.sub_token}", headers={"user-agent": "Mozilla/5.0", "accept": "text/html"}
        )
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        assert "hiddify://import/" in page.text
        assert "style=" not in page.text  # the CSP forbids inline styles
        assert (await client.get("/s/_/page.css")).headers["content-type"].startswith("text/css")

        # The redesigned page: four tabs, a search box, the ring and one row per config.
        for fragment in ('id="home"', 'id="configs"', 'id="share"', 'id="apps"', 'class="nav"'):
            assert fragment in page.text
        assert "/s/_/sub.css" in page.text and 'id="sq"' in page.text and 'id="rp"' in page.text
        assert "vless://" in page.text  # the browser page shows the config list
        qr_link = await client.get(f"/s/{service.sub_token}/qr.png")
        assert qr_link.status_code == 200 and qr_link.headers["content-type"] == "image/png"
        assert (await client.get(f"/s/{service.sub_token}/qr.png?i=0")).status_code == 200
        assert (await client.get(f"/s/{service.sub_token}/qr.png?i=9")).status_code == 404
        assert (await client.get(f"/s/{service.sub_token}/qr.png?i=x")).status_code == 404
        assert (await client.get("/s/_/font400.ttf")).status_code == 200
        assert (await client.get("/s/_/nope.ttf")).status_code == 404

        # Revoking issues a new token, so the old branded link stops working.
        old = service.sub_token
        await lifecycle.revoke(session, service)
        await session.commit()
        assert service.sub_token != old
        assert (await client.get(f"/s/{old}", headers={"user-agent": "v2rayNG"})).status_code == 404


def test_app_link_validation() -> None:
    assert subscription.validate_app_links([{"name": "Hiddify", "url": "hiddify://import/{url}"}])
    for bad in (
        [{"name": "x", "url": "javascript://alert(1)"}],
        [{"name": "", "url": "hiddify://x"}],
        [{"name": "x", "url": "not a url"}],
        "nope",
    ):
        with pytest.raises(ValueError):
            subscription.validate_app_links(bad)
    buttons = subscription.app_buttons({}, "https://bot.example.com/s/abc", "My Shop")
    assert buttons[0]["url"].startswith("hiddify://import/https%3A%2F%2Fbot.example.com%2Fs%2Fabc")
    assert subscription.wants_page("Mozilla/5.0", "text/html,application/xhtml+xml")
    assert not subscription.wants_page("Hiddify/2.0", "text/html")
    assert not subscription.wants_page("Mozilla/5.0", "*/*")
