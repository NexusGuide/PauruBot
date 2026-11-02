"""SEC-F09 (rest): changes made by people own the service like background work does.

Staff enable/disable/extend/delete/revoke and the customer's own revoke or delete ran without the
service lease. `extend` computes absolute values from the loaded row, so a gift landing while a
renewal was being applied could write the old end date plus the gift over the renewal. Adding
time or data also switched a banned or voided service back on.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot import kv
from paurubot.db.base import utcnow
from paurubot.db.models import OrderKind, PanelKind, Service, ServiceStatus
from paurubot.panels import GB, registry
from paurubot.provisioning import lifecycle
from paurubot.worker import queue
from paurubot.worker.runner import Defer
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.unit import test_provisioning
from tests.unit.test_provisioning import _buy, _setup, drain

fakes = test_provisioning.fakes
isolate_queue = test_provisioning.isolate_queue


async def _service(session, fakes):  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session, fakes)
    order = await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    await drain(session, "provision")
    await session.refresh(order)
    service = await session.get(Service, order.service_id)
    assert service is not None and service.expire_at is not None
    return shop, service, fake


async def test_a_gift_during_a_renewal_builds_on_the_renewal(engine, fakes) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as setup:  # committed for real: the staff and renewal sessions see it
        shop = await factories.shop(setup)
        panel = await factories.panel(setup, shop, PanelKind.MARZBAN)
        fake = FakeTokenPanel("marzban")
        registry.test_transports[panel.id] = transport(fake)
        cust = await factories.customer(setup, shop)
        expire = utcnow().replace(microsecond=0) + timedelta(days=10)
        service = Service(
            shop_id=shop.id,
            customer_id=cust.id,
            panel_id=panel.id,
            panel_username="u_gift_race",
            status=ServiceStatus.ACTIVE,
            data_limit=10 * GB,
            expire_at=expire,
        )
        setup.add(service)
        fake.users["u_gift_race"] = {
            "username": "u_gift_race",
            "status": "active",
            "used_traffic": 0,
            "data_limit": 10 * GB,
            "expire": int(expire.timestamp()),
            "sub": "s",
        }
        await setup.commit()
    service_id, stale = service.id, service.expire_at
    renewed = stale + timedelta(days=30)
    async with maker() as staff:
        row = await staff.get(Service, service_id)  # the staff copy, loaded before the renewal lands
        async with kv.lease(lifecycle.service_lease(service_id), ttl=10):  # a renewal is running
            gift = asyncio.create_task(_extend(staff, row))
            await asyncio.sleep(0.3)
            assert not gift.done()  # the gift waits for the renewal
            async with maker() as renewal:
                await renewal.execute(
                    update(Service).where(Service.id == service_id).values(expire_at=renewed)
                )
                await renewal.commit()
        await asyncio.wait_for(gift, 10)
    async with maker() as check:
        final = await check.get(Service, service_id)
        assert final is not None and final.expire_at == renewed + timedelta(
            days=2
        )  # renewal kept, gift on top
    assert fake.users[service.panel_username]["expire"] == int((renewed + timedelta(days=2)).timestamp())


async def _extend(staff, row) -> None:  # type: ignore[no-untyped-def]
    async with lifecycle.exclusive(staff, row):
        await lifecycle.extend(staff, row, days=2)
    await staff.commit()


async def test_a_busy_service_says_so(session, fakes, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop, service, fake = await _service(session, fakes)
    await session.commit()
    monkeypatch.setattr(lifecycle, "EXCLUSIVE_WAIT", 0.2)
    async with kv.lease(lifecycle.service_lease(service.id), ttl=10):
        with pytest.raises(lifecycle.ServiceBusy):
            async with lifecycle.exclusive(session, service):
                pass
        from paurubot.worker import jobs

        job = queue.Claimed(
            id=0,
            kind="service",
            payload={"service_id": service.id, "action": "extend", "days": 1},
            attempts=1,
            max_attempts=5,
            shop_id=shop.id,
            token="t",
        )
        with pytest.raises(Defer):
            await jobs.bulk_service(session, job)


async def test_more_time_never_switches_a_banned_service_back_on(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, service, fake = await _service(session, fakes)
    async with lifecycle.exclusive(session, service):
        await lifecycle.set_enabled(session, service, False, "ban")
    await session.commit()
    async with lifecycle.exclusive(session, service):
        await lifecycle.extend(session, service, days=3, gb=1)
    await session.commit()
    assert service.status == ServiceStatus.DISABLED and service.disabled_reason == "ban"
    assert fake.users[service.panel_username]["status"] == "disabled"
    registry.test_transports.clear()
