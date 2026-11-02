"""REF-09: staff see a ticket's service as the panel shows it now, not a guess."""

from __future__ import annotations

import httpx
from aiogram.methods import SendMessage
from sqlalchemy import select

from paurubot.core import ticket_media, tickets
from paurubot.db.models import LogChannel, OrderKind, Service, Ticket
from paurubot.i18n import Texts
from paurubot.panels import GB, registry
from paurubot.worker import queue
from tests.bot import test_notifications
from tests.bot.test_notifications import drain
from tests.unit.test_provisioning import _buy, _setup

tg = test_notifications.tg
FA = Texts("fa")


async def _ticket_on_service(session, fakes_store):  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session, fakes_store)
    session.add(LogChannel(shop_id=shop.id, topic="tickets", chat_id=-100555))
    order = await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    await drain(session, "provision")
    await session.refresh(order)
    service = await session.get(Service, order.service_id)
    assert service is not None
    ticket = await tickets.open_ticket(session, cust, "no internet", source="webapp")
    ticket.service_id = service.id
    ticket.service_context = ticket_media.service_context(service)  # what the Mini App stores
    await tickets.add_message(session, ticket, author_type="customer", author_id=cust.id, text="help")
    await session.commit()
    return shop, panel, service, ticket, fake


async def _notify(session, ticket) -> str:  # type: ignore[no-untyped-def]
    await queue.enqueue(
        session, "notify", "ticket_new_message", {"ticket_id": ticket.id}, shop_id=ticket.shop_id
    )
    await session.commit()
    await drain(session, "notify")


async def test_the_notice_carries_the_live_service(session, tg) -> None:  # type: ignore[no-untyped-def]
    store: dict[int, object] = {}
    shop, panel, service, ticket, fake = await _ticket_on_service(session, store)
    fake.users[service.panel_username]["used_traffic"] = 7 * GB  # the customer used more since the sync
    await _notify(session, ticket)
    sent = [c.text for c in tg.calls if isinstance(c, SendMessage) and c.chat_id == -100555]
    assert sent and "7" in sent[-1] and FA("adm_ticket_service_stored") not in sent[-1]
    row = await session.scalar(
        select(Ticket).where(Ticket.id == ticket.id).execution_options(populate_existing=True)
    )
    assert row.service_context["checked"] == "live" and row.service_context["used_bytes"] == 7 * GB
    assert "password" not in row.service_context and "subscription_url" not in row.service_context
    registry.test_transports.clear()


async def test_an_unreachable_panel_is_said_not_hidden(session, tg) -> None:  # type: ignore[no-untyped-def]
    store: dict[int, object] = {}
    shop, panel, service, ticket, fake = await _ticket_on_service(session, store)
    # A department with its own chat and a staff member reached directly: both still get the notice.
    from paurubot.auth.staff import create_staff
    from paurubot.db.models import StaffRole, TicketDepartment

    department = TicketDepartment(shop_id=shop.id, title="Tech", log_chat_id=-100666)
    session.add(department)
    await session.flush()
    ticket.department_id = department.id
    await create_staff(
        session, shop.id, role=StaffRole.SUPPORT, username="tech1", password=None, telegram_id=88123
    )
    await session.commit()
    registry.test_transports[panel.id] = httpx.MockTransport(lambda _r: httpx.Response(503, text="down"))
    registry._cache.clear()
    await _notify(session, ticket)
    sent = [c.text for c in tg.calls if isinstance(c, SendMessage) and c.chat_id in (-100666, 88123)]
    assert len(sent) == 2 and all(FA("adm_ticket_service_down") in text for text in sent)
    row = await session.scalar(
        select(Ticket).where(Ticket.id == ticket.id).execution_options(populate_existing=True)
    )
    assert row.service_context["panel_down"] is True and row.service_context["checked"] == "stored"
