"""Usage sync for panels without webhooks (NEW-03), customer alerts (SVC-05) and cleanup.

Sync and cleanup share one owner per service with modifications (SEC-F09): each service is
handled in its own transaction under the service lease and a row lock taken without waiting.
A service that a renewal, move or reconciliation is changing is skipped until the next pass.
Its panel state is never read early and written late over the newer change, and it is never
deleted while it has an open order.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from paurubot import kv
from paurubot.config import get_settings
from paurubot.core import alert_policy, orders
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Job, JobStatus, Order, OrderStatus, Panel, Service, ServiceStatus
from paurubot.db.session import session_scope
from paurubot.panels import PanelError, PanelUser
from paurubot.provisioning.lifecycle import delete_service, lock_fresh, refresh, service_lease
from paurubot.worker import queue

log = logging.getLogger(__name__)

# Seconds; renewed while held. Covers one panel read or delete plus the commit.
BACKGROUND_LEASE_TTL = 60
# Panels whose usage is read at the same time (SEC-F27). Each runs in its own sessions, so a
# slow or dead panel no longer holds up the others. Within a panel, the driver's
# `parallel_reads` (lowered by the panel option `sync_concurrency`) bounds requests in flight.
PANEL_CONCURRENCY = 3
DOWN_AFTER = 5  # failures with nothing synced: the panel looks down, stop hammering it
CLEANUP_BATCH = 200  # services warned or deleted per shop in one cleanup run

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
            if alert_policy.is_low_volume(
                service.data_limit,
                service.used_bytes,
                alert_policy.warn_remaining_percent(cfg),
                warn_gb=alert_policy.warn_remaining_gb(cfg),
            ):
                once("low_volume", f"{period}:{service.data_limit}", {"remaining": remaining})
        if service.expire_at:
            seconds = (service.expire_at - datetime.now(UTC)).total_seconds()
            if alert_policy.is_expiring(
                seconds,
                alert_policy.warn_days(cfg),
                total_seconds=alert_policy.service_period_seconds(service),
                warn_time_pct=alert_policy.warn_time_percent(cfg),
            ):
                once("expiring", period, {"days": max(1, int(seconds // 86400) + 1)})
        nudge = int(cfg.get("first_use_nudge_hours") or 0)
        created = getattr(service, "created_at", None)
        if nudge > 0 and not service.used_bytes and created is not None:
            # Bought but never connected: one nudge with the connection guide (settings › reminders).
            age = (datetime.now(UTC) - created).total_seconds()
            if nudge * 3600 <= age <= nudge * 3600 + 7 * 86400:
                once("first_use", "1", {})
    if service.status == ServiceStatus.EXPIRED and previous != ServiceStatus.EXPIRED:
        once("expired", period, {})
    if service.status == ServiceStatus.LIMITED and previous != ServiceStatus.LIMITED:
        once("limited", f"{period}:{service.data_limit}", {})
    service.alerts_sent = sent
    # Inside the shop's quiet hours the alerts wait for the morning (settings › reminders).
    later = alert_policy.quiet_until(datetime.now(UTC), cfg, get_settings().tz) if jobs else None
    for payload in jobs:
        await queue.enqueue(
            session,
            "notify",
            "service_alert",
            payload,
            shop_id=service.shop_id,
            dedupe_key=f"alert:{service.id}:{payload['alert']}:{sent[str(payload['alert'])]}",
            run_at=later,
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


async def _sync_service(session: AsyncSession, panel_id: int, service_id: int) -> str:
    """One service under its lease; the session is clean again afterwards. Returns the outcome."""
    try:
        async with kv.lease(service_lease(service_id), ttl=BACKGROUND_LEASE_TTL):
            return await _sync_one(session, panel_id, service_id)
    except kv.LockNotAcquired:  # includes a lease lost mid-way: nothing was committed
        await session.rollback()
        return "busy"
    except PanelError as exc:
        await session.rollback()
        log.info("sync failed for service %s: %s", service_id, exc)
        return "failed"


async def _page(session: AsyncSession, panel_id: int, after: int, batch: int) -> list[int]:
    service_ids = list(
        await session.scalars(
            select(Service.id)
            .where(Service.panel_id == panel_id, Service.status.in_(LIVE_STATUSES), Service.id > after)
            .order_by(Service.id)
            .limit(batch)
        )
    )
    await session.commit()  # no lock or snapshot is kept across the panel calls
    return service_ids


class _Tally:
    def __init__(self) -> None:
        self.synced = self.failed = self.busy = 0

    def add(self, outcome: str) -> None:
        if outcome == "synced":
            self.synced += 1
        elif outcome == "failed":
            self.failed += 1
        elif outcome == "busy":
            self.busy += 1

    @property
    def down(self) -> bool:
        return self.failed >= DOWN_AFTER and self.synced == 0


async def sync_panel(session: AsyncSession, panel: Panel, *, batch: int = 200) -> tuple[int, int]:
    """Refresh every live service on one panel, one at a time in `session`. Returns (synced, failed)."""
    panel_id = panel.id
    tally = _Tally()
    last_id = 0
    while service_ids := await _page(session, panel_id, last_id, batch):
        for service_id in service_ids:
            last_id = service_id
            tally.add(await _sync_service(session, panel_id, service_id))
            if tally.down:
                return tally.synced, tally.failed
    if tally.busy:
        log.info("sync panel=%s skipped %s service(s) being changed", panel_id, tally.busy)
    return tally.synced, tally.failed


async def read_width(panel: Panel) -> int:
    """Requests the sync may keep in flight on this panel: the driver's safe limit, or less."""
    from paurubot.panels import driver_for

    try:
        limit = max(1, int((await driver_for(panel)).parallel_reads))
    except PanelError:
        return 1
    wanted = (panel.options or {}).get("sync_concurrency")
    if type(wanted) is int and wanted >= 1:
        limit = min(limit, wanted)
    return limit


async def sync_panel_isolated(panel_id: int, *, batch: int = 200) -> tuple[int, int]:
    """Refresh one panel with up to `read_width` workers, each in its own session (SEC-F27).

    An AsyncSession is never shared between concurrent tasks; each worker holds one session
    and commits per service, exactly like the sequential sync.
    """
    async with session_scope() as session:
        panel = await session.get(Panel, panel_id)
        if panel is None or not panel.is_active:
            return 0, 0
        width = await read_width(panel)
    tally = _Tally()
    last_id = 0
    while not tally.down:
        async with session_scope() as session:
            service_ids = await _page(session, panel_id, last_id, batch)
        if not service_ids:
            break
        last_id = service_ids[-1]
        pending = iter(service_ids)  # shared by the workers; next() never awaits

        async def worker(queue: Iterable[int]) -> None:
            async with session_scope() as session:
                for service_id in queue:
                    if tally.down:
                        return
                    tally.add(await _sync_service(session, panel_id, service_id))

        # A TaskGroup cancels the sibling workers if one fails: no worker outlives this call.
        async with asyncio.TaskGroup() as group:
            for _ in range(width):
                group.create_task(worker(pending))
    if tally.busy:
        log.info("sync panel=%s skipped %s service(s) being changed", panel_id, tally.busy)
    return tally.synced, tally.failed


async def sync_all(panel_ids: list[int]) -> dict[int, tuple[int, int] | BaseException]:
    """Sync several panels at once, `PANEL_CONCURRENCY` at a time; one panel's error stays its own."""
    gate = asyncio.Semaphore(PANEL_CONCURRENCY)

    async def one(panel_id: int) -> tuple[int, int]:
        async with gate:
            return await sync_panel_isolated(panel_id)

    results = await asyncio.gather(*(one(panel_id) for panel_id in panel_ids), return_exceptions=True)
    return dict(zip(panel_ids, results, strict=True))


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
    # A refresh that has not started yet reads the panel when it runs, so later events can join
    # it (AUD-3): a burst no longer buries paid orders in the provision queue. A running or
    # finished one may already be behind this event, so it never absorbs it.
    waiting = await session.scalar(
        select(Job.id)
        .where(
            Job.queue == "provision",
            Job.status == JobStatus.QUEUED,
            Job.kind == "refresh_service",
            Job.shop_id == service.shop_id,
            Job.payload["service_id"].as_integer() == service.id,
        )
        .limit(1)
    )
    if waiting is not None:
        return service
    await queue.enqueue(
        session,
        "provision",
        "refresh_service",
        {"service_id": service.id, "panel_id": panel.id},
        shop_id=service.shop_id,
    )
    return service


def ended_at(service: Service) -> datetime | None:
    """When a lapsed service stopped working: its end date, or the moment its data ran out (SEC-F15).

    An expired service without an end date has no better clock than its last row update.
    """
    if service.status not in {ServiceStatus.EXPIRED, ServiceStatus.LIMITED}:
        return None
    moments = [service.expire_at] if service.expire_at is not None else []
    if service.status == ServiceStatus.LIMITED and service.limited_at is not None:
        moments.append(service.limited_at)
    if (
        service.status == ServiceStatus.EXPIRED
        and service.expire_at is None
        and service.updated_at is not None
    ):
        moments.append(service.updated_at)
    return min(moments) if moments else None


def _expired_long_enough(service: Service, cutoff: datetime) -> bool:
    """Ended before `cutoff`: its end date passed then, or its data ran out then (SEC-F15)."""
    ended = ended_at(service)
    return ended is not None and ended < cutoff


def _ended_before(cutoff: datetime) -> ColumnElement[bool]:
    """`_expired_long_enough` in SQL."""
    return Service.status.in_([ServiceStatus.EXPIRED, ServiceStatus.LIMITED]) & or_(
        Service.expire_at < cutoff,
        (Service.status == ServiceStatus.LIMITED) & (Service.limited_at < cutoff),
        (Service.status == ServiceStatus.EXPIRED)
        & Service.expire_at.is_(None)
        & (Service.updated_at < cutoff),
    )


def _not_held_after(moment: datetime) -> ColumnElement[bool]:
    """Staff did not keep it out of the cleanup for good, or past `moment`."""
    return Service.cleanup_exempt.is_(False) & (Service.keep_until.is_(None) | (Service.keep_until <= moment))


def _held_after(service: Service, moment: datetime) -> bool:
    return bool(service.cleanup_exempt) or (service.keep_until is not None and service.keep_until > moment)


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


@dataclass(frozen=True, slots=True)
class CleanupRule:
    """When the cleanup deletes and warns, from the shop's settings."""

    now: datetime
    days: int  # delete this long after the service ended
    warn_days: int  # tell the customer this long before; the deletion waits this long after it

    @property
    def cutoff(self) -> datetime:
        return self.now - timedelta(days=self.days)

    def deletable(self, service: Service) -> bool:
        if not _expired_long_enough(service, self.cutoff) or _held_after(service, self.now):
            return False
        if self.warn_days <= 0 or service.is_trial:
            return True  # a trial is not renewed: nothing to warn about
        warned = service.cleanup_warned_at
        return warned is not None and warned <= self.now - timedelta(days=self.warn_days)

    def warnable(self, service: Service) -> bool:
        if self.warn_days <= 0 or service.is_trial or service.cleanup_warned_at is not None:
            return False
        soon = self.now + timedelta(days=self.warn_days)
        return _expired_long_enough(
            service, self.cutoff + timedelta(days=self.warn_days)
        ) and not _held_after(service, soon)

    def days_left(self, service: Service) -> int:
        """Whole days until the deletion, counted as the customer reads the warning."""
        ended = ended_at(service) or self.now
        due = max(ended + timedelta(days=self.days), self.now + timedelta(days=self.warn_days))
        if service.keep_until is not None:
            due = max(due, service.keep_until)
        return max(1, math.ceil((due - self.now).total_seconds() / 86400 - 0.01))


async def _locked_lapsed(session: AsyncSession, shop_id: int, service_id: int) -> Service | None:
    """The current row under its lock, or None (and the transaction closed) when it must be left."""
    service = await session.get(Service, service_id)
    if service is None or not await lock_fresh(session, service, skip_locked=True):
        return None
    if service.shop_id != shop_id or await _has_open_order(session, service.id):
        await session.commit()
        return None
    return service


async def _cleanup_one(session: AsyncSession, shop_id: int, service_id: int, rule: CleanupRule) -> bool:
    """Delete one stale service under its lease and row lock, and commit. Returns whether it did."""
    service = await _locked_lapsed(session, shop_id, service_id)
    if service is None:
        return False
    # Decide again on the locked, current row: it may have been renewed since the page was read.
    if not rule.deletable(service):
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


async def _warn_one(session: AsyncSession, shop_id: int, service_id: int, rule: CleanupRule) -> bool:
    """Tell the customer their lapsed service will be deleted soon; the deletion waits for it."""
    service = await _locked_lapsed(session, shop_id, service_id)
    if service is None:
        return False
    if not rule.warnable(service):
        await session.commit()
        return False
    cfg = await shop_settings.load(session, shop_id)
    # Inside the quiet hours the warning waits for the morning, and so does its countdown.
    later = alert_policy.quiet_until(rule.now, cfg, get_settings().tz)
    service.cleanup_warned_at = later or rule.now
    await queue.enqueue(
        session,
        "notify",
        "service_alert",
        {"service_id": service.id, "alert": "delete_soon", "days": rule.days_left(service)},
        shop_id=shop_id,
        dedupe_key=f"alert:{service.id}:delete_soon:{service.cleanup_warned_at.isoformat()}",
        run_at=later,
    )
    await session.commit()
    return True


async def _each_under_lease(
    session: AsyncSession,
    shop_id: int,
    service_ids: list[int],
    rule: CleanupRule,
    step: Callable[[AsyncSession, int, int, CleanupRule], Awaitable[bool]],
) -> int:
    done = 0
    for service_id in service_ids:
        try:
            async with kv.lease(service_lease(service_id), ttl=BACKGROUND_LEASE_TTL):
                if await step(session, shop_id, service_id, rule):
                    done += 1
        except kv.LockNotAcquired:
            await session.rollback()  # being changed right now; look again next run
        except PanelError:
            await session.rollback()
    return done


async def cleanup_expired(session: AsyncSession, shop_id: int) -> int:
    """Delete services that stayed expired or exhausted longer than the shop allows.

    With `delete_warn_days` on, the customer is told that many days before, and a service is only
    deleted that long after its warning. Services staff keep (for a while or for good) are skipped
    by the queries themselves, so any number of them never crowd out the rest of a batch.
    Returns how many were deleted.
    """
    cfg = await shop_settings.load(session, shop_id)
    days = int(cfg.get("delete_expired_after_days") or 0)
    if days <= 0:
        return 0
    rule = CleanupRule(utcnow(), days, max(0, min(30, int(cfg.get("delete_warn_days") or 0))))
    if rule.warn_days:
        soon = rule.now + timedelta(days=rule.warn_days)
        to_warn = list(
            await session.scalars(
                select(Service.id)
                .where(
                    Service.shop_id == shop_id,
                    _ended_before(rule.cutoff + timedelta(days=rule.warn_days)),
                    _not_held_after(soon),
                    Service.is_trial.is_(False),
                    Service.cleanup_warned_at.is_(None),
                )
                .order_by(Service.id)
                .limit(CLEANUP_BATCH)
            )
        )
        await session.commit()
        await _each_under_lease(session, shop_id, to_warn, rule, _warn_one)
    stale = select(Service.id).where(
        Service.shop_id == shop_id, _ended_before(rule.cutoff), _not_held_after(rule.now)
    )
    if rule.warn_days:
        warned_enough = Service.cleanup_warned_at <= rule.now - timedelta(days=rule.warn_days)
        stale = stale.where(Service.is_trial.is_(True) | warned_enough)
    service_ids = list(await session.scalars(stale.order_by(Service.id).limit(CLEANUP_BATCH)))
    await session.commit()
    return await _each_under_lease(session, shop_id, service_ids, rule, _cleanup_one)
