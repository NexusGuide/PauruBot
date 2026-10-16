"""Reseller ranking score behavior."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import update

from paurubot.core import resale
from paurubot.core import tickets as tickets_core
from paurubot.core.reseller_ranking import rankings, score
from paurubot.db.base import utcnow
from paurubot.db.models import Ticket
from tests.unit.test_reseller_bot import TOKEN, _sell
from tests.unit.test_reseller_bot import env as env  # noqa: F401


def test_reseller_score_rewards_sales_and_revenue() -> None:
    assert score(10, 1_000_000, 0) == 110
    assert score(10, 1_000_000, 2) == 70


def test_reseller_score_never_goes_below_zero() -> None:
    assert score(0, 0, 1_000) == 0


async def test_only_confirmed_complaints_in_the_window_count(session, env) -> None:  # type: ignore[no-untyped-def]
    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    await session.commit()
    buyer, _order = await _sell(session, child, env["plan"], 63210, 96_000)
    await tickets_core.open_ticket(session, buyer, "How do I connect?")
    confirmed = await tickets_core.open_ticket(session, buyer, "Service keeps dropping")
    await tickets_core.update(session, confirmed, complaint=True)
    stale = await tickets_core.open_ticket(session, buyer, "Old confirmed complaint")
    await tickets_core.update(session, stale, complaint=True)
    stale_id = stale.id
    await session.commit()
    await session.execute(
        update(Ticket).where(Ticket.id == stale_id).values(created_at=utcnow() - timedelta(days=40))
    )
    await session.commit()

    all_time = (await rankings(session, env["shop"].id))[0]
    assert (all_time.sales, all_time.complaints) == (1, 2)

    recent = (await rankings(session, env["shop"].id, days=30))[0]
    assert (recent.sales, recent.complaints) == (1, 1)
