"""A reseller's own bot (TEN-04).

A reseller with the `resellers.bot` right can connect its own Telegram bot. That bot serves a child
shop: its own customers, wallets, payments, texts and staff (the reseller, as a merchant), selling
the parent shop's plans. Each price is the reseller's own price for the plan (after its discount)
plus the reseller's markup, or a fixed price the reseller sets, never below its own price.

When a sale is provisioned, the reseller pays its own price from its wallet in the parent shop
first. Without enough balance the sale fails and the buyer is refunded, so a reseller bot can
never hand out services the reseller has not paid for. Purchases and renewals are always sold
there; trials, extra data and days, upgrades and referrals only when the shop's staff allow that
reseller to offer them and the reseller turns them on (TEN-18). Each of them is paid the same way:
the reseller's own price comes out of its wallet before anything is delivered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import pricing
from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer, Order, Plan, ResellerProfile, Shop, ShopStatus
from paurubot.ledger.accounts import wallet_account
from paurubot.ledger.posting import InsufficientFunds, transfer

MAX_MARKUP = 500


class ResaleError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(slots=True)
class Resale:
    shop: Shop
    parent_id: int
    profile: ResellerProfile
    reseller: Customer
    markup: int
    prices: dict[str, int] = field(default_factory=dict)
    cfg: dict[str, Any] = field(default_factory=dict)

    def offers(self, feature: str) -> bool:
        """Staff allow this reseller to offer `feature` and the reseller turned it on (TEN-18)."""
        return feature in allowed_features(self.profile) and bool(self.cfg.get(FEATURES[feature]))

    @property
    def open(self) -> bool:
        return (
            self.shop.status == ShopStatus.ACTIVE
            and self.profile.is_current
            and self.profile.suspended_at is None
            and not self.profile.is_metered
        )


def catalog_owner(shop_id: int | Any) -> ColumnElement[Any]:
    """SQL for the shop whose catalog `shop_id` sells: its parent for a reseller bot, else itself."""
    parent = (
        select(Shop.parent_id)
        .where(Shop.id == shop_id, Shop.reseller_profile_id.is_not(None))
        .scalar_subquery()
    )
    return func.coalesce(parent, shop_id)


async def for_shop(session: AsyncSession, shop_id: int) -> Resale | None:
    shop = await session.get(Shop, shop_id)
    if shop is None or shop.reseller_profile_id is None or shop.parent_id is None:
        return None
    profile = await session.get(ResellerProfile, shop.reseller_profile_id)
    if profile is None:
        return None
    reseller = await session.get(Customer, profile.customer_id)
    if reseller is None:
        return None
    await session.refresh(reseller, ["reseller_profile"])
    cfg = await shop_settings.load(session, shop.id)
    prices = {str(k): int(v) for k, v in (cfg.get("resale_prices") or {}).items() if str(v).isdigit()}
    markup = max(0, min(MAX_MARKUP, int(cfg.get("resale_markup_percent") or 0)))
    return Resale(shop, shop.parent_id, profile, reseller, markup, prices, cfg)


async def wholesale(session: AsyncSession, resale: Resale, plan_id: int | None, base_price: int) -> int:
    """What the reseller pays for something listed at `base_price` in the parent shop, its fixed
    reseller price included (TEN-12)."""
    return (await pricing.plan_quote(session, resale.reseller, plan_id, base_price)).amount


def retail(resale: Resale, plan_id: int | None, cost: int) -> int:
    fixed = resale.prices.get(str(plan_id)) if plan_id is not None else None
    if fixed:
        return max(int(fixed), cost)
    return max(cost, pricing.round_price(cost * (100 + resale.markup) / 100))


# --- extra features (TEN-18) ------------------------------------------------------------------

# What staff can let one reseller offer in its bot, and the bot setting that turns each one on.
FEATURES: dict[str, str] = {
    "trial": "trial_enabled",
    "extras": "extras_enabled",
    "upgrade": "upgrade_enabled",
    "referral": "referral_enabled",
}
FEATURE_RIGHT = "resale."


def allowed_features(profile: ResellerProfile) -> set[str]:
    rights = set(profile.permissions or [])
    return {name for name in FEATURES if FEATURE_RIGHT + name in rights}


def set_feature_right(profile: ResellerProfile, feature: str, on: bool) -> None:
    if feature not in FEATURES:
        raise ResaleError("resale_feature_unknown")
    right = FEATURE_RIGHT + feature
    rights = set(profile.permissions or [])
    profile.permissions = sorted(rights | {right} if on else rights - {right})


async def allow_feature(session: AsyncSession, profile: ResellerProfile, feature: str, on: bool) -> None:
    """Staff allow or take back one feature. Taking it back also turns it off in the bot at once."""
    set_feature_right(profile, feature, on)
    if not on:
        shop = await shop_of(session, profile)
        if shop is not None:
            await shop_settings.set_many(session, shop.id, {FEATURES[feature]: False})
    await session.flush()


async def extra_units(session: AsyncSession, resale: Resale, service: Any) -> tuple[int | None, int | None]:
    """(reseller's cost, listed price) of one extra GB and one extra day, from the parent shop's
    own extra prices. Prices are None where extras are not sold for this service."""
    from paurubot.core.orders import service_extras

    if not resale.offers("extras"):
        return None, None
    parent_cfg = await shop_settings.load(session, resale.parent_id)
    out: list[int | None] = []
    for unit in service_extras(parent_cfg, service):
        out.append(None if unit is None else await wholesale(session, resale, None, int(unit)))
    return out[0], out[1]


async def listed_extras(
    session: AsyncSession, service: Any, shop_id: int, cfg: dict[str, Any]
) -> tuple[int | None, int | None]:
    """Unit prices of extra data and days as `shop_id` lists them to its customers."""
    from paurubot.core.orders import service_extras

    found = await for_shop(session, shop_id)
    if found is None:
        return service_extras(cfg, service)
    costs = await extra_units(session, found, service)
    return tuple(None if c is None else retail(found, None, c) for c in costs)  # type: ignore[return-value]


async def upgrade_prices(
    session: AsyncSession, resale: Resale, service: Any, plan: Plan
) -> tuple[int, int, int]:
    """(reseller's cost, listed price, reseller's credit) of moving `service` to `plan`.

    Both sides credit what was really paid for the current period, times the unused share
    (SEC-F01). That is the customer's payment on the listed side and the reseller's wholesale
    on the cost side, so the reseller's margin stays its own.
    """
    from paurubot.core.orders import period_basis

    basis = await period_basis(session, service)
    new_cost = await wholesale(session, resale, plan.id, int(plan.price))
    new_list = retail(resale, plan.id, new_cost)
    cost = max(0, new_cost - basis.wholesale_credit)
    listed = max(cost, pricing.round_price(new_list - basis.credit))
    return cost, listed, new_cost - cost


async def list_price(session: AsyncSession, plan: Plan, shop_id: int) -> int:
    """The plan's price as listed in `shop_id`, before any customer's own discount."""
    resale = await for_shop(session, shop_id)
    if resale is None:
        return int(plan.price)
    return retail(resale, plan.id, await wholesale(session, resale, plan.id, int(plan.price)))


def validate_prices(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or len(value) > 500:
        raise ResaleError("resale_prices_invalid")
    out: dict[str, int] = {}
    for key, price in value.items():
        if not str(key).isdigit() or not isinstance(price, int) or not 0 < price <= 10**10:
            raise ResaleError("resale_prices_invalid")
        out[str(key)] = price
    return out


# --- money ------------------------------------------------------------------------------


async def pay_wholesale(session: AsyncSession, order: Order) -> bool:
    """Charge the reseller its price for a sale made in its bot. Idempotent; False when the order
    was not sold through a reseller bot. Raises ResaleError when the reseller cannot pay."""
    params = dict(order.params or {})
    cost = int(params.get("wholesale") or 0)
    if not params.get("resale_profile") or cost <= 0:
        return False
    if params.get("wholesale_paid"):
        return True
    profile = await session.get(ResellerProfile, int(params["resale_profile"]))
    if profile is None or not profile.is_current or profile.suspended_at is not None:
        raise ResaleError("reseller_unavailable")
    source = await wallet_account(session, profile.shop_id, profile.customer_id)
    from paurubot.ledger.accounts import system_account

    revenue = await system_account(session, profile.shop_id, "revenue")
    try:
        async with session.begin_nested():
            await transfer(
                session,
                shop_id=profile.shop_id,
                source_id=source.id,
                target_id=revenue.id,
                amount=cost,
                kind="charge",
                idempotency_key=f"resale:{order.id}:wholesale",
                reference_type="order",
                reference_id=order.id,
                memo=f"sold through reseller bot (order #{order.id})",
            )
    except InsufficientFunds as exc:
        raise ResaleError("reseller_balance_low") from exc
    params["wholesale_paid"] = True
    order.params = params
    await session.flush()
    return True


async def refund_wholesale(session: AsyncSession, order: Order) -> None:
    """Give the reseller its money back when a paid sale could not be delivered."""
    params = dict(order.params or {})
    if not params.get("wholesale_paid") or params.get("wholesale_refunded"):
        return
    profile = await session.get(ResellerProfile, int(params["resale_profile"]))
    if profile is None:
        return
    from paurubot.ledger.accounts import system_account

    target = await wallet_account(session, profile.shop_id, profile.customer_id)
    revenue = await system_account(session, profile.shop_id, "revenue")
    await transfer(
        session,
        shop_id=profile.shop_id,
        source_id=revenue.id,
        target_id=target.id,
        amount=int(params["wholesale"]),
        kind="refund",
        idempotency_key=f"resale:{order.id}:wholesale-refund",
        reference_type="order",
        reference_id=order.id,
        memo=f"reseller bot sale not delivered (order #{order.id})",
    )
    params["wholesale_refunded"] = True
    order.params = params
    await session.flush()


# --- connecting a bot -------------------------------------------------------------------

TOKEN_SHAPE = r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$"
# What a reseller can change about its bot's shop; everything else follows the platform.
EDITABLE = {
    "card_accounts",
    "card_enabled",
    "support_username",
    "resale_markup_percent",
    "resale_prices",
    "bot_enabled",
    "receipt_duplicate_block",
    *FEATURES.values(),
}
# A new reseller bot starts with only what works there: plain purchases and renewals.
STARTING_SETTINGS: dict[str, Any] = {
    "trial_enabled": False,
    "referral_enabled": False,
    "extras_enabled": False,
    "upgrade_enabled": False,
    "move_enabled": False,
    "custom_buy_enabled": False,
    "bulk_buy_enabled": False,
    "wheel_enabled": False,
    "lottery_enabled": False,
    "loyalty_enabled": False,
    "guest_buy_enabled": False,
    "stars_enabled": False,
    "miniapp_enabled": False,
    "card_enabled": True,
}


async def bot_identity(token: str) -> tuple[int, str]:
    """(id, username) of a bot token, asked from Telegram itself."""
    from aiogram.exceptions import TelegramAPIError

    from paurubot.notifications.telegram import make_bot

    bot = make_bot(token)
    try:
        me = await bot.me()
    except TelegramAPIError as exc:
        raise ResaleError("bot_token_invalid") from exc
    finally:
        await bot.session.close()
    if not me.username:
        raise ResaleError("bot_token_invalid")
    return int(me.id), str(me.username)


async def shop_of(session: AsyncSession, profile: ResellerProfile) -> Shop | None:
    return await session.scalar(select(Shop).where(Shop.reseller_profile_id == profile.id))


async def connect(session: AsyncSession, profile: ResellerProfile, token: str, title: str) -> Shop:
    """Create or re-point the reseller's bot shop and make the reseller its merchant."""
    import re

    from paurubot.auth.staff import create_staff
    from paurubot.config import get_settings
    from paurubot.db.models import Staff, StaffRole
    from paurubot.notifications.telegram import bot_id_of, forget_shop_bot
    from paurubot.security import secretbox

    token = token.strip()
    if not re.match(TOKEN_SHAPE, token):
        raise ResaleError("bot_token_invalid")
    if bot_id_of(token) == bot_id_of(get_settings().bot_token.get_secret_value()):
        raise ResaleError("bot_token_is_main")
    bot_id, username = await bot_identity(token)
    taken = await session.scalar(select(Shop).where(Shop.bot_user_id == bot_id))
    shop = await shop_of(session, profile)
    if taken is not None and (shop is None or taken.id != shop.id):
        raise ResaleError("bot_token_in_use")
    if shop is not None and (await shop_settings.load(session, shop.id, fresh=True)).get("resale_blocked"):
        raise ResaleError("reseller_bot_blocked")
    parent = await session.get(Shop, profile.shop_id)
    assert parent is not None
    if shop is None:
        shop = Shop(
            parent_id=parent.id,
            title=title.strip()[:96] or username,
            currency=parent.currency,
            default_locale=parent.default_locale,
            reseller_profile_id=profile.id,
        )
        session.add(shop)
        await session.flush()
        await shop_settings.set_many(session, shop.id, STARTING_SETTINGS)
    elif title.strip():
        shop.title = title.strip()[:96]
    shop.bot_token_enc = secretbox.encrypt("bot-token", token)
    shop.bot_user_id, shop.bot_username, shop.status = bot_id, username, ShopStatus.ACTIVE
    reseller = await session.get(Customer, profile.customer_id)
    assert reseller is not None
    merchant = await session.scalar(
        select(Staff).where(Staff.shop_id == shop.id, Staff.telegram_id == reseller.telegram_id)
    )
    if merchant is None:
        await create_staff(
            session,
            shop.id,
            role=StaffRole.MERCHANT,
            username=None,
            password=None,
            telegram_id=reseller.telegram_id,
            display_name=reseller.display_name,
        )
    else:
        merchant.is_active = True
    await session.flush()
    forget_shop_bot(shop.id)
    return shop


async def disconnect(
    session: AsyncSession, profile: ResellerProfile, *, by_staff: bool = False
) -> Shop | None:
    """Stop the bot. Its customers, wallets and history stay; connecting again brings them back,
    unless the shop's staff stopped it: then only they can let the reseller connect again."""
    from paurubot.notifications.telegram import forget_shop_bot

    shop = await shop_of(session, profile)
    if shop is None:
        return None
    if by_staff:
        await shop_settings.set_many(session, shop.id, {"resale_blocked": True})
    shop.status = ShopStatus.SUSPENDED
    shop.bot_token_enc = None
    shop.bot_user_id = None
    forget_shop_bot(shop.id)
    await session.flush()
    return shop


async def update(session: AsyncSession, shop: Shop, values: dict[str, Any]) -> dict[str, Any]:
    unknown = set(values) - EDITABLE
    if unknown:
        raise ResaleError("resale_setting_unknown")
    clean = dict(values)
    switched_on = {name for name, key in FEATURES.items() if clean.get(key)}
    if switched_on:
        profile = await session.get(ResellerProfile, shop.reseller_profile_id or 0)
        if profile is None or not switched_on <= allowed_features(profile):
            raise ResaleError("resale_feature_not_allowed")
    if "resale_prices" in clean:
        clean["resale_prices"] = validate_prices(clean["resale_prices"])
    markup = clean.get("resale_markup_percent")
    if markup is not None and (not isinstance(markup, int) or not 0 <= markup <= MAX_MARKUP):
        raise ResaleError("resale_markup_invalid")
    return await shop_settings.set_many(session, shop.id, clean)


async def bots(session: AsyncSession) -> list[tuple[Shop, str]]:
    """Every reseller bot that should be running, with its token."""
    from paurubot.security import secretbox

    rows = await session.scalars(
        select(Shop).where(
            Shop.reseller_profile_id.is_not(None),
            Shop.bot_token_enc.is_not(None),
            Shop.status == ShopStatus.ACTIVE,
        )
    )
    return [(shop, secretbox.decrypt("bot-token", shop.bot_token_enc)) for shop in rows if shop.bot_token_enc]


async def point_webhook(shop_id: int) -> None:
    """In webhook mode, tell Telegram where this reseller bot's updates go."""
    from paurubot.api.routes.telegram import dispatcher, shop_webhook_secret
    from paurubot.bot.app import set_commands
    from paurubot.config import get_settings
    from paurubot.i18n import Texts
    from paurubot.notifications.telegram import bot_for
    from paurubot.security import secretbox

    settings = get_settings()
    from paurubot.db.session import session_scope

    async with session_scope() as session:
        shop = await session.get(Shop, shop_id)
        token = secretbox.decrypt("bot-token", shop.bot_token_enc) if shop and shop.bot_token_enc else None
    if token is None:
        return
    bot = await bot_for(shop_id)
    if bot is None:
        return
    if settings.bot_mode == "webhook":
        await bot.set_webhook(
            f"{settings.public_url}/api/telegram/webhook/{shop_id}",
            secret_token=shop_webhook_secret(token),
            drop_pending_updates=False,
            allowed_updates=dispatcher().resolve_used_update_types(),
        )
    await set_commands(bot, Texts("fa"), Texts("en"))


# --- rights and figures (TEN-17) ----------------------------------------------------------

BOT_RIGHT = "resellers.bot"
MAX_CARDS = 20


def may_have_bot(profile: ResellerProfile, cfg: dict[str, Any]) -> bool:
    """Granted one by one, or to every prepaid reseller with `reseller_bot_open`."""
    if BOT_RIGHT in (profile.permissions or []):
        return True
    return profile.parent_id is None and bool(cfg.get("reseller_bot_open"))


def set_bot_right(profile: ResellerProfile, on: bool) -> None:
    rights = set(profile.permissions or [])
    rights = rights | {BOT_RIGHT} if on else rights - {BOT_RIGHT}
    profile.permissions = sorted(rights)


def parse_card(text: str) -> dict[str, str]:
    """A card number (or IBAN) and the holder's name, as one message, into a card account.
    Spaces and dashes inside the number are fine: "6037-9912 3456 7893 Sara"."""
    import re

    from paurubot.core.cards import luhn_ok
    from paurubot.core.formatting import to_latin_digits

    found = re.match(r"^\s*((?:IR)?[\d\s-]+)(.*)$", to_latin_digits(text or ""), re.IGNORECASE | re.DOTALL)
    if found is None:
        raise ResaleError("card_number_invalid")
    number = re.sub(r"[\s-]", "", found.group(1)).upper()
    holder = " ".join(found.group(2).split())[:64]
    if number.startswith("IR") and number[2:].isdigit() and len(number) == 26:
        return {"number": number, "holder": holder}
    if not number.isdigit() or len(number) != 16 or not luhn_ok(number):
        raise ResaleError("card_number_invalid")
    return {"number": number, "holder": holder}


@dataclass(slots=True)
class Figures:
    customers: int
    active: int
    orders_month: int
    revenue_month: int


async def figures(session: AsyncSession, shop: Shop) -> Figures:
    from datetime import timedelta

    from paurubot.db.base import utcnow
    from paurubot.db.models import OrderStatus, Service, ServiceStatus

    month = utcnow() - timedelta(days=30)
    sold = await session.execute(
        select(func.count(Order.id), func.coalesce(func.sum(Order.amount), 0)).where(
            Order.shop_id == shop.id, Order.status == OrderStatus.COMPLETED, Order.completed_at >= month
        )
    )
    orders, revenue = sold.one()
    customers = await session.scalar(select(func.count(Customer.id)).where(Customer.shop_id == shop.id))
    active = await session.scalar(
        select(func.count(Service.id)).where(
            Service.shop_id == shop.id, Service.status == ServiceStatus.ACTIVE
        )
    )
    return Figures(int(customers or 0), int(active or 0), int(orders or 0), int(revenue or 0))


async def allow(session: AsyncSession, profile: ResellerProfile) -> Shop | None:
    """Lift a stop made by staff so the reseller may connect its bot again."""
    shop = await shop_of(session, profile)
    if shop is not None:
        await shop_settings.set_many(session, shop.id, {"resale_blocked": False})
    return shop
