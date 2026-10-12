"""Reseller broadcast scope and daily safety cap."""

from __future__ import annotations

from sqlalchemy import select

from paurubot.core import resale_broadcasts
from paurubot.db.models import Broadcast, Customer, JobStatus
from paurubot.notifications import handlers  # noqa: F401
from tests import factories


async def test_reseller_broadcasts_are_scoped_and_capped(session) -> None:  # type: ignore[no-untyped-def]
    parent = await factories.shop(session, "Main")
    child = await factories.shop(session, "Reseller")
    own = await factories.customer(session, child, telegram_id=71001)
    other = await factories.customer(session, parent, telegram_id=71002)
    for _ in range(resale_broadcasts.DAILY_LIMIT):
        session.add(Broadcast(shop_id=child.id, status=JobStatus.QUEUED))
    await session.commit()

    rows = list(
        await session.scalars(
            select(Customer).where(*handlers.audience_conditions(child.id, "all")).order_by(Customer.id)
        )
    )
    assert [row.id for row in rows] == [own.id]
    assert await resale_broadcasts.remaining_today(session, child.id) == 0
    assert await resale_broadcasts.remaining_today(session, parent.id) == resale_broadcasts.DAILY_LIMIT
    assert other.id not in [row.id for row in rows]
