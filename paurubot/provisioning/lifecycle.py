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
    service.data_limit = info.data_limit
    service.expire_at = info.expire_at
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


def renewal_target(service: Service, plan: Plan, resets_usage: bool) -> tuple[datetime | None, int, bool]:
    """New expiry, data limit and whether usage resets when renewing with `plan`."""
    now = datetime.now(UTC).replace(microsecond=0)
    lapsed = service.status in {ServiceStatus.EXPIRED, ServiceStatus.LIMITED, ServiceStatus.DISABLED}
    if plan.days > 0:
        start = service.expire_at if service.expire_at and service.expire_at > now and not lapsed else now
        new_expire: datetime | None = start + timedelta(days=plan.days)
    else:
        new_expire = None
    plan_bytes = int(plan.data_gb) * GB if plan.data_gb > 0 else 0
    if plan_bytes == 0:
        return new_expire, 0, resets_usage or lapsed
    if resets_usage or lapsed:
        return new_expire, plan_bytes, True
    remaining = max(0, int(service.data_limit) - int(service.used_bytes)) if service.data_limit else 0
    return new_expire, int(service.used_bytes) + remaining + plan_bytes, False


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
        params["panel_username"] = await usernames.generate(
            session, panel, customer, params.get("label"), plan.username_prefix if plan else None
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


def _target_for(service: Service, order: Order, plan: Plan | None, resets_usage: bool) -> dict[str, object]:
    """Absolute end state of a modification, computed once so retries are idempotent."""
    now = datetime.now(UTC).replace(microsecond=0)
    target: dict[str, object] = {"set_expire": False, "expire": None, "limit": None, "reset": False}
    if order.kind == OrderKind.UPGRADE:
        assert plan is not None
        expire = now + timedelta(days=plan.days) if plan.days > 0 else None
        target.update(
            set_expire=True,
            expire=expire.isoformat() if expire else None,
            limit=plan.data_gb * GB if plan.data_gb > 0 else 0,
            reset=True,
        )
    elif order.kind == OrderKind.RENEW:
        assert plan is not None
        expire, limit, reset = renewal_target(service, plan, resets_usage)
        target.update(
            set_expire=True, expire=expire.isoformat() if expire else None, limit=limit, reset=reset
        )
    elif order.kind == OrderKind.EXTRA_VOLUME:
        extra = int(order.params.get("extra_gb") or 0) * GB
        target["limit"] = int(service.data_limit) + extra if service.data_limit else 0
    elif order.kind == OrderKind.EXTRA_DAYS:
        days = int(order.params.get("extra_days") or 0)
        if service.expire_at:
            base = service.expire_at if service.expire_at > now else now
            target.update(set_expire=True, expire=(base + timedelta(days=days)).isoformat())
    else:
        raise ProvisionError("unsupported_order")
    return target


async def _modify_service(session: AsyncSession, order: Order) -> Service:
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
        panel_mode = (panel.options or {}).get("renew_resets_usage")
        default = bool(cfg["renew_resets_usage"]) if panel_mode is None else bool(panel_mode)
        resets = default or bool(params.get("queued"))
        params["target"] = _target_for(service, order, plan, resets)
        order.params = params
        await session.commit()
    target = params["target"]
    expire_value = target.get("expire")
    expire_at: datetime | None = datetime.fromisoformat(str(expire_value)) if expire_value else None
    driver = await driver_for(panel)
    if target.get("reset"):
        await driver.reset_usage(ref(service))
    info = await driver.update_user(
        ref(service),
        expire_at=expire_at if target.get("set_expire") else UNSET,
        data_limit=target.get("limit"),  # type: ignore[arg-type]
        enabled=True,
    )
    apply_panel_user(service, info)
    if target.get("reset"):
        service.used_bytes = 0
    if target.get("set_expire"):
        service.expire_at = expire_at
    if target.get("limit") is not None:
        service.data_limit = int(target["limit"])  # type: ignore[call-overload]
    if plan is not None and order.kind in {OrderKind.RENEW, OrderKind.UPGRADE}:
        service.plan_id = plan.id
        service.is_trial = False
    service.status = ServiceStatus.ACTIVE
    service.disabled_reason = None
    service.alerts_sent = {}
    await session.flush()
    return service


async def fulfill(session: AsyncSession, order_id: int) -> Service | None:
    """Run one provisioning attempt. Panel errors propagate so the queue can retry."""
    order = await claim(session, order_id)
    if order is None:
        existing = await session.get(Order, order_id)
        if existing is not None and existing.status == OrderStatus.COMPLETED and existing.service_id:
            return await session.get(Service, existing.service_id)
        return None
    if order.kind in {OrderKind.PURCHASE, OrderKind.TRIAL}:
        service = await _create_service(session, order)
    else:
        service = await _modify_service(session, order)
    order.status = OrderStatus.COMPLETED
    order.service_id = service.id
    order.completed_at = utcnow()
    from paurubot.provisioning.rewards import reward_referrer

    await reward_referrer(session, order)
    from paurubot.core.resellers import pay_commission

    await pay_commission(session, order)
    from paurubot.worker import queue

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


async def give_up(session: AsyncSession, order_id: int, reason: str) -> None:
    """Final failure: refund, release a trial reservation and tell the customer and the staff."""
    from paurubot.core import trials
    from paurubot.core.orders import refund_failed
    from paurubot.worker import queue

    order = await session.get(Order, order_id, populate_existing=True)
    if order is None or order.status in {OrderStatus.COMPLETED, OrderStatus.REFUNDED}:
        return
    await refund_failed(session, order, reason)
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
    """Add time and/or volume to a service without charging (staff gifts, outage compensation)."""
    panel = await session.get(Panel, service.panel_id)
    assert panel is not None
    driver = await driver_for(panel)
    expire: Any = UNSET
    if days and service.expire_at:
        now = utcnow().replace(microsecond=0)
        expire = (service.expire_at if service.expire_at > now else now) + timedelta(days=days)
    limit = int(service.data_limit) + gb * GB if gb and service.data_limit else None
    if expire is UNSET and limit is None:
        return
    info = await driver.update_user(ref(service), expire_at=expire, data_limit=limit, enabled=True)
    apply_panel_user(service, info)
    await session.flush()
