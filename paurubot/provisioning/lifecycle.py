"""Fulfilling paid orders against panels (SVC-01) and mapping panel state onto services."""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from paurubot import kv
from paurubot.core import catalog, usernames
from paurubot.core import settings as shop_settings
from paurubot.db.base import affected, utcnow
from paurubot.db.models import (
    Customer,
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    Plan,
    Service,
    ServiceStatus,
    TrialGrant,
)
from paurubot.panels import (
    GB,
    UNSET,
    PanelError,
    PanelRef,
    PanelUser,
    UserSpec,
    driver_for,
)
from paurubot.provisioning import claims, create_state, renewal_policy, renewal_state

log = logging.getLogger(__name__)

STATUS_MAP = {
    "active": ServiceStatus.ACTIVE,
    "on_hold": ServiceStatus.ON_HOLD,
    "limited": ServiceStatus.LIMITED,
    "expired": ServiceStatus.EXPIRED,
    "disabled": ServiceStatus.DISABLED,
}


def new_sub_token() -> str:
    return secrets.token_urlsafe(24)


async def ensure_sub_token(session: AsyncSession, service: Service) -> str:
    if not service.sub_token:
        service.sub_token = new_sub_token()
        await session.flush()
    return service.sub_token


class ServiceBusy(Exception):
    """A live or unresolved predecessor owns this service; defer, never refund it."""


def service_lease(service_id: int) -> str:
    """One owner for panel-changing work on a service (SEC-F09).

    Modifications, moves and renewal reconciliation hold it across their intent and progress
    commits. Background readers and cleanup take it non-blocking and skip a busy service, so
    they never act on a service mid-change.
    """
    return f"service:modify:{service_id}"


async def lock_fresh(session: AsyncSession, service: Service, *, skip_locked: bool = False) -> bool:
    """Row-lock `service` and reload it before reading or changing the panel (SEC-F09).

    The lock lasts until the caller's commit, so a panel answer read under it can only be written
    over a newer state if that state was committed after this read, and then it is not.
    Pending changes are flushed first so the reload keeps them. Returns False (and leaves
    the object untouched) when `skip_locked` and another transaction holds the row.
    """
    await session.flush()
    if skip_locked:
        locked = await session.scalar(
            select(Service.id).where(Service.id == service.id).with_for_update(skip_locked=True)
        )
        if locked is None:
            return False
    await session.refresh(service, with_for_update=True)
    return True


# How long a person's change waits for a renewal or sync holding the service, before saying busy.
EXCLUSIVE_WAIT = 5.0
EXCLUSIVE_TTL = 60


@asynccontextmanager
async def exclusive(session: AsyncSession, service: Service) -> AsyncIterator[Service]:
    """Own `service` for a staff or customer change of its panel account (SEC-F09).

    Background work already held the service lease; changes made by people (enable, disable,
    extend, delete, revoke, reset) did not, so one could compute its absolute values from a stale
    copy while a renewal was applying its own, and write over it. This takes the same lease
    (waiting a few seconds for a running job) and reloads the row under its lock, which then
    lasts until the caller commits. Raises ServiceBusy when the service stays taken.
    """
    try:
        async with kv.lease(service_lease(service.id), ttl=EXCLUSIVE_TTL, wait_seconds=EXCLUSIVE_WAIT):
            await lock_fresh(session, service)
            yield service
    except kv.LockNotAcquired as exc:
        if isinstance(exc, kv.LockLost):
            raise
        raise ServiceBusy(f"service {service.id} is being changed") from exc


class ProvisionError(Exception):
    """Permanent failure: retrying cannot help (e.g. no panel with capacity)."""


def ref(service: Service) -> PanelRef:
    return PanelRef(service.panel_username, dict(service.panel_data or {}))


def apply_panel_user(service: Service, info: PanelUser) -> None:
    service.used_bytes = info.used_bytes
    if info.limits_known:
        service.data_limit = info.data_limit
        service.expire_at = info.expire_at
    else:  # the panel only knows usage; limits are what we sold (PNL-11)
        info.data_limit, info.expire_at = service.data_limit, service.expire_at
    if info.online_at:
        service.online_at = info.online_at
    if info.subscription_url:
        service.subscription_url = info.subscription_url
    if info.data:
        merged = dict(service.panel_data or {})
        merged.update(info.data)
        service.panel_data = merged
    status = STATUS_MAP.get(info.status, ServiceStatus.ACTIVE)
    if status == ServiceStatus.ACTIVE:
        if info.data_limit and info.used_bytes >= info.data_limit:
            status = ServiceStatus.LIMITED
        elif info.expire_at and info.expire_at <= datetime.now(UTC):
            status = ServiceStatus.EXPIRED
    if service.status != ServiceStatus.DELETED:
        service.status = status
    service.synced_at = utcnow()


# How a renewal treats the time and data left (SVC-13): the end date either extends from the
# current one or starts again today; data either resets to the plan or carries what's left over.
# The rules themselves live in the renewal policy engine.
RENEW_MODES = renewal_policy.RENEW_MODES


def renew_mode(plan: Plan | None, panel: Panel | None, cfg: dict[str, Any]) -> str:
    """The plan's own mode, else the panel's reset choice, else the shop's."""
    if plan is not None and plan.renew_mode in RENEW_MODES:
        return str(plan.renew_mode)
    panel_reset = (panel.options or {}).get("renew_resets_usage") if panel is not None else None
    if panel_reset is not None:
        return "extend_reset" if panel_reset else "extend_carry"
    if cfg.get("renew_mode") in RENEW_MODES:
        return str(cfg["renew_mode"])
    return "extend_reset" if cfg.get("renew_resets_usage", True) else "extend_carry"


def _plan_bytes(plan: Plan) -> int:
    return int(plan.data_gb) * GB if plan.data_gb > 0 else 0


def renewal_target(service: Service, plan: Plan, mode: str) -> tuple[datetime | None, int, bool]:
    """New expiry, data limit and whether usage resets when renewing with `plan`."""
    policy = renewal_policy.policy_for(OrderKind.RENEW, mode, lapsed=service.status in renewal_policy.LAPSED)
    target = renewal_policy.compute(service, policy, days=plan.days, data_bytes=_plan_bytes(plan))
    return target.expire, int(target.limit or 0), target.reset


async def claim(session: AsyncSession, order_id: int) -> Order | None:
    """PAID → PROVISIONING. A retried job finds the order already provisioning and continues it."""
    result = await session.execute(
        update(Order)
        .where(Order.id == order_id, Order.status.in_([OrderStatus.PAID, OrderStatus.PROVISIONING]))
        .values(status=OrderStatus.PROVISIONING)
    )
    if not affected(result):
        return None
    order = await session.get(Order, order_id, populate_existing=True)
    await session.commit()
    return order


async def _panel_for_new(session: AsyncSession, order: Order) -> Panel:
    if order.kind == OrderKind.TRIAL:
        panel = await session.get(Panel, int(order.params.get("panel_id") or 0))
    elif order.params.get("custom"):
        if order.params.get("panel_id"):
            panel = await session.get(Panel, int(order.params["panel_id"]))
        else:
            cfg = await shop_settings.load(session, order.shop_id)
            panel = await catalog.pick_panel_for(
                session, order.shop_id, cfg["custom_panel_id"], cfg["custom_location_id"]
            )
    else:
        plan = await session.get(Plan, order.plan_id) if order.plan_id else None
        if plan is None:
            raise ProvisionError("plan_unavailable")
        if order.params.get("panel_id"):
            panel = await session.get(Panel, int(order.params["panel_id"]))
        else:
            panel = await catalog.pick_panel(session, plan)
    if panel is None or not panel.is_active:
        raise ProvisionError("panel_unavailable")
    if order.kind == OrderKind.TRIAL and (panel.options or {}).get("allow_trials") is False:
        raise ProvisionError("panel_unavailable")
    return panel


async def _claim_name(session: AsyncSession, order: Order, panel: Panel, first: str) -> str:
    """Claim `first`, else a random-tailed variant of it, for this order on `panel` (SEC-F03)."""
    candidate = first
    for _ in range(claims.MAX_TRIES):
        if await claims.claim(session, panel.id, candidate, order.id):
            return candidate
        candidate = claims.alternative(first)
    raise ProvisionError("panel_username_unavailable")


def _created_here(order: Order, panel_id: int, username: str) -> bool:
    """Whether this order itself asked the panel to create exactly this account."""
    rec = create_state.record(order)
    return (
        create_state.state(order) == create_state.ATTEMPTED
        and rec.get("panel_id") == panel_id
        and rec.get("username") == username
    )


async def _create_service(session: AsyncSession, order: Order) -> Service:
    customer = await session.get(Customer, order.customer_id)
    assert customer is not None
    panel = await _panel_for_new(session, order)
    plan = await session.get(Plan, order.plan_id) if order.plan_id else None
    params = dict(order.params or {})
    # Claim the chosen panel and username before calling the panel, in one commit: no other
    # order can pick the same name, and a retry after a crash finds its own account.
    if not params.get("panel_username") or params.get("panel_id") != panel.id:
        params["panel_id"] = panel.id
        prefix = plan.username_prefix if plan else None
        if params.get("wanted_username") and customer.telegram_id > 0:
            first = await usernames.chosen(session, panel, str(params["wanted_username"]), prefix)
        else:
            first = await usernames.generate(session, panel, customer, params.get("label"), prefix)
        params["panel_username"] = await _claim_name(session, order, panel, first)
        params["username_claimed"] = True
        order.params = params
        await session.commit()
    username = params["panel_username"]
    on_hold = False
    options: dict[str, Any] = {}
    if order.kind == OrderKind.TRIAL:
        days, data_gb, ip_limit = int(params.get("days") or 1), int(params.get("gb") or 1), 0
    elif params.get("custom"):
        custom = params["custom"]
        days, data_gb, ip_limit = int(custom.get("days") or 0), int(custom.get("gb") or 0), 0
    else:
        assert plan is not None
        days, data_gb, ip_limit = plan.days, plan.data_gb, plan.ip_limit
        on_hold = bool(plan.on_hold) and days > 0
        options = {k: v for k, v in (plan.panel_options or {}).items() if v not in (None, "", [], {})}
    expire_at = (
        datetime.now(UTC).replace(microsecond=0) + timedelta(days=days) if days > 0 and not on_hold else None
    )
    spec = UserSpec(
        username=username,
        data_limit=data_gb * GB if data_gb > 0 else 0,
        expire_at=expire_at,
        note=f"tg:{customer.telegram_id}",
        ip_limit=ip_limit,
        on_hold_days=days if on_hold else None,
        options=options,
    )
    driver = await driver_for(panel)
    existing_service = await session.scalar(
        select(Service).where(Service.panel_id == panel.id, Service.panel_username == username)
    )
    if existing_service is not None:
        creator = await session.scalar(
            select(Order.id)
            .where(
                Order.service_id == existing_service.id,
                Order.kind.in_([OrderKind.PURCHASE, OrderKind.TRIAL]),
                Order.id != order.id,
            )
            .limit(1)
        )
        if (
            existing_service.customer_id != order.customer_id
            or existing_service.shop_id != order.shop_id
            or creator is not None
        ):
            raise ProvisionError("panel username belongs to another order")
        return existing_service
    info = await driver.get_user(PanelRef(username, params.get("panel_data") or {}))
    if info is not None and params.get("username_claimed") and not _created_here(order, panel.id, username):
        # An account this order never asked for already uses the name (made by staff, another
        # bot or an earlier install). It is someone else's: never hand it over (SEC-F03).
        # Orders claimed before this rule existed keep their old adopt-on-retry behaviour.
        log.warning(
            "order %s: panel %s already has an account named %s; renaming", order.id, panel.id, username
        )
        for _ in range(claims.MAX_TRIES):
            username = await _claim_name(session, order, panel, claims.alternative(username))
            order.params = {**(order.params or {}), "panel_username": username}
            await session.commit()
            info = await driver.get_user(PanelRef(username, {}))
            if info is None:
                break
        else:
            raise ProvisionError("panel_username_unavailable")
        params = dict(order.params or {})
        spec.username = username
    if info is None:
        # Commit the intent BEFORE the remote call (SEC-F08): from here on a lost response,
        # timeout or crash may leave an account on the panel, so giving up must not refund.
        create_state.attempt(order, panel.id, username)
        await session.commit()
        info = await driver.create_user(spec)
    create_state.confirm(order)
    service = Service(
        shop_id=order.shop_id,
        customer_id=customer.id,
        panel_id=panel.id,
        plan_id=plan.id if plan else None,
        panel_username=info.username or username,
        panel_data=info.data or {},
        label=params.get("label") if order.kind != OrderKind.TRIAL else None,
        is_trial=order.kind == OrderKind.TRIAL,
        data_limit=spec.data_limit,
        expire_at=expire_at,
        status=ServiceStatus.ON_HOLD if on_hold else ServiceStatus.ACTIVE,
        sub_token=new_sub_token(),
    )
    apply_panel_user(service, info)
    service.expire_at = service.expire_at or expire_at
    session.add(service)
    await session.flush()
    if order.kind == OrderKind.TRIAL and params.get("grant_id"):
        grant = await session.get(TrialGrant, int(params["grant_id"]))
        if grant is not None:
            grant.status = "issued"
            grant.service_id = service.id
    return service


def _target_for(
    service: Service, order: Order, plan: Plan | None, mode: str, *, queued: bool = False
) -> dict[str, object]:
    """Absolute end state of a modification, computed once so retries are idempotent."""
    try:
        policy = renewal_policy.policy_for(
            order.kind, mode, queued=queued, lapsed=service.status in renewal_policy.LAPSED
        )
    except renewal_policy.UnsupportedChange as exc:
        raise ProvisionError("unsupported_order") from exc
    if order.kind in {OrderKind.RENEW, OrderKind.UPGRADE}:
        assert plan is not None
        days, data_bytes = plan.days, _plan_bytes(plan)
    else:
        days = int(order.params.get("extra_days") or 0) if order.kind == OrderKind.EXTRA_DAYS else 0
        data_bytes = (
            int(order.params.get("extra_gb") or 0) * GB if order.kind == OrderKind.EXTRA_VOLUME else 0
        )
    target = renewal_policy.compute(
        service, policy, days=days, data_bytes=data_bytes, addon=order.kind in renewal_policy.ADDONS
    )
    return target.as_params()


async def _modify_service(session: AsyncSession, order: Order) -> Service:
    """Apply a renewal, upgrade or add-on through the renewal state machine (renewal_state)."""
    service = (
        await session.get(Service, order.service_id, populate_existing=True) if order.service_id else None
    )
    if service is None or service.status == ServiceStatus.DELETED:
        raise ProvisionError("service_not_found")
    panel = await session.get(Panel, service.panel_id)
    assert panel is not None
    plan = await session.get(Plan, order.plan_id) if order.plan_id else None
    if order.kind in {OrderKind.RENEW, OrderKind.UPGRADE} and plan is None:
        raise ProvisionError("plan_unavailable")
    params = dict(order.params or {})
    if "target" not in params:
        cfg = await shop_settings.load(session, order.shop_id)
        mode = renew_mode(plan, panel, cfg)
        params["target"] = _target_for(service, order, plan, mode, queued=bool(params.get("queued")))
        params["reset_protocol"] = 1
        order.params = params
        renewal_state.move(order, renewal_state.PENDING)
        await session.commit()
    elif renewal_state.state(order) is None:
        # Started before the state machine existed: the panel may already hold it.
        renewal_state.move(order, renewal_state.PENDING, "resumed")
        renewal_state.move(order, renewal_state.RUNNING)
        renewal_state.move(order, renewal_state.UNCERTAIN, "started before the state machine")
        await session.commit()
    if renewal_state.is_settled(order):
        return service  # an earlier attempt or reconciliation already applied it; just finish
    target = dict(order.params["target"])
    expire_value = target.get("expire")
    expire_at: datetime | None = datetime.fromisoformat(str(expire_value)) if expire_value else None
    driver = await driver_for(panel)
    if order.kind.value not in driver.changes:
        raise ProvisionError("unsupported_order")
    if plan is not None and order.kind in {OrderKind.RENEW, OrderKind.UPGRADE}:
        chosen = (plan.panel_options or {}).get("profile")
        if chosen and (service.panel_data or {}).get("profile") != chosen:
            # Panels that sell profiles (PNL-11) renew or upgrade to the plan's own profile.
            service.panel_data = {**(service.panel_data or {}), "profile": str(chosen)}
            await session.flush()
    verifies = renewal_policy.should_verify(panel.options, driver)
    if renewal_state.state(order) == renewal_state.RUNNING:
        renewal_state.move(order, renewal_state.UNCERTAIN, "interrupted")  # a crashed worker
        await session.commit()
    if renewal_policy.reset_untracked(target, order.params or {}):
        # Old workers had no reservation protocol. Never infer "not called" from missing keys.
        if renewal_state.state(order) == renewal_state.PENDING:
            renewal_state.move(order, renewal_state.RUNNING)
            renewal_state.move(order, renewal_state.UNCERTAIN, "legacy reset has no reservation")
        renewal_state.park(order, "legacy_reset_uncertain")
        await session.commit()
        raise renewal_policy.NotConfirmed(["legacy_reset_uncertain"])
    if renewal_state.state(order) == renewal_state.UNCERTAIN and verifies:
        # Look before acting: an earlier attempt may have reached the panel after all. A panel that
        # cannot report limits proves nothing; re-applying the absolute target is safe instead
        # (the usage reset is never repeated, see _apply_target).
        current = await driver.get_user(ref(service))
        if (
            current is not None
            and renewal_policy.can_confirm(current)
            and (
                not renewal_policy.reset_pending(order.params or {})
                or renewal_policy.reset_confirmed(target, current)
            )
            and not renewal_policy.verify(target, current)
        ):
            if renewal_policy.reset_pending(order.params or {}):
                order.params = {**(order.params or {}), "reset_done": True}
            apply_panel_user(service, current)
            _settle_service(service, order, plan, target, verified=True)
            renewal_state.move(order, renewal_state.RECONCILED, "found applied on retry")
            await session.commit()
            return service
    renewal_state.move(order, renewal_state.RUNNING)
    await session.commit()
    try:
        verified = await _apply_target(session, order, service, driver, target, expire_at, verifies)
    except Exception as exc:
        renewal_state.move(order, renewal_state.UNCERTAIN, f"{exc.__class__.__name__}: {exc}")
        try:
            await session.commit()
        except Exception:
            log.exception("order %s: could not record the uncertain renewal", order.id)
        raise
    _settle_service(service, order, plan, target, verified=verified)
    renewal_state.move(order, renewal_state.APPLIED)
    await session.commit()  # from here a retry only finishes the order, it never re-applies
    return service


async def _apply_target(
    session: AsyncSession,
    order: Order,
    service: Service,
    driver: Any,
    target: dict[str, Any],
    expire_at: datetime | None,
    verifies: bool,
) -> bool:
    """The panel calls of a modification. Returns whether the panel confirmed the target."""
    if target.get("reset"):
        # Re-read under a row lock: a stale worker must see another worker's reset reservation.
        await session.refresh(order, with_for_update=True)
        params = dict(order.params or {})
        if renewal_policy.reset_untracked(target, params):
            raise renewal_policy.NotConfirmed(["legacy_reset_uncertain"])
        if renewal_policy.reset_pending(params):
            fresh = await driver.get_user(ref(service))
            if fresh is None or not renewal_policy.reset_confirmed(target, fresh):
                order.params = {
                    **params,
                    "verification": {
                        "ok": False,
                        "problems": ["reset_uncertain"],
                        "checked_at": utcnow().isoformat(),
                    },
                }
                await session.commit()
                raise renewal_policy.NotConfirmed(["reset_uncertain"])
            order.params = {**params, "reset_done": True}
            await session.commit()
        elif not params.get("reset_done"):
            # Commit the intent BEFORE the remote call. A lost response (or a killed worker)
            # leaves an uncertain reservation, never permission to reset usage a second time.
            order.params = {**params, "reset_attempted": utcnow().isoformat()}
            await session.commit()
            await driver.reset_usage(ref(service))
            order.params = {**(order.params or {}), "reset_done": True}
            await session.commit()
    info = await driver.update_user(
        ref(service),
        expire_at=expire_at if target.get("set_expire") else UNSET,
        data_limit=target.get("limit"),
        enabled=True,
    )
    apply_panel_user(service, info)
    if not verifies:
        return False
    # Read the account back: the change counts only once the panel really holds the target.
    fresh = await driver.get_user(ref(service))
    problems = ["missing_on_panel"] if fresh is None else renewal_policy.verify(target, fresh)
    order.params = {
        **(order.params or {}),
        "verification": {"ok": not problems, "problems": problems, "checked_at": utcnow().isoformat()},
    }
    if problems:
        log.warning("order %s: panel did not confirm the change: %s", order.id, problems)
        raise renewal_policy.NotConfirmed(problems)
    assert fresh is not None
    apply_panel_user(service, fresh)
    return True


def _settle_service(
    service: Service, order: Order, plan: Plan | None, target: dict[str, Any], *, verified: bool
) -> None:
    """Record the applied target on the service."""
    expire_value = target.get("expire")
    if target.get("reset") and not verified:
        service.used_bytes = 0  # unverified: assume the reset worked
    if target.get("set_expire"):
        service.expire_at = datetime.fromisoformat(str(expire_value)) if expire_value else None
    if target.get("limit") is not None:
        service.data_limit = int(target["limit"])
    if plan is not None and order.kind in {OrderKind.RENEW, OrderKind.UPGRADE}:
        service.plan_id = plan.id
        service.is_trial = False
    service.status = ServiceStatus.ACTIVE
    service.disabled_reason = None
    service.alerts_sent = {}


# --- reconciliation of parked renewals ------------------------------------------------------


async def parked_renewals(
    session: AsyncSession, limit: int = 200, *, after_id: int = 0, through_id: int | None = None
) -> list[int]:
    """Filter BEFORE limiting; keyset pages let unresolved old rows coexist with newer ones."""
    stmt = (
        select(Order.id)
        .where(
            Order.status == OrderStatus.PROVISIONING,
            Order.kind.in_(list(renewal_state.KINDS)),
            Order.params["renewal"]["parked"].as_boolean().is_(True),
            Order.id > after_id,
        )
        .order_by(Order.id)
        .limit(limit)
    )
    if through_id is not None:
        stmt = stmt.where(Order.id <= through_id)
    return list(await session.scalars(stmt))


async def _resume(session: AsyncSession, order: Order) -> None:
    """Run the order's provisioning job again so it finishes (rewards, notification)."""
    from paurubot.db.models import Job
    from paurubot.worker import queue

    key = f"order:{order.id}:provision"
    job_id = await session.scalar(select(Job.id).where(Job.dedupe_key == key))
    if job_id is None or not await queue.retry_dead(session, job_id):
        await queue.enqueue(
            session, "provision", "order", {"order_id": order.id}, shop_id=order.shop_id, dedupe_key=key
        )


async def _alert_uncertain(
    session: AsyncSession, order: Order, outcome: str, detail: str | None = None
) -> None:
    from paurubot.notifications.handlers import report

    text = (
        f"⚠️ Order #{order.id} ({order.kind.value}): panel state is uncertain ({outcome}).\n"
        + (detail or "It matches neither the renewal target nor the previous state")
        + "; no refund was made. Please check the service."
    )
    try:
        await report(session, order.shop_id, "errors", text)
    except Exception:
        log.exception("could not report uncertain renewal %s", order.id)


# Panels that cannot report limits (PNL-11) can prove neither that a renewal is there nor that it
# is not: settling it from what they show would either give the change away or keep the money
# for nothing. Reconciliation runs the job again instead; the target is absolute and the usage
# reset is never repeated, so applying it again is safe. After a few tries the staff decide.
UNVERIFIABLE_RETRIES = 3


async def _reapply_unverifiable(session: AsyncSession, order: Order) -> str:
    checks = renewal_state.note_check(order, "unverifiable")
    if checks <= UNVERIFIABLE_RETRIES:
        order.params = {**(order.params or {}), "renewal": {**renewal_state.record(order), "parked": False}}
        await _resume(session, order)
        await session.commit()
        return "reapplied"
    if checks == UNVERIFIABLE_RETRIES + 1:
        await _alert_uncertain(
            session,
            order,
            "unverifiable",
            f"The panel cannot report limits and {UNVERIFIABLE_RETRIES} more attempts did not finish it",
        )
    await session.commit()
    return "uncertain"


async def reconcile_renewal(session: AsyncSession, order_id: int) -> str:
    order = await session.get(Order, order_id)
    if order is None or not order.service_id:
        return await _reconcile_renewal(session, order_id)
    try:
        async with kv.lease(service_lease(order.service_id), ttl=180):
            await session.refresh(order)
            if await _modification_predecessor(session, order) is not None:
                raise ServiceBusy("another unresolved modification owns the service")
            return await _reconcile_renewal(session, order_id)
    except kv.LockNotAcquired as exc:
        raise ServiceBusy("service reconciliation is busy") from exc


async def _reconcile_renewal(session: AsyncSession, order_id: int) -> str:
    """Settle one parked order by reading the panel. Never re-applies the change by itself.

    - the panel holds the target → ``reconciled`` and the order is finished;
    - the panel still shows the state before the change → ``failed`` and the order is refunded;
    - the panel cannot report limits → the job runs again (``reapplied``), at most
      ``UNVERIFIABLE_RETRIES`` times, then it stays ``uncertain`` for the staff;
    - anything else (partial change, panel unreachable) → stays ``uncertain`` for the staff.
    """
    order = await session.get(Order, order_id, with_for_update=True, populate_existing=True)
    if order is None or order.status != OrderStatus.PROVISIONING or not renewal_state.parked(order):
        return "skipped"
    if renewal_state.is_settled(order):
        renewal_state.note_check(order, "settled")
        await _resume(session, order)
        order.params = {**(order.params or {}), "renewal": {**renewal_state.record(order), "parked": False}}
        await session.commit()
        return "resumed"
    service = await session.get(Service, order.service_id) if order.service_id else None
    panel = await session.get(Panel, service.panel_id) if service is not None else None
    target = dict((order.params or {}).get("target") or {})
    if service is None or panel is None or not target:
        outcome = "service_missing"
    else:
        try:
            current = await (await driver_for(panel)).get_user(ref(service))
        except PanelError as exc:
            current, outcome = None, f"panel_unreachable: {exc}"
        else:
            outcome = "missing_on_panel" if current is None else "partial"
        if renewal_policy.reset_untracked(target, order.params or {}):
            current, outcome = None, "legacy_reset_uncertain"
        if current is not None and renewal_policy.reset_pending(order.params or {}):
            if renewal_policy.reset_confirmed(target, current):
                order.params = {**(order.params or {}), "reset_done": True}
                await session.commit()
            else:
                # Even an unchanged account is not proof that the reserved reset never ran.
                current, outcome = None, "reset_uncertain"
        if current is not None and not renewal_policy.can_confirm(current):
            return await _reapply_unverifiable(session, order)
        if current is not None:
            if not renewal_policy.verify(target, current):
                apply_panel_user(service, current)
                plan = await session.get(Plan, order.plan_id) if order.plan_id else None
                _settle_service(service, order, plan, target, verified=True)
                renewal_state.move(order, renewal_state.RECONCILED, "found applied by reconciliation")
                await _resume(session, order)
                await session.commit()
                return "reconciled"
            before = renewal_policy.untouched(target)
            reset_done = bool((order.params or {}).get("reset_done"))
            if (
                before is not None
                and not reset_done
                and not (order.params or {}).get("reset_attempted")
                and not renewal_policy.verify(before, current, check_enabled=False)
            ):
                renewal_state.move(order, renewal_state.FAILED, "panel unchanged")
                await session.commit()
                await give_up(session, order.id, "renewal was not applied on the panel")
                return "failed"
    if renewal_state.note_check(order, outcome) == 1:
        await _alert_uncertain(session, order, outcome)
    await session.commit()
    return "uncertain"


# --- reconciliation of parked purchases (SEC-F08) ------------------------------------------


async def parked_purchases(
    session: AsyncSession, limit: int = 200, *, after_id: int = 0, through_id: int | None = None
) -> list[int]:
    stmt = (
        select(Order.id)
        .where(
            Order.status == OrderStatus.PROVISIONING,
            Order.kind.in_(list(create_state.KINDS)),
            Order.params["create"]["parked"].as_boolean().is_(True),
            Order.id > after_id,
        )
        .order_by(Order.id)
        .limit(limit)
    )
    if through_id is not None:
        stmt = stmt.where(Order.id <= through_id)
    return list(await session.scalars(stmt))


async def reconcile_purchase(session: AsyncSession, order_id: int) -> str:
    """Settle one parked purchase or trial by reading the panel. Never creates by itself.

    - the account exists → provisioning resumes and adopts it (``resumed``);
    - the panel says it does not exist → the order is refunded (``refunded``);
    - the last create request is still recent → look again later (``waiting``);
    - panel unreachable, username held by another service, or too many resumes → stays
      parked for the staff (``uncertain``), who are told once.
    """
    order = await session.get(Order, order_id, with_for_update=True, populate_existing=True)
    if (
        order is None
        or order.status != OrderStatus.PROVISIONING
        or order.kind not in create_state.KINDS
        or not create_state.parked(order)
    ):
        return "skipped"
    if create_state.state(order) != create_state.ATTEMPTED:
        # Parked by an older code path or edited: nothing claims the panel was touched.
        create_state.unpark(order)
        await session.commit()
        await give_up(session, order.id, "no create request is on record")
        return "refunded"
    since = create_state.attempted_at(order)
    if since is not None and utcnow() - since < create_state.SETTLE_DELAY:
        return "waiting"
    rec = create_state.record(order)
    params = order.params or {}
    panel_id, username = (
        rec.get("panel_id") or params.get("panel_id"),
        rec.get("username") or params.get("panel_username"),
    )
    panel = await session.get(Panel, int(panel_id)) if panel_id else None
    if panel is None or not isinstance(username, str) or not username:
        outcome = "panel_missing"
    else:
        holder = await session.scalar(
            select(Service).where(Service.panel_id == panel.id, Service.panel_username == username)
        )
        if holder is not None and (
            holder.customer_id != order.customer_id or holder.shop_id != order.shop_id
        ):
            outcome = "username_taken"
        else:
            try:
                info = await (await driver_for(panel)).get_user(
                    PanelRef(username, params.get("panel_data") or {})
                )
            except PanelError as exc:
                outcome = f"panel_unreachable: {exc}"
            else:
                if info is None:
                    create_state.absent(order)
                    await session.commit()
                    await give_up(session, order.id, "the account was not created on the panel")
                    return "refunded"
                if create_state.note_resume(order) <= create_state.MAX_RESUMES:
                    create_state.unpark(order)
                    await _resume(session, order)
                    await session.commit()
                    return "resumed"
                outcome = "resume_exhausted"
    if create_state.note_check(order, outcome) == 1:
        await _alert_uncertain(
            session, order, outcome, "The bot cannot tell whether the panel account exists"
        )
    await session.commit()
    return "uncertain"


def _move_target(service: Service, plan: Plan | None) -> dict[str, Any]:
    """What the service carries to its new panel: the data still left and the same end date (SVC-04)."""
    left = 0
    if service.data_limit:
        left = max(int(service.data_limit) - int(service.used_bytes), 0)
    on_hold_days = None
    if service.status == ServiceStatus.ON_HOLD and plan is not None and plan.days > 0:
        on_hold_days = plan.days
    return {
        "limit": left if service.data_limit else 0,
        "unlimited": not service.data_limit,
        "expire": service.expire_at.isoformat() if service.expire_at and on_hold_days is None else None,
        "on_hold_days": on_hold_days,
        "ip_limit": plan.ip_limit if plan is not None else 0,
    }


async def _move_service(session: AsyncSession, order: Order) -> Service:
    """Recreate the service on another panel with what it has left, then remove the old account.

    The branded subscription link (sub_token) is kept, so customers using it need no new link.
    Every step is recorded in the order first, so a retried job continues instead of duplicating.
    """
    service = await session.get(Service, order.service_id) if order.service_id else None
    if service is None or service.status == ServiceStatus.DELETED:
        raise ProvisionError("service_not_found")
    params = dict(order.params or {})
    target = await session.get(Panel, int(params.get("move_to") or 0))
    if target is None or target.shop_id != service.shop_id or not target.is_active:
        raise ProvisionError("panel_unavailable")
    if service.panel_id == target.id and params.get("moved"):
        return service  # finished on an earlier attempt
    plan = await session.get(Plan, service.plan_id) if service.plan_id else None
    if "move" not in params:
        old_panel = await session.get(Panel, service.panel_id)
        if old_panel is not None:
            try:
                await refresh(session, service)
            except PanelError:
                log.info("old panel unreachable while moving service %s; using last known usage", service.id)
            if service.status == ServiceStatus.DELETED:
                await session.commit()
                raise ProvisionError("service_not_found")
        customer = await session.get(Customer, service.customer_id)
        assert customer is not None
        first = await usernames.generate(
            session, target, customer, service.label, plan.username_prefix if plan else None
        )
        params["move"] = {
            **_move_target(service, plan),
            "username": await _claim_name(session, order, target, first),
            "claimed": True,
            "from_panel": service.panel_id,
            "from_username": service.panel_username,
            "from_data": dict(service.panel_data or {}),
        }
        order.params = params
        await session.commit()
    move = params["move"]
    customer = await session.get(Customer, service.customer_id)
    assert customer is not None
    options: dict[str, Any] = {}
    if plan is not None and plan.panel_id == target.id:
        options = {k: v for k, v in (plan.panel_options or {}).items() if v not in (None, "", [], {})}
    spec = UserSpec(
        username=move["username"],
        data_limit=int(move["limit"]) if not move.get("unlimited") else 0,
        expire_at=datetime.fromisoformat(move["expire"]) if move.get("expire") else None,
        note=f"tg:{customer.telegram_id}",
        ip_limit=int(move.get("ip_limit") or 0),
        on_hold_days=move.get("on_hold_days"),
        options=options,
    )
    if not move.get("unlimited") and spec.data_limit <= 0:
        spec.data_limit = 1  # nothing left: keep it limited instead of turning it unlimited
    driver = await driver_for(target)
    info = await driver.get_user(PanelRef(spec.username, {}))
    if info is not None and move.get("claimed") and move.get("created") != spec.username:
        # Someone else's account under the claimed name: never move the service onto it (SEC-F03).
        log.warning("move order %s: panel %s already has %s; renaming", order.id, target.id, spec.username)
        for _ in range(claims.MAX_TRIES):
            spec.username = await _claim_name(session, order, target, claims.alternative(spec.username))
            move = {**move, "username": spec.username}
            order.params = params = {**(order.params or {}), "move": move}
            await session.commit()
            info = await driver.get_user(PanelRef(spec.username, {}))
            if info is None:
                break
        else:
            raise ProvisionError("panel_username_unavailable")
    if info is None:
        # The intent before the call: a retry adopts only an account it asked for itself.
        move = {**move, "created": spec.username}
        order.params = params = {**(order.params or {}), "move": move}
        await session.commit()
        info = await driver.create_user(spec)
    service.panel_id = target.id
    service.panel_username = info.username or spec.username
    service.panel_data = info.data or {}
    service.subscription_url = None
    service.billed_bytes = 0  # usage starts again on the new panel; metered billing follows it
    apply_panel_user(service, info)
    if move.get("on_hold_days"):
        service.status = ServiceStatus.ON_HOLD
    params["moved"] = True
    order.params = params
    await session.flush()
    from paurubot.worker import queue

    await queue.enqueue(
        session,
        "provision",
        "cleanup_user",
        {"panel_id": move["from_panel"], "username": move["from_username"], "data": move["from_data"]},
        shop_id=service.shop_id,
        dedupe_key=f"order:{order.id}:cleanup",
        max_attempts=12,
    )
    return service


async def remove_panel_user(
    session: AsyncSession, panel_id: int, username: str, data: dict[str, Any]
) -> None:
    """Delete an account the bot no longer tracks (left behind by a move). Missing is fine."""
    from paurubot.panels import PanelNotFound

    panel = await session.get(Panel, panel_id)
    if panel is None:
        return
    tracked = await session.scalar(
        select(Service.id).where(
            Service.panel_id == panel_id,
            Service.panel_username == username,
            Service.status != ServiceStatus.DELETED,
        )
    )
    if tracked is not None:
        return  # a live service uses this account again; never delete it
    try:
        await (await driver_for(panel)).delete_user(PanelRef(username, data))
    except PanelNotFound:
        return


async def _modification_predecessor(session: AsyncSession, order: Order) -> int | None:
    """Started unresolved changes outrank fresh orders, even with a higher order ID.

    This also quarantines pre-deploy overlapping targets: they need staff resolution, not an
    arbitrary winner. FIFO by ID applies only when the current order has never stored a target.
    """
    conflicts: list[ColumnElement[bool]] = [
        Order.params["target"].is_not(None),
        Order.params["move"].is_not(None),
    ]
    params = order.params or {}
    if not params.get("target") and not params.get("move"):
        conflicts.append(Order.id < order.id)
    return await session.scalar(
        select(Order.id)
        .where(
            Order.service_id == order.service_id,
            Order.kind.in_([*renewal_state.KINDS, OrderKind.MOVE]),
            Order.status.in_([OrderStatus.PAID, OrderStatus.PROVISIONING]),
            Order.id != order.id,
            or_(*conflicts),
        )
        .order_by(Order.id)
        .limit(1)
    )


async def fulfill(session: AsyncSession, order_id: int) -> Service | None:
    order = await session.get(Order, order_id, populate_existing=True)
    modifying = {*renewal_state.KINDS, OrderKind.MOVE}
    if order is None or order.kind not in modifying or not order.service_id:
        return await _fulfill(session, order_id)
    try:
        # Unlike a row lock, this lease survives the intent/progress commits around panel calls.
        async with kv.lease(service_lease(order.service_id), ttl=180):
            await session.refresh(order)
            if order.status not in {OrderStatus.PAID, OrderStatus.PROVISIONING}:
                return await _fulfill(session, order_id)
            blocker = await _modification_predecessor(session, order)
            if blocker is not None:
                raise ServiceBusy(f"service waits for order {blocker}")
            return await _fulfill(session, order_id)
    except kv.LockNotAcquired as exc:
        raise ServiceBusy("service modification lease unavailable") from exc


async def _fulfill(session: AsyncSession, order_id: int) -> Service | None:
    """Run one provisioning attempt. Panel errors propagate so the queue can retry."""
    order = await claim(session, order_id)
    if order is None:
        existing = await session.get(Order, order_id)
        if existing is not None and existing.status == OrderStatus.COMPLETED and existing.service_id:
            return await session.get(Service, existing.service_id)
        return None
    from paurubot.core import resale

    try:
        if await resale.pay_wholesale(session, order):  # a reseller bot sale: the reseller pays first
            await session.commit()
    except resale.ResaleError as exc:
        raise ProvisionError(exc.key) from exc
    from paurubot.core import panel_accounts

    if order.kind in panel_accounts.KINDS:
        await _apply_account(session, order)
        return None
    plan = await session.get(Plan, order.plan_id) if order.plan_id else None
    if order.params.get("stock_plan_id") or (
        order.kind == OrderKind.PURCHASE and plan is not None and plan.delivery == "stock"
    ):
        await _deliver_stock(session, order)
        return None
    if order.kind in {OrderKind.PURCHASE, OrderKind.TRIAL}:
        service = await _create_service(session, order)
    elif order.kind == OrderKind.MOVE:
        service = await _move_service(session, order)
    else:
        service = await _modify_service(session, order)
    order.status = OrderStatus.COMPLETED
    order.service_id = service.id
    order.completed_at = utcnow()
    from paurubot.provisioning.rewards import reward_referrer

    await reward_referrer(session, order)
    from paurubot.core.resellers import pay_commission

    await pay_commission(session, order)
    await after_sale(session, order)
    from paurubot.worker import queue

    if order.params.get("batch"):
        await batch_progress(session, order)  # one message for the whole batch (CAT-07)
    else:
        await queue.enqueue(
            session,
            "notify",
            "order_completed",
            {"order_id": order.id},
            shop_id=order.shop_id,
            dedupe_key=f"order:{order.id}:notify",
        )
    await session.commit()
    return service


async def _apply_account(session: AsyncSession, order: Order) -> None:
    """A panel admin account order (TEN-14): no service, and no level-1 commission since
    account plans carry no reseller discount."""
    from paurubot.core import panel_accounts
    from paurubot.provisioning.rewards import reward_referrer
    from paurubot.worker import queue

    try:
        account = await panel_accounts.apply(session, order)
    except panel_accounts.ProvisionError as exc:
        raise ProvisionError(str(exc)) from exc
    order.status = OrderStatus.COMPLETED
    order.completed_at = utcnow()
    await reward_referrer(session, order)
    await after_sale(session, order)
    await queue.enqueue(
        session,
        "notify",
        "account_ready",
        {"order_id": order.id, "account_id": account.id},
        shop_id=order.shop_id,
        dedupe_key=f"order:{order.id}:notify",
    )
    await session.commit()


async def _deliver_stock(session: AsyncSession, order: Order) -> None:
    """Hand over one ready-made config from the plan's stock (CAT-05). No panel is involved."""
    from paurubot.core import stock
    from paurubot.core.resellers import pay_commission
    from paurubot.provisioning.rewards import reward_referrer
    from paurubot.worker import queue

    item = await stock.take(session, order)
    if item is None:
        await session.commit()
        raise ProvisionError("out_of_stock")
    order.status = OrderStatus.COMPLETED
    order.completed_at = utcnow()
    order.params = {**(order.params or {}), "stock_item_id": item.id}
    await reward_referrer(session, order)
    await pay_commission(session, order)
    await after_sale(session, order)
    if order.params.get("batch"):
        await batch_progress(session, order)
    else:
        await queue.enqueue(
            session,
            "notify",
            "stock_delivered",
            {"order_id": order.id},
            shop_id=order.shop_id,
            dedupe_key=f"order:{order.id}:notify",
        )
    await session.commit()


async def after_sale(session: AsyncSession, order: Order) -> None:
    """Loyalty tier and trust-channel posts for a completed order (GRW-02, GRW-08)."""
    from paurubot.core import channels, growth
    from paurubot.db.models import TrustChannel
    from paurubot.worker import queue

    cfg = await shop_settings.load(session, order.shop_id)
    customer = await session.get(Customer, order.customer_id)
    if customer is None:
        return
    if order.amount > 0 and order.kind not in {OrderKind.TRIAL, OrderKind.MOVE}:
        if (cfg.get("loyalty_enabled") or customer.tier_percent) and not customer.is_reseller:
            if await growth.refresh_tier(session, customer, cfg):
                await queue.enqueue(
                    session,
                    "notify",
                    "tier_up",
                    {"customer_id": customer.id, "percent": customer.tier_percent},
                    shop_id=order.shop_id,
                    dedupe_key=f"tier:{customer.id}:{customer.tier_percent}",
                )
    # Free trials post too; free extras and moves do not. The handler checks each channel's kinds.
    if order.kind.value in channels.TRUST_KINDS and (order.amount > 0 or order.kind == OrderKind.TRIAL):
        posts_here = await session.scalar(
            select(TrustChannel.id)
            .where(TrustChannel.shop_id == order.shop_id, TrustChannel.is_active.is_(True))
            .limit(1)
        )
        if posts_here:
            await queue.enqueue(
                session,
                "notify",
                "trust_post",
                {"order_id": order.id},
                shop_id=order.shop_id,
                dedupe_key=f"order:{order.id}:trust",
            )


async def batch_progress(session: AsyncSession, order: Order) -> None:
    from paurubot.worker import queue

    await queue.enqueue(
        session,
        "notify",
        "batch_progress",
        {"customer_id": order.customer_id, "batch": str(order.params["batch"])},
        shop_id=order.shop_id,
        dedupe_key=f"batch:{order.params['batch']}:{order.id}",
    )


async def give_up(session: AsyncSession, order_id: int, reason: str) -> None:
    """Final failure: refund, release a trial reservation and tell the customer and the staff."""
    from paurubot.core import trials
    from paurubot.core.orders import refund_failed
    from paurubot.worker import queue

    order = await session.get(Order, order_id, populate_existing=True)
    if order is None or order.status in {OrderStatus.COMPLETED, OrderStatus.REFUNDED}:
        return
    if not create_state.refundable(order):
        # A create request may have reached the panel: settle it by reading the panel.
        first = not create_state.parked(order)
        create_state.park(order, reason)
        log.warning("order %s parked: the panel may hold its account: %s", order.id, reason[:200])
        if first:
            await _alert_uncertain(
                session,
                order,
                "create_uncertain",
                "A create request may have reached the panel and the job gave up "
                "(the bot will keep checking the panel)",
            )
        await session.commit()
        return
    if not renewal_state.refundable(order):
        # The panel may hold the change: refunding could give it away. Reconcile instead.
        if renewal_state.state(order) == renewal_state.RUNNING:
            renewal_state.move(order, renewal_state.UNCERTAIN, "job gave up")
        renewal_state.park(order, reason)
        log.warning("order %s parked for reconciliation: %s", order.id, reason[:200])
        await session.commit()
        return
    if order.kind in renewal_state.KINDS and renewal_state.state(order) == renewal_state.PENDING:
        renewal_state.move(order, renewal_state.FAILED, "gave up before touching the panel")
    await refund_failed(session, order, reason)
    if order.kind in create_state.KINDS or not ((order.params or {}).get("move") or {}).get("created"):
        await claims.release(session, order.id)  # nothing was left on a panel under these names
    from paurubot.core import resale

    await resale.refund_wholesale(session, order)
    if order.kind == OrderKind.TRIAL and order.params.get("grant_id"):
        await trials.release(session, int(order.params["grant_id"]))
    await queue.enqueue(
        session,
        "notify",
        "order_failed",
        {"order_id": order.id, "reason": reason[:200]},
        shop_id=order.shop_id,
        dedupe_key=f"order:{order.id}:failed",
    )
    if order.params.get("batch"):
        await batch_progress(session, order)
    await session.commit()


async def delete_service(session: AsyncSession, service: Service, reason: str | None = None) -> None:
    panel = await session.get(Panel, service.panel_id)
    if panel is not None:
        try:
            await (await driver_for(panel)).delete_user(ref(service))
        except PanelError:
            log.warning("panel delete failed for service %s", service.id)
            raise
    service.status = ServiceStatus.DELETED
    service.deleted_at = utcnow()
    service.delete_reason = reason
    await session.flush()


async def refresh(session: AsyncSession, service: Service) -> PanelUser | None:
    """Read the panel into `service`. The row stays locked until the caller commits (SEC-F09).

    Reloading under the lock matters: a stale copy could still point at the panel a move has
    just left, and that panel's "no such user" would mark the moved service deleted.
    """
    await lock_fresh(session, service)
    if service.status == ServiceStatus.DELETED:
        return None
    panel = await session.get(Panel, service.panel_id)
    assert panel is not None
    if panel.national_mode:
        return None  # Do not mark existing subscriptions deleted during an outage.
    info = await (await driver_for(panel)).get_user(ref(service))
    if info is None:
        service.status = ServiceStatus.DELETED
        service.deleted_at = utcnow()
        service.delete_reason = "missing on panel"
    else:
        apply_panel_user(service, info)
        from paurubot.core import usage_history

        await usage_history.observe(session, service, info, source="poll")
    await session.flush()
    return info


async def set_enabled(
    session: AsyncSession, service: Service, enabled: bool, reason: str | None = None
) -> None:
    """Turn a service on or off on its panel. `reason` records why the bot disabled it (SVC-06)."""
    panel = await session.get(Panel, service.panel_id)
    assert panel is not None
    info = await (await driver_for(panel)).update_user(ref(service), enabled=enabled)
    apply_panel_user(service, info)
    if enabled:
        service.disabled_reason = None
    else:
        service.status = ServiceStatus.DISABLED
        service.disabled_reason = (reason or "staff")[:32]
    await session.flush()


async def revoke(session: AsyncSession, service: Service) -> None:
    panel = await session.get(Panel, service.panel_id)
    assert panel is not None
    info = await (await driver_for(panel)).revoke_subscription(ref(service))
    apply_panel_user(service, info)
    service.sub_token = new_sub_token()  # the old branded link must stop working too
    await session.flush()


async def links(session: AsyncSession, service: Service) -> list[str]:
    panel = await session.get(Panel, service.panel_id)
    assert panel is not None
    if panel.national_mode:
        from paurubot.core import manual_stock, national

        item = await national.replacement(session, service)
        if item is None:
            return []
        return [p.text for p in manual_stock.parts(item) if p.data is None]
    return await (await driver_for(panel)).fetch_links(service.subscription_url)


async def extend(session: AsyncSession, service: Service, *, days: int = 0, gb: int = 0) -> None:
    """Add time and/or volume to a service without charging (staff gifts, outage compensation).

    Panels whose limits live on their side (PNL-11) cannot take a partial gift; they are skipped.
    """
    panel = await session.get(Panel, service.panel_id)
    assert panel is not None
    driver = await driver_for(panel)
    if "gift" not in driver.changes:
        log.info("panel %s cannot take extra time or data; service %s left as it is", panel.id, service.id)
        return
    expire: Any = UNSET
    if days and service.expire_at:
        now = utcnow().replace(microsecond=0)
        expire = (service.expire_at if service.expire_at > now else now) + timedelta(days=days)
    limit = int(service.data_limit) + gb * GB if gb and service.data_limit else None
    if expire is UNSET and limit is None:
        return
    # More time or data brings an expired or used-up service back, but never one a person or a
    # rule switched off (ban, reseller suspension, voided payment): enabled stays as it is then.
    enabled = None if service.status == ServiceStatus.DISABLED else True
    info = await driver.update_user(ref(service), expire_at=expire, data_limit=limit, enabled=enabled)
    apply_panel_user(service, info)
    if expire is not UNSET:
        service.expire_at = expire
    if limit is not None:
        service.data_limit = limit
    await session.flush()
