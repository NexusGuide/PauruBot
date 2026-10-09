"""Order pricing: reseller discounts and fixed reseller prices (TEN-12), discount codes and sticky
discounts (GRW-05)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import (
    Customer,
    DiscountCode,
    DiscountKind,
    DiscountRedemption,
    Order,
    OrderStatus,
    ResellerPrice,
    ResellerProfile,
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
    """Round to whole hundreds so prices stay readable in Toman (half away from banker's rounding)."""
    return int((Decimal(str(value)) / Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) * 100


def reseller_percent(customer: Customer | None) -> int:
    """The customer's standing discount: a reseller's own, otherwise their loyalty tier (GRW-02)."""
    if customer is None:
        return 0
    if customer.reseller_profile is None or not customer.reseller_profile.is_current:
        return max(0, min(50, int(customer.tier_percent or 0)))
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


async def fixed_reseller_price(
    session: AsyncSession, profile: ResellerProfile | None, plan_id: int | None
) -> int | None:
    """The fixed price a reseller pays for a plan: its own price, else its group's (TEN-12).

    A level-2 reseller is priced by its level-1 reseller's discount, so only its own price counts.
    """
    if profile is None or not profile.is_current or plan_id is None:
        return None
    use_group = profile.parent_id is None and profile.group is not None and profile.group.is_active
    rows = (
        await session.execute(
            select(ResellerPrice.profile_id, ResellerPrice.price).where(
                ResellerPrice.plan_id == plan_id,
                or_(
                    ResellerPrice.profile_id == profile.id,
                    (ResellerPrice.group_id == profile.group_id) if use_group else false(),
                ),
            )
        )
    ).all()
    own = next((int(price) for owner, price in rows if owner is not None), None)
    group = next((int(price) for owner, price in rows if owner is None), None)
    return own if own is not None else group


async def plan_quote(
    session: AsyncSession,
    customer: Customer | None,
    plan_id: int | None,
    base_price: int,
    code: DiscountCode | None = None,
) -> Quote:
    """`quote` for a plan, with fixed reseller prices. A level-2 reseller never pays less than its
    level-1 reseller does, so the level-1 margin can't turn negative."""
    profile = customer.reseller_profile if customer is not None else None
    fixed = await fixed_reseller_price(session, profile, plan_id)
    standard = quote(base_price, customer, None)
    after = fixed if fixed is not None else standard.amount
    if profile is not None and profile.is_current and profile.parent_id is not None:
        parent = await session.get(ResellerProfile, profile.parent_id)
        if parent is not None and parent.is_current:
            parent_customer = await session.get(Customer, parent.customer_id)
            floor = (await plan_quote(session, parent_customer, plan_id, base_price)).amount
            after = max(after, floor)
    base = max(int(base_price), after)
    off = code_discount_amount(code, after) if code else 0
    return Quote(base, base - after, off, max(0, after - off), code)


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
    if code.customer_id is not None and code.customer_id != customer.id:
        raise DiscountError("discount_invalid")  # someone else's private code (GRW-10)
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


async def private_codes(session: AsyncSession, customer: Customer) -> list[DiscountCode]:
    """Codes made for this customer that it can still use (GRW-10)."""
    now = datetime.now(UTC)
    rows = await session.scalars(
        select(DiscountCode)
        .where(
            DiscountCode.customer_id == customer.id,
            DiscountCode.is_active.is_(True),
            (DiscountCode.expires_at.is_(None)) | (DiscountCode.expires_at > now),
            (DiscountCode.max_uses.is_(None)) | (DiscountCode.used_count < DiscountCode.max_uses),
        )
        .order_by(DiscountCode.id.desc())
        .limit(20)
    )
    out = []
    for code in rows:
        try:
            await validate_code(session, code, customer, plan_id=None, amount=10**12)
        except DiscountError as exc:
            if exc.key not in {"discount_wrong_plan", "discount_not_started"}:
                continue
        out.append(code)
    return out


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


def role_extra_prices(
    gb_price: int,
    day_price: int,
    role: str | None,
    cfg: dict,  # type: ignore[type-arg]
) -> tuple[int, int]:
    """Apply a per-role extras multiplier (`extra_price_roles`); empty map = no change."""
    roles = cfg.get("extra_price_roles") or {}
    if not isinstance(roles, dict) or not role or role not in roles:
        return gb_price, day_price
    try:
        mult = float(roles[role])
    except (TypeError, ValueError):
        return gb_price, day_price
    if mult <= 0:
        return gb_price, day_price
    return round_price(gb_price * mult), round_price(day_price * mult)


def renew_cashback(amount: int, cfg: dict, is_agent: bool = False) -> int:  # type: ignore[type-arg]
    """Wallet cashback on a renewal for agents; 0 unless enabled and `is_agent`.

    TODO: wire into the bonus engine's renewal payout once the agent-renewal
    hook lands; callers should use this helper so the rate stays in one place.
    """
    if not is_agent or amount <= 0:
        return 0
    percent = max(0, min(100, int(cfg.get("renew_cashback_percent") or 0)))
    return amount * percent // 100 if percent else 0
