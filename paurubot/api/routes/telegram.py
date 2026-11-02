"""Telegram webhook endpoint for BOT_MODE=webhook.

Telegram gets its answer as soon as an update is accepted, and the update is handled after
(AUD-4), as aiogram's own webhook handler does by default: a handler that waits on a panel or a
gateway must not keep Telegram's request open past its timeout, or the same update comes again.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine
from typing import Any

from aiogram import Dispatcher
from aiogram.types import Update
from fastapi import APIRouter, HTTPException, Request

from paurubot.config import get_settings
from paurubot.security.tokens import constant_equals, sha256_hex

log = logging.getLogger(__name__)
router = APIRouter(tags=["telegram"])
_dispatcher: Dispatcher | None = None
# Telegram's updates are a few kilobytes; a megabyte leaves room for any message it can send.
MAX_UPDATE = 1024 * 1024
# Updates being handled: the loop keeps only weak references to tasks.
_handling: set[asyncio.Task[Any]] = set()


def _handle(work: Coroutine[Any, Any, Any], update_id: int | None) -> None:
    task = asyncio.create_task(work, name=f"telegram-update:{update_id}")
    _handling.add(task)

    def done(finished: asyncio.Task[Any]) -> None:
        _handling.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            log.error("telegram update %s failed", update_id, exc_info=finished.exception())

    task.add_done_callback(done)


async def drain(grace: float = 20.0) -> None:
    """Let updates already accepted finish before the API stops (on shutdown)."""
    if _handling:
        await asyncio.wait(set(_handling), timeout=grace)


async def _update_body(request: Request) -> dict[str, Any]:
    from paurubot.api.body import BodyTooLarge, read_limited_body

    try:
        raw = await read_limited_body(request, MAX_UPDATE)
    except BodyTooLarge as exc:
        raise HTTPException(status_code=413, detail="too_large") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bad_update") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="bad_update")
    return data


def webhook_secret() -> str:
    """Derived from the bot token so it needs no extra configuration and rotates with the token."""
    return sha256_hex("webhook:" + get_settings().bot_token.get_secret_value())[:48]


def dispatcher() -> Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        from paurubot.bot.app import build_dispatcher

        _dispatcher = build_dispatcher()
    return _dispatcher


@router.post("/api/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, Any]:
    if get_settings().bot_mode != "webhook":
        raise HTTPException(status_code=404, detail="not_found")
    if not constant_equals(request.headers.get("x-telegram-bot-api-secret-token"), webhook_secret()):
        raise HTTPException(status_code=403, detail="forbidden")
    from paurubot.notifications.telegram import get_bot

    update = Update.model_validate(await _update_body(request), context={"bot": get_bot()})
    _handle(dispatcher().feed_update(get_bot(), update), update.update_id)
    return {"ok": True}


def shop_webhook_secret(token: str) -> str:
    return sha256_hex("webhook:" + token)[:48]


@router.post("/api/telegram/webhook/{shop_id}")
async def reseller_webhook(shop_id: int, request: Request) -> dict[str, Any]:
    """Updates for a reseller's own bot (TEN-04); the secret is derived from that bot's token."""
    if get_settings().bot_mode != "webhook":
        raise HTTPException(status_code=404, detail="not_found")
    from paurubot.db.models import Shop, ShopStatus
    from paurubot.db.session import session_scope
    from paurubot.notifications.telegram import bot_for
    from paurubot.security import secretbox

    async with session_scope() as session:
        shop = await session.get(Shop, shop_id)
        token = (
            secretbox.decrypt("bot-token", shop.bot_token_enc)
            if shop is not None
            and shop.bot_token_enc
            and shop.reseller_profile_id
            and shop.status == ShopStatus.ACTIVE
            else None
        )
    if token is None:
        raise HTTPException(status_code=404, detail="not_found")
    if not constant_equals(
        request.headers.get("x-telegram-bot-api-secret-token"), shop_webhook_secret(token)
    ):
        raise HTTPException(status_code=403, detail="forbidden")
    bot = await bot_for(shop_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="not_found")
    update = Update.model_validate(await _update_body(request), context={"bot": bot})
    _handle(dispatcher().feed_update(bot, update, shop_id=shop_id), update.update_id)
    return {"ok": True}
