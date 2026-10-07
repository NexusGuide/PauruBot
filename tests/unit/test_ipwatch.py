"""Device cut-off notices (JOB-03)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, update

from paurubot.core import orders
from paurubot.core import settings as shop_settings
from paurubot.db.models import Job, JobStatus, OrderKind, PanelKind, Service
from paurubot.ledger import wallet
from paurubot.notifications import handlers  # noqa: F401  (registers the notify queue)
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from paurubot.panels.base import PanelRef, parse_ips
from paurubot.provisioning import ipwatch
from tests import factories
from tests.contract.test_drivers import make
from tests.fakes.panels import FakeXuiPanel, transport
from tests.fakes.telegram import make_bot
from tests.unit.test_provisioning import drain


def test_parse_ips_accepts_every_panel_shape() -> None:
    assert parse_ips(["1.2.3.4 (2026-10-07 10:00)", {"ip": "5.6.7.8"}, "1.2.3.4", "junk", 5]) == [
        "1.2.3.4",
        "5.6.7.8",
    ]
    assert parse_ips("9.9.9.9\n2001:db8::1 (x)") == ["9.9.9.9", "2001:db8::1"]
    assert parse_ips('["8.8.8.8"]') == ["8.8.8.8"]
    assert parse_ips("No IP Record") == [] and parse_ips(None) == [] and parse_ips("[broken") == []


def test_mask_and_replacement_rules() -> None:
    assert ipwatch.mask("185.12.33.7") == "185.12.33.x"
    assert ipwatch.mask("2001:db8:1:2::9") == "2001:0db8:0001:…"
    assert ipwatch.mask("nope") == "?"
    assert ipwatch.replaced([], ["1.1.1.1"]) == ([], [])  # first look: nothing to compare
    assert ipwatch.replaced(["1.1.1.1"], ["1.1.1.1", "2.2.2.2"]) == ([], [])  # just one more device
    assert ipwatch.replaced(["1.1.1.1"], []) == ([], [])  # went offline
    assert ipwatch.replaced(["1.1.1.1", "2.2.2.2"], ["2.2.2.2", "3.3.3.3"]) == (["1.1.1.1"], ["3.3.3.3"])


async def test_xui_and_rebecca_report_addresses() -> None:
    driver, fake = make("xui")
    fake.ips["u1"] = ["10.0.0.1", "10.0.0.2"]
    assert await driver.client_ips(PanelRef("u1")) == ["10.0.0.1", "10.0.0.2"]
    assert await driver.client_ips(PanelRef("nobody")) == []
    rebecca, rfake = make("rebecca")
    rfake.sources = {"u1": ["7.7.7.7"], "u2": ["8.8.8.8"]}
    assert await rebecca.client_ips(PanelRef("u1")) == ["7.7.7.7"]
    marzban, _ = make("marzban")
    assert not marzban.supports_ips


@pytest.fixture
async def xui_service(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session)
    panel = await factories.panel(
        session, shop, PanelKind.XUI, inbound_id=3, sub_base_url="https://sub.example.com/sub/"
    )
    fake = FakeXuiPanel()
    registry.test_transports[panel.id] = transport(fake)
    plan = await factories.plan(session, shop, panel, price=50_000)
    plan.ip_limit = 2
    cust = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, cust.id, 100_000, provider="card", payment_id=cust.id)
    order = await orders.create(
        session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    )
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    await drain(session, "provision")
    service = await session.scalar(select(Service).where(Service.customer_id == cust.id))
    yield shop, service, fake
    registry.test_transports.clear()
    registry._cache.clear()


async def _notices(session) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    rows = await session.scalars(
        select(Job).where(Job.queue == "notify", Job.kind == "ip_replaced", Job.status == JobStatus.QUEUED)
    )
    return [row.payload for row in rows]


async def test_customer_hears_when_a_device_is_pushed_out(session, xui_service) -> None:  # type: ignore[no-untyped-def]
    shop, service, fake = xui_service
    email = service.panel_username
    fake.ips[email] = ["10.0.0.1", "10.0.0.2"]
    assert await ipwatch.run(session) == 0  # first look only remembers
    await session.refresh(service)
    assert service.client_ips == ["10.0.0.1", "10.0.0.2"]

    fake.ips[email] = ["10.0.0.2", "10.0.0.9"]
    assert await ipwatch.run(session) == 1
    [notice] = await _notices(session)
    assert notice["dropped"] == ["10.0.0.x"] and notice["arrived"] == ["10.0.0.x"] and notice["limit"] == 2

    fake.ips[email] = ["10.0.0.9", "10.0.0.5"]
    assert await ipwatch.run(session) == 0  # at most one notice an hour
    await session.refresh(service)
    assert service.client_ips == ["10.0.0.9", "10.0.0.5"]

    bot, tg = make_bot()
    notify_telegram.set_bot(bot)
    try:
        await drain(session, "notify")
    finally:
        notify_telegram.set_bot(None)
    sent = tg.texts(service.customer.telegram_id)
    assert any("10.0.0.x" in text and service.panel_username in text for text in sent)


async def test_switched_off_per_shop(session, xui_service) -> None:  # type: ignore[no-untyped-def]
    shop, service, fake = xui_service
    await shop_settings.set_many(session, shop.id, {"ip_notice_enabled": False})
    await session.commit()
    shop_settings.invalidate(shop.id)
    fake.ips[service.panel_username] = ["10.0.0.1"]
    assert await ipwatch.watched(session) == []
