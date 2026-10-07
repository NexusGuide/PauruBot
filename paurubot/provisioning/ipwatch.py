"""Tell customers when a new device pushed an older one off their service (JOB-03).

Panels with a per-user address limit drop the oldest connection when one more device connects.
Without a word the customer only sees "it stopped working". For services whose plan has an
address limit, on panels that report live addresses, we keep the last list seen; when a later
check shows some addresses gone and new ones arrived, the customer gets one short notice.
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Panel, Plan, Service, ServiceStatus
from paurubot.panels import PanelError, registry
from paurubot.provisioning.lifecycle import ref
from paurubot.worker import queue

log = logging.getLogger(__name__)

NOTICE_EVERY = timedelta(hours=1)


def mask(address: str) -> str:
    """Enough to recognise your own network, not enough to publish someone else's address."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "?"
    if ip.version == 4:
        head = address.rsplit(".", 1)[0]
        return f"{head}.x"
    return ":".join(ip.exploded.split(":")[:3]) + ":…"


def replaced(previous: list[str], current: list[str]) -> tuple[list[str], list[str]]:
    """(dropped, arrived) when a connection was pushed out by a new one, else two empty lists."""
    if not previous:
        return [], []
    dropped = [ip for ip in previous if ip not in current]
    arrived = [ip for ip in current if ip not in previous]
    if dropped and arrived:
        return dropped, arrived
    return [], []


async def check(session: AsyncSession, service: Service, limit: int) -> bool:
    """Read one service's addresses; queue a notice when one was pushed out. True if noticed."""
    driver = await registry.driver_for(service.panel)
    current = (await driver.client_ips(ref(service)))[:limit]
    previous = list(service.client_ips or [])
    dropped, arrived = replaced(previous, current)
    if current != previous:
        service.client_ips = current
    if not dropped:
        return False
    alerts = dict(service.alerts_sent or {})
    last = alerts.get("ip_notice_at")
    now = utcnow()
    if last and now - _parse(last) < NOTICE_EVERY:
        return False
    alerts["ip_notice_at"] = now.isoformat()
    service.alerts_sent = alerts
    await queue.enqueue(
        session,
        "notify",
        "ip_replaced",
        {
            "service_id": service.id,
            "dropped": [mask(ip) for ip in dropped[:3]],
            "arrived": [mask(ip) for ip in arrived[:3]],
            "limit": limit,
        },
        shop_id=service.shop_id,
    )
    return True


def _parse(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return utcnow() - NOTICE_EVERY * 2


async def watched(session: AsyncSession, limit: int = 60) -> list[tuple[int, int]]:
    """(service id, address limit) for live services worth checking, a random slice each run."""
    kinds = [kind for kind, cls in registry.DRIVERS.items() if getattr(cls, "supports_ips", False)]
    rows = await session.execute(
        select(Service.id, Plan.ip_limit, Service.shop_id)
        .join(Plan, Plan.id == Service.plan_id)
        .join(Panel, Panel.id == Service.panel_id)
        .where(
            Service.status == ServiceStatus.ACTIVE,
            Plan.ip_limit > 0,
            Panel.kind.in_(kinds),
            Panel.is_active.is_(True),
        )
        .order_by(func.random())
        .limit(limit)
    )
    picked: list[tuple[int, int]] = []
    enabled: dict[int, bool] = {}
    for service_id, ip_limit, shop_id in rows:
        if shop_id not in enabled:
            enabled[shop_id] = bool((await shop_settings.load(session, shop_id))["ip_notice_enabled"])
        if enabled[shop_id]:
            picked.append((int(service_id), int(ip_limit)))
    return picked


async def run(session: AsyncSession) -> int:
    """One pass; returns how many notices were queued. Commits per service."""
    noticed = 0
    for service_id, limit in await watched(session):
        service = await session.get(Service, service_id)
        if service is None:
            continue
        try:
            noticed += int(await check(session, service, limit))
        except PanelError as exc:
            log.info("address check failed for service %s: %s", service_id, exc)
            await session.rollback()
            continue
        await session.commit()
    return noticed
