"""Support tickets between customers and staff (SUP-01)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Ticket, TicketMessage, TicketStatus

MAX_OPEN_PER_CUSTOMER = 3


class TicketError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


async def open_ticket(session: AsyncSession, customer: Customer, subject: str, source: str = "bot") -> Ticket:
    open_count = await session.scalar(
        select(func.count(Ticket.id)).where(
            Ticket.customer_id == customer.id, Ticket.status != TicketStatus.CLOSED
        )
    )
    if int(open_count or 0) >= MAX_OPEN_PER_CUSTOMER:
        raise TicketError("ticket_limit")
    ticket = Ticket(
        shop_id=customer.shop_id, customer_id=customer.id, subject=subject.strip()[:128] or "-", source=source
    )
    session.add(ticket)
    await session.flush()
    return ticket


async def add_message(
    session: AsyncSession,
    ticket: Ticket,
    *,
    author_type: str,
    author_id: int | None,
    text: str | None,
    attachment_file_id: str | None = None,
    attachment_kind: str | None = None,
) -> TicketMessage:
    if ticket.status == TicketStatus.CLOSED:
        raise TicketError("ticket_closed")
    message = TicketMessage(
        ticket_id=ticket.id,
        author_type=author_type,
        author_id=author_id,
        text=(text or "")[:4000] or None,
        attachment_file_id=attachment_file_id,
        attachment_kind=attachment_kind,
    )
    session.add(message)
    ticket.status = TicketStatus.ANSWERED if author_type == "staff" else TicketStatus.OPEN
    ticket.updated_at = utcnow()
    await session.flush()
    return message


async def close(session: AsyncSession, ticket: Ticket) -> None:
    ticket.status = TicketStatus.CLOSED
    ticket.updated_at = utcnow()
    await session.flush()


async def for_customer(session: AsyncSession, customer: Customer, limit: int = 10) -> list[Ticket]:
    stmt = (
        select(Ticket)
        .where(Ticket.customer_id == customer.id)
        .order_by(Ticket.updated_at.desc())
        .limit(limit)
    )
    return list(await session.scalars(stmt))


async def messages(session: AsyncSession, ticket_id: int, limit: int = 30) -> list[TicketMessage]:
    stmt = (
        select(TicketMessage)
        .where(TicketMessage.ticket_id == ticket_id)
        .order_by(TicketMessage.id.desc())
        .limit(limit)
    )
    return list(reversed(list(await session.scalars(stmt))))
