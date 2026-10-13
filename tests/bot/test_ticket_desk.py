"""F-14 through the real dispatcher: departments, internal notes, canned replies and priority."""

from __future__ import annotations

from sqlalchemy import func, select

from paurubot.bot.callbacks import Admin
from paurubot.bot.callbacks import Ticket as TicketCb
from paurubot.db.models import (
    CannedReply,
    Customer,
    Job,
    Ticket,
    TicketDepartment,
    TicketMessage,
    TicketStatus,
)
from tests.bot.test_admin_tools import FA, OWNER, env  # noqa: F401  (shared fixture)
from tests.fakes.telegram import callback_update, message_update
from tests.unit.test_resellers import drain

CUSTOMER = 7711


def _buttons(fake) -> list[str]:  # type: ignore[no-untyped-def]
    labels = []
    for call in fake.calls:
        markup = getattr(call, "reply_markup", None)
        for row in getattr(markup, "inline_keyboard", None) or []:
            labels.extend(button.text for button in row)
    return labels


def _texts(fake) -> list[str]:  # type: ignore[no-untyped-def]
    return [str(getattr(c, "text", "") or getattr(c, "caption", "") or "") for c in fake.calls]


async def test_ticket_desk_flow(session, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    await send(message_update(CUSTOMER, "/start"))
    customer = await session.scalar(select(Customer).where(Customer.telegram_id == CUSTOMER))
    assert customer is not None
    desk = TicketDepartment(shop_id=customer.shop_id, title="Sales desk")
    closed_desk = TicketDepartment(shop_id=customer.shop_id, title="Old desk", is_active=False)
    session.add_all([desk, closed_desk])
    await session.flush()
    canned = CannedReply(shop_id=customer.shop_id, title="Greeting", text="Hello {name}, ticket {ticket}")
    session.add(canned)
    await session.commit()

    # The customer picks a department before writing (inactive desks are not offered).
    fake.reset()
    await send(callback_update(CUSTOMER, TicketCb(action="new").pack()))
    offered = _buttons(fake)
    assert "Sales desk" in offered and "Old desk" not in offered
    await send(callback_update(CUSTOMER, TicketCb(action="dept", id=closed_desk.id).pack()))
    await send(callback_update(CUSTOMER, TicketCb(action="dept", id=desk.id).pack()))
    await send(message_update(CUSTOMER, "Billing"))
    await send(message_update(CUSTOMER, "Please help"))
    ticket = await session.scalar(select(Ticket).where(Ticket.customer_id == customer.id))
    assert ticket is not None
    assert ticket.department_id == desk.id and ticket.priority == "normal"
    assert ticket.last_customer_at is not None and ticket.last_staff_at is None

    fake.reset()
    await drain(session, "notify")
    staff_alerts = [c for c in fake.calls if getattr(c, "chat_id", None) == OWNER]
    assert any("Sales desk" in str(getattr(c, "text", "")) for c in staff_alerts)

    # An internal note: saved, staff-only, no status change and nothing sent to the customer.
    await send(callback_update(OWNER, Admin(section="ticket", action="note", id=ticket.id).pack()))
    await send(message_update(OWNER, "VIP, be quick"))
    note = await session.scalar(select(TicketMessage).where(TicketMessage.is_internal.is_(True)))
    assert note is not None and note.text == "VIP, be quick"
    await session.refresh(ticket)
    assert ticket.status == TicketStatus.OPEN and ticket.last_staff_at is None
    replies = await session.scalar(select(func.count(Job.id)).where(Job.kind == "ticket_reply"))
    assert int(replies or 0) == 0

    fake.reset()
    await send(callback_update(CUSTOMER, TicketCb(action="view", id=ticket.id).pack()))
    assert not any("VIP, be quick" in text for text in _texts(fake))
    fake.reset()
    await send(callback_update(OWNER, Admin(section="ticket", id=ticket.id).pack()))
    assert any("VIP, be quick" in text for text in _texts(fake))  # staff see their notes

    # A canned reply goes out with the placeholders filled in.
    await send(callback_update(OWNER, Admin(section="tcanned", id=ticket.id, page=canned.id).pack()))
    await session.refresh(ticket)
    assert ticket.status == TicketStatus.ANSWERED and ticket.last_staff_at is not None
    last = await session.scalar(
        select(TicketMessage).where(TicketMessage.ticket_id == ticket.id).order_by(TicketMessage.id.desc())
    )
    assert last is not None and last.text == f"Hello {customer.display_name}, ticket {ticket.id}"
    assert not last.is_internal

    await send(callback_update(OWNER, Admin(section="ticket", action="prio", id=ticket.id).pack()))
    await session.refresh(ticket)
    assert ticket.priority == "high"
