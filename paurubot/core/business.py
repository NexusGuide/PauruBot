"""Telegram Business automation: answer people who message the shop owner's own account (BOT-07).

The owner adds the bot to their Telegram Business account. When someone writes to that account,
the bot answers a question it has a keyword for, or once in a while sends the shop's standard
reply pointing to the bot. It stays quiet while the owner is chatting with that person, never acts
for accounts that are not staff, and never answers itself.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.db.models import BusinessConnection

MAX_RULES = 40
MAX_REPLY = 1000


class BusinessError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def validate_rules(value: Any) -> list[dict[str, Any]]:
    """[{"keywords": [..], "reply": ".."}]; keywords may arrive as one comma-separated string."""
    if not isinstance(value, list) or len(value) > MAX_RULES:
        raise BusinessError("business_rules_invalid")
    rules: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise BusinessError("business_rules_invalid")
        raw = item.get("keywords")
        words = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, list) else []
        keywords = [str(w).strip().lower()[:40] for w in words if str(w).strip()][:10]
        reply = str(item.get("reply") or "").strip()
        if not keywords or not reply or len(reply) > MAX_REPLY:
            raise BusinessError("business_rules_invalid")
        rules.append({"keywords": keywords, "reply": reply})
    return rules


def match(rules: list[dict[str, Any]], text: str) -> str | None:
    """The reply of the first rule with a keyword inside the message, ignoring case."""
    lowered = (text or "").lower()
    for rule in rules or []:
        if any(word and word in lowered for word in rule.get("keywords") or []):
            return str(rule.get("reply") or "") or None
    return None


async def remember(
    session: AsyncSession,
    shop_id: int,
    connection_id: str,
    user_id: int,
    *,
    enabled: bool,
    can_reply: bool,
    trusted: bool,
) -> BusinessConnection:
    row = await session.get(BusinessConnection, connection_id)
    if row is None:
        row = BusinessConnection(id=connection_id, shop_id=shop_id, user_id=user_id)
        session.add(row)
    row.user_id, row.enabled, row.can_reply, row.trusted = user_id, enabled, can_reply, trusted
    await session.flush()
    return row


def _quiet_key(connection_id: str, chat_id: int) -> str:
    return f"biz:owner:{connection_id}:{chat_id}"


async def owner_spoke(connection_id: str, chat_id: int, minutes: int) -> None:
    """The owner is in this conversation: hold automatic replies for a while."""
    if minutes > 0:
        await kv.get_redis().set(_quiet_key(connection_id, chat_id), "1", ex=minutes * 60)


async def reply_for(
    cfg: dict[str, Any], row: BusinessConnection | None, chat_id: int, text: str, bot_username: str | None
) -> str | None:
    """What to send to a customer's message, or None to stay silent."""
    if row is None or not (row.trusted and row.enabled and row.can_reply) or not cfg.get("business_enabled"):
        return None
    if await kv.get_redis().exists(_quiet_key(row.id, chat_id)):
        return None
    answer = match(cfg.get("business_rules") or [], text)
    if answer:
        return answer
    standard = str(cfg.get("business_reply") or "").strip()
    if not standard:
        return None
    hours = max(1, int(cfg.get("business_reply_every_hours") or 12))
    if not await kv.set_once(f"biz:standard:{row.id}:{chat_id}", hours * 3600):
        return None
    link = f"https://t.me/{bot_username}" if bot_username else ""
    return standard.replace("{bot}", link)
