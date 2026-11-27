"""Support tickets between customers and staff (SUP-01), with departments, priorities, internal
notes and canned replies (F-14)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import utcnow
from paurubot.db.models import (
    CannedReply,
    Customer,
    Ticket,
    TicketDepartment,
    TicketMessage,
    TicketStatus,
)

MAX_OPEN_PER_CUSTOMER = 3
PRIORITIES = ("low", "normal", "high", "urgent")
PRIORITY_ICON = {"low": "⚪️", "normal": "🔵", "high": "🟠", "urgent": "🔴"}


class TicketError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


async def departments(
    session: AsyncSession, shop_id: int, *, active_only: bool = True
) -> list[TicketDepartment]:
    stmt = select(TicketDepartment).where(TicketDepartment.shop_id == shop_id)
    if active_only:
        stmt = stmt.where(TicketDepartment.is_active.is_(True))
    return list(await session.scalars(stmt.order_by(TicketDepartment.sort, TicketDepartment.id)))


async def department(
    session: AsyncSession, shop_id: int, department_id: int | None, *, active_only: bool = True
) -> TicketDepartment | None:
    """The shop's department with this id, or None when it is not one of the shop's (open) desks."""
    if not department_id:
        return None
    row = await session.get(TicketDepartment, department_id)
    if row is None or row.shop_id != shop_id or (active_only and not row.is_active):
        return None
    return row


async def open_ticket(
    session: AsyncSession,
    customer: Customer,
    subject: str,
    source: str = "bot",
    *,
    department_id: int | None = None,
    priority: str = "normal",
) -> Ticket:
    open_count = await session.scalar(
        select(func.count(Ticket.id)).where(
            Ticket.customer_id == customer.id, Ticket.status != TicketStatus.CLOSED
        )
    )
    if int(open_count or 0) >= MAX_OPEN_PER_CUSTOMER:
        raise TicketError("ticket_limit")
    if department_id is not None and await department(session, customer.shop_id, department_id) is None:
        raise TicketError("ticket_department_unknown")
    ticket = Ticket(
        shop_id=customer.shop_id,
        customer_id=customer.id,
        subject=subject.strip()[:128] or "-",
        source=source,
        department_id=department_id,
        priority=priority if priority in PRIORITIES else "normal",
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
    internal: bool = False,
) -> TicketMessage:
    """Add a message. An internal note is staff-only: the customer never sees it, it does not change
    the ticket's status and it does not count as an answer."""
    if internal and author_type != "staff":
        raise TicketError("ticket_note_staff_only")
    if ticket.status == TicketStatus.CLOSED and not internal:
        raise TicketError("ticket_closed")
    message = TicketMessage(
        ticket_id=ticket.id,
        author_type=author_type,
        author_id=author_id,
        text=(text or "")[:4000] or None,
        attachment_file_id=attachment_file_id,
        attachment_kind=attachment_kind,
        is_internal=internal,
    )
    session.add(message)
    now = utcnow()
    if not internal:
        ticket.status = TicketStatus.ANSWERED if author_type == "staff" else TicketStatus.OPEN
        if author_type == "staff":
            ticket.last_staff_at = now
        else:
            ticket.last_customer_at = now
    ticket.updated_at = now
    await session.flush()
    return message


def awaiting_staff(ticket: Ticket) -> bool:
    """The customer wrote last and nobody has answered yet."""
    if ticket.status == TicketStatus.CLOSED or ticket.last_customer_at is None:
        return False
    return ticket.last_staff_at is None or ticket.last_staff_at < ticket.last_customer_at


def waiting_since(ticket: Ticket) -> datetime | None:
    return ticket.last_customer_at if awaiting_staff(ticket) else None


async def update(
    session: AsyncSession,
    ticket: Ticket,
    *,
    department_id: int | None = None,
    clear_department: bool = False,
    priority: str | None = None,
    assigned_staff_id: int | None = None,
    unassign: bool = False,
    complaint: bool | None = None,
) -> dict[str, object]:
    """Change a ticket's department, priority, assignee or complaint flag; returns the changed fields."""
    changed: dict[str, object] = {}
    if clear_department and ticket.department_id is not None:
        ticket.department_id = None
        changed["department_id"] = None
    elif department_id is not None and department_id != ticket.department_id:
        if await department(session, ticket.shop_id, department_id, active_only=False) is None:
            raise TicketError("ticket_department_unknown")
        ticket.department_id = department_id
        changed["department_id"] = department_id
    if priority is not None and priority != ticket.priority:
        if priority not in PRIORITIES:
            raise TicketError("ticket_priority_unknown")
        ticket.priority = priority
        changed["priority"] = priority
    if unassign and ticket.assigned_staff_id is not None:
        ticket.assigned_staff_id = None
        changed["assigned_staff_id"] = None
    elif assigned_staff_id is not None and assigned_staff_id != ticket.assigned_staff_id:
        ticket.assigned_staff_id = assigned_staff_id
        changed["assigned_staff_id"] = assigned_staff_id
    if complaint is not None and complaint != ticket.is_complaint:
        ticket.is_complaint = complaint
        changed["is_complaint"] = complaint
    if changed:
        ticket.updated_at = utcnow()
        await session.flush()
    return changed


def next_priority(current: str) -> str:
    """The next priority in the cycle low → normal → high → urgent → low (the bot's button)."""
    index = PRIORITIES.index(current) if current in PRIORITIES else 0
    return PRIORITIES[(index + 1) % len(PRIORITIES)]


async def close(session: AsyncSession, ticket: Ticket) -> None:
    from paurubot import kv

    ticket.status = TicketStatus.CLOSED
    ticket.updated_at = utcnow()
    try:
        await kv.unmark(f"ticket_alerted:{ticket.id}")
    except Exception:
        pass
    await session.flush()


async def reopen(session: AsyncSession, ticket: Ticket) -> bool:
    if ticket.status != TicketStatus.CLOSED:
        return False
    ticket.status = TicketStatus.OPEN
    ticket.updated_at = utcnow()
    await session.flush()
    return True


async def auto_close(session: AsyncSession) -> int:
    """Close answered tickets the customer has left alone for the shop's set number of days.

    Only tickets the staff answered last: one where the customer is waiting on us is never closed
    for us. Off unless a shop sets ``ticket_auto_close_days``. Returns how many were closed."""
    from paurubot.core import settings as shop_settings

    closed = 0
    shops = await session.scalars(
        select(Ticket.shop_id).where(Ticket.status == TicketStatus.ANSWERED).distinct()
    )
    for shop_id in list(shops):
        days = int((await shop_settings.load(session, shop_id)).get("ticket_auto_close_days") or 0)
        if days <= 0:
            continue
        cutoff = utcnow() - timedelta(days=days)
        rows = await session.scalars(
            select(Ticket).where(
                Ticket.shop_id == shop_id,
                Ticket.status == TicketStatus.ANSWERED,
                Ticket.last_staff_at.is_not(None),
                Ticket.last_staff_at <= cutoff,
                or_(Ticket.last_customer_at.is_(None), Ticket.last_customer_at <= Ticket.last_staff_at),
            )
        )
        for ticket in list(rows):
            await close(session, ticket)
            closed += 1
    await session.commit()
    return closed


async def for_customer(session: AsyncSession, customer: Customer, limit: int = 10) -> list[Ticket]:
    stmt = (
        select(Ticket)
        .where(Ticket.customer_id == customer.id)
        .order_by(Ticket.updated_at.desc())
        .limit(limit)
    )
    return list(await session.scalars(stmt))


async def messages(
    session: AsyncSession, ticket_id: int, limit: int = 30, *, include_internal: bool = False
) -> list[TicketMessage]:
    """The latest messages, oldest first. Internal notes only come back for staff views."""
    stmt = select(TicketMessage).where(TicketMessage.ticket_id == ticket_id)
    if not include_internal:
        stmt = stmt.where(TicketMessage.is_internal.is_(False))
    stmt = stmt.order_by(TicketMessage.id.desc()).limit(limit)
    return list(reversed(list(await session.scalars(stmt))))


# --- canned replies -----------------------------------------------------------------------


async def canned_replies(
    session: AsyncSession, shop_id: int, department_id: int | None = None
) -> list[CannedReply]:
    """The shop's saved answers; with a department, its own answers and the shared ones."""
    stmt = select(CannedReply).where(CannedReply.shop_id == shop_id)
    if department_id is not None:
        stmt = stmt.where((CannedReply.department_id == department_id) | CannedReply.department_id.is_(None))
    return list(await session.scalars(stmt.order_by(CannedReply.sort, CannedReply.id)))


def render_canned(text: str, *, customer: Customer | None, ticket: Ticket) -> str:
    """Fill {name} and {ticket}; anything else is left as written."""
    values = {"name": customer.display_name if customer else "", "ticket": str(ticket.id)}
    out = text
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out[:4000]
