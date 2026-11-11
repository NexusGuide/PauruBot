"""Add a previous service (settings › services): a customer brings a service they already have on
one of the shop's panels, by its username or subscription link.

The panel user is looked up on the shop's own active panels only. It joins the customer's
services at once when the panel user's note (or username) names the customer's Telegram id and
the shop allows that; otherwise staff approve or reject the request. A panel user the shop
already tracks for anyone is never handed over, and nothing changes on the panel itself.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Panel, Service, ServiceClaim, ServiceStatus
from paurubot.panels import PanelError, PanelUser, driver_for
from paurubot.panels.base import PanelRef
from paurubot.provisioning.importer import telegram_id_of
from paurubot.provisioning.lifecycle import apply_panel_user, new_sub_token

log = logging.getLogger(__name__)

MAX_PANELS = 20
PENDING_PER_CUSTOMER = 5
_NAME = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")


class ClaimError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(slots=True)
class ClaimResult:
    claim: ServiceClaim
    service: Service | None  # set when it joined at once


def candidates(text: str) -> list[str]:
    """Usernames a pasted username or subscription link may stand for, most likely first."""
    text = text.strip()
    if not text:
        return []
    names: list[str] = []
    if "://" in text:
        parts = [p for p in urlparse(text).path.split("/") if p]
        names.extend(reversed(parts[-2:]))
    else:
        names.append(text.lstrip("@"))
    return [n for n in dict.fromkeys(names) if _NAME.match(n)]


async def _panels(session: AsyncSession, shop_id: int) -> list[Panel]:
    rows = await session.scalars(
        select(Panel)
        .where(Panel.shop_id == shop_id, Panel.is_active.is_(True), Panel.national_mode.is_(False))
        .order_by(Panel.id)
        .limit(MAX_PANELS)
    )
    return list(rows)


async def find(session: AsyncSession, shop_id: int, text: str) -> tuple[Panel, PanelUser] | None:
    """The first panel user of this shop that the text names."""
    names = candidates(text)
    if not names:
        return None
    for panel in await _panels(session, shop_id):
        try:
            driver = await driver_for(panel)
        except PanelError:
            continue
        for name in names:
            try:
                user = await driver.get_user(PanelRef(name))
            except PanelError:
                log.info("panel %s could not look up a claimed user", panel.id)
                break
            if user is not None:
                return panel, user
    return None


async def _tracked(session: AsyncSession, panel_id: int, username: str) -> Service | None:
    return await session.scalar(
        select(Service).where(
            Service.panel_id == panel_id,
            Service.panel_username == username,
            Service.status != ServiceStatus.DELETED,
        )
    )


def _service(customer: Customer, panel: Panel, user: PanelUser) -> Service:
    service = Service(
        shop_id=customer.shop_id,
        customer_id=customer.id,
        panel_id=panel.id,
        panel_username=user.username,
        panel_data=user.data or {},
        label=user.username,
        status=ServiceStatus.ACTIVE,
        sub_token=new_sub_token(),
    )
    apply_panel_user(service, user)
    return service


async def claim(session: AsyncSession, customer: Customer, text: str) -> ClaimResult:
    cfg = await shop_settings.load(session, customer.shop_id)
    if not cfg.get("service_import_enabled"):
        raise ClaimError("import_off")
    found = await find(session, customer.shop_id, text)
    if found is None:
        raise ClaimError("import_not_found")
    panel, user = found
    tracked = await _tracked(session, panel.id, user.username)
    if tracked is not None:
        raise ClaimError("import_already_yours" if tracked.customer_id == customer.id else "import_taken")
    row = await session.scalar(
        select(ServiceClaim).where(
            ServiceClaim.customer_id == customer.id,
            ServiceClaim.panel_id == panel.id,
            ServiceClaim.panel_username == user.username,
        )
    )
    if row is not None and row.status == "pending":
        raise ClaimError("import_pending")
    if row is None:
        waiting = await session.scalars(
            select(ServiceClaim.id).where(
                ServiceClaim.customer_id == customer.id, ServiceClaim.status == "pending"
            )
        )
        if len(list(waiting)) >= PENDING_PER_CUSTOMER:
            raise ClaimError("import_too_many")
        row = ServiceClaim(
            shop_id=customer.shop_id, customer_id=customer.id, panel_id=panel.id, panel_username=user.username
        )
        session.add(row)
    row.status, row.decided_at, row.decided_by = "pending", None, None
    if cfg.get("service_import_auto") and telegram_id_of(user) == customer.telegram_id:
        service = _service(customer, panel, user)
        session.add(service)
        await session.flush()
        row.status, row.service_id, row.decided_at, row.decided_by = "approved", service.id, utcnow(), "auto"
        return ClaimResult(row, service)
    await session.flush()
    return ClaimResult(row, None)


async def decide(session: AsyncSession, row: ServiceClaim, approve: bool, actor: str) -> Service | None:
    """Staff approve (the service joins the customer) or reject a pending claim."""
    if row.status != "pending":
        raise ClaimError("import_decided")
    row.decided_at, row.decided_by = utcnow(), actor[:128]
    if not approve:
        row.status = "rejected"
        return None
    panel = await session.get(Panel, row.panel_id)
    customer = await session.get(Customer, row.customer_id)
    if panel is None or customer is None or panel.shop_id != row.shop_id:
        raise ClaimError("import_not_found")
    if await _tracked(session, panel.id, row.panel_username) is not None:
        raise ClaimError("import_taken")
    try:
        user = await (await driver_for(panel)).get_user(PanelRef(row.panel_username))
    except PanelError as exc:
        raise ClaimError("import_panel_error") from exc
    if user is None:
        raise ClaimError("import_not_found")
    service = _service(customer, panel, user)
    session.add(service)
    await session.flush()
    row.status, row.service_id = "approved", service.id
    return service


def view(row: ServiceClaim, customer: Customer | None, panel: Panel | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "status": row.status,
        "panel_username": row.panel_username,
        "panel": {"id": panel.id, "name": panel.name} if panel else None,
        "customer": {"id": customer.id, "name": customer.display_name} if customer else None,
        "service_id": row.service_id,
        "created_at": row.created_at.isoformat(),
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "decided_by": row.decided_by,
    }
