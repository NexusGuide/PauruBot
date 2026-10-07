"""Per-update context: database session, shop, customer, staff, texts and abuse guards (SEC-14)."""

from __future__ import annotations

import logging
import time
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
from paurubot.core import customers
from paurubot.core import settings as shop_settings
from paurubot.core.shops import root_shop
from paurubot.db.models import Customer, ForcedChannel, Shop, Staff, StaffRole
from paurubot.db.session import get_sessionmaker
from paurubot.i18n import Texts, texts_for

log = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]
FLOOD_LIMIT = 25
FLOOD_WINDOW = 10
MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}
_join_ok: dict[int, float] = {}


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
    if telegram_id in get_settings().owner_telegram_ids:
        return StaffIdentity(StaffRole.OWNER, staff, frozenset(rbac.permissions_for(StaffRole.OWNER)))
    return None


def _event_message(event: TelegramObject) -> Message | CallbackQuery | None:
    if isinstance(event, Update):
        return event.message or event.callback_query
    if isinstance(event, (Message, CallbackQuery)):
        return event
    return None


async def _notify(event: TelegramObject, text: str) -> None:
    target = _event_message(event)
    try:
        if isinstance(target, Message):
            await target.answer(text)
        elif isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
    except TelegramAPIError:
        pass


class ContextMiddleware(BaseMiddleware):
    """Outer update middleware: opens the session and loads everything handlers need.

    A dispatcher serves one shop: the root shop by default, or a reseller shop with its own bot.
    """

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        async with get_sessionmaker()() as session:
            data["session"] = session
            shop_id = data.get("shop_id")
            shop = await session.get(Shop, shop_id) if shop_id else await root_shop(session)
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
            staff = await resolve_staff(session, shop.id, tg_user.id)
            texts = await texts_for(
                session,
                shop.id,
                customer.locale or shop.default_locale,
                {
                    "name": tg_user.first_name or "",
                    "first_name": tg_user.first_name or "",
                    "username": tg_user.username or "",
                    "shop": shop.title,
                    "support": cfg.get("support_username") or "",
                },
            )
            data.update(customer=customer, is_new_customer=created, staff=staff, t=texts)
            if staff is None:
                if customer.is_banned:
                    await _notify(event, texts("banned"))
                    await session.commit()
                    return None
                if not cfg["bot_enabled"]:
                    await _notify(event, texts("maintenance"))
                    await session.commit()
                    return None
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise


def _initial_locale(tg_user: TgUser, shop: Shop, cfg: dict[str, Any]) -> str:
    code = (tg_user.language_code or "").split("-")[0].lower()
    languages = cfg.get("languages") or [shop.default_locale]
    return code if code in languages else shop.default_locale


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
        if isinstance(event, CallbackQuery) and event.data and event.data.startswith(("n:joined", "n:terms")):
            return await handler(event, data)
        texts: Texts = data["t"]
        cfg: dict[str, Any] = data["cfg"]
        session: AsyncSession = data["session"]
        from paurubot.bot import keyboards

        missing = await missing_channels(data["bot"], session, customer)
        if missing:
            if isinstance(event, CallbackQuery):
                await event.answer(texts("join_required_alert"), show_alert=True)
            elif isinstance(event, Message):
                await event.answer(texts("join_required"), reply_markup=keyboards.join(texts, missing))
            return None
        if cfg["require_phone"] and not customer.phone:
            if isinstance(event, Message):
                await event.answer(texts("phone_required"), reply_markup=keyboards.share_phone(texts))
            elif isinstance(event, CallbackQuery):
                await event.answer(texts("phone_required_alert"), show_alert=True)
            return None
        if cfg["terms_required"] and customer.terms_accepted_version != int(cfg["terms_version"]):
            if isinstance(event, Message):
                await event.answer(texts("terms_text"), reply_markup=keyboards.terms(texts))
            elif isinstance(event, CallbackQuery):
                await event.answer(texts("terms_required_alert"), show_alert=True)
            return None
        return await handler(event, data)


async def missing_channels(bot: Bot, session: AsyncSession, customer: Customer) -> list[ForcedChannel]:
    channels = list(
        await session.scalars(
            select(ForcedChannel)
            .where(ForcedChannel.shop_id == customer.shop_id)
            .order_by(ForcedChannel.sort)
        )
    )
    if not channels or _join_ok.get(customer.telegram_id, 0) > time.monotonic():
        return []
    missing: list[ForcedChannel] = []
    for channel in channels:
        try:
            member = await bot.get_chat_member(channel.chat_id, customer.telegram_id)
        except TelegramAPIError as exc:
            log.warning("cannot check membership in %s: %s", channel.chat_id, exc)
            continue
        status = getattr(member, "status", "")
        if status not in MEMBER_STATUSES or (
            status == "restricted" and not getattr(member, "is_member", True)
        ):
            missing.append(channel)
    if not missing:
        _join_ok[customer.telegram_id] = time.monotonic() + 300
    return missing


def forget_join_check(telegram_id: int) -> None:
    _join_ok.pop(telegram_id, None)
