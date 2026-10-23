from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.audit.log import Actor, record
from paurubot.auth import sessions
from paurubot.db.base import utcnow
from paurubot.db.models import ResellerProfile, Staff, StaffApiKey, StaffRole
from paurubot.security import login_guard
from paurubot.security.tokens import (
    constant_equals,
    hash_password_async,
    sha256_hex,
    verify_password_async,
)


class AuthError(Exception):
    pass


async def authenticate(session: AsyncSession, username: str, password: str, ip: str) -> Staff:
    """Check credentials behind the brute-force guard. Raises AuthError or LoginLocked.

    Staff usernames are unique across all shops, so login needs no shop selector.
    """
    username = username.strip()
    await login_guard.check(username, ip)
    staff = await session.scalar(select(Staff).where(func.lower(Staff.username) == username.lower()))
    shop_id = staff.shop_id if staff else None
    # Missing accounts still run a password KDF; legacy bcrypt remains readable.
    password_ok = await verify_password_async(password, staff.password_hash if staff else None)
    if staff is None or not staff.is_active or not password_ok:
        await login_guard.failed(username, ip)
        await record(
            session,
            shop_id=shop_id,
            actor=Actor("anonymous", None, ip),
            action="auth.login_failed",
            entity="staff",
            entity_id=staff.id if staff else None,
        )
        raise AuthError("invalid credentials")
    await login_guard.succeeded(username, ip)
    staff.last_login_at = utcnow()
    await record(
        session,
        shop_id=shop_id,
        actor=Actor("staff", staff.id, ip),
        action="auth.login",
        entity="staff",
        entity_id=staff.id,
    )
    return staff


async def create_staff(
    session: AsyncSession,
    shop_id: int,
    *,
    role: StaffRole,
    username: str | None,
    password: str | None,
    telegram_id: int | None = None,
    display_name: str | None = None,
    reseller_profile_id: int | None = None,
) -> Staff:
    if username and await session.scalar(
        select(Staff.id).where(func.lower(Staff.username) == username.strip().lower())
    ):
        raise AuthError("username already taken")
    staff = Staff(
        shop_id=shop_id,
        role=role,
        username=username.strip() if username else None,
        password_hash=await hash_password_async(password) if password else None,
        telegram_id=telegram_id,
        display_name=display_name,
        reseller_profile_id=reseller_profile_id,
    )
    session.add(staff)
    await session.flush()
    return staff


async def change_password(session: AsyncSession, staff: Staff, new_password: str) -> None:
    locked = await session.scalar(
        select(Staff).where(Staff.id == staff.id).with_for_update().execution_options(populate_existing=True)
    )
    if locked is None or not locked.is_active:
        raise AuthError("invalid credentials")
    staff = locked
    staff.password_hash = await hash_password_async(new_password)
    staff.session_version += 1
    await session.flush()
    await sessions.destroy_all(staff.id)


async def resolve(session: AsyncSession, data: sessions.StaffSession) -> Staff | None:
    """Load the staff row for a session; stale session versions and disabled accounts are rejected."""
    staff = await session.get(Staff, data.staff_id)
    if staff is None or not staff.is_active or staff.shop_id != data.shop_id:
        return None
    if staff.session_version != data.session_version:
        return None
    return staff


async def reseller_allow_list(session: AsyncSession, staff: Staff) -> list[str] | None:
    if staff.role != StaffRole.RESELLER or staff.reseller_profile_id is None:
        return None
    profile = await session.get(ResellerProfile, staff.reseller_profile_id)
    if profile is None or not profile.is_current:
        return []
    rights = list(profile.permissions or [])
    if "resellers.bot" not in rights and profile.parent_id is None:
        from paurubot.core import settings as shop_settings

        if (await shop_settings.load(session, profile.shop_id)).get("reseller_bot_open"):
            rights.append("resellers.bot")  # every reseller may run its own bot (TEN-17)
    return rights


async def by_telegram(session: AsyncSession, shop_id: int, telegram_id: int) -> Staff | None:
    return await session.scalar(
        select(Staff).where(
            Staff.shop_id == shop_id,
            Staff.telegram_id == telegram_id,
            Staff.is_active.is_(True),
        )
    )


# --- one-time codes sent by the bot (SEC-02) -----------------------------------------------

CODE_TTL = 300
CODE_ATTEMPTS = 5
OTP_CONSUME = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1], KEYS[2])
    return 1
end
return 0
"""


def _code_hash(staff_id: int, code: str) -> str:
    return sha256_hex(f"{staff_id}:{code}")


async def request_code(session: AsyncSession, username: str, ip: str) -> Staff | None:
    """Send a 6-digit login code to the staff member's Telegram. Returns the staff row when sent.

    The caller answers the same way whether or not the account exists, so this can't be used
    to discover usernames.
    """
    from paurubot.i18n import texts_for
    from paurubot.notifications import telegram

    username = username.strip()
    await login_guard.check(username, ip)
    allowed, _ = await kv.hit(f"otp:send:{username.lower()}", 3, 600)
    if not allowed:
        raise login_guard.LoginLocked(600)
    staff = await session.scalar(select(Staff).where(func.lower(Staff.username) == username.lower()))
    if staff is None or not staff.is_active or not staff.telegram_id:
        return None
    code = f"{secrets.randbelow(1_000_000):06d}"
    redis = kv.get_redis()
    await redis.set(f"otp:code:{staff.id}", _code_hash(staff.id, code), ex=CODE_TTL)
    await redis.delete(f"otp:tries:{staff.id}")
    t = await texts_for(session, staff.shop_id, None)
    try:
        await telegram.send_text(staff.telegram_id, t("staff_login_code", code=code, ip=ip))
    except telegram.Undeliverable:
        return None
    await record(
        session,
        shop_id=staff.shop_id,
        actor=Actor("anonymous", None, ip),
        action="auth.code_sent",
        entity="staff",
        entity_id=staff.id,
    )
    return staff


async def verify_code(session: AsyncSession, username: str, code: str, ip: str) -> Staff:
    username = username.strip()
    await login_guard.check(username, ip)
    staff = await session.scalar(select(Staff).where(func.lower(Staff.username) == username.lower()))
    redis = kv.get_redis()
    stored = await redis.get(f"otp:code:{staff.id}") if staff else None
    ok = bool(
        staff
        and staff.is_active
        and stored
        and constant_equals(
            stored if isinstance(stored, str) else stored.decode(),
            _code_hash(staff.id, code.strip()),
        )
    )
    if ok and staff is not None:
        ok = bool(
            await redis.eval(
                OTP_CONSUME,
                2,
                f"otp:code:{staff.id}",
                f"otp:tries:{staff.id}",
                _code_hash(staff.id, code.strip()),
            )
        )
    if not ok or staff is None:
        await login_guard.failed(username, ip)
        if staff is not None:
            tries = await redis.incr(f"otp:tries:{staff.id}")
            await redis.expire(f"otp:tries:{staff.id}", CODE_TTL)
            if tries >= CODE_ATTEMPTS:
                await redis.delete(f"otp:code:{staff.id}")
        await record(
            session,
            shop_id=staff.shop_id if staff else None,
            actor=Actor("anonymous", None, ip),
            action="auth.code_failed",
            entity="staff",
            entity_id=staff.id if staff else None,
        )
        raise AuthError("invalid code")
    await login_guard.succeeded(username, ip)
    staff.last_login_at = utcnow()
    await record(
        session,
        shop_id=staff.shop_id,
        actor=Actor("staff", staff.id, ip),
        action="auth.login_code",
        entity="staff",
        entity_id=staff.id,
    )
    return staff


# --- personal API keys (SEC-03) ------------------------------------------------------------

API_KEY_PREFIX = "pbk_"


def _key_hash(secret: str) -> str:
    return sha256_hex(f"api-key:{secret}")


async def create_api_key(
    session: AsyncSession,
    staff: Staff,
    name: str,
    *,
    read_only: bool,
    expires_at: datetime | None,
) -> tuple[StaffApiKey, str]:
    """Return the new key row and the full key, which is never stored or shown again."""
    prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    row = StaffApiKey(
        staff_id=staff.id,
        name=name.strip()[:64] or "key",
        prefix=prefix,
        key_hash=_key_hash(secret),
        read_only=read_only,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row, f"{API_KEY_PREFIX}{prefix}_{secret}"


async def resolve_api_key(session: AsyncSession, token: str) -> tuple[Staff, StaffApiKey] | None:
    if not token.startswith(API_KEY_PREFIX) or len(token) > 120:
        return None
    prefix, _, secret = token[len(API_KEY_PREFIX) :].partition("_")
    if not prefix or not secret:
        return None
    row = await session.scalar(select(StaffApiKey).where(StaffApiKey.prefix == prefix))
    if row is None or not constant_equals(row.key_hash, _key_hash(secret)):
        return None
    now = utcnow()
    if row.revoked_at is not None or (row.expires_at is not None and row.expires_at <= now):
        return None
    staff = await session.get(Staff, row.staff_id)
    if staff is None or not staff.is_active:
        return None
    if row.last_used_at is None or now - row.last_used_at > timedelta(minutes=5):
        row.last_used_at = now
    return staff, row
