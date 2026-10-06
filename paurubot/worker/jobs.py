"""Queue handlers and periodic tasks wired into the worker."""

from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import orders
from paurubot.db.base import utcnow
from paurubot.db.models import Job, JobStatus, Panel, Shop
from paurubot.db.session import session_scope
from paurubot.panels import PanelError, PanelUnavailable
from paurubot.payments import engine as payments
from paurubot.provisioning import lifecycle, sync
from paurubot.worker import queue
from paurubot.worker.runner import Permanent, QueueSpec, Retry, every, register_queue

log = logging.getLogger(__name__)


# --- provisioning -----------------------------------------------------------


async def provision_order(session: AsyncSession, job: queue.Claimed) -> None:
    order_id = int(job.payload["order_id"])
    try:
        await lifecycle.fulfill(session, order_id)
    except lifecycle.ProvisionError as exc:
        raise Permanent(str(exc)) from exc
    except PanelUnavailable as exc:
        raise Retry(str(exc)) from exc
    except PanelError as exc:
        if exc.status is not None and 400 <= exc.status < 500 and exc.status not in (401, 404, 409, 429):
            raise Permanent(f"panel rejected request: {exc}") from exc
        raise


async def provision_dead(session: AsyncSession, job: queue.Claimed, error: str) -> None:
    await lifecycle.give_up(session, int(job.payload["order_id"]), error)


register_queue(
    "provision", QueueSpec({"order": provision_order}, on_dead=provision_dead, batch=5, lease_seconds=180)
)


# --- payments ---------------------------------------------------------------


async def fulfill_payment(session: AsyncSession, job: queue.Claimed) -> None:
    await payments.fulfill_one(session, int(job.payload["payment_id"]))


register_queue("payments", QueueSpec({"fulfill": fulfill_payment}, batch=10))


# --- periodic ---------------------------------------------------------------


@every(30, "payments.sweep")
async def sweep_payments() -> None:
    """Catch paid payments whose fulfillment job was lost (belt and braces for PAY-02)."""
    async with session_scope() as session:
        ids = await payments.pending_fulfillment(session)
    for payment_id in ids:
        async with session_scope() as session:
            await payments.fulfill_one(session, payment_id)


@every(60, "expiry.sweep")
async def expire_open_items() -> None:
    async with session_scope() as session:
        expired_payments = await payments.expire_stale(session)
        expired_orders = await orders.expire_stale(session)
    if expired_payments or expired_orders:
        log.info("expired payments=%s orders=%s", expired_payments, expired_orders)


@every(300, "usage.sync")
async def usage_sync() -> None:
    async with session_scope() as session:
        panels = await sync.due_panels(session)
        panel_ids = [p.id for p in panels]
    for panel_id in panel_ids:
        async with session_scope() as session:
            panel = await session.get(Panel, panel_id)
            if panel is None:
                continue
            synced, failed = await sync.sync_panel(session, panel)
            panel.is_healthy = failed == 0 or synced > 0
            panel.health_checked_at = utcnow()
        log.info("usage sync panel=%s synced=%s failed=%s", panel_id, synced, failed)


@every(3600, "services.cleanup")
async def cleanup_services() -> None:
    async with session_scope() as session:
        shop_ids = list(await session.scalars(select(Shop.id)))
    for shop_id in shop_ids:
        async with session_scope() as session:
            await sync.cleanup_expired(session, shop_id)


@every(3600, "ledger.reconcile")
async def reconcile_ledger() -> None:
    from paurubot.ledger.posting import reconcile

    async with session_scope() as session:
        drift = await reconcile(session)
    if drift:
        log.error("ledger drift detected: %s", [(d.code, d.cached, d.actual) for d in drift][:20])


@every(86400, "jobs.cleanup")
async def cleanup_jobs() -> None:
    """Drop finished queue rows after two weeks (JOB-04)."""
    from datetime import timedelta

    async with session_scope() as session:
        await session.execute(
            delete(Job).where(
                Job.status.in_([JobStatus.DONE, JobStatus.CANCELED]),
                Job.finished_at < utcnow() - timedelta(days=14),
            )
        )
