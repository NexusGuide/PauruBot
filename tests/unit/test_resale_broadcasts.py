"""Reseller broadcast scope and daily safety cap."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
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


async def test_capacity_locks_the_shop_before_counting(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    events: list[str] = []

    async def get(model: object, row_id: int, *, with_for_update: bool) -> object:
        assert row_id == 7 and with_for_update
        events.append("locked")
        return object()

    async def remaining(session: object, row_id: int) -> int:
        events.append("counted")
        return 1

    monkeypatch.setattr(resale_broadcasts, "remaining_today", remaining)
    session = SimpleNamespace(get=AsyncMock(side_effect=get))
    assert await resale_broadcasts.lock_capacity(session, 7)  # type: ignore[arg-type]
    assert events == ["locked", "counted"]
