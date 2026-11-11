"""ClockBot parity: staff hear once when a panel fills past the shop's warning percent of its
capacity, and again only after it drops back below."""

from __future__ import annotations

from sqlalchemy import select, update

from paurubot.core import capacity
from paurubot.core import settings as shop_settings
from paurubot.db.models import Job, JobStatus, Service, ServiceStatus
from tests import factories


async def _alerts(session) -> list[Job]:  # type: ignore[no-untyped-def]
    rows = await session.scalars(select(Job).where(Job.kind == "panel_alert", Job.status == JobStatus.QUEUED))
    return [j for j in rows if j.payload.get("event") == "capacity"]


async def test_warn_once_when_a_panel_fills_up(session) -> None:  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    panel.capacity = 10
    customer = await factories.customer(session, shop)
    for i in range(8):
        session.add(
            Service(
                shop_id=shop.id,
                customer_id=customer.id,
                panel_id=panel.id,
                panel_username=f"c{i}",
                status=ServiceStatus.ACTIVE,
            )
        )
    await session.commit()
    assert await capacity.check(session) == 0  # off by default
    await shop_settings.set_many(session, shop.id, {"capacity_warn_percent": 80})
    await session.commit()
    assert await capacity.check(session) == 1
    [job] = await _alerts(session)
    assert job.payload == {"panel_id": panel.id, "event": "capacity", "percent": 80}
    assert await capacity.check(session) == 0  # once
    await session.execute(
        update(Service).where(Service.panel_username.in_(["c0", "c1"])).values(status=ServiceStatus.DELETED)
    )
    await session.commit()
    assert await capacity.check(session) == 0  # back below: re-armed
    session.add(
        Service(
            shop_id=shop.id,
            customer_id=customer.id,
            panel_id=panel.id,
            panel_username="c9",
            status=ServiceStatus.ACTIVE,
        )
    )
    session.add(
        Service(
            shop_id=shop.id,
            customer_id=customer.id,
            panel_id=panel.id,
            panel_username="c10",
            status=ServiceStatus.ACTIVE,
        )
    )
    await session.commit()
    assert await capacity.check(session) == 1
