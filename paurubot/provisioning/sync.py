"""Usage sync for panels without webhooks (NEW-03), customer alerts (SVC-05) and cleanup."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Panel, Service, ServiceStatus
from paurubot.panels import PanelError, PanelUser
from paurubot.provisioning.lifecycle import apply_panel_user, delete_service, refresh
from paurubot.worker import queue

log = logging.getLogger(__name__)

LIVE_STATUSES = [
    ServiceStatus.ACTIVE,
    ServiceStatus.ON_HOLD,
    ServiceStatus.LIMITED,
    ServiceStatus.EXPIRED,
    ServiceStatus.DISABLED,
]


async def evaluate_alerts(
    session: AsyncSession, service: Service, previous: ServiceStatus | None
) -> list[str]:
    """Queue at most one alert of each kind per service period; returns the alert kinds queued."""
    cfg = await shop_settings.load(session, service.shop_id)
    sent = dict(service.alerts_sent or {})
    queued: list[str] = []
    jobs: list[dict[str, object]] = []
    period = service.expire_at.date().isoformat() if service.expire_at else "none"

    def once(kind: str, marker: str, payload: dict[str, object]) -> None:
        if sent.get(kind) == marker:
            return
        sent[kind] = marker
        queued.append(kind)
        payload.update({"service_id": service.id, "alert": kind})
        jobs.append(payload)

    if service.status == ServiceStatus.ACTIVE:
        if service.data_limit:
            remaining = max(0, service.data_limit - service.used_bytes)
            if remaining * 100 <= int(cfg["warn_remaining_percent"] or 0) * service.data_limit:
                once("low_volume", f"{service.data_limit}", {"remaining": remaining})
        if service.expire_at:
            seconds = (service.expire_at - datetime.now(UTC)).total_seconds()
            if 0 < seconds <= int(cfg["warn_days"] or 0) * 86400:
                once("expiring", period, {"days": max(1, int(seconds // 86400) + 1)})
    if service.status == ServiceStatus.EXPIRED and previous != ServiceStatus.EXPIRED:
        once("expired", period, {})
    if service.status == ServiceStatus.LIMITED and previous != ServiceStatus.LIMITED:
        once("limited", f"{service.data_limit}", {})
    service.alerts_sent = sent
    for payload in jobs:
        await queue.enqueue(
            session,
            "notify",
            "service_alert",
            payload,
            shop_id=service.shop_id,
            dedupe_key=f"alert:{service.id}:{payload['alert']}:{sent[str(payload['alert'])]}",
        )
    return queued


async def sync_panel(session: AsyncSession, panel: Panel, *, batch: int = 200) -> tuple[int, int]:
    """Refresh every live service on one panel. Returns (synced, failed)."""
    synced = failed = 0
    last_id = 0
    while True:
        services = list(
            await session.scalars(
                select(Service)
                .where(Service.panel_id == panel.id, Service.status.in_(LIVE_STATUSES), Service.id > last_id)
                .order_by(Service.id)
                .limit(batch)
            )
        )
        if not services:
            break
        for service in services:
            last_id = service.id
            previous = service.status
            try:
                await refresh(session, service)
            except PanelError as exc:
                failed += 1
                log.info("sync failed for service %s: %s", service.id, exc)
                if failed >= 5 and synced == 0:
                    await session.commit()
                    return synced, failed  # panel looks down; stop hammering it
                continue
            synced += 1
            if service.status != ServiceStatus.DELETED:
                await evaluate_alerts(session, service, previous)
        await session.commit()
    return synced, failed


async def due_panels(session: AsyncSession) -> list[Panel]:
    return list(await session.scalars(select(Panel).where(Panel.is_active.is_(True)).order_by(Panel.id)))


async def apply_webhook_user(session: AsyncSession, panel: Panel, info: PanelUser) -> Service | None:
    service = await session.scalar(
        select(Service).where(Service.panel_id == panel.id, Service.panel_username == info.username)
    )
    if service is None or service.status == ServiceStatus.DELETED:
        return None
    previous = service.status
    apply_panel_user(service, info)
    await evaluate_alerts(session, service, previous)
    return service


async def cleanup_expired(session: AsyncSession, shop_id: int) -> int:
    """Delete services that stayed expired or exhausted longer than the shop allows."""
    days = int(await shop_settings.get(session, shop_id, "delete_expired_after_days") or 0)
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    stale = list(
        await session.scalars(
            select(Service)
            .where(
                Service.shop_id == shop_id,
                Service.status.in_([ServiceStatus.EXPIRED, ServiceStatus.LIMITED]),
                or_(
                    Service.expire_at < cutoff, (Service.expire_at.is_(None)) & (Service.updated_at < cutoff)
                ),
            )
            .limit(200)
        )
    )
    removed = 0
    for service in stale:
        try:
            await delete_service(session, service, "expired")
            removed += 1
        except PanelError:
            continue
        await queue.enqueue(
            session,
            "notify",
            "service_alert",
            {"service_id": service.id, "alert": "deleted"},
            shop_id=shop_id,
            dedupe_key=f"alert:{service.id}:deleted",
        )
    await session.commit()
    return removed
