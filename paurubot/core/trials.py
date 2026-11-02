"""Trial policy with generations, daily quota and race-free reservations (CAT-04).

A reservation is the unique (shop, customer, generation) row in `trial_grants`. A transaction
advisory lock also serializes the shared shop quota across different customers. Raising
`trial_generation` lets every customer try again without resetting the rolling 24-hour quota.

A panel can also carry its own rolling 24-hour cap (`options.trial_daily_quota`, F-20): trials go
to the first panel that allows them and still has room, so a busy panel hands over to the next.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Order, OrderKind, OrderStatus, Panel, TrialGrant
from paurubot.worker import queue

# Stable namespaced key; hashing to bigint also supports the full bigint Shop.id range.
_QUOTA_LOCK_NAMESPACE = "paurubot:trial-quota:"


class TrialError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


async def eligibility(session: AsyncSession, customer: Customer) -> str | None:
    """None when the customer may take a trial, else a locale key with the reason."""
    from paurubot.core import resale

    cfg = await shop_settings.load(session, customer.shop_id)
    sale = await resale.for_shop(session, customer.shop_id)
    # The reseller toggle is only an additional gate. The parent shop remains the source of
    # truth for trial policy, so a reseller cannot bypass a disabled trial, phone requirement,
    # quota or generation reset (TEN-18).
    policy = await shop_settings.load(session, sale.parent_id) if sale is not None else cfg
    if not cfg["trial_enabled"] or not policy["trial_enabled"]:
        return "trial_disabled"
    if sale is not None and (not sale.open or not sale.offers("trial")):
        return "trial_disabled"  # a reseller bot offers trials only when staff allow it (TEN-18)
    if customer.risk == "high":
        return "trial_unavailable"
    if (policy["trial_requires_phone"] or policy["require_phone"]) and not customer.phone:
        return "trial_needs_phone"
    generation = int(policy["trial_generation"] or 1)
    taken = await session.scalar(
        select(TrialGrant.id).where(
            TrialGrant.shop_id == customer.shop_id,
            TrialGrant.customer_id == customer.id,
            TrialGrant.generation == generation,
        )
    )
    if taken:
        return "trial_already_used"
    if customer.phone:
        # One number, one trial: a second account with the same phone gets none (REF-04).
        same_phone = await session.scalar(
            select(TrialGrant.id)
            .join(Customer, Customer.id == TrialGrant.customer_id)
            .where(
                TrialGrant.shop_id == customer.shop_id,
                TrialGrant.generation == generation,
                Customer.phone == customer.phone,
                Customer.id != customer.id,
            )
            .limit(1)
        )
        if same_phone is not None:
            return "trial_phone_used"
    wait = int(policy.get("trial_min_account_minutes") or 0)
    if (
        wait > 0
        and customer.created_at is not None
        and customer.created_at > utcnow() - timedelta(minutes=wait)
    ):
        return "trial_too_new"  # accounts made just to take a trial must wait (REF-04)
    quota = int(policy["trial_daily_quota"] or 0)
    if quota:
        since = utcnow() - timedelta(days=1)
        today = await session.scalar(
            select(func.count(TrialGrant.id)).where(
                TrialGrant.shop_id == customer.shop_id, TrialGrant.created_at >= since
            )
        )
        if int(today or 0) >= quota:
            return "trial_quota_reached"
    return None


PANEL_QUOTA = "trial_daily_quota"


def panel_quota(panel: Panel) -> int:
    """Trials this panel may give in any 24 hours; 0 is no cap of its own (F-20)."""
    value = (panel.options or {}).get(PANEL_QUOTA)
    return value if type(value) is int and value > 0 else 0


async def panel_used(session: AsyncSession, panel_id: int) -> int:
    """Trials given on this panel in the last 24 hours, by every shop selling on it."""
    since = utcnow() - timedelta(days=1)
    return int(
        await session.scalar(
            select(func.count(TrialGrant.id)).where(
                TrialGrant.panel_id == panel_id, TrialGrant.created_at >= since
            )
        )
        or 0
    )


async def _trial_panel(session: AsyncSession, shop_id: int, panel_id: int | None) -> Panel:
    """The panel for the next trial: the chosen one, else the first that allows trials and has room."""
    if panel_id:
        panel = await session.get(Panel, int(panel_id))
        allowed = (
            panel is not None
            and panel.shop_id == shop_id
            and panel.is_active
            and (panel.options or {}).get("allow_trials") is not False
        )
        candidates = [panel] if allowed and panel is not None else []
    else:
        candidates = list(
            await session.scalars(
                select(Panel)
                .where(
                    Panel.shop_id == shop_id,
                    Panel.is_active.is_(True),
                    Panel.options["allow_trials"].as_boolean().is_not(False),
                )
                .order_by(Panel.id)
            )
        )
    if not candidates:
        raise TrialError("trial_disabled")
    for panel in candidates:
        quota = panel_quota(panel)
        if not quota or await panel_used(session, panel.id) < quota:
            return panel
    raise TrialError("trial_quota_reached")


async def request(session: AsyncSession, customer: Customer) -> Order:
    """Reserve a trial and queue its provisioning as a zero-amount order."""
    reason = await eligibility(session, customer)
    if reason:
        raise TrialError(reason)
    # Keep the eligibility count and grant insertion in one serialized transaction. The
    # customer uniqueness constraint alone cannot protect a shop-wide quota. The lock lasts
    # until the caller commits/rolls back, not just until this function returns.
    lock_key = func.hashtextextended(f"{_QUOTA_LOCK_NAMESPACE}{customer.shop_id}", 0)
    await session.execute(select(func.pg_advisory_xact_lock(lock_key)))
    reason = await eligibility(session, customer)
    if reason:
        raise TrialError(reason)
    from paurubot.core import resale

    cfg = await shop_settings.load(session, customer.shop_id)
    sale = await resale.for_shop(session, customer.shop_id)
    rules = await shop_settings.load(session, sale.parent_id) if sale is not None else cfg
    generation = int(rules["trial_generation"] or 1)
    extra: dict[str, int] = {}
    # A reseller bot gives the parent shop's trial, on its panels, and pays a fee for it (TEN-18).
    owner = sale.parent_id if sale is not None else customer.shop_id
    # Panel quotas are shared by the owner shop and every reseller selling on its panels, which
    # the per-shop lock above does not cover: one more lock, always taken after it (F-20).
    panels_key = func.hashtextextended(f"{_QUOTA_LOCK_NAMESPACE}panels:{owner}", 0)
    await session.execute(select(func.pg_advisory_xact_lock(panels_key)))
    panel = await _trial_panel(session, owner, rules["trial_panel_id"])
    if sale is not None:
        fee = int(rules["resale_trial_fee"] or 0)
        if fee > 0:
            from paurubot.ledger.accounts import wallet_balance

            if await wallet_balance(session, sale.parent_id, sale.reseller.id) < fee:
                raise TrialError("trial_unavailable")
            extra = {"wholesale": fee, "resale_profile": sale.profile.id}
    grant_id = await session.scalar(
        insert(TrialGrant)
        .values(
            shop_id=customer.shop_id,
            customer_id=customer.id,
            generation=generation,
            panel_id=panel.id,
            status="reserved",
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["shop_id", "customer_id", "generation"])
        .returning(TrialGrant.id)
    )
    if grant_id is None:
        raise TrialError("trial_already_used")
    order = Order(
        shop_id=customer.shop_id,
        customer_id=customer.id,
        kind=OrderKind.TRIAL,
        status=OrderStatus.PAID,
        params={
            "panel_id": panel.id,
            "days": int(rules["trial_days"] or 1),
            "gb": int(rules["trial_gb"] or 1),
            "grant_id": grant_id,
            "label": "trial",
            **extra,
        },
        amount=0,
        paid_at=utcnow(),
    )
    session.add(order)
    await session.flush()
    await queue.enqueue(
        session,
        "provision",
        "order",
        {"order_id": order.id},
        shop_id=customer.shop_id,
        dedupe_key=f"order:{order.id}:provision",
        max_attempts=4,
    )
    customer.trial_generation = generation
    return order


async def release(session: AsyncSession, grant_id: int) -> None:
    """Give the reservation back after a failed provisioning so the customer can retry."""
    await session.execute(delete(TrialGrant).where(TrialGrant.id == grant_id))


async def allow_again(session: AsyncSession, customer: Customer) -> bool:
    """One more trial for this customer (CAT-11). Its grant in the current round moves out of the
    way (kept for reports) so a new one can be reserved. False when it had none to give back."""
    cfg = await shop_settings.load(session, customer.shop_id, fresh=True)
    generation = int(cfg["trial_generation"] or 1)
    result = await session.execute(
        update(TrialGrant)
        .where(
            TrialGrant.shop_id == customer.shop_id,
            TrialGrant.customer_id == customer.id,
            TrialGrant.generation == generation,
        )
        .values(generation=-TrialGrant.id)
    )
    return bool(getattr(result, "rowcount", 0))


async def new_round(session: AsyncSession, shop_id: int) -> int:
    """Everyone may take a trial again: the round number goes up by one (CAT-11)."""
    cfg = await shop_settings.load(session, shop_id, fresh=True)
    generation = int(cfg["trial_generation"] or 1) + 1
    await shop_settings.set_many(session, shop_id, {"trial_generation": generation})
    return generation
