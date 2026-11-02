"""REF-05: a newer reminder about a service replaces the older one, and offers a top-up."""

from __future__ import annotations

from aiogram.methods import DeleteMessage, SendMessage

from paurubot.db.models import Service
from paurubot.worker import queue
from tests import factories
from tests.bot import test_notifications
from tests.bot.test_notifications import drain

tg = test_notifications.tg


def _deleted(fake) -> list[DeleteMessage]:  # type: ignore[no-untyped-def]
    return [call for call in fake.calls if isinstance(call, DeleteMessage)]


async def _alert(session, service, alert: str, **extra) -> None:  # type: ignore[no-untyped-def]
    await queue.enqueue(
        session,
        "notify",
        "service_alert",
        {"service_id": service.id, "alert": alert, **extra},
        shop_id=service.shop_id,
    )
    await session.commit()
    await drain(session, "notify")


async def test_a_newer_reminder_replaces_the_older_one(session, tg) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    cust = await factories.customer(session, shop, telegram_id=7702)
    home, office = (
        Service(shop_id=shop.id, customer_id=cust.id, panel_id=panel.id, panel_username=name, label=name)
        for name in ("home", "office")
    )
    session.add_all([home, office])
    await session.flush()
    await _alert(session, home, "low_volume", remaining=10)
    first = tg.last(SendMessage)
    assert any("w:topup" in (cb or "") for _, cb in tg.buttons(first))
    assert _deleted(tg) == []
    await _alert(session, office, "expiring", days=2)  # another service: nothing is replaced
    assert _deleted(tg) == []
    await _alert(session, home, "expiring", days=1)
    deleted = _deleted(tg)
    assert len(deleted) == 1 and deleted[0].chat_id == 7702
    sent = [call for call in tg.calls if isinstance(call, SendMessage)]
    assert len(sent) == 3
