"""ClockBot parity: each reseller gets its own nightly figures; off by default."""

from __future__ import annotations

from datetime import timedelta

from paurubot.core import reports
from paurubot.db.base import utcnow
from paurubot.db.models import Order, OrderKind, OrderStatus, ResellerProfile
from tests import factories


async def test_a_resellers_own_day(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    reseller = await factories.customer(session, shop, telegram_id=66001234)
    other = await factories.customer(session, shop, telegram_id=66005678)
    profile = ResellerProfile(shop_id=shop.id, customer_id=reseller.id)
    session.add(profile)
    now = utcnow()
    for who, amount, status, when in (
        (reseller, 100_000, OrderStatus.COMPLETED, now - timedelta(hours=2)),
        (reseller, 50_000, OrderStatus.COMPLETED, now - timedelta(hours=3)),
        (reseller, 70_000, OrderStatus.CANCELED, now - timedelta(hours=1)),
        (reseller, 90_000, OrderStatus.COMPLETED, now - timedelta(days=2)),
        (other, 40_000, OrderStatus.COMPLETED, now - timedelta(hours=1)),
    ):
        order = Order(
            shop_id=shop.id,
            customer_id=who.id,
            kind=OrderKind.PURCHASE,
            status=status,
            base_price=amount,
            amount=amount,
            completed_at=when,
        )
        session.add(order)
    await session.flush()
    day = await reports.reseller_day(session, profile, now - timedelta(days=1), now)
    assert (day.orders, day.spent) == (2, 150_000)
    assert day.balance == 0 and day.bot_orders == 0 and day.bot_revenue == 0


async def test_resellers_get_theirs_once_even_with_the_staff_report_off(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot import kv
    from paurubot.config import get_settings
    from paurubot.core import settings as shop_settings
    from paurubot.notifications import telegram
    from paurubot.worker import jobs

    shop = await factories.shop(session)
    reseller = await factories.customer(session, shop, telegram_id=66009999)
    session.add(ResellerProfile(shop_id=shop.id, customer_id=reseller.id))
    hour = (utcnow().astimezone(get_settings().tz) - timedelta(hours=2)).hour
    await shop_settings.set_many(
        session,
        shop.id,
        {"daily_report_hour": hour, "daily_report_enabled": False, "reseller_daily_report": True},
    )
    await session.commit()
    sent: list[int] = []

    async def capture(chat_id, text, **kwargs):  # type: ignore[no-untyped-def]
        sent.append(chat_id)

    monkeypatch.setattr(telegram, "send_text", capture)
    await kv.get_redis().delete(
        *[k async for k in kv.get_redis().scan_iter(f"report:*:{shop.id}:*")] or ["-"]
    )
    await jobs.daily_report()
    await jobs.daily_report()  # the next 5-minute run: already sent
    assert sent == [66009999]
