"""Safe owner transfers for customers between the main shop and reseller shops."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import (
    Customer,
    DiscountCode,
    DiscountRedemption,
    GiftRedemption,
    LedgerAccount,
    Order,
    PanelAccount,
    Payment,
    ResellerApplication,
    ResellerProfile,
    Service,
    Shop,
    ShopStatus,
    StickyDiscount,
    Ticket,
    TrialGrant,
    VerifiedCard,
    WheelSpin,
)


class CustomerTransferError(Exception):
    """A transfer was rejected before any rows were changed."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True, slots=True)
class TransferResult:
    customer_id: int
    source_shop_id: int
    target_shop_id: int


def allowed_target(source: Shop, target: Shop) -> bool:
    """Only owner-approved reseller shops or the reseller's parent are valid targets."""
    if source.id == target.id or target.status != ShopStatus.ACTIVE:
        return False
    if source.parent_id is None:
        return target.parent_id == source.id and target.reseller_profile_id is not None
    return target.id == source.parent_id


async def target_shops(session: AsyncSession, source_shop_id: int) -> list[Shop]:
    """Return the only shops an owner can select from this source shop."""
    source = await session.get(Shop, source_shop_id)
    if source is None:
        return []
    if source.parent_id is None:
        return list(
            await session.scalars(
                select(Shop)
                .where(
                    Shop.parent_id == source.id,
                    Shop.reseller_profile_id.is_not(None),
                    Shop.status == ShopStatus.ACTIVE,
                )
                .order_by(Shop.title)
            )
        )
    parent = await session.get(Shop, source.parent_id)
    return [parent] if parent is not None and parent.status == ShopStatus.ACTIVE else []


# Every customer-owned row with a shop_id moves together. Ticket messages follow their ticket.
_CUSTOMER_ROWS = (
    DiscountCode,
    DiscountRedemption,
    GiftRedemption,
    LedgerAccount,
    Order,
    PanelAccount,
    Payment,
    ResellerApplication,
    Service,
    StickyDiscount,
    Ticket,
    TrialGrant,
    VerifiedCard,
    WheelSpin,
)


async def transfer_customer(session: AsyncSession, customer_id: int, target_shop_id: int) -> TransferResult:
    """Move one customer and all shop-scoped records in one transaction.

    The caller commits on success. A reseller identity itself is deliberately not moved;
    its profile and wallet ownership must stay in the main shop.
    """
    customer = await session.get(Customer, customer_id, with_for_update=True)
    target = await session.get(Shop, target_shop_id)
    if customer is None:
        raise CustomerTransferError("not_found")
    if target is None:
        raise CustomerTransferError("target_not_allowed")
    source = await session.get(Shop, customer.shop_id)
    if source is None or not allowed_target(source, target):
        raise CustomerTransferError("target_not_allowed")
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.customer_id == customer.id))
    if profile is not None:
        raise CustomerTransferError("reseller")
    duplicate = await session.scalar(
        select(Customer.id).where(
            Customer.shop_id == target.id,
            Customer.telegram_id == customer.telegram_id,
        )
    )
    if duplicate is not None:
        raise CustomerTransferError("already_exists")

    for model in _CUSTOMER_ROWS:
        await session.execute(update(model).where(model.customer_id == customer.id).values(shop_id=target.id))
    customer.shop_id = target.id
    # Referrer IDs are shop-local; do not leave a cross-shop graph behind.
    customer.referrer_id = None
    await session.flush()
    return TransferResult(customer.id, source.id, target.id)
