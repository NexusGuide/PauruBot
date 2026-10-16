"""Customer-owned, shop-scoped advertising preference (F-15)."""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import Actor, record
from paurubot.db.base import utcnow
from paurubot.db.models import Customer

FIELDS = ["marketing_opt_out", "marketing_changed_at", "marketing_change_source"]


def view(customer: Customer) -> dict[str, Any]:
    return {
        "marketing_opt_out": customer.marketing_opt_out,
        "changed_at": customer.marketing_changed_at.isoformat() if customer.marketing_changed_at else None,
    }


async def set_preference(
    session: AsyncSession,
    customer: Customer,
    *,
    opt_out: bool,
    source: Literal["bot", "miniapp"],
) -> dict[str, Any]:
    """Set an explicit value, never toggle. Serialize concurrent requests and audit atomically.

    The caller supplies the authenticated Customer, not a customer ID from the request.
    Caller owns commit; repeated identical choices are no-ops after the first explicit choice.
    """
    await session.refresh(customer, FIELDS, with_for_update=True)
    before = view(customer)
    if customer.marketing_opt_out == opt_out and customer.marketing_changed_at is not None:
        return before
    customer.marketing_opt_out = opt_out
    customer.marketing_changed_at = utcnow()
    customer.marketing_change_source = source
    await record(
        session,
        shop_id=customer.shop_id,
        actor=Actor("customer", customer.id),
        action="customer.marketing_preference",
        entity="customer",
        entity_id=customer.id,
        before=before,
        after={**view(customer), "source": source},
    )
    return view(customer)


async def allowed(session: AsyncSession, customer: Customer) -> bool:
    """A fresh scalar read: do not trust a recipient cached earlier in a worker batch."""
    return (
        await session.scalar(
            select(Customer.id).where(
                Customer.id == customer.id,
                Customer.shop_id == customer.shop_id,
                Customer.marketing_opt_out.is_(False),
            )
        )
    ) is not None
