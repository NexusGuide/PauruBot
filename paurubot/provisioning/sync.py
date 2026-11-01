"""Usage sync for panels without webhooks (NEW-03), customer alerts (SVC-05) and cleanup.

Sync and cleanup share one owner per service with modifications (SEC-F09): each service is
handled in its own transaction under the service lease and a row lock taken without waiting.
A service that a renewal, move or reconciliation is changing is skipped until the next pass.
Its panel state is never read early and written late over the newer change, and it is never
deleted while it has an open order.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.core import alert_policy, orders
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Order, OrderStatus, Panel, Service, ServiceStatus
from paurubot.panels import PanelError, PanelUser
from paurubot.provisioning.lifecycle import delete_service, lock_fresh, refresh, service_lease
from paurubot.worker import queue

log = logging.getLogger(__name__)

# Seconds; renewed while held. Covers one panel read or delete plus the commit.
BACKGROUND_LEASE_TTL = 60

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
    lapsed_now = (
        service.status in {ServiceStatus.EXPIRED, ServiceStatus.LIMITED} and previous != service.status
    )
    if lapsed_now and await orders.release_queued(session, service.id):
        # A renewal was already paid for; it runs now instead of at the planned end date (SVC-02).
        return []
    if await orders.has_queued(session, service.id):
        return []

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
            if remaining * 100 <= alert_policy.warn_remaining_percent(cfg) * service.data_limit:
                once("low_volume", f"{service.data_limit}", {"remaining": remaining})
        if service.expire_at:
            seconds = (service.expire_at - datetime.now(UTC)).total_seconds()
            if 0 < seconds <= alert_policy.warn_days(cfg) * 86400:
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


async def _sync_one(session: AsyncSession, panel_id: int, service_id: int) -> str:
    """Refresh one service under its lease and row lock, and commit. Returns the outcome."""
    service = await session.get(Service, service_id)
    if service is None:
        return "skipped"
    if not await lock_fresh(session, service, skip_locked=True):
        return "busy"
    if service.panel_id != panel_id or service.status not in LIVE_STATUSES:
        await session.commit()  # moved or deleted since the page was read
        return "skipped"
    previous = service.status
    await refresh(session, service)
    if service.status != ServiceStatus.DELETED:
        await evaluate_alerts(session, service, previous)
    await session.commit()
    return "synced"


async def sync_panel(session: AsyncSession, panel: Panel, *, batch: int = 200) -> tuple[int, int]:
    """Refresh every live service on one panel. Returns (synced, failed); busy ones are skipped."""
    panel_id = panel.id
    synced = failed = busy = 0
    last_id = 0
    while True:
        service_ids = list(
            await session.scalars(
                select(Service.id)
                .where(Service.panel_id == panel_id, Service.status.in_(LIVE_STATUSES), Service.id > last_id)
                .order_by(Service.id)
                .limit(batch)
            )
        )
        await session.commit()  # no lock or snapshot is kept across the panel calls below
        if not service_ids:
            break
        for service_id in service_ids:
            last_id = service_id
            try:
                async with kv.lease(service_lease(service_id), ttl=BACKGROUND_LEASE_TTL):
                    outcome = await _sync_one(session, panel_id, service_id)
            except kv.LockNotAcquired:  # includes a lease lost mid-way: nothing was committed
                await session.rollback()
                busy += 1
                continue
            except PanelError as exc:
                await session.rollback()
                failed += 1
                log.info("sync failed for service %s: %s", service_id, exc)
                if failed >= 5 and synced == 0:
                    return synced, failed  # panel looks down; stop hammering it
                continue
            if outcome == "synced":
                synced += 1
            elif outcome == "busy":
                busy += 1
    if busy:
        log.info("sync panel=%s skipped %s service(s) being changed", panel_id, busy)
    return synced, failed


async def due_panels(session: AsyncSession) -> list[Panel]:
    return list(await session.scalars(select(Panel).where(Panel.is_active.is_(True)).order_by(Panel.id)))


async def apply_webhook_user(session: AsyncSession, panel: Panel, info: PanelUser) -> Service | None:
    """Persist a refresh hint; unordered provider events are not state snapshots.

    Enqueue in the caller's transaction. Do not permanently dedupe by service:
    completed jobs must not suppress later events (including real usage resets).
    """
    if not panel.is_active:
        return None
    service = await session.scalar(
        select(Service).where(
            Service.panel_id == panel.id,
            Service.shop_id == panel.shop_id,
            Service.panel_username == info.username,
        )
    )
    if service is None or service.status == ServiceStatus.DELETED:
        return None
    await queue.enqueue(
        session,
        "provision",
        "refresh_service",
        {"service_id": service.id, "panel_id": panel.id},
        shop_id=service.shop_id,
    )
    return service


def _expired_long_enough(service: Service, cutoff: datetime) -> bool:
    """Ended before `cutoff`: its end date passed then, or its data ran out then (SEC-F15)."""
    if service.status not in {ServiceStatus.EXPIRED, ServiceStatus.LIMITED}:
        return False
    if service.expire_at is not None and service.expire_at < cutoff:
        return True
    if service.status == ServiceStatus.LIMITED:
        return service.limited_at is not None and service.limited_at < cutoff
    return service.expire_at is None and service.updated_at is not None and service.updated_at < cutoff


async def _has_open_order(session: AsyncSession, service_id: int) -> bool:
    """A paid, running or parked order still needs this service: renewal, add-on or move."""
    found = await session.scalar(
        select(Order.id)
        .where(
            Order.service_id == service_id,
            Order.status.in_([OrderStatus.PAID, OrderStatus.PROVISIONING]),
        )
        .limit(1)
    )
    return found is not None


async def _cleanup_one(session: AsyncSession, shop_id: int, service_id: int, cutoff: datetime) -> bool:
    """Delete one stale service under its lease and row lock, and commit. Returns whether it did."""
    service = await session.get(Service, service_id)
    if service is None or not await lock_fresh(session, service, skip_locked=True):
        return False
    # Decide again on the locked, current row: it may have been renewed since the page was read.
    if service.shop_id != shop_id or not _expired_long_enough(service, cutoff):
        await session.commit()
        return False
    if await _has_open_order(session, service.id):
        await session.commit()
        return False
    await delete_service(session, service, "expired")
    await queue.enqueue(
        session,
        "notify",
        "service_alert",
        {"service_id": service.id, "alert": "deleted"},
        shop_id=shop_id,
        dedupe_key=f"alert:{service.id}:deleted",
    )
    await session.commit()
    return True


async def cleanup_expired(session: AsyncSession, shop_id: int) -> int:
    """Delete services that stayed expired or exhausted longer than the shop allows."""
    days = int(await shop_settings.get(session, shop_id, "delete_expired_after_days") or 0)
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    stale = list(
        await session.scalars(
            select(Service.id)
            .where(
                Service.shop_id == shop_id,
                Service.status.in_([ServiceStatus.EXPIRED, ServiceStatus.LIMITED]),
                or_(
                    Service.expire_at < cutoff,
                    (Service.status == ServiceStatus.LIMITED) & (Service.limited_at < cutoff),
                    (Service.status == ServiceStatus.EXPIRED)
                    & Service.expire_at.is_(None)
                    & (Service.updated_at < cutoff),
                ),
            )
            .order_by(Service.id)
            .limit(200)
        )
    )
    await session.commit()
    removed = 0
    for service_id in stale:
        try:
            async with kv.lease(service_lease(service_id), ttl=BACKGROUND_LEASE_TTL):
                if await _cleanup_one(session, shop_id, service_id, cutoff):
                    removed += 1
        except kv.LockNotAcquired:
            await session.rollback()  # being changed right now; look again next run
        except PanelError:
            await session.rollback()
    return removed
