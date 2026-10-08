"""A reseller's own bot (TEN-04).

A reseller with the `resellers.bot` right can connect its own Telegram bot. That bot serves a child
shop: its own customers, wallets, payments, texts and staff (the reseller, as a merchant), selling
the parent shop's plans. Each price is the reseller's own price for the plan (after its discount)
plus the reseller's markup, or a fixed price the reseller sets, never below its own price.

When a sale is provisioned, the reseller pays its own price from its wallet in the parent shop
first. Without enough balance the sale fails and the buyer is refunded, so a reseller bot can
never hand out services the reseller has not paid for. Only purchases and renewals are sold there.
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
    return Resale(shop, shop.parent_id, profile, reseller, markup, prices)


async def wholesale(session: AsyncSession, resale: Resale, plan_id: int | None, base_price: int) -> int:
    """What the reseller pays for something listed at `base_price` in the parent shop, its fixed
    reseller price included (TEN-12)."""
    return (await pricing.plan_quote(session, resale.reseller, plan_id, base_price)).amount


def retail(resale: Resale, plan_id: int | None, cost: int) -> int:
    fixed = resale.prices.get(str(plan_id)) if plan_id is not None else None
    if fixed:
        return max(int(fixed), cost)
    return max(cost, pricing.round_price(cost * (100 + resale.markup) / 100))


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


async def allow(session: AsyncSession, profile: ResellerProfile) -> Shop | None:
    """Lift a stop made by staff so the reseller may connect its bot again."""
    shop = await shop_of(session, profile)
    if shop is not None:
        await shop_settings.set_many(session, shop.id, {"resale_blocked": False})
    return shop
