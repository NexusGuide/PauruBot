"""Fulfilling paid orders against panels (SVC-01) and mapping panel state onto services."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

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
from paurubot.panels import GB, UNSET, PanelError, PanelRef, PanelUser, UserSpec, driver_for
from paurubot.provisioning import renewal_policy, renewal_state

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


async def _create_service(session: AsyncSession, order: Order) -> Service:
    customer = await session.get(Customer, order.customer_id)
    assert customer is not None
    panel = await _panel_for_new(session, order)
    plan = await session.get(Plan, order.plan_id) if order.plan_id else None
    params = dict(order.params or {})
    # Remember the chosen panel and username before calling the panel, so a retry after a
    # crash adopts the account it already created instead of creating a second one.
    if not params.get("panel_username") or params.get("panel_id") != panel.id:
        params["panel_id"] = panel.id
        prefix = plan.username_prefix if plan else None
        if params.get("wanted_username") and customer.telegram_id > 0:
            params["panel_username"] = await usernames.chosen(
                session, panel, str(params["wanted_username"]), prefix
            )
        else:
            params["panel_username"] = await usernames.generate(
                session, panel, customer, params.get("label"), prefix
            )
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
        return existing_service
    info = await driver.get_user(PanelRef(username, params.get("panel_data") or {}))
    if info is None:
        info = await driver.create_user(spec)
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
    service = await session.get(Service, order.service_id) if order.service_id else None
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
    if renewal_state.state(order) == renewal_state.UNCERTAIN and verifies:
        # Look before acting: an earlier attempt may have reached the panel after all.
        current = await driver.get_user(ref(service))
        if current is not None and not renewal_policy.verify(target, current):
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
    params = dict(order.params or {})
    last_check = params.get("verification") or {}
    redo_reset = not params.get("reset_done") or "usage_not_reset" in (last_check.get("problems") or [])
    if target.get("reset") and redo_reset:
        await driver.reset_usage(ref(service))
        # A retry must not wipe the traffic used since this reset, unless the panel ignored it.
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


async def parked_renewals(session: AsyncSession, limit: int = 200) -> list[int]:
    """Modification orders whose job gave up while the panel state was unknown."""
    rows = await session.scalars(
        select(Order)
        .where(Order.status == OrderStatus.PROVISIONING, Order.kind.in_(list(renewal_state.KINDS)))
        .order_by(Order.id)
        .limit(limit)
    )
    return [order.id for order in rows if renewal_state.parked(order)]


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


async def _alert_uncertain(session: AsyncSession, order: Order, outcome: str) -> None:
    from paurubot.notifications.handlers import report

    text = (
        f"⚠️ سفارش #{order.id} ({order.kind.value}): وضعیت پنل نامطمئن است ({outcome}).\n"
        "نه با هدف تمدید یکی است و نه با وضعیت قبل؛ مبلغ برگشت داده نشد. لطفاً سرویس را بررسی کنید."
    )
    try:
        await report(session, order.shop_id, "errors", text)
    except Exception:
        log.exception("could not report uncertain renewal %s", order.id)


async def reconcile_renewal(session: AsyncSession, order_id: int) -> str:
    """Settle one parked order by reading the panel. Never re-applies the change by itself.

    - the panel holds the target → ``reconciled`` and the order is finished;
    - the panel still shows the state before the change → ``failed`` and the order is refunded;
    - anything else (partial change, panel unreachable) → stays ``uncertain`` for the staff.
    """
    order = await session.get(Order, order_id, with_for_update=True, populate_existing=True)
    if order is None or order.status != OrderStatus.PROVISIONING or not renewal_state.parked(order):
        return "skipped"
    if renewal_state.is_settled(order):
        renewal_state.note_check(order, "settled")
        await _resume(session, order)
        order.params = {**order.params, "renewal": {**renewal_state.record(order), "parked": False}}
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
        params["move"] = {
            **_move_target(service, plan),
            "username": await usernames.generate(
                session, target, customer, service.label, plan.username_prefix if plan else None
            ),
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
    if info is None:
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


async def fulfill(session: AsyncSession, order_id: int) -> Service | None:
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
    if order.kind == OrderKind.PURCHASE and plan is not None and plan.delivery == "stock":
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
    """Loyalty tier and trust-channel post for a completed paid order (GRW-02, GRW-08)."""
    from paurubot.core import growth
    from paurubot.worker import queue

    if order.amount <= 0 or order.kind in {OrderKind.TRIAL, OrderKind.MOVE}:
        return
    cfg = await shop_settings.load(session, order.shop_id)
    customer = await session.get(Customer, order.customer_id)
    if customer is None:
        return
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
    if cfg.get("trust_channel_id") and order.kind in {OrderKind.PURCHASE, OrderKind.RENEW}:
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
    panel = await session.get(Panel, service.panel_id)
    assert panel is not None
    info = await (await driver_for(panel)).get_user(ref(service))
    if info is None:
        service.status = ServiceStatus.DELETED
        service.deleted_at = utcnow()
        service.delete_reason = "missing on panel"
    else:
        apply_panel_user(service, info)
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
    info = await driver.update_user(ref(service), expire_at=expire, data_limit=limit, enabled=True)
    apply_panel_user(service, info)
    if expire is not UNSET:
        service.expire_at = expire
    if limit is not None:
        service.data_limit = limit
    await session.flush()
