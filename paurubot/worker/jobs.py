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


# --- bulk service actions (SVC-07) --------------------------------------------


async def bulk_service(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.audit.log import Actor, record
    from paurubot.db.models import Service, ServiceStatus

    data = job.payload
    service = await session.get(Service, int(data["service_id"]))
    if service is None or service.status == ServiceStatus.DELETED:
        return
    action = str(data["action"])
    try:
        if action in {"enable", "disable"}:
            if action == "enable" and data.get("reason") and service.disabled_reason != data["reason"]:
                return  # someone else disabled it since, for another reason
            await lifecycle.set_enabled(session, service, action == "enable", data.get("reason") or "staff")
        elif action == "extend":
            await lifecycle.extend(
                session, service, days=int(data.get("days") or 0), gb=int(data.get("gb") or 0)
            )
        elif action == "delete":
            await lifecycle.delete_service(session, service, data.get("reason") or "bulk delete")
        else:
            raise Permanent(f"unknown bulk action {action}")
    except PanelUnavailable as exc:
        raise Retry(str(exc)) from exc
    await record(
        session,
        shop_id=service.shop_id,
        actor=Actor("staff", data["staff_id"], None) if data.get("staff_id") else Actor("system", None, None),
        action=f"service.{action}",
        entity="service",
        entity_id=service.id,
        after={
            "batch": data.get("batch"),
            "days": data.get("days"),
            "gb": data.get("gb"),
            "reason": data.get("reason"),
        },
    )


register_queue("bulk", QueueSpec({"service": bulk_service}, batch=10, lease_seconds=120))


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
            # Health is the monitor's job (PNL-12); this only refreshes usage.
            synced, failed = await sync.sync_panel(session, panel)
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


# --- backups (OPS-05, OPS-06) --------------------------------------------------


async def run_backup(label: str) -> None:
    from paurubot.config import get_settings
    from paurubot.ops import backup, delivery

    settings = get_settings()
    info = await backup.create(label=label)
    removed = backup.prune(settings.backup_keep)
    log.info("backup %s written (%s bytes), pruned %s", info.name, info.size, len(removed))
    if settings.backup_to_telegram:
        await delivery.send_backup(info)


async def backup_now(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.ops.backup import BackupError

    try:
        await run_backup(str(job.payload.get("label") or "manual"))
    except BackupError as exc:
        raise Permanent(str(exc)) from exc


async def import_panel_users(session: AsyncSession, job: queue.Claimed) -> None:
    """TEN-10: adopt users that already exist on a panel."""
    from paurubot.audit.log import Actor, record
    from paurubot.provisioning import importer

    data = job.payload
    panel = await session.get(Panel, int(data["panel_id"]))
    if panel is None:
        raise Permanent("panel not found")
    try:
        result = await importer.run_import(
            session,
            panel,
            usernames=data.get("usernames"),
            customer_id=data.get("customer_id"),
            match_telegram=bool(data.get("match_telegram")),
        )
    except PanelUnavailable as exc:
        raise Retry(str(exc)) from exc
    except ValueError as exc:
        raise Permanent(str(exc)) from exc
    await record(
        session,
        shop_id=panel.shop_id,
        actor=Actor("staff", data["staff_id"], None) if data.get("staff_id") else Actor.system(),
        action="panel.import_users",
        entity="panel",
        entity_id=panel.id,
        after={
            "imported": result.imported,
            "skipped_existing": result.skipped_existing,
            "unassigned": result.unassigned,
            "customers_created": result.customers_created,
            "sample": result.usernames[:20],
        },
    )
    await session.commit()


register_queue(
    "ops",
    QueueSpec({"backup": backup_now, "import_panel": import_panel_users}, batch=1, lease_seconds=1800),
)


@every(600, "backups.daily")
async def daily_backup() -> None:
    from datetime import timedelta

    from paurubot.config import get_settings
    from paurubot.ops import backup

    settings = get_settings()
    if not settings.backup_passphrase.get_secret_value():
        return
    if utcnow().astimezone(settings.tz).hour != settings.backup_hour:
        return
    newest = backup.listing()[:1]
    if newest and utcnow() - newest[0].created_at < timedelta(hours=20):
        return
    await run_backup("daily")


# --- daily report (FIN-04, JOB-02) ----------------------------------------------


@every(300, "reports.daily")
async def daily_report() -> None:
    from datetime import timedelta

    from paurubot import kv
    from paurubot.config import get_settings
    from paurubot.core import settings as shop_settings
    from paurubot.core.formatting import jdate, money
    from paurubot.core.reports import as_params, daily_summary, day_bounds
    from paurubot.i18n import Texts, load_overrides
    from paurubot.notifications import telegram
    from paurubot.notifications.handlers import staff_chats

    now = utcnow()
    async with session_scope() as session:
        shop_ids = list(await session.scalars(select(Shop.id)))
    for shop_id in shop_ids:
        async with session_scope() as session:
            cfg = await shop_settings.load(session, shop_id)
            local = now.astimezone(get_settings().tz)
            if local.hour != int(cfg.get("daily_report_hour") or 23):
                continue
            # A report sent in the small hours covers the day that just ended.
            start, end = day_bounds(now - timedelta(hours=6) if local.hour < 6 else now)
            marker = f"report:daily:{shop_id}:{start.date().isoformat()}"
            if not await kv.get_redis().set(marker, "1", nx=True, ex=3 * 86400):
                continue
            summary = as_params(await daily_summary(session, shop_id, start, end))
            texts = Texts("fa", await load_overrides(session, shop_id))
            params = {k: money(v) if k in {"revenue", "wallet_total"} else v for k, v in summary.items()}
            text = texts("daily_report", date=jdate(start), **params)
            from paurubot.db.models import LogChannel

            channel = await session.scalar(
                select(LogChannel)
                .where(LogChannel.shop_id == shop_id, LogChannel.topic.in_(["reports", "all"]))
                .order_by(LogChannel.topic != "reports")
            )
            targets = (
                [(channel.chat_id, channel.thread_id)]
                if channel
                else [(chat, None) for chat in await staff_chats(session, shop_id, "reports.read")]
            )
        for chat_id, thread_id in targets:
            try:
                await telegram.send_text(chat_id, text, message_thread_id=thread_id)
            except telegram.Undeliverable:
                log.warning("daily report to %s was not delivered", chat_id)


# --- exchange rates and on-chain payments (PAY-11, PAY-13) ----------------------


@every(300, "fx.refresh")
async def refresh_rates() -> None:
    from paurubot.core import settings as shop_settings
    from paurubot.payments import fx

    uses_nobitex = False
    async with session_scope() as session:
        for shop_id in list(await session.scalars(select(Shop.id))):
            cfg = await shop_settings.load(session, shop_id)
            if cfg.get("fx_source") == "nobitex" and (
                cfg.get("crypto_enabled") or cfg.get("nowpayments_enabled") or cfg.get("plisio_enabled")
            ):
                uses_nobitex = True
                break
    if uses_nobitex:
        await fx.refresh()


@every(30, "crypto.scan")
async def scan_crypto() -> None:
    from paurubot.payments import watch

    async with session_scope() as session:
        paid = await watch.scan(session)
    if paid:
        log.info("crypto scan credited %s payment(s)", paid)


@every(600, "resellers.billing")
async def reseller_billing() -> None:
    """Metered reseller billing, debt collection, suspension and low-balance warnings (TEN-05, TEN-06)."""
    from paurubot.core import resellers

    async with session_scope() as session:
        billed = await resellers.run_billing(session)
    if billed:
        log.info("billed %s metered resellers", billed)


@every(120, "panels.monitor")
async def monitor_panels() -> None:
    """Uptime probes, live stats and node / quota alerts for every active panel (PNL-12)."""
    from paurubot.provisioning import monitor

    async with session_scope() as session:
        await monitor.run(session)
