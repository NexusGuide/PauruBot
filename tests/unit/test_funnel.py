"""Sales funnel report (ADM-06)."""

from __future__ import annotations

from datetime import timedelta

from paurubot.core.reports import funnel
from paurubot.db.base import utcnow
from paurubot.db.models import Order, OrderKind, OrderStatus, TrialGrant
from tests import factories


async def test_funnel_counts_each_step(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    people = [await factories.customer(session, shop) for _ in range(4)]
    people[0].phone = "989120000000"
    session.add(TrialGrant(shop_id=shop.id, customer_id=people[1].id, status="issued"))
    now = utcnow()
    for customer, count in ((people[1], 1), (people[2], 3)):
        for _ in range(count):
            session.add(
                Order(
                    shop_id=shop.id,
                    customer_id=customer.id,
                    kind=OrderKind.PURCHASE,
                    status=OrderStatus.COMPLETED,
                    amount=50_000,
                    completed_at=now,
                )
            )
    session.add(
        Order(
            shop_id=shop.id,
            customer_id=people[3].id,
            kind=OrderKind.TRIAL,
            status=OrderStatus.COMPLETED,
            amount=0,
            completed_at=now,
        )
    )
    await session.flush()
    data = await funnel(session, shop.id, now - timedelta(days=1), now + timedelta(days=1))
    assert {s["key"]: s["count"] for s in data["steps"]} == {
        "joined": 4,
        "phone": 1,
        "trial": 1,
        "first_purchase": 2,
        "repeat": 1,
    }
    assert data["days_to_first"] is not None and data["days_to_first"] < 1
    empty = await funnel(session, shop.id, now - timedelta(days=9), now - timedelta(days=8))
    assert empty["steps"][0]["count"] == 0 and empty["days_to_first"] is None
    scoped = await funnel(session, shop.id, now - timedelta(days=1), now + timedelta(days=1), [people[2].id])
    assert [s["count"] for s in scoped["steps"]] == [1, 0, 0, 1, 1]
