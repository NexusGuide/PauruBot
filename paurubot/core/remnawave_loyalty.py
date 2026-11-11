"""Remnawave loyalty tags (Foxima parity): each Remnawave user is tagged GOLD, SILVER or BRONZE by
how many active services its buyer has in the shop. Off unless the shop turns it on; a tag that is
already right is never sent again."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import settings as shop_settings
from paurubot.db.models import Panel, PanelKind, Service, ServiceStatus
from paurubot.panels import driver_for
from paurubot.provisioning.lifecycle import ref

log = logging.getLogger(__name__)

TIERS = ((5, "GOLD"), (3, "SILVER"), (1, "BRONZE"))
BATCH = 200


def tier_for(count: int) -> str | None:
    return next((tag for least, tag in TIERS if count >= least), None)


async def run(session: AsyncSession) -> int:
    """Tag what changed on every shop that asked for it; returns how many tags were sent."""
    sent = 0
    panels = list(
        await session.scalars(
            select(Panel).where(Panel.kind == PanelKind.REMNAWAVE, Panel.is_active.is_(True))
        )
    )
    for panel in panels:
        if not (await shop_settings.load(session, panel.shop_id)).get("remnawave_loyalty_tags"):
            continue
        live = (Service.status == ServiceStatus.ACTIVE, Service.is_trial.is_(False))
        counts = dict(
            (
                await session.execute(
                    select(Service.customer_id, func.count(Service.id))
                    .where(Service.shop_id == panel.shop_id, *live)
                    .group_by(Service.customer_id)
                )
            ).all()
        )
        rows = list(
            await session.scalars(
                select(Service).where(Service.panel_id == panel.id, *live).order_by(Service.id).limit(BATCH)
            )
        )
        driver = None
        for service in rows:
            tag = tier_for(int(counts.get(service.customer_id, 0)))
            if (service.panel_data or {}).get("tag") == tag:
                continue
            try:
                driver = driver or await driver_for(panel)
                set_tag = getattr(driver, "set_tag", None)  # Remnawave only
                if set_tag is None:
                    break
                await set_tag(ref(service), tag)
            except Exception:  # one panel or user failing must not stop the others
                log.warning("remnawave tag for service %s failed", service.id, exc_info=True)
                continue
            service.panel_data = {**(service.panel_data or {}), "tag": tag}
            sent += 1
        await session.commit()
    return sent
