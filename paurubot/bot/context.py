"""Per-update context: database session, shop, customer, staff, texts and abuse guards (SEC-14)."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, TelegramObject, Update
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.auth import rbac
from paurubot.config import get_settings
from paurubot.core import channels as channel_rules
from paurubot.core import customers
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc, money
from paurubot.core.shops import root_shop
from paurubot.db.models import Customer, ForcedChannel, Shop, ShopStatus, Staff, StaffRole
from paurubot.db.session import get_sessionmaker
from paurubot.i18n import Texts, texts_for

log = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]
FLOOD_LIMIT = 25
FLOOD_WINDOW = 10
_join_ok: OrderedDict[tuple[int, int], float] = OrderedDict()
JOIN_CACHE_LIMIT = 4096


@dataclass(slots=True)
class StaffIdentity:
    """A staff member using the bot; owners from OWNER_TELEGRAM_IDS work before any row exists."""

    role: StaffRole
    staff: Staff | None
    permissions: frozenset[str]

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    @property
    def id(self) -> int | None:
        return self.staff.id if self.staff else None


async def resolve_staff(session: AsyncSession, shop_id: int, telegram_id: int) -> StaffIdentity | None:
    staff = await session.scalar(
        select(Staff).where(
            Staff.shop_id == shop_id, Staff.telegram_id == telegram_id, Staff.is_active.is_(True)
        )
    )
    if staff is not None and staff.role != StaffRole.RESELLER:
        return StaffIdentity(staff.role, staff, frozenset(rbac.permissions_for(staff.role)))
    if telegram_id in get_settings().owner_telegram_ids and await _is_root(session, shop_id):
        # The platform's owners run the main shop; in a reseller's or customer's own bot they are
        # customers like anyone else, never its admins.
        return StaffIdentity(StaffRole.OWNER, staff, frozenset(rbac.permissions_for(StaffRole.OWNER)))
    return None


async def _is_root(session: AsyncSession, shop_id: int) -> bool:
    shop = await session.get(Shop, shop_id)
    return shop is not None and shop.parent_id is None


def _event_message(event: TelegramObject) -> Message | CallbackQuery | None:
    if isinstance(event, Update):
        return event.message or event.callback_query
    if isinstance(event, (Message, CallbackQuery)):
        return event
    return None


def flood_exempt(update: Any) -> bool:
    """Payment updates skip the flood guard: an unanswered pre-checkout makes Telegram cancel the
    payment, and a dropped successful_payment would leave a paid order unfulfilled."""
    if update is None:
        return False
    if getattr(update, "pre_checkout_query", None) is not None:
        return True
    message = getattr(update, "message", None)
    return message is not None and getattr(message, "successful_payment", None) is not None


async def _notify(event: TelegramObject, text: str) -> None:
    target = _event_message(event)
    try:
        if isinstance(target, Message):
            await target.answer(text)
        elif isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
    except TelegramAPIError:
        pass


UNKNOWN_BOT = -1  # a bot that is neither the main bot nor a connected reseller bot


async def _shop_of_bot(session: AsyncSession, bot: Any) -> int | None:
    """The reseller shop a bot belongs to (TEN-04); None for the main bot."""
    bot_id = getattr(bot, "id", None)
    if not bot_id:
        return None
    from paurubot.notifications.telegram import bot_id_of

    if bot_id == bot_id_of(get_settings().bot_token.get_secret_value()):
        return None
    shop_id = await session.scalar(select(Shop.id).where(Shop.bot_user_id == bot_id))
    return shop_id if shop_id is not None else UNKNOWN_BOT


class ContextMiddleware(BaseMiddleware):
    """Outer update middleware: opens the session and loads everything handlers need.

    A dispatcher serves one shop: the root shop by default, or a reseller shop with its own bot.
    """

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        async with get_sessionmaker()() as session:
            data["session"] = session
            shop_id = data.get("shop_id") or await _shop_of_bot(session, data.get("bot"))
            if shop_id == UNKNOWN_BOT:
                return None  # e.g. a reseller bot that was just stopped: never serve the main shop
            shop = await session.get(Shop, shop_id) if shop_id else await root_shop(session)
            financial_update = flood_exempt(event if isinstance(event, Update) else data.get("event_update"))
            if (
                shop is not None
                and (shop.reseller_profile_id is not None or shop.own_account_id is not None)
                and shop.status != ShopStatus.ACTIVE
                and not financial_update
            ):
                return None  # a reseller bot that was switched off
            if shop is None:
                await _notify(event, "This bot is not set up yet.")
                return None
            data["shop"] = shop
            cfg = await shop_settings.load(session, shop.id)
            data["cfg"] = cfg
            update = event if isinstance(event, Update) else data.get("event_update")
            business = update is not None and any(
                getattr(update, name, None) is not None
                for name in (
                    "business_connection",
                    "business_message",
                    "edited_business_message",
                    "deleted_business_messages",
                )
            )
            if tg_user is None or tg_user.is_bot or business:
                # Business updates come from people writing to the owner's own account: they are not
                # bot customers, so nothing is created for them (BOT-07).
                data["t"] = await texts_for(session, shop.id, shop.default_locale)
                result = await handler(event, data)
                await session.commit()
                return result
            if not flood_exempt(update):
                allowed, count = await kv.hit(f"flood:{tg_user.id}", FLOOD_LIMIT, FLOOD_WINDOW)
                if not allowed:
                    if count == FLOOD_LIMIT + 1:
                        log.info("flood guard engaged for %s", tg_user.id)
                    return None
            customer, created = await customers.upsert_from_telegram(
                session,
                shop.id,
                tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                locale=_initial_locale(tg_user, shop, cfg),
            )
            if created:
                from paurubot.core import referral

                start_text = (
                    (update.message.text or "").strip()
                    if update is not None and update.message is not None
                    else ""
                )
                ref_id: int | None = None
                if start_text.startswith("/start"):
                    parts = start_text.split(maxsplit=1)
                    if len(parts) > 1 and parts[1].startswith("ref_") and parts[1][4:].isdigit():
                        ref_id = int(parts[1][4:])
                try:
                    await referral.remember_pending_start(shop.id, customer.id, ref_id)
                except Exception:
                    log.debug("could not cache pending start for customer %s", customer.id)
            staff = await resolve_staff(session, shop.id, tg_user.id)
            from paurubot.core import trials
            from paurubot.ledger.accounts import wallet_balance

            bal = await wallet_balance(session, shop.id, customer.id)
            trial_used = (
                await trials.has_used_current(session, customer)
                if bool(cfg.get("trial_hide_after_use"))
                else False
            )
            texts = await texts_for(
                session,
                shop.id,
                allowed_locale(customer.locale, shop, cfg),
                # Messages are sent as HTML: a name like "<3" must not break (or style) them.
                {
                    "name": esc(tg_user.first_name or ""),
                    "first_name": esc(tg_user.first_name or ""),
                    "username": esc(tg_user.username or ""),
                    "shop": esc(shop.title),
                    "support": esc(cfg.get("support_username") or ""),
                    "balance": money(bal),
                    "ref_code": f"ref_{customer.telegram_id}",
                },
            )
            data.update(
                customer=customer,
                is_new_customer=created,
                staff=staff,
                t=texts,
                trial_used=trial_used,
            )
            if staff is None and not financial_update:
                if customer.is_banned:
                    await _notify(event, texts("banned"))
                    await session.commit()
                    return None
                if not cfg["bot_enabled"]:
                    await _notify(event, texts("maintenance"))
                    await session.commit()
                    return None
            started = time.monotonic()
            try:
                result = await handler(event, data)
                await session.commit()
                elapsed = time.monotonic() - started
                if elapsed > 1.5:
                    log.warning("slow telegram update %.2fs type=%s", elapsed, type(event).__name__)
                return result
            except Exception:
                await session.rollback()
                raise


def _initial_locale(tg_user: TgUser, shop: Shop, cfg: dict[str, Any]) -> str:
    return allowed_locale((tg_user.language_code or "").split("-")[0].lower(), shop, cfg)


def allowed_locale(code: str | None, shop: Shop, cfg: dict[str, Any]) -> str:
    """`code` when the shop offers it, else the shop's own language: a language the shop has
    since turned off (or a dedicated bot never had, following the main shop) is not used."""
    languages = cfg.get("languages") or [shop.default_locale]
    if code and code in languages:
        return code
    return shop.default_locale if shop.default_locale in languages else str(languages[0])


class GateMiddleware(BaseMiddleware):
    """Inner middleware: forced channels (GRW-07), phone verification and terms (SEC-13)."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        customer: Customer | None = data.get("customer")
        if customer is None or data.get("staff") is not None or isinstance(event, PreCheckoutQuery):
            return await handler(event, data)
        if isinstance(event, Message) and event.chat.type != "private":
            return None
        if isinstance(event, Message) and (event.successful_payment or event.contact):
            return await handler(event, data)
        if (
            isinstance(event, CallbackQuery)
            and event.data
            and event.data.startswith(("n:joined", "n:terms", "n:rterms"))
        ):
            return await handler(event, data)
        texts: Texts = data["t"]
        cfg: dict[str, Any] = data["cfg"]
        session: AsyncSession = data["session"]
        from paurubot.bot import keyboards
        from paurubot.core import referral

        missing = await missing_channels(data["bot"], session, customer)
        if missing:
            if isinstance(event, CallbackQuery):
                await event.answer(texts("join_required_alert"), show_alert=True)
            elif isinstance(event, Message):
                await event.answer(texts("join_required"), reply_markup=keyboards.join(texts, missing))
            return None
        if cfg.get("referral_required") and await referral.needs_inviter(session, customer):
            if isinstance(event, Message) and await referral.join_by_code(
                session, customer, event.text or "", cfg
            ):
                if (event.text or "").startswith("/start"):
                    return await handler(event, data)
                await event.answer(texts("referral_accepted"))
                return None
            if isinstance(event, Message):
                bad = bool(referral.code_of(event.text or "")) and not (event.text or "").startswith("/start")
                await event.answer(texts("referral_required_invalid" if bad else "referral_required"))
            elif isinstance(event, CallbackQuery):
                await event.answer(texts("referral_required"), show_alert=True)
            return None
        if cfg["require_phone"] and cfg.get("phone_when") != "purchase" and not customer.phone:
            if isinstance(event, Message):
                await event.answer(texts("phone_required"), reply_markup=keyboards.share_phone(texts))
            elif isinstance(event, CallbackQuery):
                await event.answer(texts("phone_required_alert"), show_alert=True)
            return None
        if (
            cfg["terms_required"]
            and cfg.get("terms_when") != "purchase"  # «before purchase»: asked when an order is made
            and customer.terms_accepted_version != int(cfg["terms_version"])
        ):
            if isinstance(event, Message):
                await event.answer(texts("terms_text"), reply_markup=keyboards.terms(texts))
            elif isinstance(event, CallbackQuery):
                await event.answer(texts("terms_required_alert"), show_alert=True)
            return None
        is_start = isinstance(event, Message) and (event.text or "").strip().startswith("/start")
        if not data.get("is_new_customer") and not is_start:
            pending, saved_ref = await referral.take_pending_start(customer.shop_id, customer.id)
            if pending:
                welcome_gift = await referral.complete_onboarding(
                    session, customer.shop_id, customer, cfg, ref_telegram_id=saved_ref
                )
                if welcome_gift and isinstance(event, Message):
                    from paurubot.core.formatting import money

                    await event.answer(texts("start_gift", amount=money(welcome_gift)))
        return await handler(event, data)


async def missing_channels(bot: Bot, session: AsyncSession, customer: Customer) -> list[ForcedChannel]:
    channels = list(
        await session.scalars(
            select(ForcedChannel)
            .where(ForcedChannel.shop_id == customer.shop_id, ForcedChannel.is_active.is_(True))
            .order_by(ForcedChannel.sort)
        )
    )
    now = time.monotonic()
    for expired in [key for key, expiry in _join_ok.items() if expiry <= now]:
        _join_ok.pop(expired, None)
    cache_key = (customer.shop_id, customer.telegram_id)
    if not channels or _join_ok.get(cache_key, 0) > now:
        return []
    missing, _unknown = await channel_rules.missing_members(bot, channels, customer.telegram_id)
    if not missing:
        _join_ok[cache_key] = time.monotonic() + 300
        _join_ok.move_to_end(cache_key)
        while len(_join_ok) > JOIN_CACHE_LIMIT:
            _join_ok.popitem(last=False)
    return missing


def forget_join_check(telegram_id: int) -> None:
    for key in [key for key in _join_ok if key[1] == telegram_id]:
        _join_ok.pop(key, None)
