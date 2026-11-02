"""A customer's "my service does not work" (SUP-05), shared by the bot and the Mini App (F-03).

A ticket with the service's state just read from its panel, and an honest answer when the panel is
already known to be down. One report per service per half hour.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.core import ticket_media, tickets
from paurubot.core.formatting import esc, jdate
from paurubot.db.models import Customer, Panel, Service, Ticket
from paurubot.i18n import Texts
from paurubot.panels import GB, PanelError
from paurubot.provisioning import lifecycle
from paurubot.worker import queue

log = logging.getLogger(__name__)
WINDOW = 1800


class ReportError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(slots=True)
class Reported:
    ticket: Ticket
    panel_down: bool


async def report(session: AsyncSession, customer: Customer, service: Service, t: Texts) -> Reported:
    """Open the ticket and queue the staff notice. The caller commits."""
    allowed, _ = await kv.hit(f"report:{service.id}", 1, WINDOW)
    if not allowed:
        raise ReportError("report_wait")
    live = False
    try:
        async with lifecycle.exclusive(session, service):  # read the panel as its one owner (SEC-F09)
            await lifecycle.refresh(session, service)
            live = True
    except lifecycle.ServiceBusy:
        log.info("reported service %s is being changed; using the last sync", service.id)
    except PanelError as exc:
        log.info("refresh failed for reported service %s: %s", service.id, exc)
    panel = await session.get(Panel, service.panel_id)
    down = panel is not None and (panel.is_healthy is False or not panel.is_active)
    details = t(
        "report_details",
        name=esc(service.label or service.panel_username),
        username=esc(service.panel_username),
        status=t(f"status_{service.status.value}"),
        used=round(int(service.used_bytes) / GB, 2),
        limit=round(int(service.data_limit) / GB, 2) if service.data_limit else t("unlimited"),
        expires=jdate(service.expire_at) if service.expire_at else t("unlimited"),
        panel=esc(panel.name if panel else "-"),
        health=t("report_panel_down") if down else t("report_panel_up"),
    )
    try:
        ticket = await tickets.open_ticket(
            session,
            customer,
            t("report_subject", name=service.label or service.panel_username),
            source="report",
        )
        # The ticket names its service, with the state just read from the panel (REF-09).
        ticket.service_id = service.id
        ticket.service_context = ticket_media.service_context(
            service, checked="live" if live else "stored", panel_down=down or not live
        )
        await tickets.add_message(
            session, ticket, author_type="customer", author_id=customer.id, text=details
        )
    except tickets.TicketError as exc:
        raise ReportError(exc.key) from exc
    await queue.enqueue(
        session, "notify", "ticket_new_message", {"ticket_id": ticket.id}, shop_id=customer.shop_id
    )
    return Reported(ticket, down)
