from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
from aiogram.types import BotCommand, CallbackQuery, Message

from paurubot.bot.context import ContextMiddleware, GateMiddleware, StaffIdentity
from paurubot.bot.dedupe import UpdateDedupeMiddleware
from paurubot.bot.routers import (
    account,
    accounts,
    admin,
    admin_config,
    admin_customer,
    admin_extra,
    admin_finance,
    admin_general,
    admin_growth,
    admin_hub,
    admin_navigation,
    admin_ops,
    admin_panels,
    admin_shop,
    admin_tools,
    admin_user_actions,
    admin_users,
    business,
    growth,
    resale_bot,
    resale_broadcast,
    resale_customization,
    resale_discounts,
    resale_reports,
    reseller,
    services,
    shop,
    start,
    wallet,
)
from paurubot.i18n import Texts

log = logging.getLogger(__name__)

fallback = Router(name="fallback")


@fallback.message(F.chat.type == "private")
async def unknown(
    message: Message, t: Texts, cfg: dict[str, Any], staff: StaffIdentity | None, state: FSMContext
) -> None:
    if await state.get_state() is not None:
        await message.answer(t("unexpected_input"))
        return
    reply = auto_reply(message.text or message.caption or "", cfg.get("auto_replies") or [])
    if reply is not None:
        try:
            await message.answer(reply)
        except TelegramBadRequest:  # broken HTML in the shop's text: send it as it is
            await message.answer(reply, parse_mode=None)
        return
    await start.home(message, t, cfg, staff, "unknown_command")


def _fold(text: str) -> str:
    """Case and Arabic/Persian letter forms do not matter when matching keywords."""
    folded = text.casefold().replace("\u0643", "\u06a9").replace("\u064a", "\u06cc")  # Arabic kaf, yeh
    return folded.replace("\u0649", "\u06cc").replace("\u200c", " ")  # alef maksura; ZWNJ


def auto_reply(text: str, rules: list[Any]) -> str | None:
    """The shop's answer for the first rule with a keyword in `text` (settings › auto replies)."""
    folded = _fold(text)
    if not folded.strip():
        return None
    for rule in rules:
        if isinstance(rule, dict) and any(_fold(str(w)).strip() in folded for w in rule.get("words") or []):
            return str(rule.get("text") or "") or None
    return None


@fallback.callback_query()
async def stale_button(call: CallbackQuery, t: Texts) -> None:
    await call.answer(t("button_expired"), show_alert=False)


_dispatcher: Dispatcher | None = None


def fsm_storage() -> RedisStorage:
    """Conversation state in Redis, on a pool that waits for a free connection under a burst."""
    from paurubot import kv

    return RedisStorage(kv.make_client(), key_builder=DefaultKeyBuilder(with_bot_id=True, prefix="fsm"))


def build_dispatcher(storage: BaseStorage | None = None, shop_id: int | None = None) -> Dispatcher:
    """One dispatcher per process (routers attach once); `shop_id` selects the shop it serves."""
    global _dispatcher
    if _dispatcher is not None:
        if storage is not None:
            _dispatcher.fsm.storage = storage
        _dispatcher.workflow_data["shop_id"] = shop_id
        return _dispatcher
    if storage is None:
        storage = fsm_storage()
    dp = Dispatcher(storage=storage, shop_id=shop_id)
    # First: a retried webhook delivery must not open a second ticket or revoke twice.
    dp.update.outer_middleware(UpdateDedupeMiddleware())
    dp.update.outer_middleware(ContextMiddleware())
    gate = GateMiddleware()
    dp.message.middleware(gate)
    dp.callback_query.middleware(gate)
    dp.include_routers(
        business.router,
        start.router,
        admin_navigation.router,
        admin.router,
        admin_users.router,
        admin_customer.router,
        admin_panels.router,
        admin_finance.router,
        admin_shop.router,
        admin_general.router,
        admin_tools.router,
        admin_user_actions.router,
        admin_ops.router,
        admin_hub.router,
        admin_config.router,
        admin_growth.router,
        admin_extra.router,
        shop.router,
        services.router,
        wallet.router,
        reseller.router,
        resale_bot.router,
        resale_broadcast.router,
        resale_customization.router,
        resale_discounts.router,
        resale_reports.router,
        accounts.router,
        account.router,
        growth.router,
        fallback,
    )
    _dispatcher = dp
    return dp


async def set_commands(bot: Bot, t_fa: Texts, t_en: Texts) -> None:
    for t, language in ((t_fa, "fa"), (t_en, None)):
        commands = [
            BotCommand(command="start", description=t("cmd_start")),
            BotCommand(command="help", description=t("cmd_help")),
            BotCommand(command="language", description=t("cmd_language")),
            BotCommand(command="marketing", description=t("cmd_marketing")),
        ]
        await bot.set_my_commands(commands, language_code=language)
