from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
from aiogram.types import BotCommand, CallbackQuery, Message

from paurubot.bot.context import ContextMiddleware, GateMiddleware, StaffIdentity
from paurubot.bot.routers import (
    account,
    accounts,
    admin,
    admin_config,
    admin_extra,
    admin_growth,
    admin_hub,
    admin_navigation,
    admin_ops,
    admin_tools,
    admin_users,
    business,
    growth,
    resale_bot,
    reseller,
    services,
    shop,
    start,
    wallet,
)
from paurubot.config import get_settings
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
    await message.answer(t("unknown_command"), reply_markup=start.menu(t, cfg, staff))


@fallback.callback_query()
async def stale_button(call: CallbackQuery, t: Texts) -> None:
    await call.answer(t("button_expired"), show_alert=False)


_dispatcher: Dispatcher | None = None


def build_dispatcher(storage: BaseStorage | None = None, shop_id: int | None = None) -> Dispatcher:
    """One dispatcher per process (routers attach once); `shop_id` selects the shop it serves."""
    global _dispatcher
    if _dispatcher is not None:
        if storage is not None:
            _dispatcher.fsm.storage = storage
        _dispatcher.workflow_data["shop_id"] = shop_id
        return _dispatcher
    if storage is None:
        storage = RedisStorage.from_url(
            get_settings().redis_url, key_builder=DefaultKeyBuilder(with_bot_id=True, prefix="fsm")
        )
    dp = Dispatcher(storage=storage, shop_id=shop_id)
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
        admin_tools.router,
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
        ]
        await bot.set_my_commands(commands, language_code=language)
