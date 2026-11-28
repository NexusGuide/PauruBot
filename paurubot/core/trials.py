"""Trial policy with generations, daily quota and race-free reservations (CAT-04).

A reservation is the unique (shop, customer, generation, panel) row in `trial_grants`, so a
customer may try every allowed panel once per round. A transaction advisory lock also serializes
the shared shop quota across different customers. Raising `trial_generation` lets every customer
try again without resetting the rolling 24-hour quota.

A panel can also carry its own rolling 24-hour cap (`options.trial_daily_quota`, F-20): trials go
to the first panel that allows them and still has room, so a busy panel hands over to the next.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Order, OrderKind, OrderStatus, Panel, Shop, TrialGrant
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
    from paurubot.core import own_panel

    blocked = await own_panel.sales_gate(session, customer.shop_id, new_user=True)
    if blocked:
        return blocked  # a dedicated bot whose own panel cannot serve now (OPB)
    if customer.risk == "high":
        return "trial_unavailable"
    if (policy["trial_requires_phone"] or policy["require_phone"]) and not customer.phone:
        return "trial_needs_phone"
    generation = int(policy["trial_generation"] or 1)
    if bool(policy.get("trial_once_per_panel", True)):
        used, legacy_grant = await _used_trial_panels(session, customer, generation)
        if legacy_grant:
            # An older grant without a panel is treated as a shop-wide trial.
            return "trial_already_used"
        owner = sale.parent_id if sale is not None else customer.shop_id
        candidates, available = await _eligible_panels(session, owner, policy, customer, exclude_used=False)
        if not candidates:
            return "trial_disabled"
        unused_available = [panel for panel in available if panel.id not in used]
        if not unused_available:
            unused_candidates = [panel for panel in candidates if panel.id not in used]
            return "trial_quota_reached" if unused_candidates else "trial_already_used"
    else:
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


def _positive_int(value: object) -> int:
    return value if type(value) is int and value > 0 else 0


def panel_trial_limits(rules: dict[str, Any], panel: Panel) -> dict[str, int]:
    """Trial days/GB/hours/MB for one panel, with per-panel overrides winning as a set.

    Setting either duration key on the panel (`trial_days`/`trial_hours`) replaces both shop
    defaults, and likewise either volume key (`trial_gb`/`trial_mb`). Empty panel values inherit
    the shop trial.
    """
    options = panel.options or {}
    days = _positive_int(options.get("trial_days"))
    hours = _positive_int(options.get("trial_hours"))
    if not days and not hours:
        days = max(1, int(rules.get("trial_days") or 1))
        hours = max(0, int(rules.get("trial_hours") or 0))
    gb = _positive_int(options.get("trial_gb"))
    mb = _positive_int(options.get("trial_mb"))
    if not gb and not mb:
        gb = max(1, int(rules.get("trial_gb") or 1))
        mb = max(0, int(rules.get("trial_mb") or 0))
    return {"days": days, "hours": hours, "gb": gb, "mb": mb}


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


def _configured_panel_ids(rules: dict[str, object]) -> list[int]:
    """Configured trial panels: multi-panel allowlist, else legacy trial_panel_id, else all."""
    raw_list = rules.get("trial_panel_ids")
    if isinstance(raw_list, list):
        ids = [int(p) for p in raw_list if isinstance(p, int) and not isinstance(p, bool) and p > 0]
        if ids:
            return ids
    single = rules.get("trial_panel_id")
    if isinstance(single, int) and not isinstance(single, bool) and single > 0:
        return [single]
    return []


async def _hidden_panels(session: AsyncSession, customer: Customer | None) -> set[int]:
    if customer is None:
        return set()
    from paurubot.core import catalog

    hidden = {int(p) for p in (customer.hidden_panel_ids or [])}
    if customer.is_reseller and customer.reseller_profile and customer.reseller_profile.group:
        hidden |= {int(p) for p in (customer.reseller_profile.group.hidden_panel_ids or [])}
    hidden |= await catalog.resale_hidden_panels(session, customer.shop_id)
    return hidden


async def _used_trial_panels(
    session: AsyncSession, customer: Customer, generation: int
) -> tuple[set[int], bool]:
    """Panels this customer already tried in the round; True when an old grant has no panel."""
    rows = list(
        await session.scalars(
            select(TrialGrant.panel_id).where(
                TrialGrant.shop_id == customer.shop_id,
                TrialGrant.customer_id == customer.id,
                TrialGrant.generation == generation,
            )
        )
    )
    return {int(panel) for panel in rows if panel is not None}, any(panel is None for panel in rows)


async def _eligible_panels(
    session: AsyncSession,
    shop_id: int,
    rules: dict[str, Any],
    customer: Customer | None = None,
    *,
    exclude_used: bool = True,
) -> tuple[list[Panel], list[Panel]]:
    """Return (candidates, with_quota_room) respecting health, national mode, capacity and quotas."""
    from paurubot.core import catalog
    from paurubot.provisioning.monitor import quota_full

    allowed_ids = _configured_panel_ids(rules)
    stmt = (
        select(Panel)
        .options(selectinload(Panel.location))
        .where(
            Panel.shop_id == shop_id,
            Panel.is_active.is_(True),
            Panel.national_mode.is_(False),
            Panel.is_healthy.is_not(False),
            Panel.options["allow_trials"].as_boolean().is_not(False),
        )
        .order_by(Panel.id)
    )
    if allowed_ids:
        stmt = stmt.where(Panel.id.in_(allowed_ids))
    rows = list((await session.scalars(stmt)).all())
    if allowed_ids:
        order_map = {pid: idx for idx, pid in enumerate(allowed_ids)}
        rows.sort(key=lambda p: order_map.get(p.id, len(order_map)))
    if customer is not None and exclude_used:
        generation = int(rules.get("trial_generation") or 1)
        if bool(rules.get("trial_once_per_panel", True)):
            used, legacy_grant = await _used_trial_panels(session, customer, generation)
            if legacy_grant:
                return [], []
            rows = [panel for panel in rows if panel.id not in used]
        elif await session.scalar(
            select(TrialGrant.id)
            .where(
                TrialGrant.shop_id == customer.shop_id,
                TrialGrant.customer_id == customer.id,
                TrialGrant.generation == generation,
            )
            .limit(1)
        ):
            return [], []
    hidden = await _hidden_panels(session, customer)
    candidates: list[Panel] = []
    for panel in rows:
        if panel.id in hidden or quota_full(panel):
            continue
        if panel.capacity and await catalog.panel_load(session, panel.id) >= panel.capacity:
            continue
        candidates.append(panel)
    available: list[Panel] = []
    for panel in candidates:
        quota = panel_quota(panel)
        if not quota or await panel_used(session, panel.id) < quota:
            available.append(panel)
    return candidates, available


async def effective_rules(
    session: AsyncSession, shop_id: int, *, fresh: bool = False
) -> tuple[int, dict[str, Any]]:
    """Return (panel_owner_shop_id, effective_trial_rules) accounting for reseller bots."""
    from paurubot.core import resale

    cfg = await shop_settings.load(session, shop_id, fresh=fresh)
    sale = await resale.for_shop(session, shop_id)
    if sale is not None:
        return sale.parent_id, await shop_settings.load(session, sale.parent_id, fresh=fresh)
    return shop_id, cfg


async def options(session: AsyncSession, customer: Customer) -> list[Panel]:
    """Eligible trial panels with quota room for `customer`, for multi-panel trial selection."""
    owner, rules = await effective_rules(session, customer.shop_id)
    _, available = await _eligible_panels(session, owner, rules, customer)
    return available


async def _trial_panel(
    session: AsyncSession,
    shop_id: int,
    rules: dict[str, Any],
    customer: Customer | None = None,
    chosen_panel_id: int | None = None,
) -> Panel:
    """The panel for the next trial: customer's pick (if valid), else the first with room."""
    candidates, available = await _eligible_panels(session, shop_id, rules, customer)
    if chosen_panel_id is not None:
        chosen_id = int(chosen_panel_id)
        if not any(p.id == chosen_id for p in candidates):
            used: set[int] = set()
            if customer is not None:
                used, _legacy = await _used_trial_panels(
                    session, customer, int(rules.get("trial_generation") or 1)
                )
            if chosen_id in used:
                raise TrialError("trial_already_used")
            raise TrialError("trial_panel_unavailable")
        for panel in available:
            if panel.id == chosen_id:
                return panel
        raise TrialError("trial_quota_reached")
    if not candidates:
        raise TrialError("trial_disabled")
    if not available:
        raise TrialError("trial_quota_reached")
    return available[0]


async def request(session: AsyncSession, customer: Customer, *, panel_id: int | None = None) -> Order:
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
    panel = await _trial_panel(session, owner, rules, customer, chosen_panel_id=panel_id)
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
        .on_conflict_do_nothing(index_elements=["shop_id", "customer_id", "generation", "panel_id"])
        .returning(TrialGrant.id)
    )
    if grant_id is None:
        raise TrialError("trial_already_used")
    limits = panel_trial_limits(rules, panel)
    trial_params: dict[str, object] = {
        "panel_id": panel.id,
        "days": limits["days"],
        "gb": limits["gb"],
        "grant_id": grant_id,
        "label": "trial",
        **extra,
    }
    if limits["hours"] > 0:
        trial_params["hours"] = limits["hours"]
    if limits["mb"] > 0:
        trial_params["mb"] = limits["mb"]
    order = Order(
        shop_id=customer.shop_id,
        customer_id=customer.id,
        kind=OrderKind.TRIAL,
        status=OrderStatus.PAID,
        params=trial_params,
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
    _, rules = await effective_rules(session, customer.shop_id, fresh=True)
    generation = int(rules["trial_generation"] or 1)
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


async def has_used_current(session: AsyncSession, customer: Customer) -> bool:
    """True when the current round has no trial panel left for this customer."""
    _, rules = await effective_rules(session, customer.shop_id)
    generation = int(rules["trial_generation"] or 1)
    if not bool(rules.get("trial_once_per_panel", True)):
        grant = await session.scalar(
            select(TrialGrant.id)
            .where(
                TrialGrant.shop_id == customer.shop_id,
                TrialGrant.customer_id == customer.id,
                TrialGrant.generation == generation,
            )
            .limit(1)
        )
        return grant is not None
    reason = await eligibility(session, customer)
    return reason in {"trial_already_used", "trial_quota_reached"}


async def _reset_scope(session: AsyncSession, shop_id: int) -> tuple[list[int], int]:
    """Shops and generation covered by a trial reset (reseller bots share the owner's round)."""
    from paurubot.core import resale

    sale = await resale.for_shop(session, shop_id)
    owner = sale.parent_id if sale is not None else shop_id
    cfg = await shop_settings.load(session, owner, fresh=True)
    generation = int(cfg["trial_generation"] or 1)
    if sale is not None:
        return [shop_id], generation
    child_ids = list(
        await session.scalars(
            select(Shop.id).where(Shop.parent_id == owner, Shop.reseller_profile_id.is_not(None))
        )
    )
    return [owner, *child_ids], generation


async def _non_buyer_grant_ids(session: AsyncSession, shop_ids: list[int], generation: int) -> list[int]:
    """Current-round trial grants whose owners are not banned, never paid, and have no active trial."""
    from paurubot.db.models import Service, ServiceStatus

    has_paid_order = (
        select(Order.id)
        .where(
            Order.shop_id == TrialGrant.shop_id,
            Order.customer_id == TrialGrant.customer_id,
            Order.kind != OrderKind.TRIAL,
            Order.status == OrderStatus.COMPLETED,
        )
        .exists()
    )
    has_active_trial = (
        select(Service.id)
        .where(
            Service.shop_id == TrialGrant.shop_id,
            Service.customer_id == TrialGrant.customer_id,
            Service.plan_id.is_(None),
            Service.status == ServiceStatus.ACTIVE,
        )
        .exists()
    )
    rows = await session.scalars(
        select(TrialGrant.id)
        .join(Customer, Customer.id == TrialGrant.customer_id)
        .where(
            TrialGrant.shop_id.in_(shop_ids),
            TrialGrant.generation == generation,
            Customer.is_banned.is_(False),
            ~has_paid_order,
            ~has_active_trial,
        )
    )
    return list(rows)


async def reset_preview(session: AsyncSession, shop_id: int) -> dict[str, int]:
    """Count grants affected by a full vs. non-buyer trial reset in the current generation."""
    shops, generation = await _reset_scope(session, shop_id)
    total = int(
        await session.scalar(
            select(func.count(TrialGrant.id)).where(
                TrialGrant.shop_id.in_(shops), TrialGrant.generation == generation
            )
        )
        or 0
    )
    non_buyers = len(await _non_buyer_grant_ids(session, shops, generation))
    return {"generation": generation, "all": total, "non_buyers": non_buyers}


async def reset_non_buyers(session: AsyncSession, shop_id: int) -> tuple[int, int]:
    """Allow non-buying past trial users to claim a trial again without opening a full round."""
    shops, generation = await _reset_scope(session, shop_id)
    ids = await _non_buyer_grant_ids(session, shops, generation)
    if not ids:
        return 0, generation
    result = await session.execute(
        update(TrialGrant).where(TrialGrant.id.in_(ids)).values(generation=-TrialGrant.id)
    )
    return int(getattr(result, "rowcount", 0) or 0), generation


async def new_round(session: AsyncSession, shop_id: int) -> int:
    """Everyone may take a trial again: the round number goes up by one (CAT-11)."""
    cfg = await shop_settings.load(session, shop_id, fresh=True)
    generation = int(cfg["trial_generation"] or 1) + 1
    await shop_settings.set_many(session, shop_id, {"trial_generation": generation})
    return generation
