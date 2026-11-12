"""Selling panel admin accounts (TEN-14).

A customer buys a PasarGuard admin with a role, a traffic quota, a user cap and a period, then
creates and manages its own users in the panel. Buying, renewing and adding traffic or user slots
are ordinary orders, so every payment method, the ledger and refunds work as for services; the
provisioning worker applies them to the panel.
"""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import settings as shop_settings
from paurubot.core.orders import ACCOUNT_KINDS, Draft, OrderError
from paurubot.db.base import utcnow
from paurubot.db.models import (
    AccountPlan,
    Category,
    Customer,
    Order,
    OrderKind,
    Panel,
    PanelAccount,
    PanelKind,
    PlanAudience,
)
from paurubot.panels import GB, PanelError, driver_for
from paurubot.provisioning import renewal_policy
from paurubot.security import secretbox

KINDS = ACCOUNT_KINDS
LIVE = ("active", "paused", "expired")
MAX_GB = 100_000
MAX_USERS = 10_000
MAX_DAYS = 3650
SPECIAL = "!@#$%^&*-_=+"


class AccountError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def new_password() -> str:
    """16 characters that pass PasarGuard's rules: two upper, two lower, two digits, a symbol."""
    pool = string.ascii_letters + string.digits
    while True:
        body = [secrets.choice(pool) for _ in range(12)]
        body += [secrets.choice(string.ascii_uppercase) for _ in range(2)]
        body += [secrets.choice(string.digits) for _ in range(1)]
        body += [secrets.choice(SPECIAL)]
        secrets.SystemRandom().shuffle(body)
        password = "".join(body)
        if (
            sum(c.isupper() for c in password) >= 2
            and sum(c.islower() for c in password) >= 2
            and sum(c.isdigit() for c in password) >= 2
        ):
            return password


def login_url(account: PanelAccount) -> str:
    """Where the buyer signs in: the panel's dashboard."""
    panel = account.panel
    base = str((panel.options or {}).get("dashboard_url") or "") if panel else ""
    if not base and panel is not None:
        base = panel.base_url.rstrip("/") + "/dashboard/"
    return base


def password_of(account: PanelAccount) -> str:
    return secretbox.decrypt("panel", account.password_enc)


async def available(session: AsyncSession, customer: Customer) -> bool:
    """Whether this customer may buy admin accounts: switched on, not a reseller bot's shop,
    and for resellers only when the shop says so."""
    from paurubot.core import resale

    cfg = await shop_settings.load(session, customer.shop_id)
    if not cfg.get("panel_accounts_enabled") or await resale.for_shop(session, customer.shop_id):
        return False
    return customer.is_reseller or not cfg.get("panel_accounts_resellers_only")


def offered_to(plan: AccountPlan, customer: Customer | None) -> bool:
    """A plan's audience: everyone, ordinary customers only, or resellers only."""
    audience = plan.audience or PlanAudience.ALL
    if audience == PlanAudience.ALL or customer is None:
        return True
    return (audience == PlanAudience.RESELLERS) == bool(customer.is_reseller)


async def plans(
    session: AsyncSession,
    shop_id: int,
    customer: Customer | None = None,
    *,
    category_id: int | None = None,
    uncategorised: bool = False,
) -> list[AccountPlan]:
    """The plans on sale; with a customer, only those offered to them. A plan in a category that
    is switched off is not on sale, as with config plans."""
    category_ok = select(Category.id).where(Category.shop_id == shop_id, Category.is_active.is_(True))
    stmt = (
        select(AccountPlan)
        .join(Panel, Panel.id == AccountPlan.panel_id)
        .where(
            AccountPlan.shop_id == shop_id,
            AccountPlan.is_active.is_(True),
            Panel.is_active.is_(True),
            Panel.kind == PanelKind.PASARGUARD,
            or_(AccountPlan.category_id.is_(None), AccountPlan.category_id.in_(category_ok)),
        )
        .order_by(AccountPlan.sort, AccountPlan.price, AccountPlan.id)
    )
    if category_id:
        stmt = stmt.where(AccountPlan.category_id == category_id)
    elif uncategorised:
        stmt = stmt.where(AccountPlan.category_id.is_(None))
    return [plan for plan in await session.scalars(stmt) if offered_to(plan, customer)]


async def categories(session: AsyncSession, items: list[AccountPlan]) -> list[Category]:
    """The categories these plans sit in, in the shop's order (empty when none has one)."""
    ids = {plan.category_id for plan in items} - {None}
    if not ids:
        return []
    rows = await session.scalars(
        select(Category)
        .where(Category.id.in_(ids), Category.is_active.is_(True))
        .order_by(Category.sort, Category.id)
    )
    return list(rows)


async def plan_for(
    session: AsyncSession, shop_id: int, plan_id: int, customer: Customer | None = None
) -> AccountPlan | None:
    return next((p for p in await plans(session, shop_id, customer) if p.id == plan_id), None)


async def accounts_of(session: AsyncSession, customer: Customer) -> list[PanelAccount]:
    rows = await session.scalars(
        select(PanelAccount)
        .where(PanelAccount.customer_id == customer.id, PanelAccount.status.in_(LIVE))
        .order_by(PanelAccount.id.desc())
    )
    return list(rows)


async def owned(session: AsyncSession, customer: Customer, account_id: int) -> PanelAccount | None:
    account = await session.get(PanelAccount, account_id)
    if account is None or account.customer_id != customer.id or account.status not in LIVE:
        return None
    return account


async def extra_bounds(
    session: AsyncSession, shop_id: int, plan: AccountPlan | None = None
) -> dict[str, tuple[int, int]]:
    """How much extra traffic (GB) or time (days) one order may buy: the plan's own range where
    it sets one, else the shop's."""
    cfg = await shop_settings.load(session, shop_id)

    def pair(unit: str, cap: int) -> tuple[int, int]:
        own_low = getattr(plan, f"{unit}_min", None) if plan is not None else None
        own_high = getattr(plan, f"{unit}_max", None) if plan is not None else None
        low_value = own_low if own_low is not None else cfg.get(f"panel_accounts_{unit}_min")
        high_value = own_high if own_high is not None else cfg.get(f"panel_accounts_{unit}_max")
        low = min(max(int(low_value or 1), 1), cap)
        high = min(max(int(high_value or cap), low), cap)
        return low, high

    return {"gb": pair("gb", MAX_GB), "days": pair("days", MAX_DAYS)}


MAX_PACKAGES = 12


def clean_packages(raw: Any) -> list[dict[str, int]]:
    """Validate renewal packages: at most 12, each whole days (1..3650), GB (0: no quota) and a
    price. Raises ValueError on anything else."""
    if not isinstance(raw, list) or len(raw) > MAX_PACKAGES:
        raise ValueError("renew_packages_invalid")
    out: list[dict[str, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("renew_packages_invalid")
        values: dict[str, int] = {}
        for key, low, high in (("days", 1, MAX_DAYS), ("gb", 0, MAX_GB), ("price", 0, 10**12)):
            value = item.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError("renew_packages_invalid")
            values[key] = value
        out.append(values)
    return out


def renew_packages(plan: AccountPlan) -> list[dict[str, int]]:
    """The plan's ready-made renewals, when they are switched on."""
    if not plan.renew_packages_enabled:
        return []
    try:
        return clean_packages(plan.renew_packages or [])
    except ValueError:
        return []


def custom_renewal(plan: AccountPlan) -> bool:
    """A renewal the buyer sizes: switched on and priced (per day, and per GB with a quota)."""
    return bool(plan.renew_custom_enabled) and plan.day_price > 0 and (plan.data_gb <= 0 or plan.gb_price > 0)


def plan_of(kind: OrderKind, plan: AccountPlan | None, account: PanelAccount | None) -> AccountPlan | None:
    """The plan an account order is priced by: the one bought, or the account's own."""
    if kind == OrderKind.ACCOUNT_PURCHASE:
        return plan
    return account.plan if account is not None else None


def discounts_allowed(plan: AccountPlan | None) -> bool:
    return plan is not None and bool(plan.discounts_allowed)


def plain_renewal(plan: AccountPlan) -> bool:
    """With neither packages nor a custom renewal on offer, a renewal repeats the plan."""
    return not renew_packages(plan) and not custom_renewal(plan)


def can_buy_days(account: PanelAccount, plan: AccountPlan | None) -> bool:
    """Extra days are sold when the plan prices them and the account has an end date."""
    return plan is not None and plan.day_price > 0 and account.expire_at is not None


async def draft(
    session: AsyncSession,
    customer: Customer,
    kind: OrderKind,
    *,
    plan: AccountPlan | None = None,
    account: PanelAccount | None = None,
    amount: int = 0,
    package: int | None = None,
    renew_days: int = 0,
    renew_gb: int = 0,
) -> Draft:
    """Price and validate an account order; `amount` is GB, days or user slots for the add-ons."""
    d = await _draft(session, customer, kind, plan, account, amount, package, renew_days, renew_gb)
    priced_by = plan_of(kind, plan, account)
    if priced_by is not None and priced_by.reseller_discount:
        d.params = {**d.params, "reseller_priced": True}  # the reseller group's discount applies
    if discounts_allowed(priced_by):
        d.params = {**d.params, "discounts": True}  # a discount code may be used
    return d


async def _draft(
    session: AsyncSession,
    customer: Customer,
    kind: OrderKind,
    plan: AccountPlan | None,
    account: PanelAccount | None,
    amount: int,
    package: int | None,
    renew_days: int,
    renew_gb: int,
) -> Draft:
    if kind not in KINDS:
        raise OrderError("plan_unavailable")
    if not await available(session, customer):
        raise OrderError("accounts_unavailable")
    if kind == OrderKind.ACCOUNT_PURCHASE:
        if (
            plan is None
            or plan.shop_id != customer.shop_id
            or not plan.is_active
            or not offered_to(plan, customer)
        ):
            raise OrderError("plan_unavailable")
        return Draft(kind, None, None, {"account_plan_id": plan.id}, int(plan.price))
    if account is None or account.customer_id != customer.id or account.status not in LIVE:
        raise OrderError("account_not_found")
    current = await session.get(AccountPlan, account.plan_id) if account.plan_id else None
    # An archived plan is no longer sold, but the accounts sold with it keep their renewals and
    # add-ons (as config services do); "renewable: off" is what stops renewals.
    if current is None:
        raise OrderError("plan_unavailable")
    params: dict[str, Any] = {"account_id": account.id, "account_plan_id": current.id}
    if kind == OrderKind.ACCOUNT_RENEW:
        if not current.renewable:
            raise OrderError("account_not_renewable")
        return await _renewal(session, customer, current, params, package, renew_days, renew_gb)
    if account.status not in {"active", "paused"}:
        raise OrderError("account_not_active")  # an ended account is renewed first
    if kind == OrderKind.ACCOUNT_GB:
        if current.gb_price <= 0 or not account.data_limit:
            raise OrderError("extras_unavailable")
        low, high = (await extra_bounds(session, customer.shop_id, current))["gb"]
        if not low <= amount <= high:
            raise OrderError("acc_amount_range", min_value=low, max_value=high)
        return Draft(kind, None, None, {**params, "gb": amount}, amount * int(current.gb_price))
    if kind == OrderKind.ACCOUNT_DAYS:
        if not can_buy_days(account, current):
            raise OrderError("extras_unavailable")
        low, high = (await extra_bounds(session, customer.shop_id, current))["days"]
        if not low <= amount <= high:
            raise OrderError("acc_amount_range", min_value=low, max_value=high)
        return Draft(kind, None, None, {**params, "days": amount}, amount * int(current.day_price))
    if current.user_price <= 0 or not account.max_users or not 1 <= amount <= MAX_USERS:
        raise OrderError("extras_unavailable")
    return Draft(kind, None, None, {**params, "users": amount}, amount * int(current.user_price))


async def _renewal(
    session: AsyncSession,
    customer: Customer,
    plan: AccountPlan,
    params: dict[str, Any],
    package: int | None,
    days: int,
    gb: int,
) -> Draft:
    """A renewal: one of the plan's packages, a custom size, or the plan itself. What it gives is
    fixed in the order, so a later change to the plan does not change a paid renewal."""
    kind = OrderKind.ACCOUNT_RENEW
    if package is not None:
        packages = renew_packages(plan)
        if not 0 <= package < len(packages):
            raise OrderError("renew_option_unavailable")
        chosen = packages[package]
        renew = {"days": chosen["days"], "gb": chosen["gb"], "package": package}
        return Draft(kind, None, None, {**params, "renew": renew}, chosen["price"])
    if days:
        if not custom_renewal(plan):
            raise OrderError("renew_option_unavailable")
        bounds = await extra_bounds(session, customer.shop_id, plan)
        low, high = bounds["days"]
        if not low <= days <= high:
            raise OrderError("acc_amount_range", min_value=low, max_value=high)
        if plan.data_gb > 0:
            low, high = bounds["gb"]
            if not low <= gb <= high:
                raise OrderError("acc_amount_range", min_value=low, max_value=high)
        else:
            gb = 0  # a plan without a quota stays without one
        price = days * int(plan.day_price) + gb * int(plan.gb_price)
        return Draft(kind, None, None, {**params, "renew": {"days": days, "gb": gb}}, price)
    if not plain_renewal(plan):
        raise OrderError("renew_option_unavailable")
    return Draft(kind, None, None, params, int(plan.price))


# --- applying paid orders (the provisioning worker) --------------------------------------


class ProvisionError(Exception):
    """Permanent: retrying cannot help."""


async def _driver(session: AsyncSession, panel_id: int) -> Any:
    from paurubot.panels.drivers.pasarguard import PasarGuardDriver

    panel = await session.get(Panel, panel_id)
    if panel is None or not panel.is_active:
        raise ProvisionError("panel_unavailable")
    driver = await driver_for(panel)
    if not isinstance(driver, PasarGuardDriver):
        raise ProvisionError("panel_unavailable")
    return driver


def _telegram_id_taken(exc: PanelError) -> bool:
    text = f"{exc} {exc.body or ''}".lower()
    return exc.status in (400, 409) and "telegram" in text and ("assigned" in text or "exist" in text)


async def _username(session: AsyncSession, customer: Customer, panel_id: int) -> str:
    base = str(customer.telegram_id) if customer.telegram_id > 0 else secrets.token_hex(4)
    while True:
        name = f"r{base}_{secrets.token_hex(3)}"
        taken = await session.scalar(
            select(PanelAccount.id).where(PanelAccount.panel_id == panel_id, PanelAccount.username == name)
        )
        if taken is None:
            return name


async def apply(session: AsyncSession, order: Order) -> PanelAccount:
    """Carry out one paid account order. Every step is recorded first, so a retry continues."""
    params = dict(order.params or {})
    plan = await session.get(AccountPlan, int(params.get("account_plan_id") or 0))
    if plan is None:
        raise ProvisionError("plan_unavailable")
    if order.kind == OrderKind.ACCOUNT_PURCHASE:
        return await _create(session, order, plan, params)
    account = await session.get(PanelAccount, int(params.get("account_id") or 0))
    if account is None or account.status not in LIVE:
        raise ProvisionError("account_not_found")
    driver = await _driver(session, account.panel_id)
    if order.kind == OrderKind.ACCOUNT_RENEW:
        if "target_expire" not in params:
            # The end state is worked out once, before the panel is touched, so a retry applies
            # exactly the same values (as for services, renewal_policy).
            params.update(await renewal_targets(driver, account, plan, params))
            order.params = dict(params)  # a new object, so the JSON change is saved
            await session.commit()
        await _renew(driver, account, plan, params)
    elif order.kind == OrderKind.ACCOUNT_DAYS:
        if "target_expire" not in params:
            # From the current end date, or from now if it already passed (a paused account).
            # An account that has no end date any more (staff made it unlimited) keeps none.
            now = utcnow()
            if account.expire_at is None:
                params["target_expire"] = None
            else:
                start = account.expire_at if account.expire_at > now else now
                params["target_expire"] = (start + timedelta(days=int(params["days"]))).isoformat()
            order.params = dict(params)  # a new object, so the JSON change is saved
            await session.commit()
        # The panel has no end date for an admin: the bot switches it off when this passes.
        target = params["target_expire"]
        account.expire_at = datetime.fromisoformat(target) if target else None
        if account.status == "expired" and (account.expire_at is None or account.expire_at > utcnow()):
            # It ran out between paying and now: the paid days switch it back on.
            await driver.modify_admin(account.username, status="active")
            await driver.set_admin_users(account.username, True)
            account.status = "active"
    elif order.kind == OrderKind.ACCOUNT_GB:
        if "target_limit" not in params:
            # An unlimited quota (0) stays unlimited: adding to it would create a limit.
            limit = int(account.data_limit or 0)
            params["target_limit"] = limit + int(params["gb"]) * GB if limit else 0
            order.params = dict(params)  # a new object, so the JSON change is saved
            await session.commit()
        account.data_limit = int(params["target_limit"])
        fields: dict[str, Any] = {"data_limit": account.data_limit}
        if account.status == "active":
            fields["status"] = "active"  # a quota that ran out is lifted by the new traffic
        await driver.modify_admin(account.username, **fields)
    else:
        if "target_users" not in params:
            params["target_users"] = int(account.max_users or 0) + int(params["users"])
            order.params = dict(params)  # a new object, so the JSON change is saved
            await session.commit()
        account.max_users = int(params["target_users"])
        await driver.modify_admin(account.username, max_users=account.max_users)
    account.alerts_sent = {}
    await session.flush()
    from paurubot.core import own_panel

    # Renewed or topped up: its dedicated bot (if any) sells again right away.
    await own_panel.announce(session, await own_panel.recheck_account(session, account))
    return account


async def _create(
    session: AsyncSession, order: Order, plan: AccountPlan, params: dict[str, Any]
) -> PanelAccount:
    existing = await session.scalar(select(PanelAccount).where(PanelAccount.id == params.get("account_id")))
    if existing is not None:
        return existing  # finished on an earlier attempt
    customer = await session.get(Customer, order.customer_id)
    assert customer is not None
    driver = await _driver(session, plan.panel_id)
    if not params.get("username"):
        params["username"] = await _username(session, customer, plan.panel_id)
        params["password_enc"] = secretbox.encrypt("panel", new_password())
        order.params = dict(params)  # a new object, so the JSON change is saved
        await session.commit()
    username = str(params["username"])
    password = secretbox.decrypt("panel", str(params["password_enc"]))
    if await driver.get_admin(username) is None:
        fields: dict[str, Any] = {
            "role_id": plan.role_id,
            "data_limit": plan.data_gb * GB if plan.data_gb > 0 else 0,
            "max_users": plan.max_users,
            "note": f"tg:{customer.telegram_id} shop:{order.shop_id}",
        }
        try:
            await driver.create_admin(username, password, telegram_id=customer.telegram_id, **fields)
        except PanelError as exc:
            # PasarGuard gives one admin per Telegram id: the customer's second account there (or an
            # admin made by hand with their id) is made without it. The id only drives the panel's
            # own notices to that admin; the account works the same.
            if not _telegram_id_taken(exc):
                raise
            await driver.create_admin(username, password, telegram_id=None, **fields)
    account = PanelAccount(
        shop_id=order.shop_id,
        customer_id=customer.id,
        panel_id=plan.panel_id,
        plan_id=plan.id,
        username=username,
        password_enc=str(params["password_enc"]),
        data_limit=plan.data_gb * GB if plan.data_gb > 0 else 0,
        max_users=plan.max_users,
        expire_at=utcnow() + timedelta(days=plan.days) if plan.days > 0 else None,
        status="active",
    )
    session.add(account)
    await session.flush()
    params["account_id"] = account.id
    order.params = dict(params)  # a new object, so the JSON change is saved
    return account


def renew_rule(plan: AccountPlan, account: PanelAccount) -> renewal_policy.Policy:
    """The plan's renewal rule; an ended account always starts a fresh period."""
    mode = plan.renew_mode if plan.renew_mode in renewal_policy.RENEW_MODES else renewal_policy.DEFAULT_MODE
    return renewal_policy.policy_for(OrderKind.RENEW, mode, lapsed=account.status == "expired")


async def renewal_targets(
    driver: Any, account: PanelAccount, plan: AccountPlan, params: dict[str, Any]
) -> dict[str, Any]:
    """What a renewal leaves the account with: its end date, its quota and whether usage resets.

    Time is added to what is left or starts from now; traffic is a fresh quota, or what is left
    (or the whole quota, when usage is not reset) plus the renewal's GB.
    """
    renew = params.get("renew") or {}
    days = int(renew.get("days", plan.days))
    gb = int(renew.get("gb", plan.data_gb))
    rule = renew_rule(plan, account)
    now = utcnow()
    if days <= 0:
        expire = None  # a plan without an end date
    else:
        start = (
            account.expire_at
            if rule.time == "append" and account.expire_at and account.expire_at > now
            else now
        )
        expire = (start + timedelta(days=days)).isoformat()
    fresh = gb * GB if gb > 0 else 0
    if rule.volume == "reset" or not fresh or not account.data_limit:
        limit = fresh  # a fresh quota; an unlimited one on either side stays unlimited
    else:
        info = await driver.get_admin(account.username)
        used = int(info.get("used_traffic") or 0) if info else int(account.used_bytes or 0)
        left = max(0, int(account.data_limit) - used)
        limit = (left if rule.reset_usage else int(account.data_limit)) + fresh
    return {
        "target_expire": expire,
        "target_limit": limit,
        "reset_usage": rule.reset_usage,
        "rule": rule.as_dict(),
    }


async def _renew(driver: Any, account: PanelAccount, plan: AccountPlan, params: dict[str, Any]) -> None:
    target = params["target_expire"]
    account.expire_at = datetime.fromisoformat(target) if target else None
    if "target_limit" in params:
        account.data_limit = int(params["target_limit"])
    else:  # an order paid before renewal rules: a fresh quota of the plan or package
        gb = int((params.get("renew") or {}).get("gb", plan.data_gb))
        account.data_limit = gb * GB if gb > 0 else 0
    account.max_users = max(int(account.max_users or 0), int(plan.max_users or 0)) or None
    if params.get("reset_usage", True):
        await driver.reset_admin_usage(account.username)
        account.used_bytes = 0
    fields: dict[str, Any] = {"data_limit": account.data_limit, "status": "active"}
    if account.max_users:
        fields["max_users"] = account.max_users
    await driver.modify_admin(account.username, **fields)
    if account.status != "active":
        await driver.set_admin_users(account.username, True)
    account.status = "active"


# --- the owner's own actions --------------------------------------------------------------


async def change_password(session: AsyncSession, account: PanelAccount) -> str:
    driver = await _driver(session, account.panel_id)
    password = new_password()
    await driver.modify_admin(account.username, password=password)
    account.password_enc = secretbox.encrypt("panel", password)
    from paurubot.core import own_panel

    await own_panel.follow_credentials(session, account)  # its dedicated bot keeps logging in
    await session.flush()
    return password


# --- staff actions (the web panel) -----------------------------------------------------------


async def gift(session: AsyncSession, account: PanelAccount, *, days: int = 0, gb: int = 0) -> None:
    """Free days and traffic from staff: days on top of the end date (from today when it has
    passed), GB on top of the quota. An ended account the days reach past today comes back on.
    An account without an end date or quota keeps none."""
    if account.status not in LIVE:
        raise AccountError("account_not_found")
    driver = await _driver(session, account.panel_id)
    now = utcnow()
    if days and account.expire_at is not None:
        start = account.expire_at if account.expire_at > now else now
        account.expire_at = start + timedelta(days=days)
    fields: dict[str, Any] = {}
    if gb and account.data_limit:
        account.data_limit = int(account.data_limit) + gb * GB
        fields["data_limit"] = account.data_limit
    if account.status == "expired" and (account.expire_at is None or account.expire_at > now):
        fields["status"] = "active"
        await driver.modify_admin(account.username, **fields)
        await driver.set_admin_users(account.username, True)
        account.status = "active"
    elif fields:
        if account.status == "active":
            fields["status"] = "active"  # a quota that ran out is lifted by the new traffic
        await driver.modify_admin(account.username, **fields)
    account.alerts_sent = {}
    await session.flush()


async def reset_usage(session: AsyncSession, account: PanelAccount) -> None:
    if account.status not in {"active", "paused"}:
        raise AccountError("account_not_active")
    driver = await _driver(session, account.panel_id)
    await driver.reset_admin_usage(account.username)
    account.used_bytes = 0
    account.alerts_sent = {}
    await session.flush()


async def set_max_users(session: AsyncSession, account: PanelAccount, max_users: int | None) -> None:
    """The user cap; None drops the override, so the role's own limit applies."""
    if account.status not in LIVE:
        raise AccountError("account_not_found")
    if max_users is not None and not 1 <= max_users <= MAX_USERS:
        raise AccountError("account_users_invalid")
    driver = await _driver(session, account.panel_id)
    await driver.modify_admin(account.username, max_users=max_users)
    account.max_users = max_users
    await session.flush()


async def set_plan(session: AsyncSession, account: PanelAccount, plan: AccountPlan) -> None:
    """Move the account to another plan on the same panel: later renewals and add-ons follow it."""
    if plan.shop_id != account.shop_id or plan.panel_id != account.panel_id:
        raise AccountError("account_plan_other_panel")
    account.plan_id = plan.id
    account.plan = plan
    await session.flush()


async def set_paused(session: AsyncSession, account: PanelAccount, paused: bool) -> None:
    """Pause: the admin can't sign in and its users stop; resume brings both back."""
    if account.status not in {"active", "paused"}:
        raise AccountError("account_not_active")
    driver = await _driver(session, account.panel_id)
    await driver.modify_admin(account.username, status="disabled" if paused else "active")
    await driver.set_admin_users(account.username, not paused)
    account.status = "paused" if paused else "active"
    await session.flush()


async def remove(session: AsyncSession, account: PanelAccount) -> None:
    """Staff only: delete the admin from the panel. Its users stay in the panel, ownerless."""
    driver = await _driver(session, account.panel_id)
    await driver.delete_admin(account.username)
    account.status = "deleted"
    await session.flush()


async def refresh(session: AsyncSession, account: PanelAccount) -> None:
    """Read usage, user count and status back from the panel."""
    driver = await _driver(session, account.panel_id)
    info = await driver.get_admin(account.username)
    if info is None:
        # Never conclude it was deleted from one empty lookup (older panels ignore the filter).
        account.panel_status = "missing"
    else:
        account.used_bytes = int(info.get("used_traffic") or 0)
        account.users_count = int(info.get("total_users") or 0)
        account.panel_status = str(info.get("status") or "")[:16] or None
    account.synced_at = utcnow()
    await session.flush()


# --- periodic work -------------------------------------------------------------------------


async def sync_all(session: AsyncSession) -> tuple[int, list[tuple[PanelAccount, str]]]:
    """Refresh every live account; returns how many synced and the alerts to send."""
    rows = list(
        await session.scalars(select(PanelAccount).where(PanelAccount.status.in_(("active", "paused"))))
    )
    alerts: list[tuple[PanelAccount, str]] = []
    synced = 0
    for account in rows:
        try:
            await refresh(session, account)
        except (PanelError, ProvisionError):
            continue
        synced += 1
        sent = dict(account.alerts_sent or {})
        if account.data_limit and account.used_bytes * 10 >= account.data_limit * 9 and not sent.get("quota"):
            sent["quota"] = True
            alerts.append((account, "account_alert_quota"))
        account.alerts_sent = sent
    await session.flush()
    return synced, alerts


async def expire_due(session: AsyncSession) -> list[tuple[PanelAccount, str]]:
    """Warn three days ahead, and switch off accounts whose period ended."""
    now = utcnow()
    rows = list(
        await session.scalars(
            select(PanelAccount).where(
                PanelAccount.status.in_(("active", "paused")),
                PanelAccount.expire_at.is_not(None),
                PanelAccount.expire_at <= now + timedelta(days=3),
            )
        )
    )
    alerts: list[tuple[PanelAccount, str]] = []
    for account in rows:
        sent = dict(account.alerts_sent or {})
        assert account.expire_at is not None
        if account.expire_at <= now:
            try:
                driver = await _driver(session, account.panel_id)
                await driver.modify_admin(account.username, status="disabled")
                await driver.set_admin_users(account.username, False)
            except (PanelError, ProvisionError):
                continue  # tried again on the next pass
            account.status = "expired"
            alerts.append((account, "account_alert_expired"))
        elif not sent.get("expiring"):
            sent["expiring"] = True
            alerts.append((account, "account_alert_expiring"))
        account.alerts_sent = sent
    await session.flush()
    return alerts


async def run(session: AsyncSession) -> int:
    """The periodic pass: usage, quota and expiry alerts, and switching off ended accounts."""
    from paurubot.core import own_panel
    from paurubot.worker import queue

    synced, alerts = await sync_all(session)
    alerts += await expire_due(session)
    await own_panel.announce(session, await own_panel.recheck_all(session))
    for account, key in alerts:
        await queue.enqueue(
            session,
            "notify",
            "account_alert",
            {"account_id": account.id, "alert": key},
            shop_id=account.shop_id,
            dedupe_key=f"account:{account.id}:{key}:{(account.expire_at or utcnow()).date()}",
        )
    await session.commit()
    return synced
