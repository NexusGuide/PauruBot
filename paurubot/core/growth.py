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
    Panel,
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


# Telegram's own dice (Foxima parity): the values that win. A slot machine shows 1-64; these four
# are its jackpots (three bars, three grapes, three lemons, 777).
DICE_EMOJI = {"dice": "\U0001f3b2", "slot": "\U0001f3b0"}
SLOT_JACKPOTS = frozenset({1, 22, 43, 64})


def dice_wins(style: str, value: int) -> bool:
    return value == 6 if style == "dice" else value in SLOT_JACKPOTS


async def dice_spin(session: AsyncSession, customer: Customer, cfg: dict[str, Any], *, roll: Any) -> Spin:
    """One throw of Telegram's dice or slot machine instead of the wheel (Foxima parity): the same
    eligibility and waiting time; `roll` throws it in the chat and returns Telegram's value."""
    if not cfg.get("wheel_enabled"):
        raise GrowthError("wheel_off")
    await _check_spin(session, customer, cfg)
    style = str(cfg.get("wheel_style") or "dice")
    value = int(await roll())
    win = dice_wins(style, value)
    amount = int(cfg.get("wheel_dice_prize") or 0) if win else 0
    prize: dict[str, Any] = {
        "label": f"{DICE_EMOJI.get(style, '')} {value}",
        "kind": "wallet" if amount > 0 else "nothing",
        "value": amount,
        "weight": 1,
    }
    row = WheelSpin(
        shop_id=customer.shop_id,
        customer_id=customer.id,
        label=prize["label"],
        kind=prize["kind"],
        value=amount,
    )
    session.add(row)
    await session.flush()
    if amount > 0:
        await wallet.reward(
            session, customer.shop_id, customer.id, amount, key=f"wheel:{row.id}", memo="lucky dice"
        )
    return Spin(0, prize, None)


async def _check_spin(session: AsyncSession, customer: Customer, cfg: dict[str, Any]) -> None:
    """Who may spin now: buyers only when the shop says so, once per period (row-locked)."""
    if cfg.get("wheel_buyers_only"):
        bought = await session.scalar(
            select(func.count(Order.id)).where(
                Order.customer_id == customer.id, Order.status == OrderStatus.COMPLETED, Order.amount > 0
            )
        )
        if not bought:
            raise GrowthError("wheel_buyers_only")
    await session.execute(select(Customer.id).where(Customer.id == customer.id).with_for_update())
    when = await next_spin_at(session, customer, cfg)
    if when is not None and when > utcnow():
        raise GrowthError("wheel_wait", at=when)


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


LOTTERY_MODES = ("join", "purchases", "points")
LOTTERY_PICKS = ("random", "top")
LOTTERY_BOARDS = ("none", "self", "top")
LOTTERY_PRIZE_KINDS = ("service", "wallet", "text")
PRIZE_RANGES = {"wallet": (1, 10**10), "service": (1, 10_000), "text": (0, 0)}
DAYS_RANGE = (1, 3650)
MAX_LOTTERY_PRIZES = 20
BOARD_TOP = 5


@dataclass(frozen=True)
class Standing:
    customer_id: int
    points: int
    purchases: int = 0
    spent: int = 0


def validate_lottery_prizes(value: Any) -> list[dict[str, Any]]:
    """[{kind, value, label}], first item for first place. A `service` prize is a new config of
    `value` GB for `days` days on `panel_id`; a `wallet` prize is credit; `text` is handed over."""
    if not isinstance(value, list) or len(value) > MAX_LOTTERY_PRIZES:
        raise GrowthError("lottery_prizes_invalid")
    clean = []
    for item in value:
        if not isinstance(item, dict) or item.get("kind") not in LOTTERY_PRIZE_KINDS:
            raise GrowthError("lottery_prizes_invalid")
        kind = str(item["kind"])
        label = str(item.get("label") or "").strip()[:64]
        numbers = [item.get("value") or 0, item.get("days") or 0, item.get("panel_id") or 0]
        if any(isinstance(n, bool) or not isinstance(n, int) for n in numbers):
            raise GrowthError("lottery_prizes_invalid")
        raw, days, panel_id = numbers
        low, high = PRIZE_RANGES[kind]
        amount = 0 if kind == "text" else raw
        if not low <= amount <= high or (kind == "text" and not label):
            raise GrowthError("lottery_prizes_invalid")
        prize: dict[str, Any] = {"kind": kind, "value": amount, "label": label}
        if kind == "service":
            if not DAYS_RANGE[0] <= days <= DAYS_RANGE[1] or panel_id < 1:
                raise GrowthError("lottery_prizes_invalid")
            prize.update(days=days, panel_id=panel_id)
        clean.append(prize)
    return clean


async def prize_panels(session: AsyncSession, shop_id: int) -> list[Panel]:
    """Panels a service prize can be made on: the shop's own, switched on, sized by the bot."""
    return list(
        await session.scalars(
            select(Panel)
            .where(Panel.shop_id == shop_id, Panel.is_active.is_(True), Panel.national_mode.is_(False))
            .order_by(Panel.id)
        )
    )


async def check_prize_panels(session: AsyncSession, shop_id: int, prizes: list[dict[str, Any]]) -> None:
    allowed = {panel.id for panel in await prize_panels(session, shop_id)}
    if any(p["kind"] == "service" and p["panel_id"] not in allowed for p in prizes):
        raise GrowthError("lottery_prize_panel")


def prize_list(lottery: Lottery) -> list[dict[str, Any]]:
    """The prizes by place. Lotteries made before ranked prizes give every winner the same one."""
    if lottery.prizes:
        try:
            return validate_lottery_prizes(lottery.prizes)
        except GrowthError:
            return []
    if not (lottery.prize or lottery.prize_amount):
        return []
    one = (
        {"kind": "wallet", "value": int(lottery.prize_amount), "label": lottery.prize[:64]}
        if lottery.prize_amount > 0
        else {"kind": "text", "value": 0, "label": lottery.prize[:64]}
    )
    return [dict(one) for _ in range(max(1, lottery.winners))]


async def standings(session: AsyncSession, lottery: Lottery) -> list[Standing]:
    """Everyone with a score, best first; equal scores go to whoever got there first."""
    if lottery.mode in {"purchases", "points"}:
        last = func.max(Order.completed_at)
        found: list[tuple[Standing, Any]] = []
        sales = await session.execute(
            select(Order.customer_id, func.count(Order.id), func.coalesce(func.sum(Order.amount), 0), last)
            .join(Customer, Customer.id == Order.customer_id)
            .where(
                Order.shop_id == lottery.shop_id,
                Order.status == OrderStatus.COMPLETED,
                Order.amount > 0,
                Order.completed_at >= lottery.starts_at,
                Order.completed_at < lottery.ends_at,
                Customer.is_banned.is_(False),
            )
            .group_by(Order.customer_id)
        )
        for customer_id, count, spent, at in sales.all():
            if lottery.mode == "points":
                points = int(spent) // lottery.points_per if lottery.points_per > 0 else 0
            else:
                points = int(count)
            found.append((Standing(int(customer_id), points, int(count), int(spent)), at))
    else:
        entries = await session.execute(
            select(LotteryEntry.customer_id, LotteryEntry.tickets, LotteryEntry.created_at)
            .join(Customer, Customer.id == LotteryEntry.customer_id)
            .where(LotteryEntry.lottery_id == lottery.id, Customer.is_banned.is_(False))
        )
        found = [(Standing(int(c), int(n)), at) for c, n, at in entries.all()]
    found = [(row, at) for row, at in found if row.points > 0]
    found.sort(key=lambda item: (-item[0].points, item[1], item[0].customer_id))
    return [row for row, _ in found]


async def tickets(session: AsyncSession, lottery: Lottery) -> dict[int, int]:
    return {row.customer_id: row.points for row in await standings(session, lottery)}


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
    """Pick the winners and hand out the prizes once; a second call returns the same result.

    Wallet prizes are paid here. A service prize is a free order through the usual provisioning
    queue, so the winner gets the new config like any purchase; its award records the order.
    """
    from paurubot.worker import queue

    if lottery.status == "drawn":
        return [int(x) for x in (lottery.result or {}).get("winners", [])]
    board = await standings(session, lottery)
    prizes = prize_list(lottery)
    if lottery.pick == "top":
        winners = [row.customer_id for row in board[: len(prizes)]]
    else:
        winners = pick_winners({row.customer_id: row.points for row in board}, len(prizes))
    by_id = {row.customer_id: row for row in board}
    awards: list[dict[str, Any]] = []
    for rank, (customer_id, prize) in enumerate(zip(winners, prizes, strict=False), start=1):
        award: dict[str, Any] = {
            "rank": rank,
            "customer_id": customer_id,
            "points": by_id[customer_id].points,
            "purchases": by_id[customer_id].purchases,
            "spent": by_id[customer_id].spent,
            **prize,
        }
        if prize["kind"] == "wallet":
            await wallet.reward(
                session,
                lottery.shop_id,
                customer_id,
                prize["value"],
                key=f"lottery:{lottery.id}:{customer_id}",
                memo=f"lottery {lottery.title}"[:120],
            )
            award["status"] = "paid"
        elif prize["kind"] == "service":
            panel = await session.get(Panel, int(prize["panel_id"]))
            if panel is None or panel.shop_id != lottery.shop_id or not panel.is_active:
                award["status"] = "failed"
            else:
                order = Order(
                    shop_id=lottery.shop_id,
                    customer_id=customer_id,
                    kind=OrderKind.PURCHASE,
                    status=OrderStatus.PAID,
                    plan_id=None,
                    params={
                        "lottery": {"id": lottery.id, "rank": rank},
                        "label": "lottery",
                        "custom": {"gb": prize["value"], "days": prize["days"]},
                        "panel_id": panel.id,
                    },
                    base_price=0,
                    amount=0,
                    paid_at=utcnow(),
                    source="lottery",
                )
                session.add(order)
                await session.flush()
                await queue.enqueue(
                    session,
                    "provision",
                    "order",
                    {"order_id": order.id},
                    shop_id=lottery.shop_id,
                    dedupe_key=f"order:{order.id}:provision",
                )
                award.update(status="queued", order_id=order.id)
        else:
            award["status"] = "manual"
        awards.append(award)
    lottery.status = "drawn"
    lottery.result = {
        "winners": winners,
        "awards": awards,
        "entrants": len(board),
        "tickets": sum(row.points for row in board),
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


def masked_id(telegram_id: int | None) -> str:
    """Hide the tail of a Telegram id: 5694312345 -> '56943******'."""
    text = str(abs(int(telegram_id))) if telegram_id is not None else ""
    return f"{text[:5]}******" if len(text) > 5 else "******"
