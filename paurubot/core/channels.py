"""Channel rules shared by the bot and the Mini App API (GRW-07, GRW-08).

Forced-join channels are checked from both surfaces: the bot's middleware and the Mini App's
auth dependency. If Telegram cannot answer (the bot was removed from the channel, a wrong chat
id, a network slip) the customer is never blocked on a guess: the check records the failure for
the admin panel and lets the request through.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from paurubot import kv
from paurubot.db.models import ForcedChannel

log = logging.getLogger(__name__)

MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}
# A restricted user with is_member=False (removed, or a chat that keeps them from posting) is out.
# Any other status (left, kicked, banned) is not a member either.
ADMIN_STATUSES = {"creator", "administrator"}
# How long a channel that Telegram could not verify stays flagged in the admin panel.
FAIL_TTL = 900
# Order kinds a trust channel may be told to report (GRW-08). Free moves and bonus extras never
# post: the sender only reports orders that were paid for, plus free trials.
TRUST_KINDS = ("purchase", "renew", "trial", "upgrade", "extra_volume", "extra_days", "move")


def is_member(member: Any) -> bool:
    """Whether a getChatMember result counts as joined."""
    # ChatMemberStatus is a str enum: compare it directly, and let equality (not str()) decide.
    status = getattr(member, "status", "")
    if status not in MEMBER_STATUSES:
        return False
    return not (status == "restricted" and not getattr(member, "is_member", True))


def is_admin(member: Any) -> bool:
    """Whether the bot itself may post in and check memberships of a channel."""
    return getattr(member, "status", "") in ADMIN_STATUSES


async def missing_members(
    bot: Bot, channels: list[ForcedChannel], telegram_id: int
) -> tuple[list[ForcedChannel], list[ForcedChannel]]:
    """(missing, unverifiable) channels for one customer.

    Telegram errors land in the second list instead of blocking: the admin panel warns about them
    and the customer keeps working, exactly like the bot's own gate.
    """
    missing: list[ForcedChannel] = []
    unknown: list[ForcedChannel] = []
    for channel in channels:
        try:
            member = await bot.get_chat_member(channel.chat_id, telegram_id)
        except TelegramAPIError as exc:
            log.warning("cannot check membership in %s: %s", channel.chat_id, exc)
            await kv.mark(f"join:{channel.shop_id}:{channel.chat_id}", FAIL_TTL, str(exc)[:200] or "1")
            unknown.append(channel)
            continue
        if not is_member(member):
            missing.append(channel)
    return missing, unknown
