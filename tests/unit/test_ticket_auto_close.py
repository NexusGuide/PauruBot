"""Tickets close by themselves after a quiet spell, when the shop asks for it: only tickets the staff
answered last and the customer left alone for the set number of days. Off by default."""

from __future__ import annotations

from datetime import timedelta

import pytest

from paurubot.core import settings as shop_settings
from paurubot.core import tickets
from paurubot.db.base import utcnow
from paurubot.db.models import Ticket, TicketStatus
from tests import factories


async def _ticket(session, shop, customer, *, status, staff_days=None, customer_days=None):  # type: ignore[no-untyped-def]
    now = utcnow()
    row = Ticket(
        shop_id=shop.id,
        customer_id=customer.id,
        subject="help",
        status=status,
        last_staff_at=now - timedelta(days=staff_days) if staff_days is not None else None,
        last_customer_at=now - timedelta(days=customer_days) if customer_days is not None else None,
    )
    session.add(row)
    await session.flush()
    return row


async def test_answered_tickets_left_quiet_close_after_the_set_days(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    old = await _ticket(session, shop, customer, status=TicketStatus.ANSWERED, staff_days=5, customer_days=6)
    fresh = await _ticket(
        session, shop, customer, status=TicketStatus.ANSWERED, staff_days=1, customer_days=2
    )
    waiting = await _ticket(session, shop, customer, status=TicketStatus.OPEN, staff_days=9, customer_days=8)
    await session.commit()

    assert await tickets.auto_close(session) == 0, "off by default: nothing closes"
    await session.refresh(old)
    assert old.status == TicketStatus.ANSWERED

    await shop_settings.set_many(session, shop.id, {"ticket_auto_close_days": 3})
    await session.commit()
    assert await tickets.auto_close(session) == 1
    for row in (old, fresh, waiting):
        await session.refresh(row)
    assert old.status == TicketStatus.CLOSED
    assert fresh.status == TicketStatus.ANSWERED, "answered only a day ago"
    assert waiting.status == TicketStatus.OPEN, "the customer is waiting on us: never closed for us"
    assert await tickets.auto_close(session) == 0, "a second run finds nothing more"


async def test_the_setting_is_bounded(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    for bad in (-1, 366):
        with pytest.raises(shop_settings.SettingError):
            await shop_settings.set_many(session, shop.id, {"ticket_auto_close_days": bad})
    await shop_settings.set_many(session, shop.id, {"ticket_auto_close_days": 0})
