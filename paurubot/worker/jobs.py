"""Queue handlers and periodic tasks wired into the worker."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.core import orders
from paurubot.db.base import utcnow
from paurubot.db.models import Job, JobStatus, Order, Panel, Service, ServiceStatus, Shop
from paurubot.db.session import session_scope
from paurubot.panels import PanelError, PanelNotFound, PanelUnavailable
from paurubot.payments import engine as payments
from paurubot.provisioning import lifecycle, sync
from paurubot.worker import queue
from paurubot.worker.runner import Defer, Permanent, QueueSpec, Retry, every, register_queue

log = logging.getLogger(__name__)


# --- provisioning -----------------------------------------------------------


async def provision_order(session: AsyncSession, job: queue.Claimed) -> None:
    order_id = int(job.payload["order_id"])
    try:
        await lifecycle.fulfill(session, order_id)
    except lifecycle.ServiceBusy as exc:
        raise Defer(str(exc)) from exc
    except lifecycle.ProvisionError as exc:
        raise Permanent(str(exc)) from exc
    except PanelUnavailable as exc:
        raise Retry(str(exc)) from exc
    except PanelError as exc:
        if exc.status is not None and 400 <= exc.status < 500 and exc.status not in (401, 404, 409, 429):
            raise Permanent(f"panel rejected request: {exc}") from exc
        raise


async def provision_dead(session: AsyncSession, job: queue.Claimed, error: str) -> None:
    if job.kind == "refresh_service":
        log.warning("panel refresh exhausted for service %s", job.payload.get("service_id"))
        return
    if job.kind == "void_service":
        # Staff already got the dead-job report; the void itself stands.
        log.warning("could not stop voided service %s: %s", job.payload.get("service_id"), error[:200])
        return
    if job.kind != "order":
        log.warning("gave up removing a moved account: %s", error[:200])
        return
    await lifecycle.give_up(session, int(job.payload["order_id"]), error)


async def cleanup_panel_user(session: AsyncSession, job: queue.Claimed) -> None:
    data = job.payload
    try:
        await lifecycle.remove_panel_user(
            session, int(data["panel_id"]), str(data["username"]), dict(data.get("data") or {})
        )
    except PanelError as exc:
        raise Retry(str(exc)) from exc


async def refresh_panel_hint(session: AsyncSession, job: queue.Claimed) -> None:
    """Fetch current provider state, never replay the webhook's stale payload."""
    if job.shop_id is None:
        raise Permanent("panel refresh requires a shop")
    service = await session.scalar(
        select(Service)
        .where(Service.id == int(job.payload["service_id"]))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if service is None:
        return
    if service.shop_id != job.shop_id or service.panel_id != int(job.payload["panel_id"]):
        raise Permanent("panel refresh scope mismatch")
    if service.status == ServiceStatus.DELETED:
        return
    panel = await session.get(Panel, service.panel_id)
    if panel is None or panel.shop_id != job.shop_id:
        raise Permanent("panel refresh scope mismatch")
    if not panel.is_active:
        return
    try:
        # Same owner as modifications (SEC-F09): never read the panel mid-renewal or mid-move.
        async with kv.lease(lifecycle.service_lease(service.id), ttl=sync.BACKGROUND_LEASE_TTL):
            previous = service.status
            # Panel failures roll back through the existing retry/dead-letter runner.
            await lifecycle.refresh(session, service)
            if service.status != ServiceStatus.DELETED:
                await sync.evaluate_alerts(session, service, previous)
            await session.commit()
    except kv.LockNotAcquired as exc:
        raise Defer("service is being changed") from exc


async def stop_voided_service(session: AsyncSession, job: queue.Claimed) -> None:
    """Turn off the service a voided payment bought (REF-01), under the service lease (SEC-F09).

    Idempotent: a retry after an unknown panel answer asks the panel to disable it again.
    """
    service = await session.get(Service, int(job.payload["service_id"]))
    if service is None:
        return
    if job.shop_id is None or service.shop_id != job.shop_id:
        raise Permanent("void scope mismatch")
    try:
        async with kv.lease(lifecycle.service_lease(service.id), ttl=sync.BACKGROUND_LEASE_TTL):
            await lifecycle.lock_fresh(session, service)
            if service.status == ServiceStatus.DELETED:
                return
            try:
                await lifecycle.set_enabled(session, service, False, "void")
            except PanelUnavailable as exc:
                raise Retry(str(exc)) from exc
            except PanelNotFound:
                log.warning("voided service %s is no longer on its panel", service.id)
                return
            service.auto_renew = None
            await session.commit()
    except kv.LockNotAcquired as exc:
        raise Defer("service is being changed") from exc


register_queue(
    "provision",
    QueueSpec(
        {
            "order": provision_order,
            "cleanup_user": cleanup_panel_user,
            "refresh_service": refresh_panel_hint,
            "void_service": stop_voided_service,
        },
        on_dead=provision_dead,
        batch=5,
        lease_seconds=180,
    ),
)


# --- bulk service actions (SVC-07) --------------------------------------------


async def bulk_service(session: AsyncSession, job: queue.Claimed) -> None:
    from paurubot.audit.log import Actor, record
    from paurubot.db.models import Service, ServiceStatus

    data = job.payload
    if data.get("gift_target"):
        service = await session.scalar(
            select(Service).where(Service.id == int(data["service_id"])).with_for_update()
        )
    else:
        service = await session.get(Service, int(data["service_id"]))
    if service is None or service.status == ServiceStatus.DELETED:
        if data.get("gift_target"):
            await queue.update_payload(session, job, {**data, "outcome": "skipped_ineligible"})
        return
    if job.shop_id is not None and service.shop_id != job.shop_id:
        raise Permanent("service does not belong to this shop")
    action = str(data["action"])
    outcome = None
    if action not in {"enable", "disable", "extend", "delete"}:
        raise Permanent(f"unknown bulk action {action}")
    try:
        # The same owner as renewals and sync (SEC-F09), and every check below on the fresh row.
        async with lifecycle.exclusive(session, service):
            if service.status == ServiceStatus.DELETED:  # deleted while this job waited
                if data.get("gift_target"):
                    await queue.update_payload(session, job, {**data, "outcome": "skipped_ineligible"})
                return
            if action in {"enable", "disable"}:
                if action == "enable" and data.get("reason") and service.disabled_reason != data["reason"]:
                    return  # someone else disabled it since, for another reason
                await lifecycle.set_enabled(
                    session, service, action == "enable", data.get("reason") or "staff"
                )
            elif action == "extend":
                if data.get("gift_target"):
                    from paurubot.core.service_gifts import apply_fixed

                    outcome = await apply_fixed(session, service, data["gift_target"])
                    await queue.update_payload(session, job, {**data, "outcome": outcome})
                else:
                    await lifecycle.extend(
                        session, service, days=int(data.get("days") or 0), gb=int(data.get("gb") or 0)
                    )
            else:
                await lifecycle.delete_service(session, service, data.get("reason") or "bulk delete")
    except lifecycle.ServiceBusy as exc:
        raise Defer(str(exc)) from exc
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
            "outcome": outcome,
            "gift_target": data.get("gift_target"),
            "batch": data.get("batch"),
            "days": data.get("days"),
            "gb": data.get("gb"),
            "reason": data.get("reason"),
        },
    )
    if data.get("notify") and action in {"delete", "disable"}:
        await queue.enqueue(
            session,
            "notify",
            "service_alert",
            {
                "service_id": service.id,
                "alert": "removed_by_staff" if action == "delete" else "disabled_by_staff",
                "reason": str(data.get("reason") or "")[:255],
            },
            shop_id=service.shop_id,
        )


async def set_panel_owner(session: AsyncSession, job: queue.Claimed) -> None:
    """Give one panel user to another panel admin (TEN-11)."""
    from paurubot.panels import PanelRef, driver_for

    data = job.payload
    panel = await session.get(Panel, int(data["panel_id"]))
    if panel is None:
        return
    try:
        await (await driver_for(panel)).set_owner(
            PanelRef(str(data["username"]), dict(data.get("data") or {})), str(data["admin"])
        )
    except PanelUnavailable as exc:
        raise Retry(str(exc)) from exc


register_queue(
    "bulk", QueueSpec({"service": bulk_service, "set_owner": set_panel_owner}, batch=10, lease_seconds=120)
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
    # Health is the monitor's job (PNL-12); this only refreshes usage, panels side by side (SEC-F27).
    for panel_id, result in (await sync.sync_all(panel_ids)).items():
        if isinstance(result, BaseException):
            if isinstance(result, asyncio.CancelledError):
                raise result
            log.error("usage sync panel=%s failed", panel_id, exc_info=result)
            continue
        log.info("usage sync panel=%s synced=%s failed=%s", panel_id, *result)


@every(3600, "usage_history.cleanup")
async def cleanup_usage_history() -> None:
    from paurubot.core import usage_history

    async with session_scope() as session:
        removed = await usage_history.prune(session)
    if removed:
        log.info("usage history pruned=%s", removed)


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


@every(600, "renewals.reconcile")
async def reconcile_renewals() -> None:
    """Settle renewals parked as uncertain by reading the panel, never by a blind retry."""
    cursor = 0
    async with session_scope() as session:
        through_id = int(await session.scalar(select(func.max(Order.id))) or 0)
    while cursor < through_id:
        async with session_scope() as session:
            order_ids = await lifecycle.parked_renewals(session, after_id=cursor, through_id=through_id)
        if not order_ids:
            break
        cursor = order_ids[-1]
        for order_id in order_ids:
            try:
                async with session_scope() as session:
                    outcome = await lifecycle.reconcile_renewal(session, order_id)
            except lifecycle.ServiceBusy:
                continue  # a live worker owns it; look again in the next bounded pass
            except Exception:
                log.exception("reconciling renewal order %s failed", order_id)
                continue
            if outcome != "uncertain":
                log.info("renewal order %s reconciled: %s", order_id, outcome)


@every(600, "purchases.reconcile")
async def reconcile_purchases() -> None:
    """Settle purchases parked because the panel may hold their account (SEC-F08)."""
    cursor = 0
    async with session_scope() as session:
        through_id = int(await session.scalar(select(func.max(Order.id))) or 0)
    while cursor < through_id:
        async with session_scope() as session:
            order_ids = await lifecycle.parked_purchases(session, after_id=cursor, through_id=through_id)
        if not order_ids:
            break
        cursor = order_ids[-1]
        for order_id in order_ids:
            try:
                async with session_scope() as session:
                    outcome = await lifecycle.reconcile_purchase(session, order_id)
            except Exception:
                log.exception("reconciling purchase order %s failed", order_id)
                continue
            if outcome not in {"uncertain", "waiting"}:
                log.info("purchase order %s reconciled: %s", order_id, outcome)


@every(60, "services.auto_renew")
async def auto_renew_services() -> None:
    """Renew or top up the services whose owners turned auto-renewal on (F-02)."""
    from paurubot.core import auto_renew

    now, cursor = utcnow(), 0
    async with session_scope() as session:
        through_id = int(await session.scalar(select(func.max(Service.id))) or 0)
    while cursor < through_id:
        async with session_scope() as session:
            service_ids = await auto_renew.due(session, now, after_id=cursor, through_id=through_id)
        if not service_ids:
            break
        cursor = service_ids[-1]  # advance even if every service in this batch is busy or short
        for service_id in service_ids:
            try:
                async with session_scope() as session:
                    outcome = await auto_renew.run_service(session, service_id)
            except Exception:
                log.exception("auto-renewing service %s failed", service_id)
                continue
            if outcome not in {"idle", "cooldown", "busy", "short"}:
                log.info("auto-renew for service %s: %s", service_id, outcome)


@every(60, "accounts.auto_renew")
async def auto_renew_accounts() -> None:
    """Renew the panel admin accounts whose owners turned auto-renewal on (TEN-14)."""
    from paurubot.core import account_auto_renew

    async with session_scope() as session:
        account_ids = await account_auto_renew.due(session)
    for account_id in account_ids:
        try:
            async with session_scope() as session:
                outcome = await account_auto_renew.run_account(session, account_id)
        except Exception:
            log.exception("auto-renewing admin account %s failed", account_id)
            continue
        if outcome not in {"idle", "cooldown", "busy", "short", "off"}:
            log.info("auto-renew for admin account %s: %s", account_id, outcome)


DEFAULT_RETENTION_DAYS = 14


def retention_days(cfg: dict[str, object]) -> int:
    try:
        days = int(cfg.get("log_retention_days") or DEFAULT_RETENTION_DAYS)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    return min(max(days, 1), 365)


@every(3600, "guests.cleanup")
async def cleanup_guests() -> None:
    """Web guests who never paid leave no rows behind (CAT-08)."""
    from paurubot.core import guest

    async with session_scope() as session:
        dropped = await guest.drop_unpaid(session)
    if dropped:
        log.info("removed %s unpaid web guests", dropped)


@every(86400, "jobs.cleanup")
async def cleanup_jobs() -> None:
    """Drop finished queue rows after each shop's log retention, two weeks by default (JOB-04)."""
    from datetime import timedelta

    from paurubot.core import settings as shop_settings

    finished = Job.status.in_([JobStatus.DONE, JobStatus.CANCELED])
    async with session_scope() as session:
        for shop_id in list(await session.scalars(select(Shop.id))):
            days = retention_days(await shop_settings.load(session, shop_id))
            await session.execute(
                delete(Job).where(
                    finished, Job.shop_id == shop_id, Job.finished_at < utcnow() - timedelta(days=days)
                )
            )
        await session.execute(
            delete(Job).where(
                finished,
                Job.shop_id.is_(None),
                Job.finished_at < utcnow() - timedelta(days=DEFAULT_RETENTION_DAYS),
            )
        )


# --- backups (OPS-05, OPS-06) --------------------------------------------------


async def run_backup(label: str) -> None:
    from paurubot.ops import backup, backup_schedule, delivery

    schedule = await backup_schedule.current()
    info = await backup.create(label=label)
    removed = backup.prune(schedule.keep)
    log.info("backup %s written (%s bytes), pruned %s", info.name, info.size, len(removed))
    if schedule.telegram:
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


async def migrate_bot(session: AsyncSession, job: queue.Claimed) -> None:
    """MIG-01: bring customers, balances and services over from another bot's database."""
    import json as _json

    from paurubot import kv
    from paurubot.audit.log import Actor, record
    from paurubot.migration import runner

    data = job.payload
    try:
        extract = await runner.load(str(data["token"]), data.get("source"))
    except ValueError as exc:
        raise Permanent(str(exc)) from exc
    panel_map = {str(k): int(v) for k, v in (data.get("panel_map") or {}).items()}
    report = await runner.apply(
        session, int(data["shop_id"]), extract, panel_map, dry_run=bool(data.get("dry_run"))
    )
    await record(
        session,
        shop_id=int(data["shop_id"]),
        actor=Actor("staff", data["staff_id"], None) if data.get("staff_id") else Actor.system(),
        action="migration.dry_run" if report.dry_run else "migration.run",
        entity="migration",
        entity_id=None,
        after=report.as_dict(),
    )
    await session.commit()
    await kv.get_redis().set(
        f"migration:{data['shop_id']}:last",
        _json.dumps({**report.as_dict(), "job_id": job.id}),
        ex=30 * 86_400,
    )


async def loyalty_recount(session: AsyncSession, job: queue.Claimed) -> None:
    """Recompute every customer's loyalty tier after the tiers change (GRW-02)."""
    from paurubot.core import growth
    from paurubot.core import settings as shop_settings
    from paurubot.db.models import Customer, Order, OrderStatus

    shop_id = job.shop_id
    if shop_id is None:
        return
    cfg = await shop_settings.load(session, shop_id, fresh=True)
    ids = list(
        await session.scalars(
            select(Customer.id).where(
                Customer.shop_id == shop_id,
                (Customer.tier_percent > 0)
                | Customer.id.in_(select(Order.customer_id).where(Order.status == OrderStatus.COMPLETED)),
            )
        )
    )
    for start in range(0, len(ids), 500):
        for customer in await session.scalars(
            select(Customer).where(Customer.id.in_(ids[start : start + 500]))
        ):
            if not customer.is_reseller:
                await growth.refresh_tier(session, customer, cfg)
        await session.commit()


register_queue(
    "ops",
    QueueSpec(
        {
            "backup": backup_now,
            "import_panel": import_panel_users,
            "migrate": migrate_bot,
            "loyalty_recount": loyalty_recount,
        },
        batch=1,
        lease_seconds=3600,
    ),
)


@every(600, "backups.daily")
async def daily_backup() -> None:
    """Automatic backups, as often as the Backups page says (every few hours, or daily at an hour)."""
    from paurubot import kv
    from paurubot.config import get_settings
    from paurubot.ops import backup, backup_schedule

    settings = get_settings()
    if not settings.backup_passphrase.get_secret_value():
        return
    if await kv.marked([backup_schedule.RETRY_KEY]):
        return
    schedule = await backup_schedule.current()
    now = utcnow()
    newest = backup.listing()[:1]
    if not backup_schedule.due(
        schedule,
        now=now,
        local_hour=now.astimezone(settings.tz).hour,
        newest=newest[0].created_at if newest else None,
    ):
        return
    try:
        await run_backup("daily" if schedule.every_hours >= 24 else "auto")
    except Exception:
        await kv.mark(backup_schedule.RETRY_KEY, int(backup_schedule.RETRY_AFTER.total_seconds()))
        raise


# --- daily report (FIN-04, JOB-02) ----------------------------------------------


@every(1800, "stars.reconcile")
async def reconcile_stars() -> None:
    """Credit Stars charges the bot never heard of (SEC-F05); see payments.stars_reconcile."""
    from paurubot.core import settings as shop_settings
    from paurubot.notifications.telegram import bot_for
    from paurubot.payments import stars_reconcile

    async with session_scope() as session:
        shop_ids = list(await session.scalars(select(Shop.id)))
    for shop_id in shop_ids:
        async with session_scope() as session:
            if not (await shop_settings.load(session, shop_id)).get("stars_enabled"):
                continue
            bot = await bot_for(shop_id)
            if bot is None:
                continue
            try:
                result = await stars_reconcile.reconcile_shop(session, shop_id, bot)
            except Exception:
                log.warning("stars reconciliation of shop %s failed", shop_id, exc_info=True)
                continue
            if result.unmatched:
                await queue.enqueue(
                    session,
                    "notify",
                    "stars_unmatched",
                    {"charges": result.unmatched[:20]},
                    shop_id=shop_id,
                    dedupe_key=f"stars:unmatched:{shop_id}:{result.unmatched[0]}",
                )
                await session.commit()
            if result.credited:
                log.info("stars reconciliation credited payments %s", result.credited)


def daily_messages(
    texts: Any, date: str, summary: dict[str, int], details: Any, sections: list[str] | None = None
) -> list[str]:
    """The nightly report as up to three messages: the summary, the day's money, and each panel;
    `sections` picks which of them are sent (all by default)."""
    from paurubot.core.formatting import esc, money

    change = details.change_percent
    params = {k: money(v) if k in {"revenue", "wallet_total"} else v for k, v in summary.items()}
    head = texts(
        "daily_report",
        date=date,
        total=money(details.total),
        purchases=details.purchases,
        renewals_count=details.renewals,
        active_users=details.active_users,
        inactive_users=details.inactive_users,
        change=texts("daily_change_none")
        if change is None
        else texts("daily_change", sign="+" if change > 0 else "", value=change),
        best_hour=texts("daily_hour_none") if details.best_hour is None else f"{details.best_hour:02d}:00",
        **params,
    )
    money_text = texts(
        "daily_report_money",
        purchases=money(details.purchases_amount),
        renewals=money(details.renewals_amount),
        others=money(details.others_amount),
        total=money(details.total),
    )
    panels = [
        texts(
            "daily_report_panel",
            name=esc(day.name),
            orders=day.orders,
            amount=money(day.amount),
            gb=money(day.gb),
        )
        for day in details.panels[:30]
    ]
    panel_text = texts(
        "daily_report_panels", panels="\n➖➖➖➖➖\n".join(panels) or texts("daily_no_panels")
    )
    parts = {"summary": head, "money": money_text, "panels": panel_text}
    wanted = set(parts) if sections is None else set(sections)
    return [text for key, text in parts.items() if key in wanted]


@every(300, "reports.daily")
async def daily_report() -> None:

    from paurubot import kv
    from paurubot.core import settings as shop_settings
    from paurubot.core.formatting import jdate
    from paurubot.core.reports import as_params, daily_details, daily_summary, day_bounds, report_day
    from paurubot.i18n import Texts, load_overrides
    from paurubot.notifications import telegram
    from paurubot.notifications.handlers import staff_chats

    now = utcnow()
    async with session_scope() as session:
        shop_ids = list(await session.scalars(select(Shop.id)))
    for shop_id in shop_ids:
        async with session_scope() as session:
            cfg = await shop_settings.load(session, shop_id)
            hour = cfg.get("daily_report_hour")
            # Due from its hour on, so a missed hour sends it late instead of never (REF-06).
            start, end = day_bounds(report_day(now, 23 if hour is None else int(hour)))
        # The resellers' own report has its own marker: it goes out even with the staff report off.
        if cfg.get("reseller_daily_report") and await kv.get_redis().set(
            f"report:reseller:{shop_id}:{start.date().isoformat()}", "1", nx=True, ex=3 * 86400
        ):
            try:
                await _reseller_daily_reports(shop_id, start, end)
            except Exception:  # one shop's resellers must not stop the other shops' reports
                log.warning("reseller daily reports for shop %s failed", shop_id, exc_info=True)
        async with session_scope() as session:
            sections = list(cfg.get("daily_report_sections") or [])
            if not cfg.get("daily_report_enabled", True) or not sections:
                continue
            marker = f"report:daily:{shop_id}:{start.date().isoformat()}"
            if not await kv.get_redis().set(marker, "1", nx=True, ex=3 * 86400):
                continue
            summary = as_params(await daily_summary(session, shop_id, start, end))
            details = await daily_details(session, shop_id, start, end)
            shop_row = await session.get(Shop, shop_id)
            texts = Texts(
                shop_row.default_locale if shop_row else "fa", await load_overrides(session, shop_id)
            )
            messages = daily_messages(texts, jdate(start), summary, details, sections)
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
            for text in messages:
                try:
                    await telegram.send_text(chat_id, text, message_thread_id=thread_id, shop_id=shop_id)
                except telegram.Undeliverable:
                    log.warning("daily report to %s was not delivered", chat_id)
                    break


async def _reseller_daily_reports(shop_id: int, start: Any, end: Any) -> None:
    """Each active reseller's own day, sent to it by the shop's bot (ClockBot parity)."""
    from paurubot.core.formatting import jdate, money
    from paurubot.core.reports import reseller_day
    from paurubot.db.models import Customer, ResellerProfile
    from paurubot.i18n import texts_for
    from paurubot.notifications import telegram

    async with session_scope() as session:
        profiles = list(
            await session.scalars(
                select(ResellerProfile).where(
                    ResellerProfile.shop_id == shop_id, ResellerProfile.is_active.is_(True)
                )
            )
        )
        for profile in profiles:
            customer = await session.get(Customer, profile.customer_id)
            if customer is None or customer.is_banned:
                continue
            day = await reseller_day(session, profile, start, end)
            texts = await texts_for(session, shop_id, customer.locale)
            text = texts(
                "reseller_daily_report",
                date=jdate(start),
                orders=day.orders,
                spent=money(day.spent),
                balance=money(day.balance),
                bot_orders=day.bot_orders,
                bot_revenue=money(day.bot_revenue),
            )
            try:
                await telegram.send_text(customer.telegram_id, text, shop_id=shop_id)
            except telegram.Undeliverable:
                continue


# --- exchange rates and on-chain payments (PAY-11, PAY-13) ----------------------


@every(300, "fx.refresh")
async def refresh_rates() -> None:
    from paurubot.core import settings as shop_settings
    from paurubot.payments import fx

    uses_nobitex = False
    own_apis: dict[str, dict[str, object]] = {}
    async with session_scope() as session:
        for shop_id in list(await session.scalars(select(Shop.id))):
            cfg = await shop_settings.load(session, shop_id)
            if not (cfg.get("crypto_enabled") or cfg.get("nowpayments_enabled") or cfg.get("plisio_enabled")):
                continue
            if cfg.get("fx_source") == "nobitex":
                uses_nobitex = True
            elif cfg.get("fx_source") == "api":  # a shop's own rate API (ClockBot parity)
                own_apis[fx.api_key(cfg)] = cfg  # one fetch per distinct URL, path and divisor
    if uses_nobitex:
        await fx.refresh()
    for cfg in own_apis.values():
        try:
            await fx.refresh_api(cfg)
        except Exception:
            log.warning("own rate API refresh failed", exc_info=True)


@every(180, "services.ip_watch")
async def ip_watch() -> None:
    """Tell customers when a new device pushed an older one off their service (JOB-03)."""
    from paurubot.provisioning import ipwatch

    async with session_scope() as session:
        noticed = await ipwatch.run(session)
    if noticed:
        log.info("address watch sent %s notice(s)", noticed)


@every(60, "gateways.poll")
async def poll_gateways() -> None:
    """BluPal and AtlasPay are settled by asking them; AtlasPay has no callback at all."""
    from paurubot.payments import engine

    async with session_scope() as session:
        confirmed = await engine.poll_gateways(session)
    if confirmed:
        log.info("gateway poll confirmed %s payment(s)", confirmed)


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


@every(600, "broadcasts.unpin")
async def unpin_due() -> None:
    """Timed broadcast pins whose hours are up (GRW-06)."""
    from paurubot.db.models import BroadcastPin

    async with session_scope() as session:
        shops = list(
            await session.scalars(
                select(BroadcastPin.shop_id)
                .where(BroadcastPin.unpin_at.is_not(None), BroadcastPin.unpin_at <= utcnow())
                .distinct()
            )
        )
        for shop_id in shops:
            await queue.enqueue(
                session,
                "broadcast",
                "unpin",
                {},
                shop_id=shop_id,
                dedupe_key=f"unpin:{shop_id}:{utcnow().strftime('%Y%m%d%H%M')[:-1]}",
            )


@every(60, "cards.late_approve")
async def approve_late_receipts() -> None:
    """Card receipts nobody reviewed in time (PAY-17), when the shop allows it."""
    from paurubot.core import cards

    async with session_scope() as session:
        approved = await cards.approve_late(session)
    if approved:
        log.info("auto-approved %s card receipts after the review window", approved)


@every(600, "accounts.sync")
async def sync_panel_accounts() -> None:
    """Usage, alerts and expiry for sold panel admin accounts (TEN-14)."""
    from paurubot.core import panel_accounts

    async with session_scope() as session:
        synced = await panel_accounts.run(session)
    if synced:
        log.info("synced %s panel admin accounts", synced)


@every(3600, "panels.remnawave_loyalty")
async def remnawave_loyalty() -> None:
    """Remnawave users tagged by their buyer's active services (Foxima parity; off by default)."""
    from paurubot.core import remnawave_loyalty as loyalty

    async with session_scope() as session:
        await loyalty.run(session)


@every(1800, "panels.capacity")
async def capacity_warning() -> None:
    """A panel filling up past the shop's warning percent (ClockBot parity)."""
    from paurubot.core import capacity

    async with session_scope() as session:
        await capacity.check(session)


@every(120, "panels.monitor")
async def monitor_panels() -> None:
    """Uptime probes, live stats and node / quota alerts for every active panel (PNL-12)."""
    from paurubot.provisioning import monitor

    async with session_scope() as session:
        await monitor.run(session)


@every(6 * 3600, "apps.refresh")
async def refresh_apps() -> None:
    """Keep app download links (and Telegram mirrors) on the latest GitHub release (BOT-05)."""
    from paurubot.core import apps
    from paurubot.core import settings as shop_settings

    async with session_scope() as session:
        shops = list(await session.scalars(select(Shop.id)))
        configs = {shop_id: await shop_settings.load(session, shop_id) for shop_id in shops}
    for shop_id, cfg in configs.items():
        if cfg.get("download_apps"):
            await apps.refresh(shop_id, cfg["download_apps"], cfg.get("app_cache_chat_id"))


@every(60, "lottery.draw")
async def draw_lotteries() -> None:
    """Draw every lottery whose time is up and tell the winners (GRW-04)."""
    from paurubot.core import growth
    from paurubot.db.models import Lottery

    async with session_scope() as session:
        due = await growth.due_lotteries(session)
    for lottery_id in due:
        async with session_scope() as session:
            lottery = await session.get(Lottery, lottery_id, with_for_update=True)
            if lottery is None or lottery.status != "open":
                continue
            await growth.draw(session, lottery)
            await queue.enqueue(
                session,
                "notify",
                "lottery_result",
                {"lottery_id": lottery.id},
                shop_id=lottery.shop_id,
                dedupe_key=f"lottery:{lottery.id}:result",
            )


@every(30, "errors.flush")
async def flush_errors() -> None:
    """Bridge a recoverable Redis batch to durable, independently retried deliveries."""
    from paurubot.notifications.error_reports import flush

    await flush()


@every(30, "worker.heartbeat")
async def worker_heartbeat() -> None:
    """Lets the doctor tell whether a worker is running (ADM-08)."""
    from paurubot.ops import doctor

    await doctor.heartbeat()


@every(3600, "tickets.auto_close")
async def auto_close_tickets() -> None:
    """Answered tickets the customer left alone past the shop's set days (off by default)."""
    from paurubot.core import tickets

    async with session_scope() as session:
        await tickets.auto_close(session)


@every(6 * 3600, "domains.check")
async def check_domains() -> None:
    """Subscription-link domains: DNS, certificate and that they still reach us (ClockBot parity B4)."""
    from paurubot.core import domains

    async with session_scope() as session:
        await domains.run_checks(session)


@every(3600, "ticket_media.cleanup")
async def cleanup_ticket_media() -> None:
    from paurubot.core import ticket_media

    async with session_scope() as session:
        await ticket_media.prune(session)
