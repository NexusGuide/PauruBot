"""Loyalty tiers, the lucky wheel, lotteries and the trust channel (GRW-02, GRW-03, GRW-04, GRW-08)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import utcnow
from paurubot.db.models import (
    Customer,
    DiscountCode,
    DiscountKind,
    Lottery,
    LotteryEntry,
    Order,
    OrderKind,
    OrderStatus,
    WheelSpin,
)
from paurubot.ledger import wallet

_random = secrets.SystemRandom()
PAID_KINDS = (
    OrderKind.PURCHASE,
    OrderKind.RENEW,
    OrderKind.EXTRA_VOLUME,
    OrderKind.EXTRA_DAYS,
    OrderKind.UPGRADE,
)


class GrowthError(Exception):
    def __init__(self, key: str, **params: Any) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


# --- loyalty tiers (GRW-02) -------------------------------------------------------------


def validate_tiers(value: Any) -> list[dict[str, Any]]:
    """[{title, min_spent, percent}], spend thresholds rising, at most 10 tiers."""
    if not isinstance(value, list) or len(value) > 10:
        raise GrowthError("loyalty_tiers_invalid")
    clean = []
    for item in value:
        if not isinstance(item, dict):
            raise GrowthError("loyalty_tiers_invalid")
        title = str(item.get("title") or "").strip()[:32]
        try:
            spent, percent = int(item.get("min_spent") or 0), int(item.get("percent") or 0)
        except (TypeError, ValueError) as exc:
            raise GrowthError("loyalty_tiers_invalid") from exc
        if not title or spent < 0 or not 0 <= percent <= 50:
            raise GrowthError("loyalty_tiers_invalid")
        clean.append({"title": title, "min_spent": spent, "percent": percent})
    return sorted(clean, key=lambda t: t["min_spent"])


def tier_for(tiers: list[dict[str, Any]], spent: int) -> dict[str, Any] | None:
    best = None
    for tier in sorted(tiers, key=lambda t: int(t.get("min_spent") or 0)):
        if spent >= int(tier.get("min_spent") or 0):
            best = tier
    return best


def next_tier(tiers: list[dict[str, Any]], spent: int) -> dict[str, Any] | None:
    for tier in sorted(tiers, key=lambda t: int(t.get("min_spent") or 0)):
        if int(tier.get("min_spent") or 0) > spent:
            return tier
    return None


async def refresh_tier(session: AsyncSession, customer: Customer, cfg: dict[str, Any]) -> bool:
    """Recount lifetime spend and the tier discount; True when the tier went up."""
    spent = await session.scalar(
        select(func.coalesce(func.sum(Order.amount), 0)).where(
            Order.customer_id == customer.id,
            Order.status == OrderStatus.COMPLETED,
            Order.kind.in_(PAID_KINDS),
        )
    )
    customer.total_spent = int(spent or 0)
    tier = (
        tier_for(cfg.get("loyalty_tiers") or [], customer.total_spent) if cfg.get("loyalty_enabled") else None
    )
    percent = int(tier["percent"]) if tier else 0
    raised = percent > int(customer.tier_percent or 0)
    customer.tier_percent = percent
    return raised


# --- lucky wheel (GRW-03) ---------------------------------------------------------------

WHEEL_KINDS = {"wallet", "discount", "nothing"}


def validate_prizes(value: Any) -> list[dict[str, Any]]:
    """[{label, kind: wallet|discount|nothing, value, weight}], 2..12 slices."""
    if not isinstance(value, list) or not 2 <= len(value) <= 12:
        raise GrowthError("wheel_prizes_invalid")
    clean = []
    for item in value:
        if not isinstance(item, dict) or item.get("kind") not in WHEEL_KINDS:
            raise GrowthError("wheel_prizes_invalid")
        label = str(item.get("label") or "").strip()[:64]
        try:
            amount, weight = int(item.get("value") or 0), int(item.get("weight") or 0)
        except (TypeError, ValueError) as exc:
            raise GrowthError("wheel_prizes_invalid") from exc
        if not label or weight < 0 or weight > 1000 or amount < 0:
            raise GrowthError("wheel_prizes_invalid")
        if item["kind"] == "discount" and not 1 <= amount <= 100:
            raise GrowthError("wheel_prizes_invalid")
        clean.append({"label": label, "kind": item["kind"], "value": amount, "weight": weight})
    if sum(p["weight"] for p in clean) <= 0:
        raise GrowthError("wheel_prizes_invalid")
    return clean


async def last_spin(session: AsyncSession, customer: Customer) -> WheelSpin | None:
    return await session.scalar(
        select(WheelSpin).where(WheelSpin.customer_id == customer.id).order_by(WheelSpin.id.desc()).limit(1)
    )


async def next_spin_at(session: AsyncSession, customer: Customer, cfg: dict[str, Any]):  # type: ignore[no-untyped-def]
    last = await last_spin(session, customer)
    if last is None:
        return None
    return last.created_at + timedelta(hours=max(1, int(cfg.get("wheel_every_hours") or 24)))


@dataclass(slots=True)
class Spin:
    index: int
    prize: dict[str, Any]
    code: str | None


async def spin(session: AsyncSession, customer: Customer, cfg: dict[str, Any]) -> Spin:
    """One spin: pick a slice by weight, pay the prize, remember when the next spin is allowed."""
    if not cfg.get("wheel_enabled"):
        raise GrowthError("wheel_off")
    prizes = validate_prizes(cfg.get("wheel_prizes") or [])
    if cfg.get("wheel_buyers_only"):
        bought = await session.scalar(
            select(func.count(Order.id)).where(
                Order.customer_id == customer.id, Order.status == OrderStatus.COMPLETED, Order.amount > 0
            )
        )
        if not bought:
            raise GrowthError("wheel_buyers_only")
    # Lock the customer row so two quick taps can never spin twice.
    await session.execute(select(Customer.id).where(Customer.id == customer.id).with_for_update())
    when = await next_spin_at(session, customer, cfg)
    if when is not None and when > utcnow():
        raise GrowthError("wheel_wait", at=when)
    pick = _random.uniform(0, sum(p["weight"] for p in prizes))
    index = 0
    for index, prize in enumerate(prizes):  # noqa: B007
        pick -= prize["weight"]
        if pick <= 0 and prize["weight"] > 0:
            break
    prize = prizes[index]
    row = WheelSpin(
        shop_id=customer.shop_id,
        customer_id=customer.id,
        label=prize["label"],
        kind=prize["kind"],
        value=prize["value"],
    )
    session.add(row)
    await session.flush()
    code = None
    if prize["kind"] == "wallet" and prize["value"] > 0:
        await wallet.reward(
            session, customer.shop_id, customer.id, prize["value"], key=f"wheel:{row.id}", memo="lucky wheel"
        )
    elif prize["kind"] == "discount":
        code = f"W{row.id}{secrets.token_hex(2).upper()}"
        session.add(
            DiscountCode(
                shop_id=customer.shop_id,
                code=code,
                kind=DiscountKind.PERCENT,
                value=prize["value"],
                max_uses=1,
                per_customer_limit=1,
                expires_at=utcnow() + timedelta(days=7),
                note=f"lucky wheel for customer {customer.id}",
            )
        )
        row.code = code
    await session.flush()
    return Spin(index, prize, code)


# --- lotteries (GRW-04) -----------------------------------------------------------------


async def open_lottery(session: AsyncSession, shop_id: int) -> Lottery | None:
    now = utcnow()
    return await session.scalar(
        select(Lottery)
        .where(
            Lottery.shop_id == shop_id,
            Lottery.status == "open",
            Lottery.starts_at <= now,
            Lottery.ends_at > now,
        )
        .order_by(Lottery.ends_at)
        .limit(1)
    )


async def join(session: AsyncSession, lottery: Lottery, customer: Customer) -> bool:
    """Enter a join-style lottery; False when already in."""
    if lottery.mode != "join" or lottery.status != "open" or lottery.ends_at <= utcnow():
        raise GrowthError("lottery_closed")
    existing = await session.scalar(
        select(LotteryEntry).where(
            LotteryEntry.lottery_id == lottery.id, LotteryEntry.customer_id == customer.id
        )
    )
    if existing is not None:
        return False
    session.add(LotteryEntry(lottery_id=lottery.id, customer_id=customer.id, tickets=1))
    await session.flush()
    return True


async def tickets(session: AsyncSession, lottery: Lottery) -> dict[int, int]:
    if lottery.mode == "purchases":
        rows = await session.execute(
            select(Order.customer_id, func.count(Order.id))
            .where(
                Order.shop_id == lottery.shop_id,
                Order.status == OrderStatus.COMPLETED,
                Order.amount > 0,
                Order.completed_at >= lottery.starts_at,
                Order.completed_at < lottery.ends_at,
            )
            .group_by(Order.customer_id)
        )
    else:
        rows = await session.execute(
            select(LotteryEntry.customer_id, LotteryEntry.tickets).where(
                LotteryEntry.lottery_id == lottery.id
            )
        )
    return {int(c): int(n) for c, n in rows.all()}


def pick_winners(pool: dict[int, int], count: int) -> list[int]:
    """Weighted by tickets, without replacement."""
    pool = {c: n for c, n in pool.items() if n > 0}
    winners: list[int] = []
    while pool and len(winners) < count:
        pick = _random.uniform(0, sum(pool.values()))
        for customer_id, weight in pool.items():  # noqa: B007  (the pick is used after the loop)
            pick -= weight
            if pick <= 0:
                break
        winners.append(customer_id)
        pool.pop(customer_id)
    return winners


async def draw(session: AsyncSession, lottery: Lottery) -> list[int]:
    """Pick and pay the winners once; a second call returns the same result."""
    if lottery.status == "drawn":
        return [int(x) for x in (lottery.result or {}).get("winners", [])]
    pool = await tickets(session, lottery)
    winners = pick_winners(pool, max(1, lottery.winners))
    for customer_id in winners:
        if lottery.prize_amount > 0:
            await wallet.reward(
                session,
                lottery.shop_id,
                customer_id,
                lottery.prize_amount,
                key=f"lottery:{lottery.id}:{customer_id}",
                memo=f"lottery {lottery.title}"[:120],
            )
    lottery.status = "drawn"
    lottery.result = {
        "winners": winners,
        "entrants": len(pool),
        "tickets": sum(pool.values()),
        "at": utcnow().isoformat(),
    }
    await session.flush()
    return winners


async def due_lotteries(session: AsyncSession) -> list[int]:
    return list(
        await session.scalars(select(Lottery.id).where(Lottery.status == "open", Lottery.ends_at <= utcnow()))
    )


# --- trust channel (GRW-08) -------------------------------------------------------------


def masked(name: str | None) -> str:
    """Hide most of a name: 'Sara' -> 'S***a'. Never shows a username or ID."""
    text = (name or "").strip()
    if len(text) <= 2:
        return "***"
    return f"{text[0]}***{text[-1]}"
