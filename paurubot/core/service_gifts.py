"""Fixed-target service gifts: previews and retries never add the same gift twice."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core.orders import can_change
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Panel, Service, ServiceStatus
from paurubot.panels import GB, UNSET, driver_for
from paurubot.provisioning import lifecycle

LIMIT = 1000


def eligible_conditions(shop_id: int) -> list[Any]:
    return [
        Service.shop_id == shop_id,
        Customer.shop_id == shop_id,
        Panel.shop_id == shop_id,
        Service.status == ServiceStatus.ACTIVE,
        Service.is_trial.is_(False),
        Customer.is_banned.is_(False),
        Customer.telegram_id > 0,
        Panel.is_active.is_(True),
    ]


async def preview(session: AsyncSession, shop_id: int, mode: str, value: int) -> list[dict[str, Any]]:
    if mode not in {"days", "gb"} or not 1 <= value <= (3650 if mode == "days" else 100_000):
        raise ValueError("invalid gift")
    stmt = (
        select(Service)
        .join(Customer, Service.customer_id == Customer.id)
        .join(Panel, Service.panel_id == Panel.id)
        .where(*eligible_conditions(shop_id))
    )
    stmt = stmt.where(Service.expire_at.is_not(None) if mode == "days" else Service.data_limit > 0)
    rows = list(await session.scalars(stmt.order_by(Service.id).limit(LIMIT + 1)))
    # Filter panel capabilities before counting; never invent a partial gift for fixed-plan panels.
    targets = []
    for service in rows:
        target = fixed_target(service, mode, value)
        if target is not None:
            targets.append(target)
    if len(rows) > LIMIT:
        raise ValueError("too many services")
    return targets


def fixed_target(service: Service, mode: str, value: int) -> dict[str, Any] | None:
    """The absolute limit one gift sets on this service, or None when it cannot take one."""
    if not can_change(service, "gift"):
        return None
    if mode == "days" and service.expire_at is None:
        return None
    if mode == "gb" and not service.data_limit:
        return None  # unlimited data: nothing to add to
    expected: Any = (
        service.expire_at.isoformat() if mode == "days" and service.expire_at else service.data_limit
    )
    target: Any = (
        (max(service.expire_at, utcnow()) + timedelta(days=value)).isoformat()
        if mode == "days" and service.expire_at
        else service.data_limit + value * GB
    )
    return {"service_id": service.id, "mode": mode, "expected": expected, "target": target}


async def customer_target(
    session: AsyncSession, shop_id: int, customer_id: int, mode: str, value: int
) -> tuple[Service, dict[str, Any]] | None:
    """One customer's prize goes to their newest active service that can take it (GRW-04)."""
    stmt = (
        select(Service)
        .join(Customer, Service.customer_id == Customer.id)
        .join(Panel, Service.panel_id == Panel.id)
        .where(*eligible_conditions(shop_id), Service.customer_id == customer_id)
        .order_by(Service.id.desc())
        .limit(20)
    )
    for service in await session.scalars(stmt):
        target = fixed_target(service, mode, value)
        if target is not None:
            return service, target
    return None


async def apply_fixed(session: AsyncSession, service: Service, gift: dict[str, Any]) -> str:
    """Called under the service row lock. Reapply absolute panel limits after uncertain replies.

    A service changed since preview is skipped, not overwritten. Deleted/disabled/banned/trial
    services and unsupported panels are never enabled as a side effect.
    """
    customer = await session.get(Customer, service.customer_id)
    panel = await session.get(Panel, service.panel_id)
    if (
        customer is None
        or panel is None
        or customer.shop_id != service.shop_id
        or panel.shop_id != service.shop_id
        or customer.is_banned
        or not panel.is_active
        or service.is_trial
        or service.status != ServiceStatus.ACTIVE
        or not can_change(service, "gift")
    ):
        return "skipped_ineligible"
    expected: Any
    target: Any
    mode = gift.get("mode")
    if mode == "days":
        expected = datetime.fromisoformat(str(gift["expected"]))
        target = datetime.fromisoformat(str(gift["target"]))
        if not service.expire_at or target <= expected:
            return "skipped_changed"
        current: Any = service.expire_at
    elif mode == "gb":
        expected, target = int(gift["expected"]), int(gift["target"])
        if not service.data_limit or target <= expected:
            return "skipped_changed"
        current = service.data_limit
    else:
        raise ValueError("invalid gift mode")
    if current == target:
        return "already_applied"
    if current != expected:
        return "skipped_changed"
    driver = await driver_for(panel)
    if "gift" not in driver.changes:
        return "skipped_ineligible"
    # Validate panel-side limits too: a manual panel edit must not be overwritten,
    # and an update that succeeded but lost its response must not be applied twice.
    remote = await driver.get_user(lifecycle.ref(service))
    if remote is None or not remote.limits_known or remote.status != "active":
        return "skipped_ineligible"
    remote_value: Any = remote.expire_at if mode == "days" else remote.data_limit
    if remote_value not in (expected, target):
        return "skipped_changed"
    if remote_value == target:
        lifecycle.apply_panel_user(service, remote)
        await session.flush()
        return "already_applied"
    info = await driver.update_user(
        lifecycle.ref(service),
        expire_at=target if mode == "days" else UNSET,
        data_limit=target if mode == "gb" else None,
    )
    lifecycle.apply_panel_user(service, info)
    if mode == "days":
        service.expire_at = target
    else:
        service.data_limit = target
    await session.flush()
    return "applied"
