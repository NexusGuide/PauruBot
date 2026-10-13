"""Keep every staff copy of a payment review in step (F-24).

A receipt goes to every reviewer at once. Once one of them decides, from the bot, the web panel or
the Mini App, the other copies must stop offering Approve / Reject and say who decided. The ids of
the copies live in Redis next to the payment (no schema change); the payment engine is untouched.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from paurubot import kv
from paurubot.notifications import telegram
from paurubot.worker.runner import Retry

log = logging.getLogger(__name__)

KEY = "review_msgs:{}"
TTL_SECONDS = 14 * 86400  # longer than any receipt waits for review
MAX_REFS = 50
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
OUTCOMES = {
    "approved": "adm_approved_by",
    "rejected": "adm_rejected_by",
    "fix_amount": "adm_fix_amount_by",  # sent back to the customer to fix (F-23)
    "fix_card": "adm_fix_card_by",
}


def _key(payment_id: int) -> str:
    return KEY.format(int(payment_id))


async def remember(payment_id: int, message: Any) -> None:
    """Note one review message that was sent to a reviewer. Never breaks the sending."""
    chat = getattr(message, "chat", None)
    message_id = getattr(message, "message_id", None)
    if chat is None or not isinstance(message_id, int):
        return
    ref = {"chat": int(chat.id), "msg": message_id, "photo": bool(getattr(message, "photo", None))}
    try:
        async with kv.get_redis().pipeline(transaction=True) as pipe:
            pipe.rpush(_key(payment_id), json.dumps(ref))
            pipe.ltrim(_key(payment_id), -MAX_REFS, -1)
            pipe.expire(_key(payment_id), TTL_SECONDS)
            await pipe.execute()
    except Exception:  # Redis trouble must not stop a receipt reaching staff
        log.warning("could not remember review message for payment %s", payment_id, exc_info=True)


async def take(payment_id: int) -> list[dict[str, Any]]:
    """Every remembered copy of one review, removed in the same step so only one sync edits them."""
    async with kv.get_redis().pipeline(transaction=True) as pipe:
        pipe.lrange(_key(payment_id), 0, -1)
        pipe.delete(_key(payment_id))
        raw, _ = await pipe.execute()
    refs: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for item in raw or []:
        try:
            ref = json.loads(item)
            pair = (int(ref["chat"]), int(ref["msg"]))
        except (TypeError, ValueError, KeyError):
            continue
        if pair not in seen:
            seen.add(pair)
            refs.append({"chat": pair[0], "msg": pair[1], "photo": bool(ref.get("photo"))})
    return refs


async def take_now(payment_id: int) -> list[dict[str, Any]] | None:
    """`take`, but None when Redis is unreachable so the job can try again itself.

    A receipt sent back for a fix (F-23) can come back for review before the sync job runs; taking
    the copies at decision time keeps the new review messages out of that sync.
    """
    try:
        return await take(payment_id)
    except Exception:
        log.warning("could not take review messages for payment %s", payment_id, exc_info=True)
        return None


async def give_back(payment_id: int, refs: list[dict[str, Any]]) -> None:
    """Put copies that could not be edited yet back, for the retried job."""
    if not refs:
        return
    async with kv.get_redis().pipeline(transaction=True) as pipe:
        pipe.rpush(_key(payment_id), *[json.dumps(ref) for ref in refs])
        pipe.expire(_key(payment_id), TTL_SECONDS)
        await pipe.execute()


def label(staff: Any) -> str:
    """How a reviewer is named on the other copies."""
    if staff is None:
        return "-"
    return str(getattr(staff, "display_name", None) or getattr(staff, "username", None) or f"#{staff.id}")


def _editor(bot: Bot, ref: dict[str, Any], body: str) -> Callable[[], Awaitable[Any]]:
    """The edit for one copy: photo receipts carry the text as a caption."""
    if ref["photo"]:
        return lambda: bot.edit_message_caption(
            chat_id=ref["chat"], message_id=ref["msg"], caption=body[:CAPTION_LIMIT], reply_markup=None
        )
    return lambda: bot.edit_message_text(
        chat_id=ref["chat"], message_id=ref["msg"], text=body[:TEXT_LIMIT], reply_markup=None
    )


async def edit_all(
    shop_id: int | None,
    refs: list[dict[str, Any]],
    body: str,
    *,
    skip: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Show the decision on every copy and drop its buttons. Returns copies to try again later."""
    bot = await telegram.bot_for(shop_id)
    if bot is None:
        return []
    pending = list(refs)
    while pending:
        ref = pending[0]
        if skip is None or (ref["chat"], ref["msg"]) != tuple(skip):
            try:
                await telegram.deliver(_editor(bot, ref, body))
            except (telegram.Undeliverable, TelegramBadRequest):
                pass  # deleted, too old to edit or already showing the decision: nothing to fix
            except Retry:
                return pending
        pending.pop(0)
    return []


async def announce(
    session: Any,
    payment_id: int,
    shop_id: int,
    outcome: str,
    by: str,
    *,
    skip: tuple[int, int] | None = None,
    refs: list[dict[str, Any]] | None = None,
) -> None:
    """Queue the update of every copy, in the same transaction as the decision itself.

    `refs` are copies already taken from Redis (see `take_now`); without them the job takes them.
    """
    from paurubot.worker import queue

    payload: dict[str, Any] = {"payment_id": payment_id, "outcome": outcome, "by": by[:96]}
    if skip is not None:
        payload["skip"] = [int(skip[0]), int(skip[1])]
    if refs is not None:
        payload["refs"] = refs
    await queue.enqueue(session, "notify", "review_decided", payload, shop_id=shop_id)
