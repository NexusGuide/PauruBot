"""Independent, single-use password recovery to an already linked Telegram account.

The token is never an ordinary login code, API credential or recovery URL. Only its
SHA-256 digest is stored in Redis. Account changes invalidate outstanding tokens.
"""

from __future__ import annotations

import json
from html import escape

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.audit.log import Actor, record
from paurubot.auth import staff as staff_service
from paurubot.db.base import utcnow
from paurubot.db.models import Staff, StaffApiKey
from paurubot.i18n import Texts
from paurubot.notifications import telegram
from paurubot.security import login_guard
from paurubot.security.tokens import new_token, sha256_hex

TTL = 300
PREFIX = "password-reset:v1:"
# Compare and delete together: two concurrent submissions cannot both consume a token.
CONSUME = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class ResetError(Exception):
    pass


def _key(token: str) -> str:
    return PREFIX + sha256_hex(token)


def _binding(staff: Staff) -> str:
    return json.dumps(
        {
            "staff_id": staff.id,
            "shop_id": staff.shop_id,
            "version": staff.session_version,
            "telegram_id": staff.telegram_id,
            "password": sha256_hex(staff.password_hash or ""),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _message(username: str, token: str) -> str:
    # Fixed built-in security warnings, never per-shop editable overrides.
    # Keep both languages: account recovery must not depend on an unknown locale.
    notices = [
        Texts(locale)("auth_password_reset_notice", username=escape(username)) for locale in ("en", "fa")
    ]
    return "\n\n".join(notices) + f"\n<code>{escape(token)}</code>"


async def request(session: AsyncSession, username: str, ip: str) -> None:
    username = username.strip().lower()
    await login_guard.check(username, ip)
    # Keys contain digests, never user-controlled raw names or IP addresses.
    for key, limit in (
        ("user:" + sha256_hex(username), 3),
        ("ip:" + sha256_hex(ip), 10),
    ):
        allowed, _ = await kv.hit(PREFIX + "send:" + key, limit, 600)
        if not allowed:
            raise login_guard.LoginLocked(600)
    staff = await session.scalar(select(Staff).where(func.lower(Staff.username) == username))
    if staff is None or not staff.is_active or not staff.telegram_id:
        return  # same public response for unknown, disabled and unlinked accounts
    token = new_token(32)
    redis = kv.get_redis()
    await redis.set(_key(token), _binding(staff), ex=TTL)
    # No URL / Host header / new destination supplied by the requester is trusted.
    message = _message(staff.username or "", token)
    try:
        delivered = await telegram.send_text(staff.telegram_id, message, shop_id=staff.shop_id)
        if delivered is None:
            await redis.delete(_key(token))
            return
    except telegram.Undeliverable:
        await redis.delete(_key(token))
        return
    except Exception:
        await redis.delete(_key(token))
        raise
    await record(
        session,
        shop_id=staff.shop_id,
        actor=Actor("anonymous", None, ip),
        action="auth.password_reset_requested",
        entity="staff",
        entity_id=staff.id,
    )


async def confirm(session: AsyncSession, token: str, new_password: str, ip: str) -> None:
    # Also enforce at the service boundary, before any token consumption.
    if not 10 <= len(new_password) <= 256 or not 32 <= len(token) <= 128:
        raise ResetError("invalid_reset")
    allowed, _ = await kv.hit(PREFIX + "verify:" + sha256_hex(ip), 10, 600)
    if not allowed:
        raise login_guard.LoginLocked(600)
    redis = kv.get_redis()
    raw = await redis.get(_key(token))
    if raw is None:
        raise ResetError("invalid_reset")
    raw = raw.decode() if isinstance(raw, bytes) else raw
    try:
        data = json.loads(raw)
        staff_id = data["staff_id"]
        if type(staff_id) is not int:
            raise ValueError("invalid id")
    except (ValueError, TypeError, KeyError):
        raise ResetError("invalid_reset") from None
    # The lock serializes resets/password updates using the same staff row. Re-read
    # even when an authenticated request has already put this row in its identity map.
    staff = await session.scalar(
        select(Staff).where(Staff.id == staff_id).with_for_update().execution_options(populate_existing=True)
    )
    if staff is None or not staff.is_active or not staff.telegram_id or _binding(staff) != raw:
        raise ResetError("invalid_reset")
    if not await redis.eval(CONSUME, 1, _key(token), raw):
        raise ResetError("invalid_reset")
    await staff_service.change_password(session, staff, new_password)
    await session.execute(
        update(StaffApiKey)
        .where(StaffApiKey.staff_id == staff.id, StaffApiKey.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await record(
        session,
        shop_id=staff.shop_id,
        actor=Actor("staff", staff.id, ip),
        action="auth.password_reset",
        entity="staff",
        entity_id=staff.id,
    )
    # No automatic login. DB commit belongs to the request transaction. A crash after
    # token consumption fails closed: request another token, never resurrect this one.
