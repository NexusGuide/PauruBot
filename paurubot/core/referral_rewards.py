"""A free config for every N counted invites (the referral reward config).

The shop chooses what an invite is worth: the cash share of the friend's purchase (GRW-01), a
config, or both (`referral_reward_mode`). For the config, an invite counts either when the friend
makes a first qualifying purchase (the same minimum order as the cash reward) or as soon as they
join through the link (`referral_config_counts`); banned friends never count. Every
`referral_config_every` counted invites earn one config: an existing plan, or a size typed in
settings with its panel (`referral_config_source`).

A claim uses up N invites and is a zero-amount order through the usual provisioning queue. A
claim whose order failed, was canceled or refunded gives its invites back, so a panel outage
never costs the customer their reward.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import utcnow
from paurubot.db.models import (
    Customer,
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    Plan,
    ReferralClaim,
)
from paurubot.worker import queue

MODES = ("cash", "config", "both")
COUNTS = ("purchase", "join")
SOURCES = ("plan", "custom")
EVERY_RANGE = (1, 1000)
GB_RANGE = (1, 10_000)
DAYS_RANGE = (1, 3650)
# A second claim this soon after one is a double tap, not a decision to take two configs.
DOUBLE_TAP = timedelta(seconds=5)
# Orders whose claim no longer holds its invites.
RELEASED = (OrderStatus.FAILED, OrderStatus.CANCELED, OrderStatus.REFUNDED)


class RewardError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def config_on(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("referral_enabled")) and cfg.get("referral_reward_mode") in ("config", "both")


def cash_on(cfg: dict[str, Any]) -> bool:
    return cfg.get("referral_reward_mode") not in ("config",)


def every(cfg: dict[str, Any]) -> int:
    value = cfg.get("referral_config_every")
    return value if type(value) is int and EVERY_RANGE[0] <= value <= EVERY_RANGE[1] else 5


@dataclass(slots=True)
class Reward:
    """What one claim gives: a plan, or a size on a panel."""

    plan: Plan | None
    gb: int
    days: int
    panel: Panel | None


async def reward(session: AsyncSession, shop_id: int, cfg: dict[str, Any]) -> Reward | None:
    """The config one claim gives, or None while settings do not describe a usable one."""
    if cfg.get("referral_config_source") == "custom":
        gb, days = cfg.get("referral_config_gb"), cfg.get("referral_config_days")
        panel_id = cfg.get("referral_config_panel_id")
        panel = await session.get(Panel, int(panel_id)) if panel_id else None
        if (
            panel is None
            or panel.shop_id != shop_id
            or not panel.is_active
            or type(gb) is not int
            or type(days) is not int
            or not GB_RANGE[0] <= gb <= GB_RANGE[1]
            or not DAYS_RANGE[0] <= days <= DAYS_RANGE[1]
        ):
            return None
        return Reward(None, gb, days, panel)
    plan_id = cfg.get("referral_config_plan_id")
    plan = await session.get(Plan, int(plan_id)) if plan_id else None
    if plan is None or plan.shop_id != shop_id or not plan.is_active:
        return None
    return Reward(plan, int(plan.data_gb or 0), int(plan.days or 0), None)


async def counted(session: AsyncSession, customer: Customer, cfg: dict[str, Any]) -> int:
    """Invites of this customer that count toward reward configs."""
    stmt = select(func.count(Customer.id)).where(
        Customer.shop_id == customer.shop_id,
        Customer.referrer_id == customer.id,
        Customer.is_banned.is_(False),
    )
    if cfg.get("referral_config_counts") != "join":
        stmt = stmt.where(Customer.referral_counted_at.is_not(None))
    return int(await session.scalar(stmt) or 0)


async def spent(session: AsyncSession, customer: Customer) -> int:
    """Invites already used by claims whose order still stands."""
    value = await session.scalar(
        select(func.coalesce(func.sum(ReferralClaim.invites), 0))
        .outerjoin(Order, Order.id == ReferralClaim.order_id)
        .where(
            ReferralClaim.customer_id == customer.id,
            ReferralClaim.order_id.is_not(None),
            Order.status.not_in(RELEASED),
        )
    )
    return int(value or 0)


@dataclass(slots=True)
class Status:
    every: int
    counted: int
    spent: int

    @property
    def unused(self) -> int:
        return max(0, self.counted - self.spent)

    @property
    def available(self) -> int:
        return self.unused // self.every

    @property
    def progress(self) -> int:
        """Counted invites toward the next config."""
        return self.unused % self.every


async def status(session: AsyncSession, customer: Customer, cfg: dict[str, Any]) -> Status:
    return Status(every(cfg), await counted(session, customer, cfg), await spent(session, customer))


def view(state: Status, given: Reward | None) -> dict[str, Any]:
    """What the bot and the Mini App show: progress, configs to take, and what one config is."""
    return {
        "every": state.every,
        "counted": state.counted,
        "progress": state.progress,
        "available": state.available if given is not None else 0,
        "reward": None
        if given is None
        else {
            "plan_title": given.plan.title if given.plan is not None else None,
            "gb": given.gb,
            "days": given.days,
        },
    }


async def claim(session: AsyncSession, customer: Customer, cfg: dict[str, Any]) -> Order:
    """Take one config for N counted invites; the caller commits. Raises RewardError."""
    if not config_on(cfg):
        raise RewardError("referral_config_off")
    if customer.is_banned:
        raise RewardError("referral_config_off")
    given = await reward(session, customer.shop_id, cfg)
    if given is None:
        raise RewardError("referral_config_unavailable")
    # One claim at a time per customer: a double tap waits here and then sees the first claim.
    await session.execute(select(Customer.id).where(Customer.id == customer.id).with_for_update())
    state = await status(session, customer, cfg)
    if state.available < 1:
        raise RewardError("referral_config_not_enough")
    recent = await session.scalar(
        select(ReferralClaim.id)
        .where(ReferralClaim.customer_id == customer.id, ReferralClaim.created_at >= utcnow() - DOUBLE_TAP)
        .limit(1)
    )
    if recent is not None:
        raise RewardError("referral_config_wait")
    params: dict[str, Any] = {"referral_claim": True, "label": "invite"}
    if given.plan is None:
        assert given.panel is not None
        params.update(custom={"gb": given.gb, "days": given.days}, panel_id=given.panel.id)
    order = Order(
        shop_id=customer.shop_id,
        customer_id=customer.id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.PAID,
        plan_id=given.plan.id if given.plan is not None else None,
        params=params,
        base_price=0,
        amount=0,
        paid_at=utcnow(),
        source="referral",
    )
    session.add(order)
    await session.flush()
    session.add(
        ReferralClaim(
            shop_id=customer.shop_id, customer_id=customer.id, invites=state.every, order_id=order.id
        )
    )
    await session.flush()
    await queue.enqueue(
        session,
        "provision",
        "order",
        {"order_id": order.id},
        shop_id=customer.shop_id,
        dedupe_key=f"order:{order.id}:provision",
    )
    return order


async def maybe_announce(session: AsyncSession, referrer: Customer, cfg: dict[str, Any]) -> bool:
    """Tell the referrer when a counted invite just completed a config they can take."""
    if not config_on(cfg) or referrer.is_banned:
        return False
    state = await status(session, referrer, cfg)
    if state.available < 1 or state.progress != 0:
        return False
    if await reward(session, referrer.shop_id, cfg) is None:
        return False
    await queue.enqueue(
        session,
        "notify",
        "referral_config_ready",
        {"customer_id": referrer.id, "available": state.available},
        shop_id=referrer.shop_id,
        dedupe_key=f"referral-config:{referrer.id}:{state.counted}",
    )
    return True


async def count_purchase(
    session: AsyncSession, order: Order, customer: Customer, cfg: dict[str, Any]
) -> bool:
    """Count the referred customer's first qualifying purchase for their referrer. True if counted."""
    if customer.referrer_id is None or customer.referral_counted_at is not None:
        return False
    if order.amount < int(cfg.get("referral_min_order") or 0):
        return False
    customer.referral_counted_at = utcnow()
    # What this order did, so voiding its payment can take the count back (REF-01).
    order.params = {**(order.params or {}), "referral_counted": True}
    await session.flush()
    if cfg.get("referral_config_counts") != "join":
        referrer = await session.get(Customer, customer.referrer_id)
        if referrer is not None:
            await maybe_announce(session, referrer, cfg)
    return True


CHOICES = {
    "referral_reward_mode": MODES,
    "referral_config_counts": COUNTS,
    "referral_config_source": SOURCES,
}
RANGES = {
    "referral_config_every": EVERY_RANGE,
    "referral_config_gb": GB_RANGE,
    "referral_config_days": DAYS_RANGE,
}


async def check_settings(session: AsyncSession, shop_id: int, values: dict[str, Any]) -> None:
    """Refuse reward-config settings that make no sense or point at another shop's plan or panel."""
    for key, allowed in CHOICES.items():
        if key in values and values[key] not in allowed:
            raise RewardError(f"{key}_invalid")
    for key, (low, high) in RANGES.items():
        if key in values:
            value = values[key]
            if type(value) is not int or not low <= value <= high:
                raise RewardError(f"{key}_invalid")
    plan_id, panel_id = values.get("referral_config_plan_id"), values.get("referral_config_panel_id")
    if plan_id is not None:
        plan = await session.get(Plan, plan_id) if type(plan_id) is int else None
        if plan is None or plan.shop_id != shop_id:
            raise RewardError("referral_config_plan_id_invalid")
    if panel_id is not None:
        panel = await session.get(Panel, panel_id) if type(panel_id) is int else None
        if panel is None or panel.shop_id != shop_id:
            raise RewardError("referral_config_panel_id_invalid")
