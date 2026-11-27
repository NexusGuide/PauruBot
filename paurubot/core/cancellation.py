"""A customer deleting its own service, with part of the unused value back in the wallet (SVC-14)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import pricing
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import (
    CancellationRequest,
    Customer,
    Order,
    OrderKind,
    OrderStatus,
    Plan,
    Service,
    ServiceStatus,
)
from paurubot.ledger.accounts import system_account, wallet_account
from paurubot.ledger.posting import transfer
from paurubot.worker import queue


class CancelError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


async def has_open_order(session: AsyncSession, service_id: int) -> bool:
    """True when a paid, running or queued order still targets this service."""
    found = await session.scalar(
        select(Order.id)
        .where(
            Order.service_id == service_id,
            Order.status.in_([OrderStatus.PAID, OrderStatus.PROVISIONING]),
        )
        .limit(1)
    )
    return found is not None


async def pending_for_service(session: AsyncSession, service_id: int) -> CancellationRequest | None:
    return await session.scalar(
        select(CancellationRequest)
        .where(CancellationRequest.service_id == service_id, CancellationRequest.status == "pending")
        .limit(1)
    )


async def last_payment(session: AsyncSession, service: Service) -> Order | None:
    """The last paid purchase or renewal of this service."""
    return await session.scalar(
        select(Order)
        .where(
            Order.service_id == service.id,
            Order.status == OrderStatus.COMPLETED,
            Order.kind.in_([OrderKind.PURCHASE, OrderKind.RENEW, OrderKind.UPGRADE]),
            Order.amount > 0,
        )
        .order_by(Order.completed_at.desc(), Order.id.desc())
        .limit(1)
    )


def unused_share(service: Service, days: int) -> float:
    """What's left of the period and of the data, whichever is smaller (0..1)."""
    shares: list[float] = []
    if days > 0 and service.expire_at is not None:
        seconds = (service.expire_at - datetime.now(UTC)).total_seconds()
        shares.append(max(0.0, min(1.0, seconds / (days * 86400))))
    if service.data_limit:
        left = max(0, int(service.data_limit) - int(service.used_bytes))
        shares.append(max(0.0, min(1.0, left / int(service.data_limit))))
    return min(shares) if shares else 0.0


async def period_days(session: AsyncSession, service: Service, paid: Order) -> int:
    """The period the last payment bought: its plan's, a custom order's own, else the service plan's.

    A custom service has no plan; without the order's own days its unused time counted as all of it.
    """
    plan = await session.get(Plan, paid.plan_id) if paid.plan_id else None
    if plan is not None and plan.days:
        return int(plan.days)
    custom = (paid.params or {}).get("custom")
    if isinstance(custom, dict) and int(custom.get("days") or 0) > 0:
        return int(custom["days"])
    return int(service.plan.days) if service.plan is not None and service.plan.days else 0


async def quote(session: AsyncSession, customer: Customer, service: Service) -> int:
    """What deleting this service gives back, after the shop's percentage."""
    cfg = await shop_settings.load(session, customer.shop_id)
    percent = max(0, min(100, int(cfg["self_delete_refund_percent"] or 0)))
    profile = customer.reseller_profile
    if service.is_trial or percent == 0 or (profile is not None and profile.is_metered):
        return 0  # metered resellers were never charged up front
    paid = await last_payment(session, service)
    if paid is None:
        return 0
    days = await period_days(session, service, paid)
    value = paid.amount * unused_share(service, days) * percent / 100
    return pricing.round_price(value) if value >= 100 else 0


async def cancel(
    session: AsyncSession,
    customer: Customer,
    service: Service,
    *,
    refund_override: int | None = None,
    require_enabled: bool = True,
    memo: str = "service deleted by the customer",
) -> int:
    """Delete the service from its panel and refund its unused value once. Returns the refund."""
    from paurubot.provisioning import lifecycle

    if require_enabled:
        cfg = await shop_settings.load(session, customer.shop_id)
        if not cfg["self_delete_enabled"]:
            raise CancelError("self_delete_off")
    try:
        # Own the service and price it from what the panel shows now (SEC-F09): a stale copy
        # could refund unused data the customer had already used, or race a renewal.
        async with lifecycle.exclusive(session, service):
            if service.customer_id != customer.id or service.status == ServiceStatus.DELETED:
                raise CancelError("service_not_found")
            if await has_open_order(session, service.id):
                raise CancelError("service_busy")
            await lifecycle.refresh(session, service)
            if service.status == ServiceStatus.DELETED:
                raise CancelError("service_not_found")
            amount = (
                max(0, int(refund_override))
                if refund_override is not None
                else await quote(session, customer, service)
            )
            refunded = await last_payment(session, service) if amount > 0 else None
            service.auto_renew = None
            await lifecycle.delete_service(session, service, memo)
    except lifecycle.ServiceBusy as exc:
        raise CancelError("service_busy") from exc
    if amount > 0:
        source = await system_account(session, customer.shop_id, "revenue")
        target = await wallet_account(session, customer.shop_id, customer.id)
        await transfer(
            session,
            shop_id=customer.shop_id,
            source_id=source.id,
            target_id=target.id,
            amount=amount,
            kind="refund",
            idempotency_key=f"service:{service.id}:cancel",
            reference_type="service",
            reference_id=service.id,
            memo=memo,
        )
        if refunded is not None and (refunded.params or {}).get("referral_counted"):
            # A purchase taken back for a refund no longer counts as an invite toward the
            # referrer's reward config; otherwise buying and deleting at once farms configs.
            customer.referral_counted_at = None
    return amount


async def request_cancel(
    session: AsyncSession,
    customer: Customer,
    service: Service,
    *,
    reason: str = "",
) -> CancellationRequest:
    """Create a pending cancellation request for staff review when approval is required."""
    cfg = await shop_settings.load(session, customer.shop_id)
    if not cfg["self_delete_enabled"]:
        raise CancelError("self_delete_off")
    if (
        service.customer_id != customer.id
        or service.shop_id != customer.shop_id
        or service.status == ServiceStatus.DELETED
    ):
        raise CancelError("service_not_found")
    if await has_open_order(session, service.id):
        raise CancelError("service_busy")
    if await pending_for_service(session, service.id) is not None:
        raise CancelError("cancel_request_pending")
    quoted = await quote(session, customer, service)
    row = CancellationRequest(
        shop_id=customer.shop_id,
        customer_id=customer.id,
        service_id=service.id,
        status="pending",
        reason=reason.strip()[:500] or None,
        quoted_refund=quoted,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise CancelError("cancel_request_pending") from exc
    await queue.enqueue(
        session,
        "notify",
        "cancellation_requested",
        {"request_id": row.id},
        shop_id=customer.shop_id,
        dedupe_key=f"cancel_req:{row.id}",
    )
    return row


async def decide(
    session: AsyncSession,
    row: CancellationRequest,
    *,
    approve: bool,
    actor: str,
    refund_override: int | None = None,
    note: str = "",
) -> int:
    """Staff approve (deletes service and refunds wallet) or reject a pending cancellation request."""
    if row.status != "pending":
        raise CancelError("cancel_request_decided")
    clean_note = note.strip()[:255] or None
    if not approve:
        row.status = "rejected"
        row.staff_note = clean_note
        row.decided_by = actor[:128]
        row.decided_at = utcnow()
        return 0
    service = await session.get(Service, row.service_id)
    customer = await session.get(Customer, row.customer_id)
    if (
        service is None
        or customer is None
        or service.shop_id != row.shop_id
        or service.status == ServiceStatus.DELETED
    ):
        raise CancelError("service_not_found")
    amount = await cancel(
        session,
        customer,
        service,
        refund_override=refund_override,
        require_enabled=False,
        memo="service cancellation approved by staff",
    )
    row.status = "approved"
    row.refund_amount = amount
    row.staff_note = clean_note
    row.decided_by = actor[:128]
    row.decided_at = utcnow()
    return amount


def view(row: CancellationRequest, customer: Customer | None, service: Service | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "status": row.status,
        "service_id": row.service_id,
        "service": (
            {
                "id": service.id,
                "label": service.label or service.panel_username,
                "username": service.panel_username,
            }
            if service
            else None
        ),
        "customer": {"id": customer.id, "name": customer.display_name} if customer else None,
        "reason": row.reason,
        "quoted_refund": row.quoted_refund,
        "refund_amount": row.refund_amount,
        "staff_note": row.staff_note,
        "created_at": row.created_at.isoformat(),
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "decided_by": row.decided_by,
    }
