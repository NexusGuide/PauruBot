"""Asking Telegram about a channel before it is saved as a forced or trust channel (GRW-07/08).

The admin panel runs in the API process, so it speaks to Telegram with the shop's own bot (the
main one, or a reseller's). A channel is only accepted when the bot is an admin there: otherwise
membership checks and report posts would fail later, silently and long after the mistake. The
lookup itself lives in core/forced_join.py, shared with the user bots' own channel settings.
"""

from __future__ import annotations

from fastapi import HTTPException

from paurubot.core import forced_join
from paurubot.core.forced_join import ResolvedChannel

__all__ = ["ResolvedChannel", "require_channel", "resolve_channel"]

_STATUS = {"bot_stopped": 503}


async def resolve_channel(shop_id: int, raw: str) -> ResolvedChannel:
    """Look a chat up with the shop's bot and report whether the bot may run checks there.

    422 when Telegram does not know the chat, 503 when the shop has no running bot to ask with.
    """
    try:
        return await forced_join.resolve(shop_id, raw)
    except forced_join.ForcedJoinError as exc:
        raise HTTPException(status_code=_STATUS.get(exc.code, 422), detail=exc.code) from None


async def require_channel(shop_id: int, raw: str) -> ResolvedChannel:
    """Resolve a channel and refuse it unless the shop's bot is an admin there."""
    info = await resolve_channel(shop_id, raw)
    if not info.bot_admin:
        raise HTTPException(status_code=422, detail="bot_not_admin")
    return info
