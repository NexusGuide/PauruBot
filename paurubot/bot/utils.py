from __future__ import annotations

from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from paurubot.bot.context import StaffIdentity
from paurubot.i18n import Texts


class Btn(BaseFilter):
    """Matches a reply-keyboard button by locale key, in every language and after admin edits."""

    def __init__(self, *keys: str) -> None:
        self.keys = keys

    async def __call__(self, message: Message, t: Texts) -> bool:
        return any(t.matches(message.text, key) for key in self.keys)


class StaffOnly(BaseFilter):
    def __init__(self, permission: str | None = None) -> None:
        self.permission = permission

    async def __call__(self, event: Message | CallbackQuery, staff: StaffIdentity | None = None) -> bool:
        if staff is None:
            return False
        return self.permission is None or staff.can(self.permission)


async def show(
    event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None, **kwargs: Any
) -> None:
    """Edit the pressed message in place when possible, else send a new one."""
    if isinstance(event, CallbackQuery):
        message = event.message
        if isinstance(message, Message):
            if message.text is not None:
                try:
                    await message.edit_text(
                        text, reply_markup=markup, disable_web_page_preview=True, **kwargs
                    )
                    return
                except TelegramBadRequest as exc:
                    if "not modified" in str(exc):
                        return
            await message.answer(text, reply_markup=markup, disable_web_page_preview=True, **kwargs)
        return
    await event.answer(text, reply_markup=markup, disable_web_page_preview=True, **kwargs)


def error_text(t: Texts, key: str, params: dict[str, Any]) -> str:
    from paurubot.core.formatting import money

    clean = {
        k: money(v) if isinstance(v, int) and k in {"balance", "required", "low", "high", "amount"} else v
        for k, v in params.items()
    }
    return t(key, **clean)
