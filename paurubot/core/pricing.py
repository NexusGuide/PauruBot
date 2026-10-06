"""Order pricing: reseller discounts, discount codes and sticky discounts (GRW-05)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import (
    Customer,
    DiscountCode,
    DiscountKind,
    DiscountRedemption,
    Order,
    OrderStatus,
    StickyDiscount,
)


class DiscountError(Exception):
    """Carries a locale key that explains the rejection to the customer."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(slots=True)
class Quote:
    base_price: int
    reseller_discount: int
    code_discount: int
    amount: int
    code: DiscountCode | None = None


def round_price(value: float) -> int:
    """Round to whole hundreds so prices stay readable in Toman."""
    return int(round(value / 100.0)) * 100


def reseller_percent(customer: Customer | None) -> int:
    if customer is None or customer.reseller_profile is None or not customer.reseller_profile.is_current:
        return 0
    return customer.reseller_profile.discount_percent


def code_discount_amount(code: DiscountCode, amount: int) -> int:
    if amount <= 0:
        return 0
    if code.kind == DiscountKind.FIXED:
        off = int(code.value)
    else:
        off = amount - round_price(amount * (100 - max(0, min(100, int(code.value)))) / 100)
    if code.max_discount:
        off = min(off, int(code.max_discount))
    return max(0, min(off, amount))


def quote(base_price: int, customer: Customer | None, code: DiscountCode | None = None) -> Quote:
    percent = reseller_percent(customer)
    after_reseller = round_price(base_price * (100 - percent) / 100) if percent else base_price
    off = code_discount_amount(code, after_reseller) if code else 0
    return Quote(base_price, base_price - after_reseller, off, max(0, after_reseller - off), code)


async def find_code(session: AsyncSession, shop_id: int, text: str) -> DiscountCode | None:
    text = text.strip()
    if not text or len(text) > 32:
        return None
    return await session.scalar(
        select(DiscountCode).where(
            DiscountCode.shop_id == shop_id, func.lower(DiscountCode.code) == text.lower()
        )
    )


async def validate_code(
    session: AsyncSession, code: DiscountCode, customer: Customer, *, plan_id: int | None, amount: int
) -> None:
    now = datetime.now(UTC)
    if not code.is_active or code.shop_id != customer.shop_id:
        raise DiscountError("discount_invalid")
    if code.starts_at and code.starts_at > now:
        raise DiscountError("discount_not_started")
    if code.expires_at and code.expires_at <= now:
        raise DiscountError("discount_expired")
    if code.max_uses is not None and code.used_count >= code.max_uses:
        raise DiscountError("discount_used_up")
    if code.plan_ids and plan_id not in code.plan_ids:
        raise DiscountError("discount_wrong_plan")
    if amount < code.min_amount:
        raise DiscountError("discount_min_amount")
    if code.per_customer_limit:
        used = await session.scalar(
            select(func.count(DiscountRedemption.id)).where(
                DiscountRedemption.code_id == code.id, DiscountRedemption.customer_id == customer.id
            )
        )
        if int(used or 0) >= code.per_customer_limit:
            raise DiscountError("discount_already_used")
    if code.first_purchase_only:
        completed = await session.scalar(
            select(func.count(Order.id)).where(
                Order.customer_id == customer.id, Order.status == OrderStatus.COMPLETED, Order.amount > 0
            )
        )
        if completed:
            raise DiscountError("discount_first_purchase_only")


async def sticky_code(session: AsyncSession, customer: Customer) -> DiscountCode | None:
    sticky = await session.scalar(select(StickyDiscount).where(StickyDiscount.customer_id == customer.id))
    if sticky is None:
        return None
    if sticky.expires_at and sticky.expires_at <= datetime.now(UTC):
        return None
    return await session.get(DiscountCode, sticky.code_id)


async def attach_sticky(session: AsyncSession, customer: Customer, code: DiscountCode) -> None:
    existing = await session.scalar(select(StickyDiscount).where(StickyDiscount.customer_id == customer.id))
    if existing is None:
        session.add(
            StickyDiscount(
                shop_id=customer.shop_id, customer_id=customer.id, code_id=code.id, expires_at=code.expires_at
            )
        )
    else:
        existing.code_id = code.id
        existing.expires_at = code.expires_at
    await session.flush()
