"""Reseller hierarchy, limits, metered billing and suspension (TEN-01, TEN-05..TEN-08), and the
reseller hub's timeline and wallet runway (F-21)."""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.core import pricing
from paurubot.core.formatting import local
from paurubot.db.base import utcnow
from paurubot.db.models import (
    AuditLog,
    Customer,
    LedgerAccount,
    LedgerEntry,
    LedgerJournal,
    Order,
    OrderKind,
    OrderStatus,
    ResellerBillingSnapshot,
    ResellerProfile,
    Service,
    ServiceStatus,
    Staff,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB

log = logging.getLogger(__name__)

LIVE = [ServiceStatus.ACTIVE, ServiceStatus.ON_HOLD, ServiceStatus.LIMITED]
BILLING_MODES = ("prepaid", "usage", "hourly")
BILL_EVERY = timedelta(hours=1)
SUSPEND_REASONS = {"debt", "staff"}
# The wallet runway (F-21): spending over the last week, and how soon a warning goes out.
RUNWAY_WINDOW_DAYS = 7
RUNWAY_WARN_DAYS = 2
SPEND_KINDS = ("charge", "usage", "membership")


class ResellerError(Exception):
    def __init__(self, key: str, **params: object) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


# --- hierarchy ---------------------------------------------------------------------


def level(profile: ResellerProfile) -> int:
    return 1 if profile.parent_id is None else 2


async def children(session: AsyncSession, profile: ResellerProfile) -> list[ResellerProfile]:
    return list(
        await session.scalars(
            select(ResellerProfile)
            .where(ResellerProfile.parent_id == profile.id)
            .order_by(ResellerProfile.id)
        )
    )


async def scope_customer_ids(session: AsyncSession, profile: ResellerProfile) -> list[int]:
    """Customers whose services and orders a reseller may see: itself and its level-2 resellers."""
    ids = [profile.customer_id]
    ids += [p.customer_id for p in await children(session, profile)]
    return ids


async def make_child(
    session: AsyncSession, parent: ResellerProfile, customer: Customer, discount: int
) -> ResellerProfile:
    """Turn one of the shop's customers into a level-2 reseller under `parent`."""
    if level(parent) != 1 or not parent.is_current:
        raise ResellerError("subreseller_not_allowed")
    if customer.shop_id != parent.shop_id or customer.id == parent.customer_id:
        raise ResellerError("customer_not_found")
    if customer.reseller_profile is not None:
        raise ResellerError("already_reseller")
    if parent.max_subresellers is not None:
        count = len(await children(session, parent))
        if count >= parent.max_subresellers:
            raise ResellerError("subreseller_limit", limit=parent.max_subresellers)
    check_child_discount(parent, discount)
    profile = ResellerProfile(
        shop_id=parent.shop_id,
        customer_id=customer.id,
        parent_id=parent.id,
        group_id=parent.group_id,
        discount_override=discount,
        permissions=[p for p in parent.permissions if p != "resellers.sub"],
        expires_at=parent.expires_at,
    )
    session.add(profile)
    await session.flush()
    await session.refresh(customer, ["reseller_profile"])
    return profile


def check_child_discount(parent: ResellerProfile, discount: int) -> None:
    if not 0 <= discount <= parent.discount_percent:
        raise ResellerError("subreseller_discount_too_high", max=parent.discount_percent)


async def pay_commission(session: AsyncSession, order: Order) -> int:
    """Give a level-1 reseller the margin on its level-2 reseller's sale."""
    if order.amount <= 0:
        return 0
    profile = await session.scalar(
        select(ResellerProfile).where(ResellerProfile.customer_id == order.customer_id)
    )
    if profile is None or profile.parent_id is None:
        return 0
    parent = await session.get(ResellerProfile, profile.parent_id)
    if parent is None or not parent.is_current:
        return 0
    child_price = order.base_price - order.reseller_discount
    if order.plan_id is not None and order.kind in {OrderKind.PURCHASE, OrderKind.RENEW}:
        # What the level-1 reseller itself would pay for this plan, fixed prices included (TEN-12).
        parent_customer = await session.get(Customer, parent.customer_id)
        quoted = await pricing.plan_quote(session, parent_customer, order.plan_id, order.base_price)
        parent_price = quoted.amount
    else:
        parent_price = pricing.round_price(order.base_price * (100 - parent.discount_percent) / 100)
    commission = max(0, min(child_price - parent_price, order.amount))
    if commission <= 0:
        return 0
    await wallet.commission(
        session,
        order.shop_id,
        parent.customer_id,
        commission,
        key=f"order:{order.id}:commission",
        memo=f"commission for order #{order.id}",
    )
    return commission


# --- limits ------------------------------------------------------------------------


async def check_purchase(
    session: AsyncSession, customer: Customer, *, kind: OrderKind, add_gb: int, bot_shop_id: int | None = None
) -> None:
    """Reseller limits that apply before an order is created (TEN-07, TEN-08). With `bot_shop_id`,
    what the reseller's own bot sold counts against the same limits (TEN-04)."""
    profile = customer.reseller_profile
    own_orders = Order.customer_id == customer.id
    own_services = Service.customer_id == customer.id
    if bot_shop_id is not None:
        own_orders = or_(own_orders, Order.shop_id == bot_shop_id)
        own_services = or_(own_services, Service.shop_id == bot_shop_id)
    if profile is None or not profile.is_current:
        return
    if profile.suspended_at is not None:
        raise ResellerError("reseller_suspended_error")
    if kind == OrderKind.PURCHASE:
        if profile.max_purchases_per_day:
            day_start = local(utcnow()).replace(hour=0, minute=0, second=0, microsecond=0)
            bought = await session.scalar(
                select(func.count(Order.id)).where(
                    own_orders,
                    Order.kind == OrderKind.PURCHASE,
                    Order.status.in_([OrderStatus.PAID, OrderStatus.PROVISIONING, OrderStatus.COMPLETED]),
                    Order.created_at >= day_start,
                )
            )
            if int(bought or 0) >= profile.max_purchases_per_day:
                raise ResellerError("reseller_daily_limit", limit=profile.max_purchases_per_day)
        cap = profile.max_active_services or (profile.group.max_active_services if profile.group else None)
        if cap:
            live = await session.scalar(
                select(func.count(Service.id)).where(own_services, Service.status.in_(LIVE))
            )
            if int(live or 0) >= cap:
                raise ResellerError("reseller_service_limit", limit=cap)
    if profile.max_total_gb and kind in {OrderKind.PURCHASE, OrderKind.EXTRA_VOLUME}:
        if add_gb <= 0:
            raise ResellerError("reseller_volume_limit", limit=profile.max_total_gb)
        allotted = await session.scalar(
            select(func.coalesce(func.sum(Service.data_limit), 0)).where(
                own_services, Service.status.in_(LIVE)
            )
        )
        if int(allotted or 0) + add_gb * GB > profile.max_total_gb * GB:
            raise ResellerError("reseller_volume_limit", limit=profile.max_total_gb)


def metered(customer: Customer) -> bool:
    profile = customer.reseller_profile
    return profile is not None and profile.is_current and profile.is_metered


# --- metered billing ---------------------------------------------------------------


@dataclass(slots=True)
class BillResult:
    snapshot: ResellerBillingSnapshot | None
    suspended: bool = False
    resumed: bool = False


def usage_amount(used_bytes: int, gb_price: int) -> int:
    """Price of `used_bytes` at `gb_price` per GB, rounded up to whole Toman."""
    return -(-used_bytes * gb_price // GB) if used_bytes > 0 and gb_price > 0 else 0


async def _collect(session: AsyncSession, profile: ResellerProfile, amount: int, key: str) -> int:
    """Take what the wallet can pay of `amount` (plus any old debt); the rest stays as debt."""
    due = amount + profile.billing_debt
    if due <= 0:
        return 0
    balance = await wallet_balance(session, profile.shop_id, profile.customer_id)
    take = max(0, min(balance, due))
    if take:
        posted = await wallet.usage_charge(session, profile.shop_id, profile.customer_id, take, key=key)
        if posted is None or not posted.created:
            return 0  # this period was already collected
    profile.billing_debt = due - take
    return take


async def bill(session: AsyncSession, profile: ResellerProfile, now: datetime | None = None) -> BillResult:
    """One billing step for a metered reseller. Idempotent per period; the caller commits."""
    now = now or utcnow()
    start = profile.last_billed_at or profile.created_at
    if now - start < BILL_EVERY:
        if profile.billing_debt > 0:
            await _collect(session, profile, 0, f"reseller:{profile.id}:debt:{secrets.token_hex(8)}")
        return BillResult(None, *await _apply_suspension(session, profile))
    hours = int((now - start) // BILL_EVERY)
    end = start + hours * BILL_EVERY
    services = list(
        await session.scalars(
            select(Service).where(
                Service.customer_id == profile.customer_id, Service.status != ServiceStatus.DELETED
            )
        )
    )
    used = 0
    for service in services:
        current = int(service.used_bytes or 0)
        billed = int(service.billed_bytes or 0)
        used += current - billed if current >= billed else current  # a reset starts counting again
        service.billed_bytes = current
    active = sum(1 for s in services if s.status == ServiceStatus.ACTIVE)
    service_hours = active * hours
    if profile.billing_mode == "usage":
        amount = usage_amount(used, profile.gb_price)
    elif profile.billing_mode == "hourly":
        amount = service_hours * profile.hourly_price
    else:
        amount = 0
    charged = await _collect(session, profile, amount, f"reseller:{profile.id}:bill:{int(end.timestamp())}")
    profile.last_billed_at = end
    snapshot = ResellerBillingSnapshot(
        shop_id=profile.shop_id,
        profile_id=profile.id,
        period_start=start,
        period_end=end,
        used_bytes=used,
        service_hours=service_hours,
        active_services=active,
        amount=amount,
        charged=charged,
        debt_after=profile.billing_debt,
    )
    session.add(snapshot)
    await session.flush()
    suspended, resumed = await _apply_suspension(session, profile)
    return BillResult(snapshot, suspended, resumed)


async def _apply_suspension(session: AsyncSession, profile: ResellerProfile) -> tuple[bool, bool]:
    over = profile.billing_debt > max(0, profile.credit_limit)
    if over and profile.suspended_at is None:
        await suspend(session, profile, "debt")
        await _audit_auto(session, profile, "reseller.suspend_auto")
        return True, False
    if not over and profile.suspended_at is not None and profile.suspend_reason == "debt":
        await resume(session, profile)
        await _audit_auto(session, profile, "reseller.resume_auto")
        return False, True
    return False, False


async def _audit_auto(session: AsyncSession, profile: ResellerProfile, action: str) -> None:
    """Billing stops and restarts a reseller on its own; keep that in its timeline (F-21)."""
    from paurubot.audit.log import Actor, record

    await record(
        session,
        shop_id=profile.shop_id,
        actor=Actor.system(),
        action=action,
        entity="reseller_profile",
        entity_id=profile.id,
        after={"debt": profile.billing_debt, "credit_limit": profile.credit_limit},
    )


async def suspend(session: AsyncSession, profile: ResellerProfile, reason: str) -> int:
    """Stop a reseller's services (they come back on resume) and tell the reseller (TEN-06)."""
    from paurubot.worker import queue

    profile.suspended_at = utcnow()
    profile.suspend_reason = reason
    ids = list(
        await session.scalars(
            select(Service.id).where(Service.customer_id == profile.customer_id, Service.status.in_(LIVE))
        )
    )
    stamp = int(utcnow().timestamp())
    for service_id in ids:
        await queue.enqueue(
            session,
            "bulk",
            "service",
            {"service_id": service_id, "action": "disable", "reason": "reseller"},
            shop_id=profile.shop_id,
            dedupe_key=f"reseller:{service_id}:off:{stamp}",
        )
    await queue.enqueue(
        session,
        "notify",
        "reseller_notice",
        {"profile_id": profile.id, "notice": "suspended", "reason": reason},
        shop_id=profile.shop_id,
        dedupe_key=f"reseller:{profile.id}:suspended:{stamp}",
    )
    return len(ids)


async def resume(session: AsyncSession, profile: ResellerProfile) -> int:
    from paurubot.worker import queue

    profile.suspended_at = None
    profile.suspend_reason = None
    ids = list(
        await session.scalars(
            select(Service.id).where(
                Service.customer_id == profile.customer_id,
                Service.status == ServiceStatus.DISABLED,
                Service.disabled_reason == "reseller",
            )
        )
    )
    stamp = int(utcnow().timestamp())
    for service_id in ids:
        await queue.enqueue(
            session,
            "bulk",
            "service",
            {"service_id": service_id, "action": "enable", "reason": "reseller"},
            shop_id=profile.shop_id,
            dedupe_key=f"reseller:{service_id}:on:{stamp}",
        )
    await queue.enqueue(
        session,
        "notify",
        "reseller_notice",
        {"profile_id": profile.id, "notice": "resumed"},
        shop_id=profile.shop_id,
        dedupe_key=f"reseller:{profile.id}:resumed:{stamp}",
    )
    return len(ids)


async def warn_low_balance(session: AsyncSession, profile: ResellerProfile) -> bool:
    """At most one low-balance warning a day per reseller (TEN-06): below its threshold, or (F-21)
    when at the current pace the wallet runs dry within RUNWAY_WARN_DAYS. The runway warning is
    for resellers that set a threshold or are billed by usage / the hour."""
    from paurubot.worker import queue

    if not profile.is_current or not (profile.low_balance_threshold or profile.is_metered):
        return False
    balance = await wallet_balance(session, profile.shop_id, profile.customer_id)
    runway = await estimate_runway(session, profile, balance=balance)
    below = bool(profile.low_balance_threshold) and balance < int(profile.low_balance_threshold or 0)
    if not below and not runway.low:
        return False
    if not await kv.set_once(f"reseller:{profile.id}:lowbalance", 86_400):
        return False
    await queue.enqueue(
        session,
        "notify",
        "reseller_notice",
        {"profile_id": profile.id, "notice": "low_balance", "balance": balance, "days": runway.days},
        shop_id=profile.shop_id,
    )
    return True


# --- the reseller hub: runway and timeline (F-21) -------------------------------------------


@dataclass(slots=True)
class Runway:
    balance: int
    debt: int
    daily_spend: int  # average over the window, rounded up
    days: float | None  # None: nothing was spent, so no estimate

    @property
    def hours(self) -> int | None:
        return None if self.days is None else int(self.days * 24)

    @property
    def low(self) -> bool:
        return self.days is not None and self.days < RUNWAY_WARN_DAYS

    def as_dict(self) -> dict[str, Any]:
        return {
            "balance": self.balance,
            "debt": self.debt,
            "daily_spend": self.daily_spend,
            "days": self.days,
            "hours": self.hours,
            "low": self.low,
            "window_days": RUNWAY_WINDOW_DAYS,
        }


def runway_from(balance: int, debt: int, spent: int, window_days: int = RUNWAY_WINDOW_DAYS) -> Runway:
    """How long `balance` (less any billing debt) lasts at the window's average daily spend."""
    daily = -(-spent // window_days) if spent > 0 and window_days > 0 else 0
    if daily <= 0:
        return Runway(balance, debt, 0, None)
    days = max(0, balance - debt) / daily
    return Runway(balance, debt, daily, round(days, 1))


def runway_line(t: Any, days: float) -> str:
    """"Lasts about N days" (or hours, under a day) in the reader's language; `t` is their Texts."""
    if days < 1:
        return str(t("reseller_runway_hours", hours=int(days * 24)))
    return str(t("reseller_runway_days", days=f"{days:g}"))


async def spent_since(session: AsyncSession, profile: ResellerProfile, since: datetime) -> int:
    """What left the reseller's wallet for purchases, renewals, usage and membership since
    `since`, less what came back as refunds. Top-ups, gifts and staff adjustments do not count."""
    rows = await session.execute(
        select(LedgerJournal.kind, func.coalesce(func.sum(LedgerEntry.amount), 0))
        .join(LedgerJournal, LedgerJournal.id == LedgerEntry.journal_id)
        .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
        .where(
            LedgerAccount.shop_id == profile.shop_id,
            LedgerAccount.customer_id == profile.customer_id,
            LedgerEntry.created_at >= since,
            LedgerJournal.kind.in_([*SPEND_KINDS, "refund"]),
        )
        .group_by(LedgerJournal.kind)
    )
    # Spending debits the wallet (negative entries) and refunds credit it back, so negating the
    # sum over both kinds gives the net spend.
    total = -sum(int(amount) for _kind, amount in rows.all())
    return max(0, total)


async def estimate_runway(
    session: AsyncSession,
    profile: ResellerProfile,
    *,
    balance: int | None = None,
    now: datetime | None = None,
) -> Runway:
    """The reseller's wallet runway at the last RUNWAY_WINDOW_DAYS days' pace (F-21)."""
    now = now or utcnow()
    if balance is None:
        balance = await wallet_balance(session, profile.shop_id, profile.customer_id)
    spent = await spent_since(session, profile, now - timedelta(days=RUNWAY_WINDOW_DAYS))
    return runway_from(balance, int(profile.billing_debt or 0), spent)


async def timeline(session: AsyncSession, profile: ResellerProfile, limit: int = 50) -> list[dict[str, Any]]:
    """The reseller account's history, newest first: wallet movements (purchases, renewals, top-ups,
    usage charges, adjustments) merged with what staff and billing did to the account (pause,
    resume, limits, group) from the audit trail."""
    items: list[dict[str, Any]] = []
    for entry, journal in await wallet.history(session, profile.shop_id, profile.customer_id, limit=limit):
        items.append(
            {
                "at": entry.created_at,
                "type": "ledger",
                "kind": journal.kind,
                "amount": entry.amount,
                "balance_after": entry.balance_after,
                "memo": journal.memo,
                "reference_type": journal.reference_type,
                "reference_id": journal.reference_id,
            }
        )
    audits = await session.scalars(
        select(AuditLog)
        .where(
            AuditLog.shop_id == profile.shop_id,
            or_(
                and_(AuditLog.entity == "reseller_profile", AuditLog.entity_id == profile.id),
                and_(AuditLog.entity == "customer", AuditLog.entity_id == profile.customer_id),
            ),
        )
        .order_by(AuditLog.id.desc())
        .limit(limit)
    )
    for row in audits:
        items.append(
            {
                "at": row.created_at,
                "type": "audit",
                "kind": row.action,
                "actor_type": row.actor_type,
                "actor_id": row.actor_id,
                "before": row.before,
                "after": row.after,
            }
        )
    items.sort(key=lambda item: item["at"], reverse=True)
    return items[:limit]


async def run_billing(session: AsyncSession) -> int:
    """Bill every metered reseller and warn low balances. Commits per reseller."""
    ids = list(
        await session.scalars(
            select(ResellerProfile.id).where(
                (ResellerProfile.billing_mode.in_(["usage", "hourly"]))
                | (ResellerProfile.billing_debt > 0)
                | (ResellerProfile.low_balance_threshold.is_not(None))
            )
        )
    )
    billed = 0
    for profile_id in ids:
        profile = await session.get(ResellerProfile, profile_id, with_for_update=True)
        if profile is None:
            continue
        try:
            if profile.is_metered or profile.billing_debt > 0:
                result = await bill(session, profile)
                billed += 1 if result.snapshot else 0
            await warn_low_balance(session, profile)
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("billing failed for reseller profile %s", profile_id)
    return billed


# --- web panel access ------------------------------------------------------------------


async def web_login(session: AsyncSession, profile: ResellerProfile) -> Staff | None:
    """The reseller's own sign-in to the web panel, made on approval: a reseller staff account
    tied to its Telegram, which signs in with a one-time code the bot sends. None when the shop
    turned this off or the reseller has no Telegram account to receive codes."""
    from paurubot.auth.staff import create_staff
    from paurubot.core import settings as shop_settings
    from paurubot.db.models import StaffRole

    existing = await session.scalar(select(Staff).where(Staff.reseller_profile_id == profile.id))
    if existing is not None:
        return existing if existing.is_active else None
    if not await shop_settings.get(session, profile.shop_id, "reseller_web_access"):
        return None
    customer = await session.get(Customer, profile.customer_id)
    if customer is None or customer.telegram_id <= 0:
        return None
    base = f"r{customer.telegram_id}"
    name = base
    while await session.scalar(select(Staff.id).where(func.lower(Staff.username) == name.lower())):
        name = f"{base}_{secrets.token_hex(2)}"
    return await create_staff(
        session,
        profile.shop_id,
        role=StaffRole.RESELLER,
        username=name,
        password=None,
        telegram_id=customer.telegram_id,
        display_name=customer.display_name[:96],
        reseller_profile_id=profile.id,
    )


def web_panel_url() -> str:
    from paurubot.config import get_settings

    return f"{get_settings().public_url.rstrip('/')}/admin/"


# --- paid membership (TEN-16) -------------------------------------------------------------------


async def membership_offer(session: AsyncSession, shop_id: int) -> tuple[int, int, Any] | None:
    """(price, days, group) when the shop sells reseller membership, else None."""
    from paurubot.core import settings as shop_settings
    from paurubot.db.models import ResellerGroup

    cfg = await shop_settings.load(session, shop_id)
    price = int(cfg["reseller_membership_price"] or 0)
    group_id = cfg["reseller_membership_group_id"]
    if price <= 0 or not group_id:
        return None
    group = await session.get(ResellerGroup, int(group_id))
    if group is None or group.shop_id != shop_id or not group.is_active:
        return None
    return price, int(cfg["reseller_membership_days"] or 0), group


async def buy_membership(session: AsyncSession, customer: Customer, *, key: str) -> ResellerProfile:
    """Pay from the wallet to become a reseller, or to extend a membership that ends. A profile
    staff switched off is never reopened this way."""
    from paurubot.auth import rbac
    from paurubot.ledger.accounts import system_account, wallet_account
    from paurubot.ledger.posting import InsufficientFunds, transfer

    offer = await membership_offer(session, customer.shop_id)
    if offer is None:
        raise ResellerError("membership_off")
    price, days, group = offer
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.customer_id == customer.id))
    if profile is not None and (not profile.is_active or profile.parent_id is not None):
        raise ResellerError("membership_off")
    if profile is not None and profile.expires_at is None:
        raise ResellerError("already_reseller")
    source = await wallet_account(session, customer.shop_id, customer.id)
    target = await system_account(session, customer.shop_id, "revenue")
    try:
        posted = await transfer(
            session,
            shop_id=customer.shop_id,
            source_id=source.id,
            target_id=target.id,
            amount=price,
            kind="membership",
            idempotency_key=f"membership:{key}",
            reference_type="customer",
            reference_id=customer.id,
            memo="reseller membership",
        )
    except InsufficientFunds as exc:
        raise ResellerError("insufficient_balance", balance=exc.balance, required=price) from exc
    if not posted.created and profile is not None:
        return profile  # the same confirmation pressed twice
    now = utcnow()
    if profile is None:
        profile = ResellerProfile(
            shop_id=customer.shop_id,
            customer_id=customer.id,
            group_id=group.id,
            permissions=sorted(rbac.DEFAULT_FOR_RESELLER),
            expires_at=now + timedelta(days=days) if days > 0 else None,
        )
        session.add(profile)
        await session.flush()
        await session.refresh(customer, ["reseller_profile"])
    else:
        start = profile.expires_at if profile.expires_at and profile.expires_at > now else now
        profile.expires_at = start + timedelta(days=days) if days > 0 else None
    await web_login(session, profile)
    await session.flush()
    return profile
